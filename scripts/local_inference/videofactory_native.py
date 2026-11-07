"""Install/check the separately owned, pinned NVIDIA Context-IR runtime. Never downloads weights."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import errno
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
RELEASE = HERE.parents[1]
MANIFEST = RELEASE/'runtime-manifests/context-ir-native-v1.json'
PROFILE = 'local-h3ir-qwen4b-v3'
QWEN_ID = 'Qwen/Qwen3-VL-4B-Instruct'

def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(path)

def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8*1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('Native runtime redirects are forbidden')

def local_url(origin):
    parsed = urlsplit(origin)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1','localhost','::1')
            or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment):
        raise ValueError('The native supervisor upstream must be a credential-free loopback HTTP origin')
    return origin.rstrip('/')

def request_json(origin, route, body=None, timeout=5):
    local_url(origin)
    if not route.startswith('/') or route.startswith('//'):
        raise ValueError('Expected a local API route')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    raw = None if body is None else json.dumps(body,ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(origin+route,data=raw,headers={'Content-Type':'application/json'})
    with opener.open(req, timeout=timeout) as response:
        return json.load(response)

def queue_idle(queue):
    if not isinstance(queue,dict) or any(not isinstance(queue.get(k),list) for k in ('queue_running','queue_pending')):
        raise ValueError('Unrecognized native Comfy queue; idle not assumed')
    return not queue['queue_running'] and not queue['queue_pending']

def connection_refused(error):
    """A known HTTP failure/timeout is not proof that no Comfy process exists."""
    if isinstance(error,urllib.error.HTTPError):return False
    reason=getattr(error,'reason',error)
    return (isinstance(reason,ConnectionRefusedError) or getattr(reason,'errno',None) in (errno.ECONNREFUSED,10061)
            or getattr(reason,'winerror',None)==10061)

def config_at(path):
    config = load(path)
    if config.get('schema_version') != 1 or config.get('profile_id') != PROFILE:
        raise ValueError('Unsupported native configuration schema/profile')
    local_url(config['comfy_origin'])
    for key in ('comfy_root','model_root','runtime_root','comfy_python','qwen_python','qwen_model'):
        if not Path(config[key]).is_absolute():
            raise ValueError('Configuration paths must be absolute: '+key)
    if Path(config['runtime_root']).resolve() in (Path(config['comfy_root']).resolve(),Path(config['model_root']).resolve()):
        raise ValueError('RuntimeRoot must be separate from ComfyRoot and ModelRoot')
    return config

def discovered(root, name):
    candidates = list(Path(root).rglob(name)) if Path(root).is_dir() else []
    candidates = [p.resolve() for p in candidates if p.is_file()]
    if len(candidates) > 1:
        raise ValueError('Several files match '+name+'; set model_paths in native-runtime.json explicitly')
    return str(candidates[0]) if candidates else None

def initialize(args):
    root = args.runtime_root.resolve()
    output = root/'native-runtime.json'
    if output.exists():
        raise ValueError('Configuration already exists; edit it explicitly instead of overwriting it')
    manifest = load(MANIFEST)
    paths = {};warnings=[]
    for item in manifest['models']:
        try:found=discovered(args.model_root,item['name'])
        except ValueError as error:found=None;warnings.append(str(error))
        paths[item['name']] = found or str(args.model_root.resolve()/item['category']/item['name'])
    qwen_dirs=[p for p in args.model_root.rglob('Qwen3-VL-4B-Instruct') if p.is_dir()]
    if args.model_root.name=='Qwen3-VL-4B-Instruct':qwen_dirs.append(args.model_root)
    if len(qwen_dirs)>1:warnings.append('Several Qwen3-VL-4B-Instruct folders; set qwen_model explicitly')
    qwen=qwen_dirs[0].resolve() if len(qwen_dirs)==1 else args.model_root.resolve()/'Qwen3-VL-4B-Instruct'
    config = {'schema_version':1,'profile_id':PROFILE,'comfy_root':str(args.comfy_root.resolve()),
              'model_root':str(args.model_root.resolve()),'runtime_root':str(root),
              'comfy_python':str(args.comfy_python.resolve()),'qwen_python':str(args.qwen_python.resolve()),
              'qwen_model':str(qwen),
              'model_paths':paths,'comfy_origin':'http://127.0.0.1:8188','listen':'127.0.0.1','port':8788,
              'allowed_clients':['127.0.0.1','::1'],
              'input_root':str(root/'input'),'output_root':str(root/'output'),
              'ffmpeg_path':shutil.which('ffmpeg') or str(args.comfy_root.resolve()/'ffmpeg.exe'),
              'ffprobe_path':shutil.which('ffprobe') or str(args.comfy_root.resolve()/'ffprobe.exe'),
              'source_manifest_sha256':sha(MANIFEST),'discovery_warnings':warnings}
    for name in ('input','output','logs','jobs','tmp','cache/qwen','cache/huggingface','cache/torch','cache/cuda'):
        (root/name).mkdir(parents=True,exist_ok=True)
    write(output,config)
    print(str(output))

def probe_python(python,names):
    code = "import importlib.metadata as m,json,sys\nout={}\nfor n in sys.argv[1:]:\n try:out[n]=m.version(n)\n except m.PackageNotFoundError:out[n]=None\nprint(json.dumps({'python':sys.version_info[:2],'packages':out}))"
    # Distribution lookups do not import torch or allocate model/CUDA memory.
    result = subprocess.run([str(python),'-B','-c',code,*names],capture_output=True,text=True,encoding='utf-8',timeout=30)
    if result.returncode:
        raise ValueError('Python dependency probe failed: '+result.stderr[-1500:])
    return json.loads(result.stdout)

def probe_sampler_sources(python,pins):
    code="import importlib.metadata as m,json,pathlib,hashlib; root=pathlib.Path(m.distribution('torch').locate_file('torch')); files=['utils/_contextlib.py','autograd/grad_mode.py']; print(json.dumps({p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in files}))"
    result=subprocess.run([str(python),'-B','-c',code],capture_output=True,text=True,encoding='utf-8',timeout=30)
    if result.returncode:raise ValueError('Cannot verify native sampler decorator source: '+result.stderr[-1000:])
    expected={key.split('site-packages/torch/',1)[1]:value for key,value in pins.items()}
    actual=json.loads(result.stdout)
    return {'kind':'native_sampler_decorator_source','ok':actual==expected,'actual':actual,'expected':expected}

def probe_hardware(python):
    """Ask the actual inference interpreter about its driver; never construct a model."""
    code = r'''
import ctypes,json,sys
result={'python':sys.executable,'cuda_available':False,'devices':[],
        'system_ram':None,'windows_commit':None,'models_loaded':False,'diagnostics':[],
        'scope':'Driver/device discovery only; does not establish video generation capacity'}
try:
 import psutil
 memory=psutil.virtual_memory()
 result['system_ram']={'total_bytes':int(memory.total),'available_bytes':int(memory.available)}
except Exception as error:
 result['diagnostics'].append({'code':'system_ram_probe_failed','error':str(error)})
if sys.platform=='win32':
 try:
  class Info(ctypes.Structure):
   _fields_=[('cb',ctypes.c_ulong)]+[(name,ctypes.c_size_t) for name in (
    'CommitTotal','CommitLimit','CommitPeak','PhysicalTotal','PhysicalAvailable',
    'SystemCache','KernelTotal','KernelPaged','KernelNonpaged','PageSize')]+[
    ('HandleCount',ctypes.c_ulong),('ProcessCount',ctypes.c_ulong),('ThreadCount',ctypes.c_ulong)]
  value=Info();value.cb=ctypes.sizeof(value)
  if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(value),value.cb):
   raise OSError('GetPerformanceInfo failed')
  total=int(value.CommitTotal*value.PageSize);limit=int(value.CommitLimit*value.PageSize)
  result['windows_commit']={'total_bytes':total,'limit_bytes':limit,'headroom_bytes':limit-total}
 except Exception as error:
  result['diagnostics'].append({'code':'windows_commit_probe_failed','error':str(error)})
try:
 print('VF_HARDWARE_STAGE import_torch',file=sys.stderr,flush=True)
 import torch
 print('VF_HARDWARE_STAGE cuda_availability',file=sys.stderr,flush=True)
 result['torch_version']=str(torch.__version__)
 result['torch_cuda_build']=torch.version.cuda
 result['cuda_available']=bool(torch.cuda.is_available())
 if result['cuda_available']:
  print('VF_HARDWARE_STAGE device_properties',file=sys.stderr,flush=True)
  for index in range(torch.cuda.device_count()):
   props=torch.cuda.get_device_properties(index)
   result['devices'].append({'index':index,'name':props.name,'total_vram_bytes':int(props.total_memory),
       'compute_capability':{'major':int(props.major),'minor':int(props.minor)},
       'multiprocessor_count':int(props.multi_processor_count)})
 else:
  code='pytorch_without_cuda' if torch.version.cuda is None else 'cuda_device_or_driver_unavailable'
  result['diagnostics'].append({'code':code,'error':
      'No usable NVIDIA CUDA device reported by this interpreter. Check its CUDA wheel, NVIDIA driver, visible devices and hardware.'})
except Exception as error:
 result['diagnostics'].append({'code':'torch_cuda_probe_failed','error':str(error)})
result['ok']=bool(result['cuda_available'] and result['devices'])
print(json.dumps(result))
'''
    try:
        result=subprocess.run([str(python),'-B','-c',code],capture_output=True,text=True,
                              encoding='utf-8',timeout=120)
    except subprocess.TimeoutExpired as error:
        progress=error.stderr.decode('utf-8',errors='replace') if isinstance(error.stderr,bytes) else error.stderr or ''
        return {'ok':False,'cuda_available':False,'devices':[],'models_loaded':False,
                'diagnostics':[{'code':'hardware_probe_timeout','error':
                    'PyTorch/CUDA discovery did not finish within 120 seconds; inspect the driver, Python environment and host memory pressure',
                    'last_probe_stage':progress[-1500:]}]}
    if result.returncode:
        raise ValueError('Actual CUDA hardware probe failed: '+result.stderr[-1500:])
    payload=json.loads(result.stdout)
    payload['probe_stages']=[line for line in result.stderr.splitlines() if line.startswith('VF_HARDWARE_STAGE ')]
    return payload

def inspect_sources(config):
    manifest = load(MANIFEST)
    checks=[]
    for relative,expected in manifest['core_pins'].items():
        path=Path(config['comfy_root'])/relative
        actual=sha(path) if path.is_file() else None
        checks.append({'kind':'comfy_source','file':relative,'expected':expected,'actual':actual,'ok':actual==expected})
    for relative,expected in manifest['openh3_node_pins'].items():
        path=Path(config['comfy_root'])/'custom_nodes/ComfyUI-OpenH3-IR'/relative
        # The installed Windows checkout was CRLF. Public source is LF; only
        # line terminators are normalized and the manifest declares this format.
        actual=hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest() if path.is_file() else None
        checks.append({'kind':'openh3_node_source','file':relative,'expected':expected,'actual':actual,'ok':actual==expected})
    for pack in manifest['project_nodes']:
        for relative,expected in pack['files'].items():
            path=Path(config['comfy_root'])/'custom_nodes'/pack['name']/relative
            actual=sha(path) if path.is_file() else None
            checks.append({'kind':'project_node','file':pack['name']+'/'+relative,'expected':expected,'actual':actual,'ok':actual==expected})
    code="import importlib.metadata as m,json,pathlib,hashlib; root=pathlib.Path(m.distribution('open-h3-ir').locate_file('h3ir')); print(json.dumps({str(p.relative_to(root)).replace(chr(92),'/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.py')}))"
    try:
        result=subprocess.run([config['comfy_python'],'-B','-c',code],capture_output=True,text=True,encoding='utf-8',timeout=30)
        if result.returncode:raise ValueError(result.stderr[-1000:])
        actual=json.loads(result.stdout)
        checks.append({'kind':'compiler_source','ok':actual==manifest['compiler_source_pins'],
                       'changed':sorted(k for k in set(actual)|set(manifest['compiler_source_pins'])
                                        if actual.get(k)!=manifest['compiler_source_pins'].get(k))})
    except Exception as error:checks.append({'kind':'compiler_source','ok':False,'error':str(error)})
    return checks

def inspect_models(config, hash_models=False):
    manifest=load(MANIFEST);cache_path=Path(config['runtime_root'])/'verified-models.json'
    cache=load(cache_path) if cache_path.is_file() else {}
    verified={};checks=[]
    items=list(manifest['models'])
    items += [dict(item,name=item['name'],absolute=str(Path(config['qwen_model'])/item['name'])) for item in manifest['qwen_files']]
    for item in items:
        path=Path(item.get('absolute') or config['model_paths'].get(item['name'],''))
        record={'kind':'model','file':item['name'],'path':str(path),'ok':False,
                'diagnostic':'linked_file_not_accepted' if path.is_symlink() else 'file_missing'}
        if path.is_file() and not path.is_symlink():
            stat=path.stat();identity={'path':str(path.resolve()),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns,'inode':stat.st_ino}
            previous=cache.get(item['name'],{})
            actual=sha(path) if hash_models else previous.get('sha256') if previous.get('identity')==identity else None
            record.update(bytes=stat.st_size,sha256=actual,expected_sha256=item['sha256'],
                          ok=stat.st_size==item['bytes'] and actual==item['sha256'])
            record['diagnostic']=('size_mismatch' if stat.st_size!=item['bytes'] else
                                  'hash_not_verified_for_current_file' if actual is None else
                                  'sha256_mismatch' if actual!=item['sha256'] else 'verified')
            if record['ok']:verified[item['name']]={'identity':identity,'sha256':actual}
        if not record['ok']:
            record['action']=('The file exists; run check --hash-models once to verify its current bytes. No download was attempted'
                              if record['diagnostic']=='hash_not_verified_for_current_file' else
                              'Check the configured path and exact artifact revision, then run check --hash-models. No download was attempted')
        checks.append(record)
    if hash_models:write(cache_path,verified)
    return checks

def inspect_media_tool(config,name):
    executable=config.get(name+'_path') or shutil.which(name)
    record={'kind':name,'ok':False,'path':executable,
            'action':'Set '+name+'_path in native-runtime.json or install your own FFmpeg distribution on PATH'}
    if executable is None:return record
    try:
        result=subprocess.run([str(executable),'-version'],capture_output=True,text=True,encoding='utf-8',
                              errors='replace',timeout=15)
        first=(result.stdout or result.stderr).splitlines()
        record.update(ok=result.returncode==0 and bool(first) and first[0].startswith(name+' version'),
                      version=first[0] if first else None)
        if not record['ok']:record['error']='Configured executable did not report a valid '+name+' version'
    except (OSError,subprocess.TimeoutExpired) as error:record['error']=str(error)
    return record

def check(config, hash_models=False):
    manifest=load(MANIFEST)
    checks=[{'kind':'platform','ok':sys.platform=='win32','actual':sys.platform,
             'required':'win32; NVIDIA CUDA. Linux source/runtime acceptance is not completed; Metal is unsupported'}]
    for name in ('ffmpeg','ffprobe'):checks.append(inspect_media_tool(config,name))
    checks+=inspect_sources(config)+inspect_models(config,hash_models)
    for key,expected in [('comfy_python',manifest['comfy_packages']),('qwen_python',manifest['qwen_packages'])]:
        try:
            actual=probe_python(config[key],expected)
            expected_python=manifest['python_versions'][key]
            ok=actual['python']==expected_python and all(actual['packages'].get(k)==v for k,v in expected.items())
            checks.append({'kind':key,'ok':ok,'actual':actual,'expected_python':expected_python,'expected_packages':expected})
        except Exception as error:checks.append({'kind':key,'ok':False,'error':str(error)})
        try:
            hardware=probe_hardware(config[key])
            checks.append({'kind':key+'_hardware','ok':hardware['ok'],'actual':hardware})
        except Exception as error:checks.append({'kind':key+'_hardware','ok':False,'error':str(error)})
    try:checks.append(probe_sampler_sources(config['comfy_python'],manifest['torch_source_pins']))
    except Exception as error:checks.append({'kind':'native_sampler_decorator_source','ok':False,'error':str(error)})
    result={'schema_version':1,'profile_id':PROFILE,'ready':all(c['ok'] for c in checks),'checks':checks,
            'weights_downloaded':False,'generation_tested':False}
    write(Path(config['runtime_root'])/'installation-check.json',result)
    return result

def install(config):
    # Explicit install only copies our bundled files. Never replaces vendor/core sources.
    if not (Path(config['comfy_root'])/'main.py').is_file():
        raise ValueError('ComfyRoot does not contain main.py; no node files copied')
    try:
        if not queue_idle(request_json(config['comfy_origin'],'/queue')):
            raise ValueError('ComfyUI is busy; no node files copied')
    except urllib.error.URLError as error:
        if not connection_refused(error):
            raise ValueError('Comfy queue availability is unknown; no node files copied: '+str(error)) from error
    manifest=load(MANIFEST);copies=[]
    for pack in manifest['project_nodes']:
        for relative,expected in pack['files'].items():
            src=HERE/'bundle/comfy_nodes'/pack['name']/relative
            target=Path(config['comfy_root'])/'custom_nodes'/pack['name']/relative
            if sha(src)!=expected:raise ValueError('Bundled source manifest mismatch: '+str(src))
            if target.exists() and sha(target)!=expected:
                raise ValueError('Different existing custom node; preserve it and review manually: '+str(target))
            copies.append((src,target))
    for src,target in copies:
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():shutil.copyfile(src,target)
    # Separate YAML, passed as an extra config; existing model configuration stays intact.
    directories={}
    for item in manifest['models']:
        directories.setdefault(item['category'],set()).add(str(Path(config['model_paths'][item['name']]).parent))
    lines=['# Generated additional user-owned model paths; no weights included.']
    for index,(category,paths) in enumerate(sorted(directories.items())):
        for subindex,path in enumerate(sorted(paths)):
            lines += [f'video_factory_{index}_{subindex}:',f'  base_path: {json.dumps(path)}',f'  {category}: .']
    (Path(config['runtime_root'])/'extra-model-paths.yaml').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # Existing runtime expects saved repository metadata to validate pinned model shards.
    for name in ('factory-model-manifest.json','official-repository-metadata.json'):
        target=Path(config['qwen_model'])/name;source=HERE/'bundle/qwen-metadata'/name
        if target.exists():
            if load(target)!=load(source):
                # manifest.directory is informational in the existing Qwen verifier.
                actual=load(target);expected=load(source)
                actual.pop('directory',None);expected.pop('directory',None)
                if name=='official-repository-metadata.json':
                    def metadata_rows(value):
                        return {item['Path']:{k:item.get(k) for k in ('Path','Sha256','Size','Revision')}
                                for item in value.get('Data',{}).get('Files',[])}
                    actual=metadata_rows(actual);expected=metadata_rows(expected)
                    actual={key:actual.get(key) for key in expected}
                if actual!=expected:raise ValueError('Existing Qwen verification metadata differs: '+str(target))
        else:
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    print(json.dumps({'project_files_installed':len(copies),'weights_downloaded':False,'services_restarted':False,
                      'next':'check --hash-models, then start-comfy; existing running Comfy must be restarted manually after its queue is idle'}))

def environment(config):
    env=dict(os.environ)
    root=Path(config['runtime_root'])
    env.update({'VF_RUNTIME_ROOT':str(root),'VF_QWEN_MODEL':config['qwen_model'],
                'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_DATASETS_OFFLINE':'1',
                'HF_HUB_DISABLE_TELEMETRY':'1','PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8',
                'H3IR_GUIDED_DECODING':'1','H3IR_LLM_THINKING':'0','H3IR_LLM_MAX_TOKENS':'4096',
                'H3IR_STATE_DIR':str(root/'cache/h3ir'),'H3IR_LLM_KEY':'not-needed',
                'HF_HOME':str(root/'cache/huggingface'),'TORCH_HOME':str(root/'cache/torch')})
    tool_dirs=[str(Path(config[name+'_path']).resolve().parent) for name in ('ffmpeg','ffprobe') if config.get(name+'_path')]
    if tool_dirs:env['PATH']=os.pathsep.join(dict.fromkeys(tool_dirs))+os.pathsep+env.get('PATH','')
    return env

def start_comfy(config):
    result=check(config)
    if not result['ready']:raise ValueError('Native installation checks failed; see installation-check.json')
    try:
        request_json(config['comfy_origin'],'/system_stats')
        raise ValueError('A ComfyUI already responds. No restart or duplicate process was attempted')
    except urllib.error.URLError as error:
        if not connection_refused(error):
            raise ValueError('Comfy availability is unknown; no duplicate service started: '+str(error)) from error
    root=Path(config['runtime_root']);core=Path(config['comfy_root'])
    port=urlsplit(config['comfy_origin']).port
    args=[config['comfy_python'],'-X','utf8','-B',str(core/'main.py'),'--listen','127.0.0.1','--port',str(port),
          '--disable-auto-launch','--disable-async-offload','--disable-pinned-memory','--disable-comfy-compiler','--cache-none',
          '--input-directory',config['input_root'],'--output-directory',config['output_root'],
          '--user-directory',str(root/'user'),'--extra-model-paths-config',str(root/'extra-model-paths.yaml')]
    with (root/'logs/comfy.stdout.log').open('ab') as stdout,(root/'logs/comfy.stderr.log').open('ab') as stderr:
        process=subprocess.Popen(args,cwd=core,env=environment(config),stdout=stdout,stderr=stderr,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    write(root/'comfy-owned-process.json',{'pid':process.pid,'args':args})
    print(json.dumps({'started_pid':process.pid,'readiness':'NOT_ASSUMED; inspect logs and runtime endpoint','origin':config['comfy_origin']}))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init')
    for name in ('comfy-root','model-root','runtime-root','comfy-python','qwen-python'):
        init.add_argument('--'+name,type=Path,required=True)
    for name in ('check','install-nodes','start-comfy','serve'):
        child=sub.add_parser(name);child.add_argument('--config',type=Path,required=True)
        if name=='check':child.add_argument('--hash-models',action='store_true')
    inspect=sub.add_parser('inspect-sources')
    inspect.add_argument('--comfy-root',type=Path,required=True)
    inspect.add_argument('--comfy-python',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='init':initialize(args);return 0
    if args.command=='inspect-sources':
        checks=inspect_sources({'comfy_root':str(args.comfy_root.resolve()),'comfy_python':str(args.comfy_python.resolve())})
        print(json.dumps({'source_checks':checks,'source_ready':all(x['ok'] for x in checks),
                          'model_or_generation_acceptance':False},ensure_ascii=False,indent=2))
        return 0 if all(x['ok'] for x in checks) else 2
    config=config_at(args.config)
    if args.command=='check':
        result=check(config,args.hash_models);print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['ready'] else 2
    if args.command=='install-nodes':install(config);return 0
    if args.command=='start-comfy':start_comfy(config);return 0
    from native_supervisor import serve
    return serve(config)

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,RuntimeError) as error:
        print(json.dumps({'error':str(error),'weights_downloaded':False},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
