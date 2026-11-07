"""Audit the fixed native SMD LoRA inside an actual OUTER_SAMPLE call.

Registration is never application evidence. This module does not download,
load a model, queue a graph, force weight reapplication, or replace a loader.
Only private adapter classes are temporarily instrumented; low-VRAM clones
retain their target tag without retaining those clones or their tensors.
"""
from __future__ import annotations

from contextvars import ContextVar
import datetime as dt
import hashlib
import json
import logging
import math
from pathlib import Path
import struct
import sys
import threading
import time
import types
import uuid
import weakref

VERSION='h3-smd-sampling-audit-v2'
NAME='SMD-MiniMax-H3-Ref2VA-4step.safetensors'
REPO='zjwfufu/SMD-MiniMax-H3-Ref2VA-4step-ComfyUI'
REVISION='9544cef6ca00bcedb34a7139032634c4558ed4f8'
SIZE=1956183672
SHA='9c6f61221a1043ee709af0d32ae083c17efeb36950b4b08a945296b5b9c39353'
HEADER_SHA='9024963cac4b5b53270794463ac7d8c45f99e06a6b164d9af5bdccb14b802783'
WRAPPER_KEY='video_factory_smd_actual_application_v1'
_CALL=ContextVar('factory_smd_native_call',default=None)
CORE_PINS={
 'comfy/weight_adapter/lora.py':'e506062b4eb189be4c36f88270e0fd4dcce038c79a98f7163604ed6b44efe4b5',
 'comfy/weight_adapter/base.py':'484f0d83a96e700f80c793e4bcc6a897d633233e51a59191d91da5e59da345c7',
 'comfy/lora.py':'24ace0806193466947662dc408642e2615f255259f5a45393043b67a47174f17',
 'comfy/lora_convert.py':'0790e993706b6e5c255fd4000f8f48abed80e318274da5264681eb799df25d0b',
 'comfy/model_patcher.py':'7edc88d9eae6a88637625d257dc73684cfc012a29446bf88e70d22e3a6be3994',
 'comfy/patcher_extension.py':'1c7b3b855278d0b95d1c1a705fa0b3c91a265f320e2f97bdeb9d15469d6d4ac7',
 'comfy/samplers.py':'f2c264ca9d394612f828e3ffe167c856a278a10e1711b269f0ba65dccb66393f',
 'comfy/k_diffusion/sampling.py':'cfc4221ed0bc1f84a6f5689d3ec440cad3008c2541ab8f5e2599d1a73fac74ce',
 'comfy/sampler_helpers.py':'53c82235bfea0c4edf3cd5219f080c44c45ac1c9a6c3171f2aae151e18b56b91',
 'comfy/sd.py':'e567fdcf7c4539a536787b4b51b8abe996c3db4e41d6d61e41b260cfc2ee3165',
 'nodes.py':'8c5a0239a4d3b9709085348a96d25184d28fa2267c2421b7f354614c03f3139c',
 'comfy_extras/nodes_minimax_h3.py':'108ceff11e5a5c9a34a57bbc380447cf9941beb673a1bd421abc818de54ccd5c',
 'comfy/ops.py':'a0b8c7f32a146b383a795363bef35e480ffa84b5b3e9c9503f323b1446876f45',
 'comfy/quant_ops.py':'37f0054dfbd8430524ac2c73dafd25b8ad4eabc490a2b7490eeebde8dffbc749',
 'comfy/model_management.py':'045ab96003386a43e647c8ab96f22fbf3be6c577e2fc578ea31c6faa22facbd5',
}
RUNTIME_PINS={
 'Lib/site-packages/torch/utils/_contextlib.py':'c4b52075af6b23784336b88d1c608c1ba22bcece245f03d20e8ff12b8bb774c9',
 'Lib/site-packages/torch/autograd/grad_mode.py':'35f0a6be03336e2e78db5fcbaf574b2122ba2f4496c83036c29203ccc0687416',
}


class SamplingAuditFailure(RuntimeError):pass


def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def file_hash(path,start=0,length=None):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        stream.seek(start)
        remaining=length
        while remaining is None or remaining:
            block=stream.read(4*1024*1024 if remaining is None else min(remaining,4*1024*1024))
            if not block:
                if remaining:raise SamplingAuditFailure('Truncated pinned LoRA bytes')
                break
            result.update(block)
            if remaining is not None:remaining-=len(block)
    return result.hexdigest()


def verify_core(root):
    for relative,expected in CORE_PINS.items():
        path=Path(root)/relative
        if not path.is_file() or file_hash(path)!=expected:
            raise SamplingAuditFailure('Pinned native sampling/quantization source changed: '+relative)
    return dict(CORE_PINS)


