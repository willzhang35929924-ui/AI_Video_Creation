"""Offline fixed IndexTTS-2.5 RuntimeJob -> new PCM WAV and bound receipt.

No downloads, serving UI, defaults or character publication. CUDA work must be
scheduled by the application's exclusive GPU gate before this CLI is called.
"""
from __future__ import annotations
import argparse, contextlib, datetime as dt, hashlib, importlib.metadata, json, math, os, random, sys, tempfile, time, wave
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.schemas_routing import RuntimeJob,RuntimeReceipt
MODEL_ID='indextts-2.5'
CODE_COMMIT='ee40fa7d6c6b8a2c7f06105f9f1e65775b74868c'
MODEL_REVISION='c39ce5ba981572cb187443877ff559dfb246ce63'
APP=Path('E:/VideoFactoryRuntime/apps/IndexTTS')
MODELS=Path('E:/Models/Factory/IndexTTS-2.5')
PLAN=Path('E:/VideoFactoryRuntime/manifests/indextts25_plan.json')
CODE_RECORD=Path('E:/VideoFactoryRuntime/manifests/indextts25_code.json')
PLAN_SHA256='d77baed1ac63c03058e03abe8dda56137b009bfc585d345db055acd306b83a59'
CODE_RECORD_SHA256='36e8b3911dba33e5865b66a6fdfd03a1a59f5f6337cfad3cdaf6d213ca62054d'

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def digest(path,algorithm='sha256'):
    path=Path(path);result=hashlib.sha1() if algorithm=='git-sha1' else hashlib.sha256()
    if algorithm=='git-sha1':result.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):result.update(block)
    return result.hexdigest()
def content_hash(value,kind):
    # Retrieval time and ZIP compression metadata are not runtime inputs. Pin
    # exact component/source paths, revisions and hashes, including any patches.
    keys={'plan':('model_id','code_commit','model_revision','files'),
          'code':('code_commit','tree_sha','method','files','patches')}[kind]
    canonical={key:value[key] for key in keys if key in value}
    return hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
def emit(stage,**data):print(json.dumps({'stage':stage,'at_utc':now(),**data},ensure_ascii=True),flush=True)
def write_new_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);descriptor,name=tempfile.mkstemp(prefix=path.name+'.',suffix='.pending',dir=path.parent)
    temporary=Path(name)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as stream:json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        os.link(temporary,path)
    finally:
        with contextlib.suppress(OSError):temporary.unlink()

def preflight(job,repo,model_dir):
    if job.slot!='dialogue_generation' or job.model_id!=MODEL_ID:raise ValueError('Fixed adapter accepts only IndexTTS2.5 dialogue_generation')
    if repo!=APP.resolve() or model_dir!=MODELS.resolve():raise ValueError('Repository/model directory differs from fixed deployment')
    output,receipt=Path(job.output_path).resolve(),Path(job.receipt_path).resolve()
    if output==receipt or output.suffix.lower()!='.wav' or output.exists() or receipt.exists():raise ValueError('Output and receipt must be new distinct files')
    reference=Path(str(job.inputs.get('reference_audio_path',''))).resolve()
    if not reference.is_file() or reference.suffix.lower() not in {'.wav','.flac'} or reference.stat().st_size>100*1024*1024:raise ValueError('A local valid reference WAV/FLAC is required')
    text=job.inputs.get('text')
    if not isinstance(text,str) or not 1<=len(text.strip())<=2000:raise ValueError('Literal spoken text must be 1..2000 characters')
    if job.inputs.get('prompt_text'):raise ValueError('IndexTTS2.5 does not accept a CosyVoice prompt transcript; choose an explicit compatible task')
    unknown=set(job.parameters)-{'device','use_bf16','speed','language','seed'}
    if unknown:raise ValueError('Unsupported runtime parameters: '+','.join(sorted(unknown)))
    device=job.parameters.get('device','cuda');bf16=job.parameters.get('use_bf16',True)
    if device not in {'cuda','cpu'} or not isinstance(bf16,bool):raise ValueError('Invalid device/use_bf16')
    if device=='cpu' and bf16:raise ValueError('CPU requires explicit use_bf16=false; no silent precision fallback')
    speed=job.parameters.get('speed',1)
    if isinstance(speed,bool) or not isinstance(speed,(int,float)) or not math.isfinite(speed) or not .5<=speed<=2:raise ValueError('speed must be finite 0.5..2')
    language=str(job.parameters.get('language','ZH')).upper()
    if language not in {'ZH','EN','JA','ES','AR'}:raise ValueError('Unsupported language')
    seed=job.parameters.get('seed',1247)
    if isinstance(seed,bool) or not isinstance(seed,int) or not 0<=seed<=2**32-1:raise ValueError('seed must be uint32')
    plan=json.loads(PLAN.read_text('utf-8'));source=json.loads(CODE_RECORD.read_text('utf-8'))
    if content_hash(plan,'plan')!=PLAN_SHA256 or content_hash(source,'code')!=CODE_RECORD_SHA256:raise ValueError('Pinned acquisition/code inputs differ from tested adapter')
    if plan['code_commit']!=CODE_COMMIT or plan['model_revision']!=MODEL_REVISION or source['code_commit']!=CODE_COMMIT:raise ValueError('Deployment source revision differs')
    for entry in plan['files']:
        path=Path(entry['target']).resolve()
        if not path.is_relative_to(model_dir) or path.stat().st_size!=entry['expected_bytes'] or digest(path,entry['hash_algorithm'])!=entry['expected_hash']:raise ValueError('Model component missing/changed: '+entry['relative_path'])
    expected_python=set()
    for entry in source['files']:
        path=(repo/entry['path']).resolve()
        if not path.is_relative_to(repo) or path.stat().st_size!=entry['bytes'] or digest(path,'git-sha1')!=entry['git_blob_sha1']:raise ValueError('Pinned source file changed: '+entry['path'])
        if path.suffix=='.py':expected_python.add(path)
    if set(repo.rglob('*.py'))!=expected_python:raise ValueError('Unexpected Python source in fixed repository')
    return output,receipt,reference,text.strip(),device,bf16,float(speed),language,seed,len(plan['files'])

