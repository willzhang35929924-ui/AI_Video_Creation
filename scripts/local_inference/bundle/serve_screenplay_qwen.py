"""Fixed, loopback-only Qwen3-VL chat adapter with offline image understanding."""
from __future__ import annotations
import base64
import gc
import hashlib
from io import BytesIO
import json
import os
import re
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid
import wave
from typing import Annotated

ROOT=Path(os.environ['VF_RUNTIME_ROOT']).resolve()
MODEL=Path(os.environ['VF_QWEN_MODEL']).resolve()
MODEL_ID='Qwen/Qwen3-VL-2B-Instruct'
PROFILE='screenplay-qwen2b'
SERVICE_ID='video-factory-screenplay-qwen-v1'
MAX_CONTEXT_TOKENS=10000
MAX_PROMPT_TOKENS=6500
MAX_INPUT_CHARACTERS=100000
MAX_IMAGE_EDGE=768
MIN_VISION_PIXELS=64*32*32
MAX_VISION_PIXELS=128*32*32
VRAM_PREFLIGHT=False
SAFE_TEXT_SDPA=False
GPU_LEASE_PATH=ROOT/'cache/qwen/gpu-inference.lock'
SCRIPT_SHA=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SOURCE_COMPONENTS={'base_script_sha256':SCRIPT_SHA}
os.environ.update({'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1',
 'HF_HOME':str(ROOT/'cache/huggingface'),'TOKENIZERS_PARALLELISM':'false','OMP_NUM_THREADS':'4',
 'TORCH_HOME':str(ROOT/'cache/torch'),'CUDA_CACHE_PATH':str(ROOT/'cache/cuda'),
 'TEMP':str(ROOT/'tmp'),'TMP':str(ROOT/'tmp')})
sys.dont_write_bytecode=True
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, field_validator

app=FastAPI(title='VideoFactory fixed local screenplay model')
lock=threading.Lock()
weight_verified=False
weights_receipt=None
grammar_tokenizer_data=None
loaded=False
inference_phase='idle'
gpu_release_status='not_used'
retained_gpu_lease=None
last_gpu_cleanup=None

class ChatTemplateOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enable_thinking:bool=Field(default=False,strict=True)

StopText=Annotated[str,Field(strict=True,min_length=1,max_length=256)]

class ChatRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    model:str
    messages:list[dict]=Field(min_length=1,max_length=20)
    max_tokens:int=Field(default=4096,ge=1,le=8192)
    temperature:float=Field(default=0.2,ge=0,le=2)
    stream:bool=False
    response_format:dict|None=None
    seed:int|None=Field(default=None,strict=True,ge=0,le=2**63-1)
    chat_template_kwargs:ChatTemplateOptions|None=None
    stop:StopText|list[StopText]|None=None

    @field_validator('stop')
    @classmethod
    def bounded_stop_sequences(cls,value):
        if isinstance(value,list) and not 1<=len(value)<=4:
            raise ValueError('Use one to four nonempty stop sequences')
        return value

class TranscriptionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    audio_base64:str=Field(min_length=1,max_length=4_000_000)

ASR_MODEL=ROOT/'optional-models/Whisper-base/base.pt'
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
    return {'service':SERVICE_ID,'profile':PROFILE,'model_id':MODEL_ID,'workspace':str(ROOT),
            'script_sha256':SCRIPT_SHA,'source_components':SOURCE_COMPONENTS,'pid':os.getpid(),
            'loaded':loaded,'busy':lock.locked() or retained_gpu_lease is not None or loaded,'offline':True,
            'inference_phase':inference_phase,'gpu_release_status':gpu_release_status,
            'gpu_memory':cuda_memory_snapshot(),'last_gpu_cleanup':last_gpu_cleanup,'max_model_len':MAX_CONTEXT_TOKENS}

@app.get('/v1/models')
def models():
    return {'object':'list','data':[{'id':MODEL_ID,'object':'model','owned_by':'local',
       'max_model_len':MAX_CONTEXT_TOKENS,'max_output_tokens':8192,
       'input_modalities':['text','image'],'video_adapter':'timestamped extracted frames; no audio transcription'}]}

def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024**2),b''):value.update(chunk)
    return value.hexdigest()