def verify_runtime(root):
    for relative,expected in RUNTIME_PINS.items():
        path=Path(root)/relative
        if not path.is_file() or file_hash(path)!=expected:
            raise SamplingAuditFailure('Pinned native sampler decorator source changed: '+relative)
    return dict(RUNTIME_PINS)


def code_payload(code):
    """Exact code fields, independent of marshal string-intern/reference flags."""
    def constant(value):
        if isinstance(value,types.CodeType):return {'code':code_payload(value)}
        if isinstance(value,tuple):return {'tuple':[constant(item) for item in value]}
        if isinstance(value,bytes):return {'bytes':value.hex()}
        if value is None:return {'none':True}
        if isinstance(value,bool):return {'bool':value}
        if isinstance(value,int):return {'int':str(value)}
        if isinstance(value,float):return {'float':value.hex()}
        if isinstance(value,str):return {'str':value}
        raise SamplingAuditFailure('Unsupported native code constant during verification')
    fields=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize','co_flags',
            'co_names','co_varnames','co_filename','co_name','co_qualname','co_firstlineno','co_freevars','co_cellvars')
    result={name:getattr(code,name) for name in fields}
    result.update(co_code=code.co_code.hex(),co_consts=[constant(value) for value in code.co_consts],
                  co_linetable=code.co_linetable.hex(),co_exceptiontable=code.co_exceptiontable.hex())
    return result


def verified_native_calculate(adapter_type):
    """Compare the live callable with compiled pinned source, without exec.

    Do not unwrap decorators: a wrapper that points __wrapped__ at the original
    must not be mistaken for execution of the original native implementation.
    """
    import torch
    function=adapter_type.calculate_weight
    if not isinstance(function,types.FunctionType) or function.__closure__ is not None:
        raise SamplingAuditFailure('Native calculate_weight is wrapped or is not an original function')
    if function.__globals__ is not sys.modules['comfy.weight_adapter.lora'].__dict__:
        raise SamplingAuditFailure('Native calculate_weight globals are not the actual native module namespace')
    path=Path(function.__code__.co_filename).resolve()
    expected_path=Path(sys.modules['comfy.weight_adapter.lora'].__file__).resolve()
    if path!=expected_path or file_hash(path)!=CORE_PINS['comfy/weight_adapter/lora.py']:
        raise SamplingAuditFailure('Native calculate_weight code does not originate in the pinned source')
    compiled=compile(path.read_bytes(),function.__code__.co_filename,'exec',dont_inherit=True,optimize=sys.flags.optimize)
    cls=next(c for c in compiled.co_consts if isinstance(c,types.CodeType) and c.co_name=='LoRAAdapter')
    expected=next(c for c in cls.co_consts if isinstance(c,types.CodeType) and c.co_name=='calculate_weight')
    if code_payload(expected)!=code_payload(function.__code__) or function.__defaults__!=(torch.float32,None):
        raise SamplingAuditFailure('Native calculate_weight in-memory code/defaults differ from the pinned implementation')
    return function


def _verified_source_function(function,module,qualname,pin):
    """Verify a live function, including its exact module namespace, without exec."""
    if not isinstance(function,types.FunctionType) or function.__globals__ is not module.__dict__:
        raise SamplingAuditFailure('Native sampler callable globals/type differ: '+qualname)
    path=Path(module.__file__).resolve()
    if Path(function.__code__.co_filename).resolve()!=path or file_hash(path)!=pin:
        raise SamplingAuditFailure('Native sampler callable source differs: '+qualname)
    expected=compile(path.read_bytes(),function.__code__.co_filename,'exec',dont_inherit=True,optimize=sys.flags.optimize)
    for name in qualname.split('.'):
        if name=='<locals>':continue
        choices=[c for c in expected.co_consts if isinstance(c,types.CodeType) and c.co_name==name]
        if len(choices)!=1:raise SamplingAuditFailure('Ambiguous pinned sampler code: '+qualname)
        expected=choices[0]
    if code_payload(expected)!=code_payload(function.__code__):
        raise SamplingAuditFailure('Native sampler in-memory implementation differs: '+qualname)
    return function


