"""Offline, fixed-repository CosyVoice3 RuntimeJob -> WAV + canonical receipt.

Call only after acquiring the production GPU gate for a CUDA job. No downloads,
installs, Comfy control, character publication, or service/default writes occur.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import random
import socket
import subprocess
import sys
import tempfile
import time
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.schemas_routing import RuntimeJob, RuntimeReceipt
from deploy_cosyvoice3 import FILES, CODE_COMMIT, MATCHA_COMMIT, REVISION, digest, environment

MODEL_ID = 'cosyvoice3-0.5b-2512'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def emit(stage, **data):
    print(json.dumps({'stage': stage, 'at_utc': now(), **data}, ensure_ascii=True), flush=True)


def write_new_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.pending', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        # An atomic hard-link publishes the completed JSON without ever replacing
        # an existing receipt, including one created after the preflight check.
        os.link(temporary, path)
    finally:
        with contextlib.suppress(OSError): temporary.unlink()


def preflight(job, repo, model_dir):
    if job.slot != 'dialogue_generation' or job.model_id != MODEL_ID:
        raise ValueError('This helper supports only dialogue_generation / cosyvoice3-0.5b-2512')
    output, receipt = Path(job.output_path).resolve(), Path(job.receipt_path).resolve()
    if output == receipt or output.suffix.lower() != '.wav':
        raise ValueError('Output must be a distinct new .wav file')
    if output.exists() or receipt.exists():
        raise FileExistsError('Existing output/receipt is never overwritten; choose a new task/output path')
    reference = Path(str(job.inputs.get('reference_audio_path', ''))).resolve()
    if not reference.is_file() or reference.suffix.lower() not in {'.wav', '.flac'}:
        raise ValueError('reference_audio_path must be an existing local WAV/FLAC; no implicit reference download')
    if reference.stat().st_size > 40 * 1024 * 1024:
        raise ValueError('Reference audio exceeds helper input bound')
    text = job.inputs.get('text'); prompt = job.inputs.get('prompt_text')
    mode = job.parameters.get('mode', 'zero_shot')
    if mode not in {'zero_shot', 'cross_lingual'}:
        raise ValueError('mode must be zero_shot or cross_lingual')
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError('text must be 1..2000 literal characters')
    if mode == 'cross_lingual' and prompt is None: prompt = ''
    if not isinstance(prompt, str) or len(prompt) > 2000 or (mode == 'zero_shot' and not prompt.strip()):
        raise ValueError('prompt_text must be the literal reference transcript')
    device = job.parameters.get('device', 'cuda')
    if device not in {'cuda', 'cpu'}:
        raise ValueError('device must be cuda or cpu')
    speed = float(job.parameters.get('speed', 1))
    if not math.isfinite(speed) or not .5 <= speed <= 2:
        raise ValueError('speed must be finite and between 0.5 and 2')
    for key in ['fp16', 'text_frontend']:
        if key in job.parameters and not isinstance(job.parameters[key], bool):
            raise ValueError(f'{key} must be a JSON boolean')
    if device == 'cpu' and job.parameters.get('fp16', False):
        raise ValueError('fp16 CPU inference is not this verified workflow; use fp16=false')
    seed = job.parameters.get('seed', 1247)
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2**32 - 1:
        raise ValueError('seed must be a 32-bit unsigned JSON integer')
    if not repo.is_dir() or not (repo / 'third_party/Matcha-TTS/matcha').is_dir():
        raise FileNotFoundError('Fixed CosyVoice repository or Matcha submodule missing')
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True, shell=False,
                            creationflags=0x08000000 if os.name == 'nt' else 0)
    if result.returncode or result.stdout.strip() != CODE_COMMIT:
        raise ValueError('CosyVoice repository HEAD differs from reviewed fixed commit')
    source_status = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain', '--ignore-submodules=all'],
        capture_output=True, text=True, shell=False, creationflags=0x08000000 if os.name == 'nt' else 0)
    if source_status.returncode or source_status.stdout.strip():
        raise ValueError('Fixed CosyVoice source has local changes; review before execution')
    result = subprocess.run(['git', '-C', str(repo / 'third_party/Matcha-TTS'), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                            shell=False, creationflags=0x08000000 if os.name == 'nt' else 0)
    if result.returncode or result.stdout.strip() != MATCHA_COMMIT:
        provenance = Path('E:/VideoFactoryRuntime/manifests/cosyvoice3_code.json')
        snapshot = json.loads(provenance.read_text(encoding='utf-8')).get('matcha_snapshot', {}) if provenance.exists() else {}
        if snapshot.get('commit') != MATCHA_COMMIT or snapshot.get('method') != 'official_fixed_commit_archive_each_Git_blob_verified' or len(snapshot.get('files', [])) != 107:
            raise ValueError('Matcha fixed submodule/snapshot provenance missing')
        for entry in snapshot['files']:
            source_file = (repo / 'third_party/Matcha-TTS' / entry['path']).resolve()
            if not source_file.is_relative_to((repo / 'third_party/Matcha-TTS').resolve()) or not source_file.is_file() or source_file.stat().st_size != entry['bytes'] or digest(source_file, 'git_blob_sha1') != entry['git_blob_sha1']:
                raise ValueError('Fixed Matcha snapshot file checksum mismatch')
    else:
        clean = subprocess.run(['git', '-C', str(repo / 'third_party/Matcha-TTS'), 'status', '--porcelain'],
            capture_output=True, text=True, shell=False, creationflags=0x08000000 if os.name == 'nt' else 0)
        if clean.returncode or clean.stdout.strip():
            raise ValueError('Fixed Matcha git submodule has local changes; review before execution')
    files = []
    for name, size, method, expected in FILES:
        path = model_dir / name
        if not path.is_file() or path.stat().st_size != size:
            raise FileNotFoundError(f'Missing/incomplete required component: {name}; offline inference did not download it')
        actual = digest(path, method)
        if actual != expected:
            raise ValueError(f'Required component checksum mismatch: {name}')
        files.append({'path': str(path), 'bytes': size, 'hash_algorithm': method, 'hash': actual})
    return output, receipt, reference, text.strip(), prompt.strip(), device, speed, files


def wav_probe(path):
    with wave.open(str(path), 'rb') as stream:
        result = {'sample_rate': stream.getframerate(), 'channels': stream.getnchannels(),
                  'frames': stream.getnframes(), 'sample_width_bytes': stream.getsampwidth(),
                  'compression': stream.getcomptype()}
        decoded = 0
        while data := stream.readframes(65536): decoded += len(data)
    expected = result['frames'] * result['channels'] * result['sample_width_bytes']
    if decoded != expected or result['frames'] < 1 or result['compression'] != 'NONE':
        raise ValueError('Full WAV PCM decode length verification failed')
    result.update({'duration_seconds': result['frames'] / result['sample_rate'], 'decoded_pcm_bytes': decoded,
                   'codec': 'pcm_s16le', 'full_pcm_read': True})
    return result


def finalize_success(receipt, runtime_probe):
    """A late metadata/probe failure must never produce succeeded+error."""
    receipt.status = 'failed'
    receipt.runtime = runtime_probe()
    receipt.error = None
    receipt.status = 'succeeded'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True, help='Canonical RuntimeJob JSON file')
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--repo', required=True)
    args = parser.parse_args(); started = time.monotonic()
    raw = json.loads(Path(args.job).read_text(encoding='utf-8-sig'))
    job = RuntimeJob.model_validate(raw)
    receipt_path = Path(job.receipt_path).resolve()
    receipt = RuntimeReceipt(task_id=job.task_id, slot=job.slot, model_id=job.model_id, status='failed')
    try:
        emit('preflight', task_id=job.task_id)
        repo, model_dir = Path(args.repo).resolve(), Path(args.model_dir).resolve()
        output, receipt_path, reference, text, prompt, device, speed, components = preflight(job, repo, model_dir)
        receipt.inputs_sha256 = {'reference_audio': digest(reference), 'text_utf8': hashlib.sha256(text.encode()).hexdigest(),
                                 'prompt_text_utf8': hashlib.sha256(prompt.encode()).hexdigest(), 'job_json': digest(args.job)}
        os.environ.update(environment())
        os.environ.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_DATASETS_OFFLINE': '1',
                           'MODELSCOPE_CACHE': 'E:/Models/.cache/modelscope', 'MODELSCOPE_OFFLINE': '1',
                           'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '2', 'MKL_NUM_THREADS': '2'})
        if device == 'cpu': os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
        # Fail closed if any library attempts an implicit network retrieval.
        def no_network(event, _args):
            if event in {'socket.connect', 'socket.getaddrinfo'}:
                raise RuntimeError('Runtime network access is disabled; prepare all model components explicitly')
        sys.addaudithook(no_network)
        sys.path.insert(0, str(repo)); sys.path.insert(0, str(repo / 'third_party/Matcha-TTS'))
        os.chdir(repo)
        # Third-party logs go to stderr; stdout retains actual machine-readable phases.
        with contextlib.redirect_stdout(sys.stderr):
            import numpy as np
            import torch
            import soundfile as sf
            from cosyvoice.cli.cosyvoice import AutoModel
        torch.set_num_threads(2)
        if device == 'cuda':
            if not torch.cuda.is_available(): raise RuntimeError('CUDA requested but unavailable; no silent CPU fallback')
            capability = torch.cuda.get_device_capability()
            if capability[0] >= 12 and 'sm_120' not in torch.cuda.get_arch_list():
                raise RuntimeError('Installed Torch does not include required Blackwell sm_120 kernels')
            torch.cuda.reset_peak_memory_stats()
        seed = int(job.parameters.get('seed', 1247)); random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
        if device == 'cuda': torch.cuda.manual_seed_all(seed)
        emit('loading', task_id=job.task_id, device=device)
        with contextlib.redirect_stdout(sys.stderr):
            model = AutoModel(model_dir=str(model_dir), load_trt=False, load_vllm=False, fp16=bool(job.parameters.get('fp16', False)))
        load_seconds = time.monotonic() - started
        effective_prompt = prompt if '<|endofprompt|>' in prompt else 'You are a helpful assistant.<|endofprompt|>' + prompt
        mode = job.parameters.get('mode', 'zero_shot')
        emit('synthesizing', task_id=job.task_id, device=device, sample_rate=model.sample_rate)
        parts = []
        with torch.inference_mode(), contextlib.redirect_stdout(sys.stderr):
            if mode == 'cross_lingual':
                effective_text = text if '<|endofprompt|>' in text else 'You are a helpful assistant.<|endofprompt|>' + text
                generated = model.inference_cross_lingual(effective_text, str(reference), stream=False, speed=speed,
                    text_frontend=bool(job.parameters.get('text_frontend', False)))
            else:
                generated = model.inference_zero_shot(text, effective_prompt, str(reference), stream=False, speed=speed,
                    text_frontend=bool(job.parameters.get('text_frontend', False)))
            for result in generated:
                part = result['tts_speech'].detach().float().cpu().numpy()
                if part.ndim != 2 or part.shape[0] != 1 or not np.isfinite(part).all():
                    raise ValueError('Model returned invalid/nonfinite mono waveform')
                parts.append(part[0])
        if not parts: raise ValueError('Model returned no audio')
        signal = np.concatenate(parts)
        if signal.size == 0 or signal.size / model.sample_rate > 300:
            raise ValueError('Generated audio is empty or exceeds helper duration bound')
        if float(np.max(np.abs(signal))) <= .000001:
            raise ValueError('Generated waveform is silent')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('xb') as stream:
            sf.write(stream, signal, int(model.sample_rate), format='WAV', subtype='PCM_16')
        actual = wav_probe(output)
        actual.update({'peak_amplitude_before_pcm': float(np.max(np.abs(signal))), 'seed': seed, 'speed': speed,
                       'rms_amplitude_before_pcm': float(np.sqrt(np.mean(np.square(signal, dtype=np.float64)))),
                       'clipped_samples_before_pcm': int(np.count_nonzero(np.abs(signal) > 1)),
                       'mode': mode,
                       'text_frontend': bool(job.parameters.get('text_frontend', False)), 'human_listening_review': 'NOT RUN',
                       'formal_character_voice_approved': False})
        receipt.output_path = str(output); receipt.output_sha256 = digest(output)
        receipt.actual = actual
        def runtime_probe():
            runtime = {'python': sys.version, 'python_executable': sys.executable, 'code_commit': CODE_COMMIT,
                'matcha_commit': MATCHA_COMMIT, 'model_revision': REVISION, 'device': device, 'network': 'disabled',
                'load_seconds_including_preflight': round(load_seconds, 3), 'helper_sha256': digest(__file__),
                'component_hashes_verified': len(components), 'versions': {name: importlib.metadata.version(name)
                    for name in ['torch', 'torchaudio', 'transformers', 'onnxruntime', 'numpy', 'soundfile']},
                'onnx_tokenizer_providers': model.frontend.speech_tokenizer_session.get_providers(),
                'onnx_speaker_embedding_providers': model.frontend.campplus_session.get_providers()}
            if device == 'cuda':
                runtime.update({'gpu': torch.cuda.get_device_name(), 'capability': torch.cuda.get_device_capability(),
                    'cuda_runtime': torch.version.cuda, 'peak_cuda_allocated_bytes': torch.cuda.max_memory_allocated(),
                    'peak_cuda_reserved_bytes': torch.cuda.max_memory_reserved()})
            return runtime
        finalize_success(receipt, runtime_probe)
        receipt.elapsed_seconds = round(time.monotonic() - started, 3)
        write_new_json(receipt_path, receipt.model_dump()); emit('succeeded', task_id=job.task_id, receipt_path=str(receipt_path),
            output_path=str(output), output_sha256=receipt.output_sha256, actual=actual)
    except Exception as error:
        receipt.status = 'failed'
        receipt.error = f'{type(error).__name__}: {error}'; receipt.elapsed_seconds = round(time.monotonic() - started, 3)
        try:
            if not receipt_path.exists(): write_new_json(receipt_path, receipt.model_dump())
        except Exception as write_error:
            emit('failed', task_id=job.task_id, error=receipt.error, receipt_path=str(receipt_path),
                 receipt_write_error=f'{type(write_error).__name__}: {write_error}')
            return 1
        emit('failed', task_id=job.task_id, error=receipt.error, receipt_path=str(receipt_path))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