def _fixed_model_file(name):
    directory=MODEL.resolve()
    if not isinstance(name,str) or name in {'.','..'} or any(char in name for char in '/\\:'):
        raise ValueError('Model manifest contains a non-local filename')
    path=(directory/name).resolve()
    if path.parent!=directory or not path.is_file():
        raise ValueError('Model file is missing or escapes the fixed model directory: '+name)
    return path

def verify_model_weights():
    """Verify fixed on-disk weights; manifest.directory is informational only."""
    manifest_path=_fixed_model_file('factory-model-manifest.json')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if manifest.get('repository')!=MODEL_ID:
        raise ValueError('Local model manifest repository does not match the fixed profile')
    if 'weights_sha256' in manifest:
        expected={'model.safetensors':{'sha256':manifest['weights_sha256'],'bytes':manifest.get('weights_bytes')}}
    elif isinstance(manifest.get('weight_files'),dict) and manifest['weight_files']:
        metadata=json.loads(_fixed_model_file('official-repository-metadata.json').read_text(encoding='utf-8-sig'))
        records={item.get('Path'):item for item in metadata.get('Data',{}).get('Files',[]) if isinstance(item,dict)}
        expected={}
        for name,sha in manifest['weight_files'].items():
            _fixed_model_file(name)
            record=records.get(name,{})
            if record.get('Sha256')!=sha or record.get('Revision')!=manifest.get('revision'):
                raise ValueError('Shard manifest and saved official metadata disagree: '+name)
            if type(record.get('Size')) is not int or record['Size']<=0:
                raise ValueError('Shard lacks its expected byte length: '+name)
            expected[name]={'sha256':sha,'bytes':record['Size']}
        index_path=_fixed_model_file('model.safetensors.index.json')
        index_record=records.get(index_path.name,{})
        if index_record.get('Size')!=index_path.stat().st_size or index_record.get('Sha256')!=digest(index_path):
            raise ValueError('Saved model shard index bytes or SHA256 changed')
        index=json.loads(index_path.read_text(encoding='utf-8-sig'))
        if not isinstance(index.get('weight_map'),dict) or set(index['weight_map'].values())!=set(expected):
            raise ValueError('Model shard index references unverified or missing shard files')
    else:
        raise ValueError('Local model manifest has no supported weight hashes')
    verified=[]
    for name,record in expected.items():
        if not name.endswith('.safetensors') or not isinstance(record['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',record['sha256']):
            raise ValueError('Invalid model weight hash or filename')
        path=_fixed_model_file(name)
        before=path.stat()
        if record['bytes'] is not None and (type(record['bytes']) is not int or before.st_size!=record['bytes']):
            raise ValueError('Model weight byte length changed: '+name)
        actual=digest(path)
        after=path.stat()
        if actual!=record['sha256'] or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError('Model weight SHA256 changed: '+name)
        verified.append({'name':name,'bytes':after.st_size,'expected_bytes':record['bytes'],'sha256':actual})
    return {'repository':MODEL_ID,'revision':manifest.get('revision'),
            'manifest_sha256':digest(manifest_path),'files':verified,
            'total_weight_file_bytes':sum(item['bytes'] for item in verified),'source':'fixed_local_directory'}

def acquire_gpu_lease():
    """Share one GPU inference slot across the 2B and 4B local processes."""
    GPU_LEASE_PATH.parent.mkdir(parents=True,exist_ok=True)
    lease=GPU_LEASE_PATH.open('a+b')
    try:
        lease.seek(0,2)
        if lease.tell()==0:lease.write(b'\0');lease.flush()
        lease.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lease.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        return lease
    except OSError as error:
        lease.close()
        raise HTTPException(409,'Another local Qwen inference holds the GPU slot; request was not repeated') from error

def release_gpu_lease(lease):
    if lease is not None:
        try:
            lease.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lease.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(),fcntl.LOCK_UN)
        finally:lease.close()

def validate_token_budget(prompt_tokens,max_output_tokens):
    if prompt_tokens>MAX_PROMPT_TOKENS or prompt_tokens+max_output_tokens>MAX_CONTEXT_TOKENS:
        raise ValueError(f'Input/output exceeds the local {MAX_CONTEXT_TOKENS}-token profile budget; reduce or split the request')