def _verified_no_grad_sampler(function,module,name,defaults):
    import torch
    import torch.utils._contextlib as contexts
    import torch.autograd.grad_mode as grad_mode
    context_pin=RUNTIME_PINS['Lib/site-packages/torch/utils/_contextlib.py']
    grad_pin=RUNTIME_PINS['Lib/site-packages/torch/autograd/grad_mode.py']
    _verified_source_function(function,contexts,'context_decorator.<locals>.decorate_context',context_pin)
    if (function.__defaults__ is not None or function.__kwdefaults__ is not None
            or function.__code__.co_freevars!=('ctx_factory','func') or len(function.__closure__ or ())!=2):
        raise SamplingAuditFailure('Native sampler no_grad wrapper contract differs: '+name)
    factory,raw=(cell.cell_contents for cell in function.__closure__)
    if (raw is not getattr(function,'__wrapped__',None) or raw.__closure__ is not None
            or raw.__defaults__!=defaults or raw.__kwdefaults__ is not None):
        raise SamplingAuditFailure('Native sampler raw callable/defaults differ: '+name)
    _verified_source_function(raw,module,name,CORE_PINS['comfy/k_diffusion/sampling.py'])
    if (not isinstance(factory,types.MethodType) or type(factory.__self__) is not grad_mode.no_grad
            or torch.no_grad is not grad_mode.no_grad or factory.__func__ is not contexts._DecoratorContextManager.clone
            or factory.__self__.__dict__!={'prev':False}):
        raise SamplingAuditFailure('Native sampler no_grad context was replaced: '+name)
    _verified_source_function(factory.__func__,contexts,'_DecoratorContextManager.clone',context_pin)
    for method in ('__init__','__enter__','__exit__'):
        _verified_source_function(getattr(grad_mode.no_grad,method),grad_mode,'no_grad.'+method,grad_pin)
    return hashlib.sha256(json.dumps(code_payload(raw.__code__),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def verified_sampler(sampler,name):
    """Fixed recipes accept only the native sampler and empty runtime options."""
    import comfy.samplers as samplers
    import comfy.k_diffusion.sampling as implementation
    if (type(sampler) is not samplers.KSAMPLER or set(vars(sampler))!={'sampler_function','extra_options','inpaint_options'}
            or type(sampler.extra_options) is not dict or sampler.extra_options
            or type(sampler.inpaint_options) is not dict or sampler.inpaint_options):
        raise SamplingAuditFailure('Native sampler type/extra_options/inpaint_options differ from the fixed recipe')
    for method in ('__init__','sample'):
        _verified_source_function(getattr(samplers.KSAMPLER,method),samplers,'KSAMPLER.'+method,CORE_PINS['comfy/samplers.py'])
    function=getattr(implementation,'sample_'+name,None)
    if sampler.sampler_function is not function:
        raise SamplingAuditFailure('Actual sampler callable is not the native module callable: '+name)
    defaults={'euler':(None,None,None,0.,0.,float('inf'),1.),'res_multistep':(None,None,None,1.,None)}
    if name not in defaults:raise SamplingAuditFailure('Unsupported fixed sampler recipe')
    digest=_verified_no_grad_sampler(function,implementation,'sample_'+name,defaults[name])
    if name=='res_multistep':
        _verified_no_grad_sampler(implementation.res_multistep,implementation,'res_multistep',(None,None,None,1.,None,1.,False))
    return {'sampler_name':name,'callable':'comfy.k_diffusion.sampling.sample_'+name,
        'native_callable_identity_verified':True,'native_callable_globals_verified':True,
        'raw_callable_code_sha256':digest,'implementation_source_sha256':CORE_PINS['comfy/k_diffusion/sampling.py'],
        'decorator_source_sha256':dict(RUNTIME_PINS),'extra_options':{},'inpaint_options':{}}


def smd_sigmas_valid(values):
    expected=[1.,0.9677419355,0.9,0.6666666667,0.]
    return len(values)==5 and all(math.isfinite(value) for value in values) and all(abs(a-b)<=1e-7 for a,b in zip(values,expected))


def baseline_sigmas_valid(values):
    return (len(values)==21 and all(math.isfinite(value) for value in values)
            and values[0]>0 and values[-1]==0 and all(a>b>=0 for a,b in zip(values,values[1:])))


def header_targets(header):
    targets={}
    for key,spec in header.items():
        if not key.endswith('.lora_up.weight'):continue
        prefix=key[:-len('.lora_up.weight')];down=prefix+'.lora_down.weight';alpha=prefix+'.alpha'
        if down not in header or alpha not in header:raise SamplingAuditFailure('Incomplete SMD adapter tensors')
        up_shape=spec['shape'];down_shape=header[down]['shape']
        if (spec['dtype']!='BF16' or header[down]['dtype']!='BF16' or len(up_shape)!=2 or len(down_shape)!=2
                or up_shape[1]!=down_shape[0] or header[alpha]['shape']!=[] or header[alpha]['dtype']!='F32'):
            raise SamplingAuditFailure('Unsupported SMD up/down/alpha representation')
        target='diffusion_model.'+prefix+'.weight'
        targets[target]={'target':target,'up_key':key,'down_key':down,'alpha_key':alpha,
                         'up_shape':up_shape,'down_shape':down_shape,'base_shape':[up_shape[0],down_shape[1]]}
    if len(targets)!=208 or sum('token_refiner.' in key for key in targets)!=8 or len(header)-('__metadata__' in header)!=624:
        raise SamplingAuditFailure('Expected exactly 208 SMD adapters including eight token_refiner targets')
    return targets


def artifact_proof(path):
    path=Path(path).resolve(strict=True)
    if path.name!=NAME or path.is_symlink() or path.stat().st_size!=SIZE:
        raise SamplingAuditFailure('Expected the fixed complete SMD LoRA file')
    before=path.stat()
    if file_hash(path)!=SHA:raise SamplingAuditFailure('Whole SMD LoRA SHA-256 mismatch')
    with path.open('rb') as stream:
        size=struct.unpack('<Q',stream.read(8))[0]
        if not 0<size<=128*1024:raise SamplingAuditFailure('Unexpected SMD header length')
        raw=stream.read(size)
    if hashlib.sha256(raw).hexdigest()!=HEADER_SHA:raise SamplingAuditFailure('SMD header differs from the reviewed source')
    header=json.loads(raw);targets=header_targets(header)
    for spec in targets.values():
        for role in ('up','down','alpha'):
            data=header[spec[role+'_key']];start,end=data['data_offsets']
            spec[role+'_sha256']=file_hash(path,8+size+start,end-start)
            if role=='alpha':
                with path.open('rb') as stream:
                    stream.seek(8+size+start);value=struct.unpack('<f',stream.read(4))[0]
                if not math.isfinite(value) or value==0:raise SamplingAuditFailure('Invalid zero/nonfinite SMD alpha')
                spec['alpha']=value
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise SamplingAuditFailure('LoRA file changed during proof')
    return {'path':str(path),'bytes':SIZE,'sha256':SHA,'header_sha256':HEADER_SHA,
            'repo':REPO,'revision':REVISION,'targets':targets}


def tensor_hash(tensor):
    # No clone, GPU-to-CPU copy, dtype conversion, or contiguous allocation.
    if str(tensor.device)!='cpu' or not tensor.is_contiguous():
        raise SamplingAuditFailure('Source tensor proof requires the original contiguous CPU LoRA tensors')
    import torch
    view=tensor.detach().view(torch.uint8).numpy()
    return hashlib.sha256(memoryview(view).cast('B')).hexdigest()


def inspect_registration(before,after,proof,adapter_type):
    if before is after or getattr(before,'model',None) is not getattr(after,'model',None):
        raise SamplingAuditFailure('Native LoRA loader must clone the same base ModelPatcher')
    if before.patches_uuid==after.patches_uuid:
        raise SamplingAuditFailure('Native added patches must have a distinct patch uuid')
    if any(getattr(before,name,None) for name in ('patches','hook_patches','injections','weight_wrapper_patches')):
        raise SamplingAuditFailure('The before model already carries patches/hooks/injections')
    if any(getattr(after,name,None) for name in ('hook_patches','injections','weight_wrapper_patches')):
        raise SamplingAuditFailure('After model carries additional hooks/injections outside native SMD patches')
    current=getattr(before.model,'current_weight_patches_uuid',None)
    if current is not None and current!=getattr(before,'patches_uuid',None):
        raise SamplingAuditFailure('The shared base has weights from another patch set; use a clean baseline')
    patches=getattr(after,'patches',{})
    if set(patches)!=set(proof['targets']):raise SamplingAuditFailure('Added native target keys differ from the exact 208 SMD targets')
    state=before.model.state_dict();registrations={};rows=[]
    for target,spec in proof['targets'].items():
        entries=patches[target]
        if len(entries)!=1 or len(entries[0])!=5:raise SamplingAuditFailure('Exactly one native adapter patch per target is required')
        strength,adapter,model_strength,offset,function=entries[0]
        if strength!=1.0 or model_strength!=1.0 or offset is not None or function is not None:
            raise SamplingAuditFailure('SMD requires strength=1/model_strength=1 with no offset/function')
        if type(adapter) is not adapter_type or len(adapter.weights)!=6:
            raise SamplingAuditFailure('Expected the actual native LoRAAdapter')
        up,down,alpha,mid,dora,reshape=adapter.weights
        if (list(up.shape)!=spec['up_shape'] or list(down.shape)!=spec['down_shape'] or alpha!=spec['alpha']
                or mid is not None or dora is not None or reshape is not None):
            raise SamplingAuditFailure('Native adapter up/down/alpha or optional fields differ from source')
        if not {spec['up_key'],spec['down_key'],spec['alpha_key']}.issubset(adapter.loaded_keys):
            raise SamplingAuditFailure('Native adapter loaded_keys do not prove the reviewed source tensors')
        if tensor_hash(up)!=spec['up_sha256'] or tensor_hash(down)!=spec['down_sha256']:
            raise SamplingAuditFailure('Native adapter tensor bytes differ from the pinned LoRA source')
        if target not in state or list(state[target].shape)!=spec['base_shape']:
            raise SamplingAuditFailure('Actual INT8 target weight shape differs from the adapter product')
        registrations[target]={'adapter':weakref.ref(adapter),'spec':dict(spec)}
        rows.append({**spec,'strength':strength,'model_strength':model_strength,'offset':offset,
                     'native_adapter_type':adapter_type.__module__+'.'+adapter_type.__qualname__,
                     'base_tensor_type':type(state[target]).__module__+'.'+type(state[target]).__qualname__,
                     'base_dtype':str(state[target].dtype),'adapter_tensor_bytes_verified':True})
    del state
    return registrations,rows


def resource_snapshot():
    result={'measurement':'instantaneous snapshots, not scope peaks'}
    try:
        import psutil
        result.update(process_rss_bytes=psutil.Process().memory_info().rss,
                      available_system_ram_bytes=psutil.virtual_memory().available)
    except Exception as error:result['process_memory_status']=type(error).__name__
    try:
        import torch
        if torch.cuda.is_initialized():
            result.update(torch_cuda_allocated_bytes=torch.cuda.memory_allocated(),
                          torch_cuda_reserved_bytes=torch.cuda.memory_reserved())
        else:result['torch_cuda_status']='NOT_INITIALIZED'
    except Exception as error:result['torch_cuda_status']=type(error).__name__
    return result


class _NativeErrors(logging.Handler):
    def __init__(self,job):super().__init__(logging.WARNING);self.job=job
    def emit(self,record):
        if not self.job.active:return
        context=_CALL.get()
        if context is not None and context is not self.job:return
        message=record.getMessage();lower=message.lower()
        if ('error lora' in lower or 'calculate weight failed' in lower or 'shape mismatch' in lower):
            with self.job.lock:self.job.errors.append({'level':record.levelname,'message':message})


class ApplicationTrace:
    """One real sampling scope. Never retain a prepared clone or tensor."""
    def __init__(self,registrations,adapter_type):
        self.registrations=registrations;self.adapter_type=adapter_type;self.lock=threading.RLock()
        self.native_calculate=None;self.native_code=None
        self.active=False;self.refs=[];self.classes=[];self.success=set();self.calls={};self.errors=[];self.pending=0
        self.handler=_NativeErrors(self);self.restored=False;self.pending_at_close=0

    def _remember(self,value):
        with self.lock:
            if self.active:self.refs.append(weakref.ref(value))
            else:value.__class__=self.adapter_type

    def _class(self,target,spec):
        job=self;native=self.adapter_type;original=self.native_calculate
        class Tracked(native):
            def __init__(self,*args,**kwargs):
                native.__init__(self,*args,**kwargs)
                job._remember(self)
            def calculate_weight(self,weight,key,strength,strength_model,offset,function,*args,**kwargs):
                with job.lock:
                    admitted=job.active
                    if admitted:job.pending+=1
                if not admitted:return native.calculate_weight(self,weight,key,strength,strength_model,offset,function,*args,**kwargs)
                token=_CALL.set(job)
                try:
                    if (native.calculate_weight is not original or original.__code__ is not job.native_code
                            or original.__globals__ is not sys.modules['comfy.weight_adapter.lora'].__dict__):
                        raise SamplingAuditFailure('The verified native calculate_weight changed during this sampling scope')
                    if (key!=target or strength!=1.0 or strength_model!=1.0 or offset is not None
                            or list(weight.shape)!=spec['base_shape'] or list(self.weights[0].shape)!=spec['up_shape']
                            or list(self.weights[1].shape)!=spec['down_shape'] or self.weights[2]!=spec['alpha']):
                        raise SamplingAuditFailure('Actual native application parameters/shape differ from registration: '+target)
                    with job.lock:
                        errors_before=len(job.errors);job.calls[target]=job.calls.get(target,0)+1
                    result=original(self,weight,key,strength,strength_model,offset,function,*args,**kwargs)
                    if result is None or list(result.shape)!=spec['base_shape']:
                        raise SamplingAuditFailure('Native calculate_weight returned no valid target tensor: '+target)
                    with job.lock:
                        if len(job.errors)==errors_before:job.success.add(target)
                    return result
                except Exception as error:
                    with job.lock:job.errors.append({'target':target,'error_type':type(error).__name__,'message':str(error)})
                    raise
                finally:
                    with job.lock:job.pending-=1
                    _CALL.reset(token)
        Tracked.__name__='SMDTracked_'+target.replace('.','_')
        return Tracked

    def __enter__(self):
        if (logging.root.disabled or logging.root.filters or logging.root.manager.disable>=logging.WARNING
                or logging.root.getEffectiveLevel()>logging.WARNING):
            raise SamplingAuditFailure('Native WARNING/ERROR logging is disabled; swallowed failures cannot be audited')
        self.native_calculate=verified_native_calculate(self.adapter_type);self.native_code=self.native_calculate.__code__
        self.active=True;logging.getLogger().addHandler(self.handler)
        try:
            for target,item in self.registrations.items():
                adapter=item['adapter']()
                if adapter is None or type(adapter) is not self.adapter_type:
                    raise SamplingAuditFailure('Registered native adapter was replaced or released before sampling')
                cls=self._class(target,item['spec']);self.classes.append(cls)
                self._remember(adapter);adapter.__class__=cls
            return self
        except BaseException:self.__exit__(None,None,None);raise

    def __exit__(self,*exc):
        failures=[]
        with self.lock:
            self.active=False;self.pending_at_close=self.pending
            for reference in self.refs:
                value=reference()
                if value is not None:
                    try:value.__class__=self.adapter_type
                    except Exception as error:failures.append(str(error))
            self.refs.clear();self.classes.clear()
        logging.getLogger().removeHandler(self.handler)
        self.restored=not failures
        if failures:self.errors.append({'restoration_failures':failures})


def attach_sampling_audit(model,registrations,adapter_type,directory,registration):
    from comfy.patcher_extension import WrappersMP
    output=model.clone()
    if output.get_wrappers(WrappersMP.OUTER_SAMPLE,WRAPPER_KEY):
        raise SamplingAuditFailure('This ModelPatcher already has an SMD audit wrapper')
    counter=[0];lock=threading.Lock()
    def outer_sample(executor,*args,**kwargs):
        with lock:counter[0]+=1;number=counter[0]
        trace=ApplicationTrace(registrations,adapter_type)
        started=time.perf_counter()
        record={'version':VERSION,'status':'FAIL','actual_outer_sample_entered':True,
            'sampling_index':number,'required_targets':sorted(registrations),
            'registration_receipt':str(Path(directory)/'registration.json'),
            'registration_sha256':file_hash(Path(directory)/'registration.json'),
            'visual_quality':'NOT_REVIEWED','memory_stability':'NOT_VERIFIED',
            'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'resources_before':resource_snapshot(),
            'timing_scope':'OUTER_SAMPLE includes native prepare/load and sampling; excludes VAE/audio decode',
            'excluded_from_timing':['Main compile/conditioning','native LoRA loader node','audit file/tensor verification','VAE/audio decode and SaveVideo'],
            'scope_memory_peak':'NOT_MEASURED; instantaneous before/after snapshots only',
            'audit_scope':'native adapter application, not loaded base-file identity or overall SMD compatibility',
            'base_file_identity':'NOT_VERIFIED_BY_THIS_NODE','SMD_recipe_compatibility':'NOT_CLAIMED',
            'paired_order':'after baseline Sampler dependency' if registration.get('baseline_sample_dependency_received') else 'standalone; cache state must be recorded'}
        try:
            active=getattr(getattr(executor,'class_obj',None),'model_patcher',None)
            if active is None or getattr(active,'model',None) is not model.model:
                raise SamplingAuditFailure('OUTER_SAMPLE did not expose the actual audited ModelPatcher')
            if set(active.patches)!=set(registrations) or active.patches_uuid!=model.patches_uuid:
                raise SamplingAuditFailure('Actual sampling patch set/uuid differs from audited registration')
            for target,item in registrations.items():
                entries=active.patches[target]
                if (len(entries)!=1 or entries[0][1] is not item['adapter']() or
                        entries[0][0]!=1. or entries[0][2]!=1. or entries[0][3] is not None or entries[0][4] is not None):
                    raise SamplingAuditFailure('Actual sampling adapter identity/strength/offset differs: '+target)
            record.update(active_model_patcher_id=id(active),active_model_patcher_type=type(active).__module__+'.'+type(active).__qualname__,
                active_patches_uuid=str(active.patches_uuid),active_patch_target_count=len(active.patches),
                actual_strengths_verified=True,current_weight_patches_uuid_before=str(getattr(active.model,'current_weight_patches_uuid',None)))
            if registration.get('registered_target_count')==208:
                if len(args)<8:raise SamplingAuditFailure('Actual SMD OUTER_SAMPLE arguments were not captured')
                sampler=args[2];sigmas=args[3]
                sampler_receipt=verified_sampler(sampler,'euler')
                sampler_name='sample_euler'
                actual_sigmas=[float(value) for value in sigmas]
                shifts=active.model_options.get('transformer_options',{})
                if (not smd_sigmas_valid(actual_sigmas)
                        or shifts.get('minimax_h3_sigma_shift_video')!=12.0 or shifts.get('minimax_h3_sigma_shift_audio')!=3.0):
                    raise SamplingAuditFailure('Actual Euler/manual sigma/12:3 shift contract differs')
                record.update(actual_sampler=sampler_name,actual_sigmas=actual_sigmas,actual_seed=args[7],
                    actual_shift_video=12.0,actual_shift_audio=3.0,actual_sampler_contract=sampler_receipt)
            with trace:result=executor(*args,**kwargs)
            current=getattr(active.model,'current_weight_patches_uuid',None)
            record['current_weight_patches_uuid_after']=str(current)
            if current!=active.patches_uuid:raise SamplingAuditFailure('Native loaded weight uuid does not match the actual SMD sampling patcher')
            if trace.errors or trace.success!=set(registrations) or not trace.restored or trace.pending_at_close:
                raise SamplingAuditFailure('SMD application was not proven for every native target; missing='+
                    str(len(set(registrations)-trace.success))+'; native_errors='+str(len(trace.errors)))
            record['status']='PASS'
            return result
        except BaseException as error:
            record.update(error_type=type(error).__name__,error=str(error));raise
        finally:
            record.update(successful_targets=sorted(trace.success),calls_by_target=dict(trace.calls),
                missing_targets=sorted(set(registrations)-trace.success),native_errors=list(trace.errors),
                token_refiner_successful_targets=sorted(k for k in trace.success if 'token_refiner.' in k),
                temporary_adapter_classes_restored=trace.restored,
                native_calls_pending_at_scope_exit=trace.pending,
                native_calls_pending_when_scope_closed=trace.pending_at_close,
                native_calculate_code_sha256=hashlib.sha256(json.dumps(code_payload(trace.native_code),sort_keys=True,separators=(',',':')).encode()).hexdigest() if trace.native_code is not None else None,
                strong_prepared_clone_references_retained=False)
            record.update(elapsed_seconds=time.perf_counter()-started,finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                          resources_after=resource_snapshot())
            write_json(Path(directory)/('sampling-'+str(number).zfill(3)+'.json'),record)
            write_json(Path(directory)/'audit.json',record)
            with trace.lock:
                if not trace.pending:
                    trace.registrations={};trace.errors.clear();trace.calls.clear();trace.success.clear()
    output.add_wrapper_with_key(WrappersMP.OUTER_SAMPLE,WRAPPER_KEY,outer_sample)
    return output


class VideoFactoryBaselineSamplingObserver:
    """Observe the patch-free INT8 comparison branch without changing sampling."""
    @classmethod
    def INPUT_TYPES(cls):return {'required':{'model':('MODEL',)}}
    RETURN_TYPES=('MODEL',)
    FUNCTION='observe'
    CATEGORY='VideoFactory/local H3'
    @classmethod
    def IS_CHANGED(cls,**kwargs):return float('nan')
    def observe(self,model):
        import folder_paths
        import comfy.model_patcher
        from comfy.patcher_extension import WrappersMP
        verify_core(Path(comfy.model_patcher.__file__).resolve().parents[1])
        if model.patches:raise SamplingAuditFailure('INT8 baseline MODEL already has weight patches')
        output=model.clone();counter=[0]
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%fZ')
        directory=Path(folder_paths.get_output_directory())/'h3_sampling_baseline_observer'/(stamp+'-'+uuid.uuid4().hex[:8])
        directory.mkdir(parents=True)
        write_json(directory/'audit.json',{'status':'REGISTERED_NOT_SAMPLED','weight_patch_count':0})
        def observe_sample(executor,*args,**kwargs):
            counter[0]+=1;started=time.perf_counter()
            record={'status':'FAIL','sampling_index':counter[0],'branch':'INT8_20step_baseline',
                'started_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'resources_before':resource_snapshot(),
                'timing_scope':'OUTER_SAMPLE includes native prepare/load and sampling; excludes VAE/audio decode',
                'excluded_from_timing':['Main compile/conditioning','native LoRA loader node','audit file/tensor verification','VAE/audio decode and SaveVideo'],
                'scope_memory_peak':'NOT_MEASURED; instantaneous before/after snapshots only'}
            try:
                active=getattr(getattr(executor,'class_obj',None),'model_patcher',None)
                if active is None or active.model is not model.model or active.patches or active.patches_uuid!=model.patches_uuid:
                    raise SamplingAuditFailure('Actual baseline sampling MODEL has unexpected weight patches/uuid')
                record.update(active_model_patcher_id=id(active),active_model_patcher_type=type(active).__module__+'.'+type(active).__qualname__,
                    active_patches_uuid=str(active.patches_uuid),actual_weight_patch_count=0,
                    current_weight_patches_uuid_before=str(getattr(active.model,'current_weight_patches_uuid',None)))
                if len(args)<8:raise SamplingAuditFailure('Actual baseline OUTER_SAMPLE arguments were not captured')
                sampler_receipt=verified_sampler(args[2],'res_multistep')
                actual_sigmas=[float(value) for value in args[3]]
                if not baseline_sigmas_valid(actual_sigmas):
                    raise SamplingAuditFailure('Actual baseline requires res_multistep and twenty sigma intervals')
                record.update(actual_sampler='sample_res_multistep',actual_sigmas=actual_sigmas,actual_seed=args[7],
                    actual_sampler_contract=sampler_receipt)
                result=executor(*args,**kwargs)
                current=getattr(active.model,'current_weight_patches_uuid',None)
                record['current_weight_patches_uuid_after']=str(current)
                if current!=active.patches_uuid:raise SamplingAuditFailure('Native baseline did not restore/stamp its own clean weight uuid')
                for _,layer in active.model.named_modules():
                    for attr in ('weight_lowvram_function','bias_lowvram_function','weight_function','bias_function'):
                        value=getattr(layer,attr,None)
                        for function in value if isinstance(value,(list,tuple)) else (value,):
                            if getattr(function,'is_lowvram_patch',False) and function.patches.get(function.key):
                                raise SamplingAuditFailure('Baseline retained a live LoRA low-VRAM weight function')
                record.update(status='PASS',residual_lora_lowvram_functions=0)
                return result
            except BaseException as error:record.update(error_type=type(error).__name__,error=str(error));raise
            finally:
                record.update(elapsed_seconds=time.perf_counter()-started,finished_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                              resources_after=resource_snapshot())
                write_json(directory/('sampling-'+str(counter[0]).zfill(3)+'.json'),record);write_json(directory/'audit.json',record)
        output.add_wrapper_with_key(WrappersMP.OUTER_SAMPLE,WRAPPER_KEY+'_baseline',observe_sample)
        return {'result':(output,),'ui':{'baseline_sampling_receipt':[str(directory/'audit.json')]}}


class VideoFactorySMDSamplingAudit:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required':{'before_model':('MODEL',),'model':('MODEL',),
                            'lora_name':([NAME],)},'optional':{'baseline_samples':('LATENT',{'advanced':True})}}
    RETURN_TYPES=('MODEL',)
    RETURN_NAMES=('model',)
    FUNCTION='audit'
    CATEGORY='VideoFactory/local H3'
    DESCRIPTION='Audit fixed native SMD tensor registration and actual calculate_weight execution in this sampling scope. Registration alone is never PASS.'
    @classmethod
    def IS_CHANGED(cls,**kwargs):return float('nan')

    def audit(self,before_model,model,lora_name,baseline_samples=None):
        verification_started=time.perf_counter();verification_started_utc=dt.datetime.now(dt.timezone.utc).isoformat()
        import folder_paths
        import comfy.model_patcher
        from comfy.weight_adapter.lora import LoRAAdapter
        if lora_name!=NAME:raise SamplingAuditFailure('Only the fixed reviewed SMD LoRA is allowed')
        core=Path(comfy.model_patcher.__file__).resolve().parents[1]
        pins=verify_core(core)
        path=folder_paths.get_full_path_or_raise('loras',lora_name)
        proof=artifact_proof(path)
        registrations,rows=inspect_registration(before_model,model,proof,LoRAAdapter)
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%fZ')
        directory=Path(folder_paths.get_output_directory())/'h3_smd_sampling_audit'/(stamp+'-'+uuid.uuid4().hex[:8])
        directory.mkdir(parents=True)
        record={'version':VERSION,'status':'REGISTERED_NOT_SAMPLED','artifact':{k:v for k,v in proof.items() if k!='targets'},
            'registered_target_count':len(rows),'token_refiner_count':sum('token_refiner.' in row['target'] for row in rows),
            'targets':rows,'core_source_sha256':pins,'base_model_old_patch_count':0,
            'before_model_patcher_id':id(before_model),'before_patches_uuid':str(before_model.patches_uuid),
            'after_model_patcher_id':id(model),'after_patches_uuid':str(model.patches_uuid),
            'current_weight_patches_uuid_at_registration':str(getattr(before_model.model,'current_weight_patches_uuid',None)),
            'baseline_sample_dependency_received':baseline_samples is not None,
            'verification_started_utc':verification_started_utc,
            'verification_elapsed_seconds':time.perf_counter()-verification_started,
            'audit_scope':'native adapter application; base-file identity and overall SMD compatibility require separate evidence',
            'base_file_identity':'NOT_VERIFIED_BY_THIS_NODE','SMD_recipe_compatibility':'NOT_CLAIMED',
            'application_verified':False,'visual_quality':'NOT_REVIEWED','memory_stability':'NOT_VERIFIED'}
        write_json(directory/'registration.json',record);write_json(directory/'audit.json',record)
        output=attach_sampling_audit(model,registrations,LoRAAdapter,directory,record)
        return {'result':(output,),'ui':{'smd_registration_receipt':[str(directory/'registration.json')],
            'smd_sampling_audit_receipt':[str(directory/'audit.json')]}}
