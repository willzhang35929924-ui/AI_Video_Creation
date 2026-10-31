"""Full-video Real-ESRGAN x4 inference using the existing Comfy Python runtime.

No downloads, installs, Comfy control, or automatic GPU scheduling occur here.
The caller must exclusively acquire the production GPU gate before CUDA use.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from fractions import Fraction


METHOD = 'RealESRGAN_x4plus framewise'
OFFICIAL_WEIGHT_SHA256 = '4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1'
WINDOW_FLAGS = 0x08000000 if os.name == 'nt' else 0


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def rational(value):
    number = Fraction(str(value))
    if number <= 0 or number > 240:
        raise ValueError('FPS must be positive and at most 240')
    return number


def command(args, *, timeout=600):
    result = subprocess.run([str(x) for x in args], capture_output=True, encoding='utf-8', errors='replace',
                            timeout=timeout, check=False, shell=False, creationflags=WINDOW_FLAGS)
    if result.returncode:
        raise RuntimeError(f'{Path(str(args[0])).name} exit {result.returncode}: {result.stderr[-2000:]}')
    return result


def probe(path, ffprobe, timeout=600):
    return json.loads(command([ffprobe, '-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', path], timeout=timeout).stdout)


def source_spec(info, width, height, requested_fps=None):
    video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), None)
    if not video:
        raise ValueError('Input has no video stream')
    w, h = int(video['width']), int(video['height'])
    if w < 2 or h < 2 or w * h > 4096 * 2160:
        raise ValueError('Source must be 2px or larger and at most 4096x2160 equivalent pixels')
    if width < 2 or height < 2 or width % 2 or height % 2:
        raise ValueError('Output width/height must be positive even integers')
    if w * height != h * width:
        raise ValueError('Output must preserve the exact source aspect ratio; no stretching/cropping')
    if width > w * 4 or height > h * 4:
        raise ValueError('Target exceeds the real x4 model output; no unlabelled extra enlargement is allowed')
    if video.get('sample_aspect_ratio') not in (None, 'N/A', '0:1', '1:1'):
        raise ValueError('Non-square-pixel video requires explicit normalization before this workflow')
    if video.get('color_transfer') in {'smpte2084', 'arib-std-b67'}:
        raise ValueError('This RGB8 SDR model does not implement HDR tone mapping')
    rotation = float(video.get('tags', {}).get('rotate', 0))
    rotation = next((float(s['rotation']) for s in video.get('side_data_list', []) if 'rotation' in s), rotation)
    if rotation % 360:
        raise ValueError('Rotated source requires explicit normalization; implicit autorotation is disabled')
    fps = rational(video.get('avg_frame_rate', '0'))
    nominal = rational(video.get('r_frame_rate', '0'))
    if abs(float(fps - nominal)) > .0001:
        raise ValueError('Variable/nonuniform frame rate is unsupported; no implicit retiming is performed')
    if requested_fps is not None and abs(float(rational(requested_fps) - fps)) > .0001:
        raise ValueError('Requested FPS differs from the source; this workflow preserves frame count and timing')
    count = int(video.get('nb_read_frames') or 0)
    if count <= 0:
        raise ValueError('ffprobe could not verify the full source video frame count')
    return {'width': w, 'height': h, 'fps': f'{fps.numerator}/{fps.denominator}',
            'fps_number': float(fps), 'frames': count, 'video': video,
            'audio': [s for s in info.get('streams', []) if s.get('codec_type') == 'audio']}


def validate_cadence(input_path, ffprobe, spec, timeout=600):
    """Stream every frame PTS; never accumulate a video's frames/timestamps in RAM."""
    process = subprocess.Popen([str(ffprobe), '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'frame=best_effort_timestamp_time', '-of', 'csv=p=0', str(input_path)], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', creationflags=WINDOW_FLAGS)
    started = time.monotonic(); previous = None; count = 0
    try:
        for line in process.stdout:
            if time.monotonic() - started > timeout:
                raise TimeoutError('Source cadence validation timed out')
            first = line.split(',', 1)[0].strip()
            if not first: continue
            stamp = float(first)
            if not math.isfinite(stamp): raise ValueError('Source frame timestamp is nonfinite')
            if previous is not None and abs(stamp - previous - 1 / spec['fps_number']) > .002:
                raise ValueError('Source frame timestamps are not constant cadence; no frames were retimed')
            previous = stamp; count += 1
        if process.wait(timeout=10) or count != spec['frames']:
            raise ValueError('Source PTS frame count does not match full frame probe')
        return {'status': 'PASS', 'timestamps_checked': count, 'policy': 'CFR only; all source frame timestamps checked'}
    finally:
        stop_process(process)


def load_model(path, device, precision, threads):
    if device == 'cpu':
        # A CPU interface test must not initialize a CUDA context while H3 runs.
        os.environ['CUDA_VISIBLE_DEVICES'] = ''
    import torch
    from spandrel import ModelLoader, ImageModelDescriptor
    torch.set_num_threads(threads)
    if device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA requested but unavailable; no silent CPU substitution')
    if device == 'cpu' and precision == 'fp16':
        raise ValueError('CPU inference uses fp32; fp16 is only offered explicitly on CUDA')
    # No arbitrary pickle/remote-code execution, torch.jit.load or unsafe fallback.
    state = torch.load(str(path), map_location='cpu', weights_only=True)
    if not isinstance(state, dict): raise ValueError('Model is not a tensor state dictionary')
    for key in ('params_ema', 'params', 'state_dict'):
        if key in state and isinstance(state[key], dict): state = state[key]; break
    if not state or any(not isinstance(value, torch.Tensor) for value in state.values()):
        raise ValueError('Expected a plain tensor state dictionary; executable model objects are rejected')
    descriptor = ModelLoader(device='cpu').load_from_state_dict(state).eval()
    if not isinstance(descriptor, ImageModelDescriptor) or descriptor.architecture.id != 'ESRGAN' or descriptor.scale != 4 or descriptor.input_channels != 3 or descriptor.output_channels != 3:
        raise ValueError('Expected the 3-channel ESRGAN x4 architecture')
    if precision == 'fp16' and not descriptor.supports_half:
        raise ValueError('This descriptor does not support half precision')
    dtype = torch.float16 if precision == 'fp16' else torch.float32
    descriptor = descriptor.to(device=torch.device(device), dtype=dtype)
    if device == 'cuda': torch.cuda.reset_peak_memory_stats()
    versions = {}
    for package in ('torch', 'spandrel', 'numpy', 'Pillow'):
        versions[package] = importlib.metadata.version(package)
    versions.update(python=sys.version.split()[0], cuda_runtime=torch.version.cuda,
                    device=device, precision=precision, threads=threads)
    if device == 'cuda': versions['gpu'] = torch.cuda.get_device_name()
    return descriptor, torch, dtype, versions


def tiled_frame(rgb, descriptor, torch, dtype, device, tile, pad):
    """A single RGB8 frame in, one reconstructed RGB8 frame out; tile context is cropped."""
    import numpy as np
    h, w, channels = rgb.shape
    if channels != 3: raise ValueError('Expected RGB input')
    scale = descriptor.scale
    output = np.empty((h * scale, w * scale, 3), dtype=np.uint8)
    with torch.inference_mode():
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                right, bottom = min(w, x + tile), min(h, y + tile)
                left_pad, top_pad = max(0, x - pad), max(0, y - pad)
                right_pad, bottom_pad = min(w, right + pad), min(h, bottom + pad)
                patch = np.ascontiguousarray(rgb[top_pad:bottom_pad, left_pad:right_pad])
                tensor = torch.from_numpy(patch).permute(2, 0, 1).unsqueeze(0).to(device=device, dtype=dtype).div_(255)
                result = descriptor(tensor)
                if tuple(result.shape) != (1, 3, patch.shape[0] * scale, patch.shape[1] * scale):
                    raise ValueError('Model did not return its declared x4 tile dimensions')
                cropped = result[0, :, (y - top_pad) * scale:(bottom - top_pad) * scale,
                                   (x - left_pad) * scale:(right - left_pad) * scale]
                core = cropped.clamp(0, 1).mul(255).round().to(dtype=torch.uint8).cpu().permute(1, 2, 0).numpy()
                output[y * scale:bottom * scale, x * scale:right * scale] = core
                del result, cropped, core, tensor
    return output


def read_exact(stream, count):
    chunks = bytearray()
    while len(chunks) < count:
        data = stream.read(count - len(chunks))
        if not data:
            if chunks: raise ValueError('Decoder returned a partial RGB video frame')
            return None
        chunks.extend(data)
    return chunks


def stop_process(process):
    if process is None: return
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
    for stream in (process.stdin, process.stdout):
        if stream:
            try: stream.close()
            except OSError: pass


def audio_hash(path, ffmpeg, timeout=600):
    return command([ffmpeg, '-v', 'error', '-i', path, '-map', '0:a', '-c:a', 'copy', '-vn',
                    '-f', 'hash', '-hash', 'sha256', '-'], timeout=timeout).stdout.strip()


def verify_output(path, source, spec, width, height, ffmpeg, ffprobe, timeout=600):
    output = probe(path, ffprobe, timeout)
    video = next((s for s in output['streams'] if s.get('codec_type') == 'video'), {})
    if video.get('width') != width or video.get('height') != height:
        raise ValueError('Actual output dimensions do not match the requested dimensions')
    frames = int(video.get('nb_read_frames') or 0)
    if frames != spec['frames'] or abs(float(rational(video['avg_frame_rate'])) - spec['fps_number']) > .0001:
        raise ValueError('Actual output frame count or FPS changed')
    expected_video_duration = spec['frames'] / spec['fps_number']
    if abs(float(video.get('duration', 0)) - expected_video_duration) > max(.025, 1 / spec['fps_number']):
        raise ValueError('Actual output video duration changed')
    audio = [s for s in output['streams'] if s.get('codec_type') == 'audio']
    if len(audio) != len(spec['audio']): raise ValueError('Audio stream count changed')
    for original, actual in zip(spec['audio'], audio):
        if any(original.get(k) != actual.get(k) for k in ('codec_name', 'sample_rate', 'channels')):
            raise ValueError('Copied audio codec/rate/channels changed')
        if abs(float(original.get('duration', 0)) - float(actual.get('duration', 0))) > .05:
            raise ValueError('Audio duration changed')
        source_offset = float(original.get('start_time', 0)) - float(spec['video'].get('start_time', 0))
        output_offset = float(actual.get('start_time', 0)) - float(video.get('start_time', 0))
        if abs(source_offset - output_offset) > .05: raise ValueError('Audio/video relative starting offset changed')
    audio_evidence = {'status': 'NOT_APPLICABLE', 'reason': 'Source contains no audio'}
    if spec['audio']:
        original_hash = audio_hash(source, ffmpeg, timeout)
        result_hash = audio_hash(path, ffmpeg, timeout)
        if original_hash != result_hash: raise ValueError('Copied compressed audio packet SHA changed')
        audio_evidence = {'status': 'PASS', 'policy': 'copy all source audio streams', 'source_packet_sha256': original_hash, 'output_packet_sha256': result_hash, 'start_offset_tolerance_seconds': .05}
    decoded = command([ffmpeg, '-v', 'error', '-xerror', '-i', path, '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'], timeout=timeout)
    return {'status': 'PASS', 'probe': output, 'full_decode': {'exit_code': decoded.returncode, 'stderr': decoded.stderr[-2000:]},
            'audio': audio_evidence, 'frames': frames, 'video_duration_seconds': expected_video_duration}


def publish_exclusive(temporary, output):
    if os.name == 'nt':
        os.rename(temporary, output)  # Windows rename fails atomically if destination exists.
    else:
        os.link(temporary, output)  # atomic, no replacement on POSIX either
        temporary.unlink()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for field in ('input', 'output', 'model', 'ffmpeg', 'ffprobe', 'receipt'):
        result.add_argument('--' + field, type=Path, required=True)
    result.add_argument('--width', type=int, required=True)
    result.add_argument('--height', type=int, required=True)
    result.add_argument('--fps', default=None, help='Must equal source CFR, e.g.24 or24000/1001')
    result.add_argument('--device', choices=('cuda', 'cpu'), default='cuda')
    result.add_argument('--precision', choices=('fp32', 'fp16'), default='fp32')
    result.add_argument('--tile', type=int, default=192)
    result.add_argument('--tile-pad', type=int, default=32)
    result.add_argument('--threads', type=int, default=2)
    result.add_argument('--timeout', type=int, default=600)
    result.add_argument('--crf', type=int, default=18)
    result.add_argument('--preset', choices=('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'), default='medium')
    result.add_argument('--expected-model-sha256', default=None)
    return result


def run(args):
    started = time.monotonic()
    source, output, model, receipt_path = [Path(getattr(args, k)).resolve() for k in ('input', 'output', 'model', 'receipt')]
    result = {'status': 'RUNNING', 'method': METHOD, 'temporal_model': False, 'source_path': str(source),
        'output_path': str(output), 'model_path': str(model), 'frames_processed': 0, 'source_frames': None,
        'scale': 4, 'tile': {'requested': args.tile, 'actual_minimum': args.tile, 'context_pad': args.tile_pad},
        'requested_width': args.width, 'requested_height': args.height, 'device': args.device,
        'encoder': {'codec': 'libx264', 'crf': args.crf, 'preset': args.preset, 'pixel_format': 'yuv420p', 'audio': 'copy'},
        'limitations': ['Single-image Real-ESRGAN applied independently to every frame; temporal flicker/texture hallucination require human review.',
                        'No face enhancement, interpolation, native 4K generation, or automatic visual-quality score.',
                        'Native x4 model output is resized with Lanczos only when target dimensions differ.'],
        'visual_review': 'NOT RUN', 'started_at_utc': dt.datetime.now(dt.timezone.utc).isoformat()}
    reader = encoder = None
    unique = uuid.uuid4().hex
    temporary = output.with_name(output.stem + '.partial-' + unique + '.mp4')
    reader_log = output.with_name(output.stem + '.sr-' + unique + '.decode.log')
    encoder_log = output.with_name(output.stem + '.sr-' + unique + '.encode.log')
    try:
        if len({source, output, model, receipt_path}) != 4:
            raise ValueError('Input, output, model and receipt paths must be distinct')
        if output.exists() or receipt_path.exists():
            raise FileExistsError('Existing output or receipt is preserved; select new version paths')
        if not source.is_file() or not model.is_file(): raise FileNotFoundError('Input/model file missing')
        if output.suffix.lower() != '.mp4': raise ValueError('Output must be a new .mp4 path')
        if not 32 <= args.tile <= 512 or not 0 <= args.tile_pad <= 64 or not 1 <= args.threads <= 16:
            raise ValueError('Tile must be32..512, tile-pad0..64 and threads1..16')
        if args.timeout < 5: raise ValueError('Timeout must be at least5 seconds')
        if not 0 <= args.crf <= 51: raise ValueError('CRF must be0..51')
        result['source_sha256'], result['model_sha256'] = sha256(source), sha256(model)
        if result['model_sha256'] != OFFICIAL_WEIGHT_SHA256:
            raise ValueError('This helper only supports the audited official RealESRGAN_x4plus v0.1.0 weights')
        if args.expected_model_sha256 and args.expected_model_sha256.lower() != result['model_sha256']:
            raise ValueError('Model SHA256 does not match the selected verified weight')
        input_probe = probe(source, args.ffprobe, args.timeout)
        spec = source_spec(input_probe, args.width, args.height, args.fps)
        result.update(source_frames=spec['frames'], fps=spec['fps'], source_width=spec['width'], source_height=spec['height'],
            source_probe=input_probe, native_model_width=spec['width'] * 4, native_model_height=spec['height'] * 4,
            post_model_resize='lanczos' if (args.width, args.height) != (spec['width'] * 4, spec['height'] * 4) else 'none',
            cadence=validate_cadence(source, args.ffprobe, spec, args.timeout))
        descriptor, torch, dtype, runtime = load_model(model, args.device, args.precision, args.threads)
        result.update(runtime_versions=runtime, model_architecture=descriptor.architecture.id, model_tags=descriptor.tags,
            model_parameters=sum(p.numel() for p in descriptor.model.parameters()))
        import numpy as np
        from PIL import Image
        output.parent.mkdir(parents=True, exist_ok=True)
        with reader_log.open('x', encoding='utf-8') as decode_errors, encoder_log.open('x', encoding='utf-8') as encode_errors:
            reader = subprocess.Popen([str(args.ffmpeg), '-v', 'error', '-threads', str(args.threads), '-noautorotate', '-i', str(source),
                '-map', '0:v:0', '-an', '-sn', '-dn', '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo', 'pipe:1'],
                stdout=subprocess.PIPE, stderr=decode_errors, creationflags=WINDOW_FLAGS)
            encoder = subprocess.Popen([str(args.ffmpeg), '-n', '-v', 'error', '-threads', str(args.threads), '-f', 'rawvideo',
                '-pix_fmt', 'rgb24', '-s:v', f'{args.width}x{args.height}', '-r', spec['fps'], '-i', 'pipe:0', '-i', str(source),
                '-map', '0:v:0', '-map', '1:a?', '-map_metadata', '1', '-c:v', 'libx264', '-preset', args.preset, '-crf', str(args.crf),
                '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough', '-c:a', 'copy', '-movflags', '+faststart', str(temporary)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encode_errors, creationflags=WINDOW_FLAGS)
            actual_tile = args.tile
            while True:
                data = read_exact(reader.stdout, spec['width'] * spec['height'] * 3)
                if data is None: break
                rgb = np.frombuffer(data, dtype=np.uint8).reshape(spec['height'], spec['width'], 3)
                while True:
                    try:
                        reconstructed = tiled_frame(rgb, descriptor, torch, dtype, args.device, actual_tile, args.tile_pad)
                        break
                    except torch.OutOfMemoryError:
                        if args.device != 'cuda' or actual_tile <= 32: raise
                        actual_tile = max(32, actual_tile // 2)
                        torch.cuda.empty_cache()
                if (reconstructed.shape[1], reconstructed.shape[0]) != (args.width, args.height):
                    image = Image.fromarray(reconstructed).resize((args.width, args.height), Image.Resampling.LANCZOS)
                    encoded = image.tobytes()
                else: encoded = reconstructed.tobytes()
                encoder.stdin.write(encoded)
                result['frames_processed'] += 1
                result['tile']['actual_minimum'] = actual_tile
                if result['frames_processed'] > spec['frames']: raise ValueError('Decoded more frames than source probe')
                print(json.dumps({'event': 'frame_processed', 'frames_processed': result['frames_processed'], 'source_frames': spec['frames']}), file=sys.stderr, flush=True)
                del rgb, data, reconstructed, encoded
            encoder.stdin.close()
            if reader.wait(timeout=args.timeout): raise RuntimeError('Source video decoding failed; see decode log')
            if encoder.wait(timeout=args.timeout): raise RuntimeError('Output encoding/mux failed; see encode log')
        if result['frames_processed'] != spec['frames']: raise ValueError('Not every source frame was processed')
        result['validation'] = verify_output(temporary, source, spec, args.width, args.height, args.ffmpeg, args.ffprobe, args.timeout)
        if sha256(source) != result['source_sha256']: raise ValueError('Source bytes changed during processing')
        if sha256(model) != result['model_sha256']: raise ValueError('Model bytes changed during processing')
        result.update(output_sha256=sha256(temporary), output_bytes=temporary.stat().st_size,
            duration_seconds=result['validation']['video_duration_seconds'],
            peak_cuda_memory_bytes=torch.cuda.max_memory_allocated() if args.device == 'cuda' else None)
        publish_exclusive(temporary, output)
        result['status'] = 'PASS'
    except Exception as exc:
        result.update(status='FAIL', error_type=type(exc).__name__, error=str(exc), partial_path=str(temporary) if temporary.exists() else None)
    finally:
        stop_process(reader); stop_process(encoder)
        result.update(elapsed_seconds=round(time.monotonic() - started, 6),
            logs={'decode': str(reader_log) if reader_log.exists() else None, 'encode': str(encoder_log) if encoder_log.exists() else None})
        # Never overwrite a previous receipt, even when processing failed.
        if not receipt_path.exists() and receipt_path not in {source, output, model}:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with receipt_path.open('x', encoding='utf-8') as stream: json.dump(result, stream, ensure_ascii=False, indent=2)
            except FileExistsError: pass
    return result


def main():
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args()
    result = run(args)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