def vram_preflight(torch,prompt_tokens,max_output_tokens):
    if not VRAM_PREFLIGHT:return None
    config=json.loads(_fixed_model_file('config.json').read_text(encoding='utf-8-sig'))['text_config']
    head_dim=config.get('head_dim') or config['hidden_size']//config['num_attention_heads']
    kv_bytes=2*2*config['num_hidden_layers']*config['num_key_value_heads']*head_dim*(prompt_tokens+max_output_tokens)
    weight_bytes=weights_receipt['total_weight_file_bytes']
    headroom=1024**3
    free,total=torch.cuda.mem_get_info()
    estimate=weight_bytes+kv_bytes+headroom
    if free<estimate:
        raise ValueError(f'Local VRAM preflight requires an estimated {estimate} free bytes but only {free} are available; no model was loaded')
    return {'free_bytes':free,'total_bytes':total,'estimated_weights_bytes':weight_bytes,
            'estimated_kv_cache_bytes':kv_bytes,'headroom_bytes':headroom,
            'result':'estimated_budget_available','guarantees_inference_success':False}

def efficient_text_sdpa(module,query,key,value,attention_mask,dropout=0.0,scaling=None,is_causal=None,**kwargs):
    """Windows-safe text attention: explicit GQA expansion and no math fallback."""
    import torch
    from torch.nn.attention import SDPBackend,sdpa_kernel
    if kwargs.get('output_attentions',False) or kwargs.get('head_mask') is not None:
        raise ValueError('Safe text SDPA does not support attention-weight output or head masks')
    if query.shape[1]!=key.shape[1]:
        if key.shape[1]!=value.shape[1] or query.shape[1]%key.shape[1]:
            raise ValueError('Unsupported GQA head dimensions')
        groups=query.shape[1]//key.shape[1]
        def expand_kv(tensor):
            batch,heads,length,dimension=tensor.shape
            return tensor[:,:,None,:,:].expand(batch,heads,groups,length,dimension).reshape(batch,heads*groups,length,dimension)
        key,value=expand_kv(key),expand_kv(value)
    if attention_mask is not None and attention_mask.ndim==4:
        attention_mask=attention_mask[:,:,:,:key.shape[-2]]
    if is_causal is None:
        is_causal=query.shape[2]>1 and attention_mask is None and getattr(module,'is_causal',True)
    # Native GQA makes HF 4.57 request enable_gqa=True when no mask is present.
    # Windows efficient SDPA rejects that path; default dispatch can then choose
    # an N*N fp32 math matrix. Repeated KV keeps the efficient backend eligible.
    with sdpa_kernel([SDPBackend.EFFICIENT_ATTENTION]):
        output=torch.nn.functional.scaled_dot_product_attention(query,key,value,attn_mask=attention_mask,
            dropout_p=dropout,scale=scaling,is_causal=is_causal,enable_gqa=False)
    return output.transpose(1,2).contiguous(),None

def configure_model_attention(model):
    if not SAFE_TEXT_SDPA:
        return {'safe_text_sdpa':False,'requested':'sdpa'}
    from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS
    name='video_factory_efficient_text_sdpa'
    ALL_ATTENTION_FUNCTIONS.register(name,efficient_text_sdpa)
    model.set_attn_implementation({'text_config':name,'vision_config':'sdpa'})
    text=[item for _,item in model.named_modules() if type(item).__name__=='Qwen3VLTextAttention']
    if not text or any(item.config._attn_implementation!=name for item in text):
        raise ValueError('Safe text attention interface was not installed on every Qwen3-VL text layer')
    return {'safe_text_sdpa':True,'text_implementation':name,'text_layers':len(text),
            'gqa':'explicit_repeat_kv','allowed_text_backend':'EFFICIENT_ATTENTION',
            'math_fallback':False,'vision_implementation':model.config.vision_config._attn_implementation}

