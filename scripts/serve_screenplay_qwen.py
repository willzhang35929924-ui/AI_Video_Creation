"""Fixed, loopback-only Qwen3-VL chat adapter with offline image understanding."""
from __future__ import annotations
import base64
import gc
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
import wave

ROOT=Path(__file__).resolve().parents[1]
MODEL=Path('E:/Models/Factory/Qwen3-VL-2B-Instruct')
MODEL_ID='Qwen/Qwen3-VL-2B-Instruct'
SCRIPT_SHA=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
os.environ.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1',
 'HF_HOME':'E:/Models/.cache/huggingface','TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'4',
 'TORCH_HOME':'E:/VideoFactoryRuntime/cache/screenplay_qwen/torch','CUDA_CACHE_PATH':'E:/VideoFactoryRuntime/cache/screenplay_qwen/cuda',
 'TEMP':'E:/VideoFactoryRuntime/temp','TMP':'E:/VideoFactoryRuntime/temp'})
sys.dont_write_bytecode=True
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

app=FastAPI(title='VideoFactory fixed local screenplay model')
lock=threading.Lock()
weight_verified=False
loaded=False

class ChatRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    model:str
    messages:list[dict]=Field(min_length=1,max_length=20)
    max_tokens:int=Field(default=4096,ge=1,le=8192)
    temperature:float=Field(default=0.2,ge=0,le=2)
    stream:bool=False
    response_format:dict|None=None

class TranscriptionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    audio_base64:str=Field(min_length=1,max_length=4_000_000)

ASR_MODEL=Path('E:/Models/Factory/Whisper-base/base.pt')
ASR_SHA='ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e'

@app.post('/factory/transcribe')
def transcribe(body:TranscriptionRequest):
    model=None
    try:
        raw=base64.b64decode(body.audio_base64,validate=True)
        with wave.open(BytesIO(raw),'rb') as audio:
            if audio.getnchannels()!=1 or audio.getsampwidth()!=2 or audio.getframerate()!=16000 or not 0<audio.getnframes()<=16000*60:
                raise ValueError('Bounded mono 16kHz PCM16 source audio required')
            pcm=audio.readframes(audio.getnframes())
        if not ASR_MODEL.is_file() or digest(ASR_MODEL)!=ASR_SHA:raise ValueError('Verified local Whisper-base is unavailable')
        import numpy as np
        import torch
        import whisper
        if not lock.acquire(blocking=False):raise HTTPException(409,'Local model is busy; transcription not repeated')
        try:
            started=time.monotonic();torch.set_num_threads(4)
            model=whisper.load_model(str(ASR_MODEL),device='cuda')
            result=model.transcribe(np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768.0,
                language=None,fp16=True,temperature=0,condition_on_previous_text=False,verbose=None)
            segments=[{'start':float(s['start']),'end':float(s['end']),'text':s['text'],
                'speaker':'unknown','needs_review':True} for s in result.get('segments',[])]
            return {'text':result['text'],'language':result.get('language','zh'),'segments':segments,
                'speaker':'unknown','needs_review':True,
                'receipt':{'model_id':'openai/whisper-base','model_sha256':ASR_SHA,'input_sha256':hashlib.sha256(raw).hexdigest(),
                  'device':torch.cuda.get_device_name(),'elapsed_seconds':round(time.monotonic()-started,3),
                  'script_sha256':SCRIPT_SHA,'offline':True,'speaker_diarization':False}}
        finally:
            model=None;gc.collect();torch.cuda.empty_cache();lock.release()
    except HTTPException:raise
    except (ValueError,wave.Error) as error:raise HTTPException(422,str(error)) from error
    except Exception as error:raise HTTPException(500,{'code':'local_transcription_failed','message':type(error).__name__+': '+str(error)[:1000]}) from error

@app.get('/health')
def health():
    return {'service':'video-factory-screenplay-qwen-v1','model_id':MODEL_ID,'workspace':str(ROOT),
            'script_sha256':SCRIPT_SHA,'pid':os.getpid(),'loaded':loaded,'busy':lock.locked(),'offline':True}

@app.get('/v1/models')
def models():
    return {'object':'list','data':[{'id':MODEL_ID,'object':'model','owned_by':'local',
       'input_modalities':['text','image'],'video_adapter':'timestamped extracted frames; no audio transcription'}]}

def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024**2),b''):value.update(chunk)
    return value.hexdigest()

def prepare(messages):
    converted=[];images=[];chars=0
    for message in messages:
        if set(message)-{'role','content'} or message.get('role') not in {'system','user','assistant'}:raise ValueError('Unsupported chat message fields')
        content=message.get('content')
        if isinstance(content,str):chars+=len(content);converted.append({'role':message['role'],'content':content});continue
        if not isinstance(content,list):raise ValueError('Content must be text or typed text/image parts')
        parts=[]
        for part in content:
            if part.get('type')=='text' and isinstance(part.get('text'),str):
                chars+=len(part['text']);parts.append({'type':'text','text':part['text']})
            elif part.get('type')=='image_url':
                url=part.get('image_url',{}).get('url','')
                if not isinstance(url,str) or not url.startswith(('data:image/png;base64,','data:image/jpeg;base64,','data:image/webp;base64,')):raise ValueError('Only uploaded inline image bytes are accepted; URLs and filesystem paths are forbidden')
                raw=base64.b64decode(url.split(',',1)[1],validate=True)
                if len(raw)>6*1024**2 or len(images)>=8:raise ValueError('At most eight images, each at most 6 MiB')
                with Image.open(BytesIO(raw)) as original:
                    if original.width*original.height>40_000_000:raise ValueError('Image dimensions are too large')
                    original.load();image=original.convert('RGB');image.thumbnail((768,768))
                images.append(image);parts.append({'type':'image','image':image})
            else:raise ValueError('Unsupported multimodal content part')
        converted.append({'role':message['role'],'content':parts})
    if chars>100000:raise ValueError('Text exceeds local context limit')
    return converted,images