def wav_probe(path):
    with wave.open(str(path),'rb') as stream:
        rate,channels,frames,width=stream.getframerate(),stream.getnchannels(),stream.getnframes(),stream.getsampwidth()
        count=0
        while data:=stream.readframes(65536):count+=len(data)
    if channels!=1 or width!=2 or frames<1 or count!=frames*channels*width:raise ValueError('Full mono PCM16 WAV decode failed')
    return {'sample_rate':rate,'channels':channels,'frames':frames,'sample_width_bytes':width,'duration_seconds':frames/rate,'codec':'pcm_s16le','full_pcm_read':True,'decoded_pcm_bytes':count}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--job',required=True);parser.add_argument('--repo',required=True);parser.add_argument('--model-dir',required=True);args=parser.parse_args()
    started=time.monotonic();job=RuntimeJob.model_validate_json(Path(args.job).read_text('utf-8-sig'));receipt_path=Path(job.receipt_path).resolve()
    receipt=RuntimeReceipt(task_id=job.task_id,slot=job.slot,model_id=job.model_id,status='failed')
    try:
        emit('preflight',task_id=job.task_id)
        repo,model_dir=Path(args.repo).resolve(),Path(args.model_dir).resolve()
        output,receipt_path,reference,text,device,bf16,speed,language,seed,component_count=preflight(job,repo,model_dir)
        receipt.inputs_sha256={'reference_audio':digest(reference),'text_utf8':hashlib.sha256(text.encode()).hexdigest(),'job_json':digest(args.job)}
        cache='E:/VideoFactoryRuntime/cache/indextts25'
        os.environ.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_DATASETS_OFFLINE':'1','MODELSCOPE_OFFLINE':'1',
            'HF_HOME':'E:/Models/.cache/huggingface','HF_HUB_CACHE':str(model_dir/'hf_cache'),'HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_IMPLICIT_TOKEN':'1',
            'MODELSCOPE_CACHE':cache+'/modelscope','TORCH_HOME':cache+'/torch','NLTK_DATA':cache+'/nltk_data',
            'CUDA_CACHE_PATH':cache+'/cuda','TORCH_EXTENSIONS_DIR':cache+'/torch_extensions','TRITON_CACHE_DIR':cache+'/triton',
            'XDG_CACHE_HOME':cache,'NUMBA_CACHE_DIR':cache+'/numba','MPLCONFIGDIR':cache+'/matplotlib',
            'TEMP':'E:/VideoFactoryRuntime/temp','TMP':'E:/VideoFactoryRuntime/temp','TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'2','MKL_NUM_THREADS':'2'})
        for path in (cache,cache+'/cuda',cache+'/torch_extensions',cache+'/triton',cache+'/numba',cache+'/matplotlib'):Path(path).mkdir(parents=True,exist_ok=True)
        if device=='cpu':os.environ['CUDA_VISIBLE_DEVICES']='-1'
        def no_network(event,_args):
            if event in {'socket.connect','socket.getaddrinfo'}:raise RuntimeError('Runtime is offline; prepare missing dependencies/assets before execution')
        sys.addaudithook(no_network);sys.path.insert(0,str(repo));os.chdir(repo)
        with contextlib.redirect_stdout(sys.stderr):
            import numpy as np
            import torch
            from indextts.infer_v2_5 import IndexTTS2
        # Upstream infer module assigns a relative HF cache at import. Reset it
        # to the actual pinned E model cache before constructing any model.
        os.environ['HF_HUB_CACHE']=str(model_dir/'hf_cache')
        torch.set_num_threads(2)
        if device=='cuda':
            if not torch.cuda.is_available():raise RuntimeError('CUDA unavailable; no automatic CPU fallback')
            if torch.cuda.get_device_capability()[0]>=12 and 'sm_120' not in torch.cuda.get_arch_list():raise RuntimeError('Torch wheel lacks Blackwell sm120 kernels')
            if bf16 and not torch.cuda.is_bf16_supported():raise RuntimeError('Requested BF16 is unsupported')
            torch.cuda.reset_peak_memory_stats()
        random.seed(seed);np.random.seed(seed);torch.manual_seed(seed)
        if device=='cuda':torch.cuda.manual_seed_all(seed)
        emit('loading',task_id=job.task_id,device=device,use_bf16=bf16)
        with contextlib.redirect_stdout(sys.stderr):
            model=IndexTTS2(cfg_path=str(model_dir/'config.yaml'),model_dir=str(model_dir),device='cuda:0' if device=='cuda' else 'cpu',use_bf16=bf16,use_cuda_kernel=False,use_deepspeed=False,use_accel=False,use_torch_compile=False,use_qwen_emo=False)
        load_seconds=time.monotonic()-started
        emit('synthesizing',task_id=job.task_id,language=language,speed=speed,duration_factor=1/speed)
        with torch.inference_mode(),contextlib.redirect_stdout(sys.stderr):
            generated=model.infer(spk_audio_prompt=str(reference),text=text,output_path=None,lang=language,
                duration_factor=1/speed,use_emo_text=False,use_random=False,text_normalization=False)
        if not isinstance(generated,tuple) or len(generated)!=2:raise ValueError('Official inference returned no audio array')
        sample_rate,pcm=generated;pcm=np.asarray(pcm)
        if pcm.dtype!=np.int16 or pcm.ndim!=2 or pcm.shape[1]!=1 or not 1<=pcm.shape[0]<=int(sample_rate)*300:raise ValueError('Official output is not bounded mono PCM16')
        if not np.isfinite(pcm).all() or not np.any(pcm):raise ValueError('Official output is nonfinite or silent')
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('xb') as stream:
            with wave.open(stream,'wb') as wav:wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(int(sample_rate));wav.writeframes(pcm.astype('<i2',copy=False).tobytes())
        actual=wav_probe(output);actual.update(language=language,speed=speed,duration_factor=1/speed,seed=seed,
            peak_pcm_abs=int(np.abs(pcm.astype(np.int32)).max()),rms_pcm=float(np.sqrt(np.mean(pcm.astype(np.float64)**2))),
            human_listening_review='NOT RUN',formal_character_voice_approved=False)
        runtime={'python':sys.version,'python_executable':sys.executable,'code_commit':CODE_COMMIT,'model_revision':MODEL_REVISION,
            'device':device,'use_bf16':bf16,'network':'disabled','helper_sha256':digest(__file__),'component_hashes_verified':component_count,
            'acquisition_plan_sha256':PLAN_SHA256,'code_provenance_sha256':CODE_RECORD_SHA256,'load_seconds_including_preflight':round(load_seconds,3),
            'deployment_hash_scheme':'canonical-deployment-inputs-v1',
            'use_qwen_emo':False,'use_cuda_kernel':False,'use_deepspeed':False,'use_accel':False,'use_torch_compile':False,
            'versions':{name:importlib.metadata.version(name) for name in ['torch','torchaudio','transformers','numpy','wetext','soundfile']}}
        if device=='cuda':runtime.update(gpu=torch.cuda.get_device_name(),capability=torch.cuda.get_device_capability(),cuda_runtime=torch.version.cuda,peak_cuda_allocated_bytes=torch.cuda.max_memory_allocated(),peak_cuda_reserved_bytes=torch.cuda.max_memory_reserved())
        receipt.output_path=str(output);receipt.output_sha256=digest(output);receipt.actual=actual;receipt.runtime=runtime;receipt.error=None;receipt.status='succeeded';receipt.elapsed_seconds=round(time.monotonic()-started,3)
        write_new_json(receipt_path,receipt.model_dump());emit('succeeded',task_id=job.task_id,output_path=str(output),receipt_path=str(receipt_path),output_sha256=receipt.output_sha256,actual=actual)
    except Exception as error:
        receipt.status='failed';receipt.error=f'{type(error).__name__}: {error}';receipt.elapsed_seconds=round(time.monotonic()-started,3)
        if not receipt_path.exists():write_new_json(receipt_path,receipt.model_dump())
        emit('failed',task_id=job.task_id,error=receipt.error,receipt_path=str(receipt_path));return 1
    return 0

if __name__=='__main__':sys.exit(main())