def cuda_memory_snapshot():
    torch=sys.modules.get('torch')
    cuda=getattr(torch,'cuda',None)
    if cuda is None or not getattr(cuda,'is_initialized',lambda:False)():
        return {'state':'not_initialized','allocated_bytes':0,'reserved_bytes':0}
    try:
        free,total=cuda.mem_get_info()
        return {'state':'ok','allocated_bytes':cuda.memory_allocated(),'reserved_bytes':cuda.memory_reserved(),
                'global_free_bytes':free,'global_total_bytes':total}
    except Exception as error:
        return {'state':'unknown','error':type(error).__name__+': '+str(error)[:300]}

def discard_exception_frames(error):
    """Drop model/activation references before releasing the cross-process slot."""
    pending=[error];seen=set()
    while pending:
        item=pending.pop()
        if id(item) in seen:continue
        seen.add(id(item))
        pending.extend(child for child in (item.__cause__,item.__context__) if isinstance(child,BaseException))
        if item.__traceback__ is not None:traceback.clear_frames(item.__traceback__)
        item.__traceback__=None;item.__cause__=None;item.__context__=None
        item.args=()
        if isinstance(getattr(item,'__dict__',None),dict):item.__dict__.clear()

def exception_http_details(error):
    if isinstance(error,HTTPException):
        # Only JSON-safe details cross this boundary, never the exception object.
        return error.status_code,json.loads(json.dumps(error.detail,default=str)),dict(error.headers or {})
    if isinstance(error,(ValueError,UnidentifiedImageError)):return 422,str(error)[:1200],{}
    return 500,{'code':'local_inference_failed','message':type(error).__name__+': '+str(error)[:1200]},{}

def cleanup_gpu(torch,baseline):
    gc.collect()
    cleanup_error=None
    try:torch.cuda.empty_cache()
    except Exception as error:
        cleanup_error=type(error).__name__+': '+str(error)[:300]
        discard_exception_frames(error)
    current=cuda_memory_snapshot()
    released=current['state']=='not_initialized'
    if current['state']=='ok':
        released=current['allocated_bytes']<=baseline.get('allocated_bytes',0)+16*1024**2
        if baseline.get('state')=='ok':
            released=released and current['global_free_bytes']>=baseline['global_free_bytes']-256*1024**2
    if cleanup_error:released=False
    return {'released':bool(released),'baseline':baseline,'after':current,'error':cleanup_error,
            'scope':'own live Torch allocation plus conservative global-free comparison; not peak memory'}

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
                    original.load();image=original.convert('RGB');image.thumbnail((MAX_IMAGE_EDGE,MAX_IMAGE_EDGE))
                images.append(image);parts.append({'type':'image','image':image})
            else:raise ValueError('Unsupported multimodal content part')
        converted.append({'role':message['role'],'content':parts})
    if chars>MAX_INPUT_CHARACTERS:raise ValueError('Text exceeds local context limit')
    return converted,images

def request_controls(body):
    # This fixed checkpoint is Instruct, not its Thinking sibling. A template
    # flag cannot turn it into a different model, so do not silently accept True.
    if body.chat_template_kwargs is not None and body.chat_template_kwargs.enable_thinking:
        raise ValueError('Qwen3-VL-2B-Instruct does not support enable_thinking=true; use false or omit it')
    return body.seed if body.seed is not None else 1247

def template_controls(body,processor):
    template=getattr(processor,'chat_template',None)
    if template is None:template=getattr(getattr(processor,'tokenizer',None),'chat_template',None)
    if isinstance(template,dict):template=template.get('default')
    if not isinstance(template,str):raise ValueError('The actual local chat template cannot be verified')
    requested=body.chat_template_kwargs.enable_thinking if body.chat_template_kwargs is not None else None
    if 'enable_thinking' in template:
        # Pass a supported control to the real template, rather than merely
        # accepting it in the OpenAI-compatible request schema.
        kwargs={'enable_thinking':False}
        mode='template_disabled'
    else:
        if '<think>' in template or 'reasoning_content' in template:
            raise ValueError('Actual chat template has unsupported thinking behavior; request not ignored')
        kwargs={}
        mode='fixed_instruct_non_thinking'
    return kwargs,{'requested_enable_thinking':requested,'effective_enable_thinking':False,
                   'mode':mode,'template_sha256':hashlib.sha256(template.encode()).hexdigest()}