def infer(body):
    global weight_verified,loaded
    if body.model!=MODEL_ID or body.stream:raise ValueError('Fixed local model and nonstreaming requests only')
    messages,images=prepare(body.messages)
    import torch
    from transformers import Qwen3VLForConditionalGeneration,AutoProcessor
    if not torch.cuda.is_available():raise RuntimeError('CUDA unavailable; no silent CPU or cloud fallback')
    if not lock.acquire(blocking=False):raise HTTPException(409,'Local model is busy; no duplicate inference started')
    model=processor=inputs=outputs=None;started=time.monotonic()
    try:
        if not weight_verified:
            source=json.loads((MODEL/'factory-model-manifest.json').read_text('utf-8-sig'))
            if digest(MODEL/'model.safetensors')!=source['weights_sha256']:raise ValueError('Local Qwen model hash changed')
            weight_verified=True
        torch.set_num_threads(4);torch.manual_seed(1247);torch.cuda.manual_seed_all(1247)
        torch.cuda.reset_peak_memory_stats()
        processor=AutoProcessor.from_pretrained(str(MODEL),local_files_only=True,min_pixels=64*32*32,max_pixels=128*32*32)
        prompt=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
        inputs=processor(text=[prompt],images=images or None,padding=True,return_tensors='pt',
            images_kwargs={'min_pixels':64*32*32,'max_pixels':128*32*32})
        prompt_tokens=int(inputs['input_ids'].shape[-1])
        if prompt_tokens>6500 or prompt_tokens+body.max_tokens>10000:raise ValueError('Input/output exceeds the bounded 16GB local inference budget; reduce or split the request')
        grids=inputs.get('image_grid_thw')
        image_grids=grids.tolist() if grids is not None else []
        model=Qwen3VLForConditionalGeneration.from_pretrained(str(MODEL),local_files_only=True,
              dtype=torch.bfloat16,device_map={'':'cuda:0'},attn_implementation='sdpa')
        loaded=True;loaded_seconds=time.monotonic()-started
        inputs=inputs.to('cuda:0')
        with torch.inference_mode():
            outputs=model.generate(**inputs,max_new_tokens=body.max_tokens,max_time=480,do_sample=False)
        generated=outputs[:,prompt_tokens:]
        result=processor.batch_decode(generated,skip_special_tokens=True,clean_up_tokenization_spaces=False)[0]
        eos=model.generation_config.eos_token_id
        eos_ids=set(eos if isinstance(eos,list) else [eos])
        ended=bool(generated.shape[-1] and int(generated[0,-1]) in eos_ids)
        finish='stop' if ended else 'length' if generated.shape[-1]>=body.max_tokens else 'time_limit'
        return {'id':'localchat_'+uuid.uuid4().hex,'object':'chat.completion','model':MODEL_ID,
          'choices':[{'index':0,'message':{'role':'assistant','content':result},'finish_reason':finish}],
          'usage':{'prompt_tokens':prompt_tokens,'completion_tokens':int(generated.shape[-1]),'total_tokens':int(outputs.shape[-1])},
          'factory_receipt':{'model_id':MODEL_ID,'model_directory':str(MODEL),'input_image_count':len(images),
            'image_sha256':[hashlib.sha256(image.tobytes()).hexdigest() for image in images],
            'image_grid_thw':image_grids,'min_pixels':64*32*32,'max_pixels':128*32*32,
            'max_output_tokens':body.max_tokens,'generation_time_limit_seconds':480,'finish_reason':finish,
            'device':torch.cuda.get_device_name(),'torch':torch.__version__,'offline':True,
            'load_seconds':round(loaded_seconds,3),'elapsed_seconds':round(time.monotonic()-started,3),
            'peak_cuda_allocated_bytes':torch.cuda.max_memory_allocated(),'script_sha256':SCRIPT_SHA}}
    finally:
        loaded=False;model=processor=inputs=outputs=None
        gc.collect();torch.cuda.empty_cache();lock.release()

@app.post('/v1/chat/completions')
def chat(body:ChatRequest):
    try:return infer(body)
    except HTTPException:raise
    except (ValueError,UnidentifiedImageError) as error:raise HTTPException(422,str(error)) from error
    except Exception as error:
        # No credentials/remote requests exist in this fixed offline adapter.
        raise HTTPException(500,{'code':'local_inference_failed','message':type(error).__name__+': '+str(error)[:1200]}) from error

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8771,workers=1)
