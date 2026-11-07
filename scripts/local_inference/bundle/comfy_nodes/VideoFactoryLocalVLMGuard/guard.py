"""Clear only ComfyUI's own cached models before a fixed local VLM compile.

This gate is not a job-wide cross-process GPU scheduler. It does not terminate
external processes and does not claim that CUDA contexts consume zero memory.
Torch and ComfyUI imports stay inside actual node execution.
"""
from __future__ import annotations

import datetime as dt
import gc
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
import uuid


MODEL_ID = 'Qwen/Qwen3-VL-4B-Instruct'
SERVICE_ID = 'video-factory-h3ir-qwen4b-v1'
PROFILE_ID = 'h3ir-qwen4b-32k'
WAIT_SECONDS = 30.0


def validate_setup(setup):
    if not isinstance(setup,dict):
        raise ValueError('Expected the OpenH3-IR Setup bundle')
    if str(setup.get('server') or '').strip():
        raise ValueError('This guard requires in-process compilation; remote compiler servers are not allowed')
    if setup.get('llm_model') != MODEL_ID:
        raise ValueError('This guard only permits the fixed local Qwen3-VL-4B-Instruct profile')
    parsed = urlsplit(str(setup.get('llm_url') or ''))
    if (parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost')
            or parsed.port!=8772 or parsed.path.rstrip('/')!='/v1'
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('The VLM URL must be the fixed credential-free loopback HTTP :8772/v1 address')
    return parsed.scheme+'://'+parsed.netloc+'/health'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise RuntimeError('Local VLM health redirected; no request sent to the new location')


def read_health(address):
    host = urlsplit(address).hostname
    # Do not let an edited hosts entry turn "localhost" into a remote inference service.
    if host=='localhost':
        resolved=socket.getaddrinfo(host,8772,type=socket.SOCK_STREAM)
        if not resolved or not all(ipaddress.ip_address(entry[4][0]).is_loopback for entry in resolved):
            raise ValueError('localhost did not resolve exclusively to loopback addresses')
    opener=build_opener(ProxyHandler({}),NoRedirect())
    with opener.open(address,timeout=5) as response:
        return json.load(response)


def check_identity(health):
    if (not isinstance(health,dict) or health.get('service')!=SERVICE_ID
            or health.get('profile')!=PROFILE_ID or health.get('model_id')!=MODEL_ID
            or health.get('offline') is not True):
        raise ValueError('VLM service/profile/model/offline identity does not match the local 4B profile')
    if not isinstance(health.get('busy'),bool) or not isinstance(health.get('loaded'),bool):
        raise ValueError('VLM health does not expose verified busy/loaded booleans')
    source_sha=health.get('script_sha256')
    if not isinstance(source_sha,str) or len(source_sha)!=64 or any(c not in '0123456789abcdef' for c in source_sha.lower()):
        raise ValueError('VLM health is missing its real source SHA-256 identity')
    if health.get('gpu_release_status')=='unconfirmed' or health.get('inference_phase')=='gpu_release_unconfirmed':
        raise RuntimeError('Local VLM reports unconfirmed GPU release; Main was not admitted')


def check_idle_release(health):
    if health.get('inference_phase')!='idle':
        raise RuntimeError('Idle/unloaded VLM did not report the idle inference phase')
    status=health.get('gpu_release_status')
    cleanup=health.get('last_gpu_cleanup')
    if status=='not_used' and cleanup is None:
        return
    if status!='confirmed' or not isinstance(cleanup,dict) or cleanup.get('released') is not True:
        raise RuntimeError('Idle/unloaded VLM has no confirmed GPU cleanup receipt')


def public_health(health):
    return {key:health.get(key) for key in ('service','profile','model_id','offline','busy','loaded','pid',
        'script_sha256','source_components','inference_phase','gpu_release_status','last_gpu_cleanup','gpu_memory')}


def execute_guard(setup,*,health_reader,statistics,unload_models,empty_cache,receipt_dir,
                  collector=gc.collect,sleeper=time.sleep,clock=time.monotonic):
    """Injectable pure orchestration for offline tests; production passes real Comfy callbacks."""
    started=clock()
    directory=Path(receipt_dir)
    directory.mkdir(parents=True,exist_ok=True)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%fZ')
    path=directory/(stamp+'-'+uuid.uuid4().hex[:8]+'.json')
    receipt={'version':1,'at':dt.datetime.now(dt.timezone.utc).isoformat(),'status':'BLOCKED',
             'scope':'unload ComfyUI-owned cached models only; no external process cleanup',
             'job_wide_gpu_mutex':False,'downloaded_models':False,
             'guard_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        address=validate_setup(setup)
        receipt['health_url']=address
        deadline=clock()+WAIT_SECONDS
        while True:
            health=health_reader(address)
            check_identity(health)
            receipt['vlm_health_before']=public_health(health)
            if health['busy'] is False and health['loaded'] is False:
                check_idle_release(health)
                break
            if clock()>=deadline:
                raise RuntimeError('Local VLM did not become idle/unloaded before the bounded guard deadline')
            sleeper(0.5)
        receipt['torch_before']=statistics()
        unload_models()
        receipt['garbage_collected_objects']=collector()
        empty_cache()
        receipt['torch_after']=statistics()
        if receipt['torch_after'].get('comfy_loaded_model_count')!=0:
            raise RuntimeError('Comfy model registry is not empty after unload_all_models; Main was not admitted')
        after=health_reader(address)
        check_identity(after)
        receipt['vlm_health_after']=public_health(after)
        if after['busy'] is not False or after['loaded'] is not False:
            raise RuntimeError('Local VLM changed to busy/loaded during Comfy cleanup; Main was not admitted')
        check_idle_release(after)
        receipt['status']='PASS'
        return receipt,path
    except Exception as error:
        receipt.update(error_type=type(error).__name__,error=str(error))
        raise RuntimeError('Local VLM guard blocked: '+str(error)+'; receipt: '+str(path)) from error
    finally:
        receipt['elapsed_seconds']=round(clock()-started,3)
        path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def torch_statistics(torch,management):
    device=management.get_torch_device()
    if not torch.cuda.is_available() or getattr(device,'type',None)!='cuda':
        raise RuntimeError('This local H3 profile requires the real CUDA device; no CPU fallback was assumed')
    torch.cuda.synchronize(device)
    free,total=torch.cuda.mem_get_info(device)
    return {'device':str(device),'device_name':torch.cuda.get_device_name(device),
            'allocated_bytes':int(torch.cuda.memory_allocated(device)),
            'reserved_bytes':int(torch.cuda.memory_reserved(device)),
            'allocated_peak_since_last_reset_bytes':int(torch.cuda.max_memory_allocated(device)),
            'reserved_peak_since_last_reset_bytes':int(torch.cuda.max_memory_reserved(device)),
            'driver_free_bytes':int(free),'driver_total_bytes':int(total),
            'comfy_loaded_model_count':len(management.current_loaded_models),
            'peak_scope':'Comfy torch process since its last reset; not another process or a new per-job peak'}


class VideoFactoryLocalVLMGuard:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required':{'setup':('H3IR_SETUP',)}}

    RETURN_TYPES=('H3IR_SETUP',)
    RETURN_NAMES=('setup',)
    FUNCTION='guard'
    CATEGORY='VideoFactory/local H3'
    DESCRIPTION='Check the fixed offline 4B service, clear only Comfy-owned caches, then pass Setup to OpenH3-IR Main.'

    @classmethod
    def IS_CHANGED(cls,**kwargs):
        return float('nan')

    def guard(self,setup):
        import folder_paths
        import torch
        import comfy.model_management as management
        receipt,path=execute_guard(
            setup,health_reader=read_health,
            statistics=lambda:torch_statistics(torch,management),
            unload_models=management.unload_all_models,
            empty_cache=lambda:management.soft_empty_cache(force=True),
            receipt_dir=Path(folder_paths.get_output_directory())/'h3_local_vlm_guard')
        print('[VideoFactoryLocalVLMGuard] PASS; receipt: '+str(path))
        return {'ui':{'guard_receipt':[str(path)],'text':['Local 4B VLM guard passed; Comfy cache cleanup recorded.']},
                'result':(setup,)}