def generation_stop_controls(body,processor):
    if body.stop is None:return {},[]
    stops=[body.stop] if isinstance(body.stop,str) else body.stop
    tokenizer=getattr(processor,'tokenizer',None)
    if tokenizer is None:raise ValueError('Stop strings require the actual model tokenizer')
    # Installed Transformers implements this as StopStringCriteria during
    # decoding, including stops that span token boundaries.
    return {'stop_strings':stops,'tokenizer':tokenizer},stops

def generation_sampling_controls(body,model):
    if body.temperature==0:
        # Clear inherited sampling-only defaults as well, so Transformers does
        # not warn that the checkpoint's temperature/top-p/top-k were ignored.
        return {'do_sample':False,'temperature':None,'top_p':None,'top_k':None}
    return {'do_sample':True,'temperature':body.temperature,
            'top_p':getattr(model.generation_config,'top_p',1.0),
            'top_k':getattr(model.generation_config,'top_k',50)}

def trim_stop_output(result,raw_result,stops):
    matches=[(raw_result.find(stop),index,stop) for index,stop in enumerate(stops) if stop in raw_result]
    if not matches:return result,None
    _,_,matched=min(matches)
    # Special stop tokens may already have been removed by skip_special_tokens.
    visible=[result.find(stop) for stop in stops if stop in result]
    return result[:min(visible)] if visible else result,matched

