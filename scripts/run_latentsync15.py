"""Offline LatentSync 1.5 adapter. Requires the production GPU lease.

The upstream code runs only in a unique ASCII E-drive job directory. Original
media is immutable, all faces must be single, and output retains source timing.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.schemas_routing import RuntimeJob, RuntimeReceipt
from deployment_common import file_digest as digest

RUNTIME = Path('E:/VideoFactoryRuntime')
MODEL_ID = 'latentsync-1.5'
COMMIT = '02b975b9ade6f55644d4629e64a56cd36fc07f1d'


def sha(path):
    return digest(Path(path), 'sha256')


def runtime_environment(ffmpeg):
    cache=RUNTIME/'cache'
    directories={
        'HF_HOME':cache/'huggingface', 'TORCH_HOME':cache/'torch',
        'CUDA_CACHE_PATH':cache/'latentsync15/cuda',
        'TORCHINDUCTOR_CACHE_DIR':cache/'latentsync15/inductor',
        'NUMBA_CACHE_DIR':cache/'latentsync15/numba',
        'MPLCONFIGDIR':cache/'latentsync15/matplotlib',
        'TEMP':RUNTIME/'temp','TMP':RUNTIME/'temp'}
    for path in directories.values():
        if not path.resolve().is_relative_to(RUNTIME.resolve()):
            raise ValueError('Runtime cache path escapes fixed E workspace')
        path.mkdir(parents=True,exist_ok=True)
    return {**{name:str(path) for name,path in directories.items()},
        'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1',
        'IMAGEIO_FFMPEG_EXE':str(Path(ffmpeg).resolve()),'MPLBACKEND':'Agg'}


def run(args):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True,
        encoding='utf-8', errors='replace', shell=False,
        creationflags=0x08000000 if os.name == 'nt' else 0)
    if result.returncode:
        raise RuntimeError('Media subprocess failed: ' + result.stderr[-1800:])
    return result.stdout


def probe(path, ffprobe):
    return json.loads(run([ffprobe, '-v', 'error', '-count_frames', '-show_streams',
                           '-show_format', '-of', 'json', path]))


def video_info(value):
    stream = next((s for s in value['streams'] if s['codec_type'] == 'video'), None)
    if not stream:
        raise ValueError('Input has no video stream')
    frames = int(stream.get('nb_read_frames') or stream.get('nb_frames') or 0)
    fps = Fraction(stream['avg_frame_rate'])
    if frames < 1 or not 1 <= float(fps) <= 25:
        raise ValueError('This verified workflow accepts constant frame rates up to 25 fps; convert higher-rate sources explicitly')
    duration = frames / float(fps)
    if duration > 30 or max(stream['width'], stream['height']) > 1920:
        raise ValueError('This tested single-shot workflow accepts at most 30 seconds / 1920 pixels')
    return {'width':stream['width'], 'height':stream['height'], 'frames':frames,
            'fps':str(fps), 'duration_seconds':duration, 'codec':stream['codec_name']}


def assert_constant_timing(path,info,ffprobe):
    value=json.loads(run([ffprobe,'-v','error','-select_streams','v:0','-show_frames',
        '-show_entries','frame=best_effort_timestamp_time','-of','json',path]))
    times=[float(frame['best_effort_timestamp_time']) for frame in value.get('frames',[]) if 'best_effort_timestamp_time' in frame]
    interval=1/float(Fraction(info['fps']))
    if len(times)!=info['frames'] or not times or abs(times[0])>interval/2:
        raise ValueError('Missing frame timestamps or nonzero source start requires explicit normalization')
    if any(abs((right-left)-interval)>max(.002,interval*.02) for left,right in zip(times,times[1:])):
        raise ValueError('Variable frame timing is not supported; normalize VFR explicitly before lip sync')
    if abs((times[-1]-times[0])-(len(times)-1)*interval)>max(.002,interval*.02):
        raise ValueError('Source frame timing drifts from declared constant rate')


def validate(job, repo, model_dir):
    if job.slot != 'lip_sync' or job.model_id != MODEL_ID:
        raise ValueError('Only lip_sync / latentsync-1.5 is implemented')
    target = job.inputs.get('target_participant_id')
    participants = job.inputs.get('participant_ids')
    if not isinstance(target, str) or not target or not isinstance(participants, list) or participants != [target]:
        raise ValueError('Exactly one explicit participant and matching target_participant_id are required')
    allowed = {'seed','inference_steps','guidance_scale','duration_policy'}
    if set(job.parameters) - allowed:
        raise ValueError('Unsupported LatentSync parameters')
    seed, steps = job.parameters.get('seed',1247), job.parameters.get('inference_steps',20)
    guidance = job.parameters.get('guidance_scale',1.5)
    if isinstance(seed,bool) or not isinstance(seed,int) or not 0 <= seed <= 2**32-1:
        raise ValueError('seed must be a 32-bit unsigned integer')
    if isinstance(steps,bool) or not isinstance(steps,int) or not 1 <= steps <= 50:
        raise ValueError('inference_steps must be 1..50')
    if isinstance(guidance,bool) or not isinstance(guidance,(int,float)) or not math.isfinite(guidance) or not 1 <= guidance <= 3:
        raise ValueError('guidance_scale must be finite, 1..3')
    if job.parameters.get('duration_policy','match_video') != 'match_video':
        raise ValueError('Only explicit source-duration preservation is implemented')
    video, audio = (Path(str(job.inputs.get(key,''))).resolve() for key in ('video_path','audio_path'))
    output, receipt = Path(job.output_path).resolve(), Path(job.receipt_path).resolve()
    if not video.is_file() or not audio.is_file() or video == audio:
        raise ValueError('Distinct existing local video/audio files are required')
    if output.suffix.lower() != '.mp4' or output in {video,audio,receipt} or receipt in {video,audio}:
        raise ValueError('Output must be a distinct new MP4 and receipt')
    if output.exists() or receipt.exists():
        raise FileExistsError('Outputs are immutable; select a new task ID/output')
    provenance = json.loads((RUNTIME/'manifests/latentsync15_code.json').read_text('utf-8'))
    if provenance.get('commit') != COMMIT or len(provenance.get('files',[])) != 119:
        raise ValueError('Pinned source archive receipt missing')
    for entry in provenance['files']:
        source = (repo/entry['path']).resolve()
        if not source.is_relative_to(repo.resolve()) or not source.is_file() or source.stat().st_size != entry['bytes'] or sha(source) != entry['sha256']:
            raise ValueError('Fixed source checksum mismatch')
    plan = json.loads((RUNTIME/'manifests/latentsync15_plan.json').read_text('utf-8'))
    for spec in plan['files']:
        path = Path(spec['target'])
        if not path.resolve().is_relative_to(model_dir.resolve()) or not path.is_file() or path.stat().st_size != spec['expected_bytes'] or digest(path,spec['hash_algorithm']) != spec['expected_hash']:
            raise ValueError('Missing or checksum-mismatched model component: ' + spec['repo_path'])
    return video,audio,output,receipt,seed,steps,guidance


@contextmanager
def offline():
    import socket
    connect = socket.socket.connect
    def denied(*args,**kwargs):
        raise RuntimeError('Offline inference blocks implicit model/network downloads')
    socket.socket.connect = denied
    try:
        yield
    finally:
        socket.socket.connect = connect


def dependency_check(args):
    """Actual CPU imports and the pinned OpenCV writer; never create a model."""
    report=Path(args.check_report).resolve()
    if report.exists():
        raise FileExistsError('Dependency report exists; preserve it and choose a new path')
    started=time.perf_counter()
    data={'status':'RUNNING','scope':'Actual imports and explicit synthetic CPU IO only; not model inference',
        'model_load':'NOT RUN','gpu_inference':'NOT RUN','source_commit':COMMIT,'python':sys.version,
        'python_executable':sys.executable,'helper_sha256':sha(__file__),'checked_at_utc':datetime.now(timezone.utc).isoformat()}
    try:
        repo=Path(args.repo).resolve()
        ffmpeg,ffprobe=shutil.which(args.ffmpeg),shutil.which(args.ffprobe)
        if not ffmpeg or not ffprobe:raise FileNotFoundError('Explicit existing FFmpeg/FFprobe required')
        os.environ.update(runtime_environment(ffmpeg));os.environ['CUDA_VISIBLE_DEVICES']='-1'
        os.environ['PATH']=str(Path(ffmpeg).parent)+os.pathsep+os.environ.get('PATH','')
        sys.path.insert(0,str(repo))
        provenance=json.loads((RUNTIME/'manifests/latentsync15_code.json').read_text('utf-8'))
        if provenance.get('commit')!=COMMIT or len(provenance.get('files',[]))!=119:
            raise ValueError('Pinned119-file code provenance differs')
        for entry in provenance['files']:
            path=(repo/entry['path']).resolve()
            if not path.is_relative_to(repo) or path.stat().st_size!=entry['bytes'] or sha(path)!=entry['sha256']:
                raise ValueError('Pinned code SHA mismatch: '+entry['path'])
        with offline():
            import numpy as np
            import torch
            import torchvision
            import cv2
            import imageio
            import imageio_ffmpeg
            from diffusers import AutoencoderKL, DDIMScheduler
            from accelerate.utils import set_seed
            from omegaconf import OmegaConf
            from latentsync.models.unet import UNet3DConditionModel
            from latentsync.pipelines.lipsync_pipeline import LipsyncPipeline
            from latentsync.whisper.audio2feature import Audio2Feature
            from latentsync.utils.image_processor import ImageProcessor
            from latentsync.utils.util import write_video
            resolved=imageio_ffmpeg.get_ffmpeg_exe()
            if Path(resolved).resolve()!=Path(ffmpeg).resolve():
                raise ValueError('imageio did not use the forced existing executable')
            directory=RUNTIME/'temp'/('latentsync_dependency_'+uuid.uuid4().hex)
            directory.mkdir(parents=True,exist_ok=False)
            output=directory/'synthetic_opencv_mp4v.mp4'
            frames=np.zeros((5,64,64,3),dtype=np.uint8)
            for index in range(5):frames[index,:,:,0]=index*40
            cv2.setNumThreads(2);write_video(str(output),frames,fps=25)
            actual=video_info(probe(output,ffprobe))
            if (actual['width'],actual['height'],actual['frames'],Fraction(actual['fps']))!=(64,64,5,Fraction(25)):
                raise ValueError('Pinned OpenCV mp4v CPU output mismatch')
            run([ffmpeg,'-v','error','-xerror','-i',output,'-f','null','-'])
            if torch.cuda.is_initialized():raise RuntimeError('CPU dependency probe unexpectedly initialized Torch CUDA')
        names=['torch','torchvision','numpy','diffusers','transformers','huggingface-hub','accelerate','decord','opencv-python',
               'opencv-contrib-python','mediapipe','imageio','imageio-ffmpeg','face-alignment','omegaconf','librosa','safetensors','pydantic']
        data.update(status='PASS_IMPORT_AND_CPU_IO_ONLY',versions={name:importlib.metadata.version(name) for name in names},
            torch_cuda_initialized=False,torch_cuda_runtime=torch.version.cuda,opencv_version=cv2.__version__,
            imported_model_classes_only=True,weights_loaded=False,imageio_ffmpeg_exe=resolved,
            source_files_verified=119,cpu_writer={'path':str(output),'sha256':sha(output),'actual':actual,'full_decode':True,
                'method':'Pinned util.write_video -> cv2.VideoWriter mp4v; synthetic5-frame CPU fixture'},
            tools={name:{'path':str(Path(path).resolve()),'sha256':sha(path),'version':run([path,'-version']).splitlines()[0]}
                   for name,path in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]})
    except Exception as error:
        data.update(status='FAIL',error=type(error).__name__+': '+str(error),error_traceback=traceback.format_exc())
    data['elapsed_seconds']=time.perf_counter()-started
    report.parent.mkdir(parents=True,exist_ok=True)
    with report.open('x',encoding='utf-8') as stream:json.dump(data,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'status':data['status'],'report':str(report),'model_load':'NOT RUN'},ensure_ascii=True))
    return 0 if data['status']=='PASS_IMPORT_AND_CPU_IO_ONLY' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--job')
    mode.add_argument('--check-dependencies',action='store_true')
    parser.add_argument('--check-report',default=str(ROOT/'reports/e_drive_deployment/latentsync15_dependency_import.json'))
    parser.add_argument('--repo',default=str(RUNTIME/'apps/LatentSync-1.5'))
    parser.add_argument('--model-dir',default='E:/Models/Factory/LatentSync-1.5')
    parser.add_argument('--ffmpeg',default='E:/ComfyUI/ffmpeg.exe')
    parser.add_argument('--ffprobe',default='E:/ComfyUI/ffprobe.exe')
    args = parser.parse_args()
    if args.check_dependencies:return dependency_check(args)
    job = RuntimeJob.model_validate_json(Path(args.job).read_text('utf-8-sig'))
    repo, model_dir = Path(args.repo).resolve(), Path(args.model_dir).resolve()
    started = time.perf_counter()
    data = {'protocol':'video-factory-runtime-1','task_id':job.task_id,'slot':job.slot,
            'model_id':job.model_id,'status':'failed','actual':{},'runtime':{},'inputs_sha256':{}}
    receipt = Path(job.receipt_path).resolve()
    if receipt.exists():
        raise FileExistsError('Receipt already exists; no duplicate execution')
    try:
        video,audio,output,receipt,seed,steps,guidance = validate(job,repo,model_dir)
        ffmpeg, ffprobe = shutil.which(args.ffmpeg), shutil.which(args.ffprobe)
        if not ffmpeg or not ffprobe:
            raise FileNotFoundError('FFmpeg and FFprobe must be configured before inference')
        os.environ.update(runtime_environment(ffmpeg))
        os.environ['PATH'] = str(Path(ffmpeg).parent) + os.pathsep + os.environ.get('PATH','')
        data['runtime'].update({'helper_sha256':sha(__file__),
            'source_manifest_sha256':sha(RUNTIME/'manifests/latentsync15_code.json'),
            'source_files_verified':119,'model_plan_sha256':sha(RUNTIME/'manifests/latentsync15_plan.json'),
            'model_components_verified':'all fixed plan files: exact size and complete pinned digest',
            'requirements_sha256':sha(ROOT/'scripts/requirements-latentsync15-inference.txt')})
        freeze=ROOT/'reports/e_drive_deployment/latentsync15_environment_freeze.txt'
        if freeze.is_file():
            data['runtime']['environment_freeze']={'path':str(freeze),'sha256':sha(freeze)}
        # The pinned main writer is OpenCV mp4v. Any imageio utilities inherit
        # this existing executable and never need runtime binary discovery.
        data['runtime']['media_tools']={name:{'path':str(Path(path).resolve()),'sha256':sha(path),
            'version':run([path,'-version']).splitlines()[0]} for name,path in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]}
        data['runtime']['imageio_ffmpeg_exe']=os.environ['IMAGEIO_FFMPEG_EXE']
        source = video_info(probe(video,ffprobe))
        assert_constant_timing(video,source,ffprobe)
        audio_probe = probe(audio,ffprobe)
        audio_stream = next((s for s in audio_probe['streams'] if s['codec_type']=='audio'),None)
        if not audio_stream:
            raise ValueError('Input has no audio stream')
        audio_duration = float(audio_stream.get('duration') or audio_probe['format']['duration'])
        if audio_duration <= 0 or audio_duration > source['duration_seconds'] + 1/float(Fraction(source['fps'])):
            raise ValueError('Audio is longer than the video; explicit editorial timing is required')
        data['inputs_sha256'] = {'video_path':sha(video),'audio_path':sha(audio)}
        workspace = RUNTIME/'temp/latentsync'/uuid.uuid4().hex
        workspace.mkdir(parents=True,exist_ok=False)
        shutil.copytree(repo/'configs',workspace/'configs')
        shutil.copy2(video,workspace/'original_video.mp4'); shutil.copy2(audio,workspace/'original_audio.wav')
        native_frames = (math.ceil(source['duration_seconds']*25/16)+1)*16
        native_duration = native_frames/25
        run([ffmpeg,'-nostdin','-v','error','-n','-i',workspace/'original_video.mp4',
            '-vf',f'fps=25,tpad=stop_mode=clone:stop_duration=2','-an','-frames:v',native_frames,
            '-c:v','libx264','-crf','16','-pix_fmt','yuv420p',workspace/'input_video.mp4'])
        run([ffmpeg,'-nostdin','-v','error','-n','-i',workspace/'original_audio.wav','-af','apad',
            '-t',f'{native_duration:.8f}','-ar','16000','-ac','1','-c:a','pcm_s16le',workspace/'input_audio.wav'])
        torch_cache = RUNTIME/'cache/torch/hub/checkpoints';torch_cache.mkdir(parents=True,exist_ok=True)
        for item in (model_dir/'checkpoints/auxiliary').iterdir():
            cached = torch_cache/item.name
            if cached.exists():
                if sha(cached) != sha(item):
                    raise ValueError('Existing face-landmark cache differs; preserved for review')
            else:
                os.link(item,cached)
        os.chdir(workspace)
        sys.path.insert(0,str(repo))
        with offline():
            import torch
            from omegaconf import OmegaConf
            from diffusers import AutoencoderKL, DDIMScheduler
            from accelerate.utils import set_seed
            from latentsync.models.unet import UNet3DConditionModel
            from latentsync.pipelines.lipsync_pipeline import LipsyncPipeline
            from latentsync.whisper.audio2feature import Audio2Feature
            from latentsync.utils.image_processor import ImageProcessor
            if not torch.cuda.is_available():
                raise RuntimeError('CUDA is required by this fixed LatentSync inference implementation')
            torch.cuda.reset_peak_memory_stats()
            # Check every frame rather than allowing upstream's implicit first face.
            original_affine = ImageProcessor.affine_transform
            def single_face(self,image,allow_multi_faces=False):
                return original_affine(self,image,allow_multi_faces=False)
            ImageProcessor.affine_transform = single_face
            config = OmegaConf.load(repo/'configs/unet/stage2.yaml')
            dtype = torch.float16
            scheduler = DDIMScheduler.from_pretrained(str(repo/'configs'),local_files_only=True)
            encoder = Audio2Feature(model_path=str(model_dir/'checkpoints/whisper/tiny.pt'),device='cuda',
                num_frames=16,audio_feat_length=config.data.audio_feat_length)
            vae = AutoencoderKL.from_pretrained(str(model_dir/'sd-vae-ft-mse'),torch_dtype=dtype,local_files_only=True)
            vae.config.scaling_factor = .18215;vae.config.shift_factor = 0
            unet,_ = UNet3DConditionModel.from_pretrained(OmegaConf.to_container(config.model),
                str(model_dir/'checkpoints/latentsync_unet.pt'),device='cpu')
            pipeline = LipsyncPipeline(vae=vae,audio_encoder=encoder,
                denoising_unet=unet.to(dtype=dtype),scheduler=scheduler).to('cuda')
            set_seed(seed)
            pipeline(video_path='input_video.mp4',audio_path='input_audio.wav',video_out_path='model_output.mp4',
                num_frames=16,num_inference_steps=steps,guidance_scale=guidance,weight_dtype=dtype,
                width=256,height=256,mask_image_path=str(repo/'latentsync/utils/mask.png'))
            torch.cuda.synchronize()
            data['runtime'].update({'python':sys.version,'torch':torch.__version__,'cuda':torch.version.cuda,
                'device':torch.cuda.get_device_name(),'peak_allocated_bytes':torch.cuda.max_memory_allocated(),
                'source_commit':COMMIT,'native_face_resolution':256,'offline':True})
        intermediate = workspace/'model_output.mp4'
        if not intermediate.is_file():
            raise RuntimeError('Upstream did not create the expected video')
        generated = video_info(probe(intermediate,ffprobe))
        if generated['duration_seconds'] + 1/25 < source['duration_seconds']:
            raise RuntimeError('Model result dropped source frames despite padding')
        staged_output = workspace/'delivery.mp4'
        run([ffmpeg,'-nostdin','-v','error','-n','-i',intermediate,'-i',workspace/'original_audio.wav',
            '-map','0:v:0','-map','1:a:0','-vf',f"fps={source['fps']}",'-frames:v',source['frames'],
            '-af','apad','-t',f"{source['duration_seconds']:.9f}",'-c:v','libx264','-crf','18',
            '-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',staged_output])
        final_probe = probe(staged_output,ffprobe); final = video_info(final_probe)
        if (final['width'],final['height'],final['frames'],Fraction(final['fps'])) != (source['width'],source['height'],source['frames'],Fraction(source['fps'])):
            raise RuntimeError('Final video does not preserve original dimensions, FPS and frames')
        if not any(s['codec_type']=='audio' for s in final_probe['streams']):
            raise RuntimeError('Final supplied audio is absent')
        run([ffmpeg,'-nostdin','-v','error','-xerror','-i',staged_output,'-map','0:v','-map','0:a','-f','null','-'])
        if sha(video) != data['inputs_sha256']['video_path'] or sha(audio) != data['inputs_sha256']['audio_path']:
            raise RuntimeError('Source media changed while processing')
        output.parent.mkdir(parents=True,exist_ok=True)
        # Same-drive atomic exclusive publication; no source or previous output is overwritten.
        with output.open('xb') as stream, staged_output.open('rb') as source_stream:
            shutil.copyfileobj(source_stream,stream,8*1024*1024)
        data.update(status='succeeded',output_path=str(output),output_sha256=sha(output),
            actual={**final,'full_decode':True,'source':source,'supplied_audio_duration_seconds':audio_duration,
                'native_video_fps':25,'native_window_frames':16,'padded_native_frames':native_frames,
                'steps':steps,'guidance_scale':guidance,'seed':seed,'source_preserved':True,
                'participant_id':job.inputs['target_participant_id'],'all_processed_25fps_frames_single_face_checked':True,
                'source_constant_frame_timing_checked':True,'single_face_detection_scope':'all model input frames after CFR <=25fps source resampling to 25fps',
                'visual_lip_accuracy':'not_human_reviewed','workspace':str(workspace)})
    except Exception as exc:
        data['error'] = f'{type(exc).__name__}: {exc}'
        data['runtime']['failure_traceback']=traceback.format_exc()
    data['elapsed_seconds'] = time.perf_counter()-started
    receipt.parent.mkdir(parents=True,exist_ok=True)
    with receipt.open('x',encoding='utf-8') as stream:
        stream.write(RuntimeReceipt.model_validate(data).model_dump_json(indent=2))
    print(json.dumps({'status':data['status'],'receipt_path':str(receipt),'elapsed_seconds':data['elapsed_seconds']}),flush=True)
    if data['status'] != 'succeeded':
        raise SystemExit(1)


if __name__ == '__main__':
    sys.exit(main())