def _strict_json_loads(text):
    def unique_object(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate JSON object key: '+key)
            result[key]=value
        return result
    def invalid_constant(value):raise ValueError('Non-JSON numeric constant: '+value)
    return json.loads(text,object_pairs_hook=unique_object,parse_constant=invalid_constant)

def _check_guided_schema(schema,depth=0):
    # Do not let a permissive parser silently ignore unsupported JSON Schema
    # structures or let the post-validator resolve anything over the network.
    if not isinstance(schema,dict) or depth>32:raise ValueError('Unsupported or deeply nested JSON schema')
    allowed={'type','properties','required','additionalProperties','items','enum','const','anyOf',
             '$ref','$defs','definitions','$schema','$id','title','description','default','examples',
             'minLength','maxLength','minItems','maxItems'}
    unknown=set(schema)-allowed
    if unknown:raise ValueError('Unsupported guided JSON schema keyword(s): '+', '.join(sorted(unknown)))
    if '$ref' in schema and not (isinstance(schema['$ref'],str) and re.fullmatch(r'#/(?:\$defs|definitions)/[^/~]+',schema['$ref'])):
        raise ValueError('Only local $defs/definitions references are supported; remote references are forbidden')
    if isinstance(schema.get('type'),list):raise ValueError('Use explicit anyOf schemas for nullable values')
    if 'const' in schema and not isinstance(schema['const'],str):
        raise ValueError('This guided backend supports string const; use typed structures for other constants')
    if 'enum' in schema:
        values=schema['enum']
        if not isinstance(values,list) or not values or not (all(isinstance(v,str) for v in values) or all(type(v) in (int,float) for v in values)):
            raise ValueError('Guided enum values must be uniformly strings or numbers')
    for key in ('properties','$defs','definitions'):
        if key in schema:
            if not isinstance(schema[key],dict):raise ValueError('Invalid guided schema '+key)
            for child in schema[key].values():_check_guided_schema(child,depth+1)
    if 'items' in schema:_check_guided_schema(schema['items'],depth+1)
    if isinstance(schema.get('additionalProperties'),dict):_check_guided_schema(schema['additionalProperties'],depth+1)
    if 'anyOf' in schema:
        if not isinstance(schema['anyOf'],list) or not schema['anyOf']:raise ValueError('Invalid guided anyOf')
        for child in schema['anyOf']:_check_guided_schema(child,depth+1)

def prepare_response_format(body):
    requested=body.response_format
    if requested is None:return None
    if body.stop is not None:raise ValueError('Explicit stop strings are unsupported with guided JSON; completion must emit real EOS')
    kind=requested.get('type')
    if kind=='json_object':
        if set(requested)!={'type'}:raise ValueError('json_object accepts only the type field')
        schema={'type':'object'}
    elif kind=='json_schema':
        if set(requested)!={'type','json_schema'}:raise ValueError('Invalid json_schema response_format fields')
        details=requested.get('json_schema')
        if not isinstance(details,dict) or not {'name','schema','strict'}<=set(details) or set(details)-{'name','schema','strict','description'}:
            raise ValueError('json_schema requires name, schema and strict=true')
        if details['strict'] is not True:raise ValueError('Only strict=true guided JSON schema requests are supported')
        if not isinstance(details['name'],str) or not details['name'].strip():raise ValueError('json_schema name must be a nonempty string')
        schema=details['schema']
        if not isinstance(schema,dict) or schema.get('type')!='object':raise ValueError('Guided response schema must have a top-level object type')
    else:raise ValueError('Supported response_format types are json_object and json_schema')
    _check_guided_schema(schema)
    try:
        from jsonschema.validators import validator_for
    except ImportError as error:
        raise HTTPException(503,'Guided JSON requires the locally installed jsonschema validator; no unguarded fallback') from error
    try:
        validator_class=validator_for(schema)
        validator_class.check_schema(schema)
        validator=validator_class(schema)
    except Exception as error:raise ValueError('Invalid JSON schema: '+str(error)[:500]) from error
    encoded=json.dumps(schema,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return {'schema':schema,'validator':validator,'receipt':{'requested':kind,'guided_decoding':True,
            'schema_sha256':hashlib.sha256(encoded).hexdigest(),'backend':'lm-format-enforcer',
            'post_validation':False,'semantic_correctness_guaranteed':False}}

def _build_grammar_prefix(tokenizer,schema):
    global grammar_tokenizer_data
    try:
        from lmformatenforcer import JsonSchemaParser
        from lmformatenforcer.integrations.transformers import (build_token_enforcer_tokenizer_data,
                                                               build_transformers_prefix_allowed_tokens_fn)
    except ImportError as error:
        raise HTTPException(503,'Guided JSON backend is unavailable; no unguarded fallback') from error
    if grammar_tokenizer_data is None:
        grammar_tokenizer_data=build_token_enforcer_tokenizer_data(tokenizer)
    return build_transformers_prefix_allowed_tokens_fn(grammar_tokenizer_data,JsonSchemaParser(schema))

def guided_generation_controls(prepared,processor,model,prompt_tokens):
    if prepared is None:return {},None
    tokenizer=getattr(processor,'tokenizer',None)
    if tokenizer is None:raise ValueError('Guided JSON requires the actual model tokenizer')
    grammar_prefix=_build_grammar_prefix(tokenizer,prepared['schema'])
    eos=model.generation_config.eos_token_id
    eos_ids=set(eos if isinstance(eos,list) else [eos])
    eos_ids={value for value in eos_ids if type(value) is int and value>=0}
    if not eos_ids:raise ValueError('Cannot verify real model EOS for guided JSON')
    stats={'prefix_calls':0,'prompt_tokens_excluded':prompt_tokens}
    def prefix(batch_id,sent):
        stats['prefix_calls']+=1
        generated=sent[prompt_tokens:]
        ids=generated.tolist()
        text=tokenizer.decode(ids,skip_special_tokens=False) if ids else ''
        if text.rstrip().endswith('}'):
            try:
                instance=_strict_json_loads(text)
                complete=isinstance(instance,dict) and prepared['validator'].is_valid(instance)
            except (ValueError,TypeError):complete=False
            if complete:return sorted(eos_ids)
        allowed=[token for token in grammar_prefix(batch_id,generated) if token not in eos_ids]
        if not allowed:raise ValueError('Guided JSON parser has no valid continuation before a complete instance; no EOS fallback')
        return allowed
    return {'prefix_allowed_tokens_fn':prefix},stats

def validate_guided_output(result,prepared,stats,ended,finish,completion_tokens):
    if prepared is None:return {'requested':None,'guided_decoding':False,'post_validation':False}
    if stats is None or stats['prefix_calls']==0:raise ValueError('Model did not execute the guided token filter')
    if not ended:raise ValueError(f'Guided JSON did not emit real EOS (finish_reason={finish}, completion_tokens={completion_tokens})')
    try:
        instance=_strict_json_loads(result)
        if not isinstance(instance,dict):raise ValueError('Expected a JSON object instance')
        prepared['validator'].validate(instance)
    except Exception as error:raise ValueError('Guided JSON post-validation failed: '+str(error)[:500]) from error
    return {**prepared['receipt'],**stats,'post_validation':True,'completion_tokens':completion_tokens}

def infer(body):
    global weight_verified,weights_receipt,loaded,inference_phase,gpu_release_status,retained_gpu_lease,last_gpu_cleanup
    if body.model!=MODEL_ID or body.stream:raise ValueError('Fixed local model and nonstreaming requests only')
    seed=request_controls(body)
    prepared_format=prepare_response_format(body)
    messages,images=prepare(body.messages)
    if retained_gpu_lease is not None or gpu_release_status=='unconfirmed':
        raise HTTPException(503,'Previous GPU cleanup is unconfirmed; restart this local profile before another inference')
    import torch
    from transformers import Qwen3VLForConditionalGeneration,AutoProcessor
    if not torch.cuda.is_available():raise RuntimeError('CUDA unavailable; no silent CPU or cloud fallback')
    if not lock.acquire(blocking=False):raise HTTPException(409,'Local model is busy; no duplicate inference started')
    model=processor=inputs=outputs=generated=None;lease=None;started=time.monotonic()
    failure=None;response=None;baseline={'state':'not_initialized','allocated_bytes':0,'reserved_bytes':0}
    inference_phase='preparing';gpu_release_status='in_use'
    try:
        lease=acquire_gpu_lease()
        if not weight_verified:
            weights_receipt=verify_model_weights()
            weight_verified=True
        torch.set_num_threads(4);torch.manual_seed(seed);torch.cuda.manual_seed_all(seed)
        torch.cuda.reset_peak_memory_stats()
        baseline=cuda_memory_snapshot()
        processor=AutoProcessor.from_pretrained(str(MODEL),local_files_only=True,min_pixels=MIN_VISION_PIXELS,max_pixels=MAX_VISION_PIXELS)
        template_kwargs,thinking_receipt=template_controls(body,processor)
        prompt=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True,**template_kwargs)
        inputs=processor(text=[prompt],images=images or None,padding=True,return_tensors='pt',
            images_kwargs={'min_pixels':MIN_VISION_PIXELS,'max_pixels':MAX_VISION_PIXELS})
        prompt_tokens=int(inputs['input_ids'].shape[-1])
        validate_token_budget(prompt_tokens,body.max_tokens)
        grids=inputs.get('image_grid_thw')
        image_grids=grids.tolist() if grids is not None else []
        memory_receipt=vram_preflight(torch,prompt_tokens,body.max_tokens)
        inference_phase='loading_model'
        model=Qwen3VLForConditionalGeneration.from_pretrained(str(MODEL),local_files_only=True,
              dtype=torch.bfloat16,device_map={'':'cuda:0'},attn_implementation='sdpa')
        loaded=True;loaded_seconds=time.monotonic()-started
        attention_receipt=configure_model_attention(model)
        inputs=inputs.to('cuda:0')
        stop_kwargs,stops=generation_stop_controls(body,processor)
        sampling_kwargs=generation_sampling_controls(body,model)
        guided_kwargs,guided_stats=guided_generation_controls(prepared_format,processor,model,prompt_tokens)
        inference_phase='generating'
        with torch.inference_mode():
            outputs=model.generate(**inputs,max_new_tokens=body.max_tokens,max_time=480,**sampling_kwargs,**stop_kwargs,**guided_kwargs)
        generated=outputs[:,prompt_tokens:]
        result=processor.batch_decode(generated,skip_special_tokens=True,clean_up_tokenization_spaces=False)[0]
        raw_result=processor.batch_decode(generated,skip_special_tokens=False,clean_up_tokenization_spaces=False)[0] if stops else result
        result,matched_stop=trim_stop_output(result,raw_result,stops)
        eos=model.generation_config.eos_token_id
        eos_ids=set(eos if isinstance(eos,list) else [eos])
        ended=bool(generated.shape[-1] and int(generated[0,-1]) in eos_ids)
        finish='stop' if ended or matched_stop is not None else 'length' if generated.shape[-1]>=body.max_tokens else 'time_limit'
        format_receipt=validate_guided_output(result,prepared_format,guided_stats,ended,finish,int(generated.shape[-1]))
        response={'id':'localchat_'+uuid.uuid4().hex,'object':'chat.completion','model':MODEL_ID,
          'choices':[{'index':0,'message':{'role':'assistant','content':result},'finish_reason':finish}],
          'usage':{'prompt_tokens':prompt_tokens,'completion_tokens':int(generated.shape[-1]),'total_tokens':int(outputs.shape[-1])},
          'factory_receipt':{'model_id':MODEL_ID,'model_directory':str(MODEL),'input_image_count':len(images),
            'profile':PROFILE,'weights_verification':weights_receipt,'source_components':SOURCE_COMPONENTS,
            'image_sha256':[hashlib.sha256(image.tobytes()).hexdigest() for image in images],
            'image_grid_thw':image_grids,'min_pixels':MIN_VISION_PIXELS,'max_pixels':MAX_VISION_PIXELS,
            'max_image_edge':MAX_IMAGE_EDGE,'prepared_image_dimensions':[[image.width,image.height] for image in images],
            'max_output_tokens':body.max_tokens,'generation_time_limit_seconds':480,'finish_reason':finish,
            'seed':seed,'seed_source':'request' if body.seed is not None else 'fixed_default',
            'chat_template_control':thinking_receipt,'stop_sequences':stops,'matched_stop':matched_stop,
            'decoding':'sampling' if sampling_kwargs['do_sample'] else 'greedy',
            'sampling_parameters':sampling_kwargs,'requested_temperature':body.temperature,
            'context_budget':{'max_model_len':MAX_CONTEXT_TOKENS,'max_prompt_tokens':MAX_PROMPT_TOKENS,
                              'actual_prompt_tokens':prompt_tokens,'requested_output_tokens':body.max_tokens},
            'vram_preflight':memory_receipt,'gpu_slot':'exclusive_local_qwen','attention_control':attention_receipt,
            'response_format_control':format_receipt,
            'device':torch.cuda.get_device_name(),'torch':torch.__version__,'offline':True,
            'load_seconds':round(loaded_seconds,3),'elapsed_seconds':round(time.monotonic()-started,3),
            'peak_cuda_allocated_bytes':torch.cuda.max_memory_allocated(),'script_sha256':SCRIPT_SHA}}
    except Exception as error:
        failure=exception_http_details(error)
        discard_exception_frames(error)
    finally:
        # Leave the original except block first: suppress_context alone would
        # still retain error.__context__ and its model.generate frame/weights.
        inference_phase='cleaning_gpu'
        model=processor=inputs=outputs=generated=None
        last_gpu_cleanup=cleanup_gpu(torch,baseline)
        loaded=not last_gpu_cleanup['released']
        gpu_release_status='confirmed' if last_gpu_cleanup['released'] else 'unconfirmed'
        inference_phase='idle' if last_gpu_cleanup['released'] else 'gpu_release_unconfirmed'
        try:
            if last_gpu_cleanup['released']:
                release_gpu_lease(lease)
            else:
                retained_gpu_lease=lease  # Keep other local profiles from loading.
        finally:lock.release()
    if failure is not None:
        status,detail,headers=failure
        if isinstance(detail,dict):detail={**detail,'gpu_cleanup':last_gpu_cleanup}
        elif not last_gpu_cleanup['released']:detail=str(detail)+'; GPU release unconfirmed, local profile restart required'
        raise HTTPException(status,detail,headers=headers) from None
    if not last_gpu_cleanup['released']:
        raise HTTPException(503,{'code':'gpu_release_unconfirmed','gpu_cleanup':last_gpu_cleanup}) from None
    response['factory_receipt']['gpu_cleanup']=last_gpu_cleanup
    return response

@app.post('/v1/chat/completions')
def chat(body:ChatRequest):
    try:return infer(body)
    except Exception as error:
        failure=exception_http_details(error)
        discard_exception_frames(error)
    # Raise outside except so the HTTP error cannot retain the original model.
    status,detail,headers=failure
    raise HTTPException(status,detail,headers=headers) from None

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8771,workers=1)
