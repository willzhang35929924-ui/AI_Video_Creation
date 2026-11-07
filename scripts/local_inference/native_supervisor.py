"""Real loopback Comfy proxy plus scoped Windows Qwen RAM handoff.

No synthetic model responses or generation. Every /prompt goes to the separately
installed ComfyUI exactly once. Receipts stay on the native node. Linux fails closed.
"""
from __future__ import annotations
import ctypes
import base64
import datetime as dt
import hashlib
import http.client
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError
import uuid

from videofactory_native import (HERE, MANIFEST, PROFILE, QWEN_ID, check, environment, load,
                                 queue_idle, request_json, sha, write)

SCOPE_RE=re.compile(r'\d{8}-\d{6}-\d{6}Z-[a-f0-9]{8}')

class SubmissionUnknown(RuntimeError):pass

class PromptRejected(ValueError):
    def __init__(self,payload):
        self.payload=payload
        super().__init__('ComfyUI rejected this graph before queue admission')

def private_ip(value):
    address=ipaddress.ip_address(value)
    if getattr(address,'ipv4_mapped',None):address=address.ipv4_mapped
    return address if address.is_loopback or (address.is_private and not address.is_link_local
                                             and not address.is_unspecified and not address.is_multicast) else None

def validate_listener(config):
    address=ipaddress.ip_address(config.get('listen','127.0.0.1'))
    if not address.is_unspecified and private_ip(str(address)) is None:
        raise ValueError('Native proxy may listen only on a private/loopback address, never a public/link-local address')
    clients=config.get('allowed_clients',[])
    if not isinstance(clients,list):raise ValueError('allowed_clients must list exact private client IP addresses')
    if not address.is_loopback and not clients:
        raise ValueError('Non-loopback listening requires explicit exact allowed_clients; no network scan or broad CIDR is accepted')
    for client in clients:
        if not isinstance(client,str) or '/' in client or private_ip(client) is None:
            raise ValueError('allowed_clients accepts exact private/loopback IPs only; no CIDR, public, multicast or link-local clients')

def client_permitted(config,client):
    try:
        address=private_ip(client)
        if address is None:return False
        configured=config.get('allowed_clients',[])
        if configured:return address in {private_ip(x) for x in configured}
        return address.is_loopback
    except ValueError:return False

def now():return dt.datetime.now(dt.timezone.utc).isoformat()

def windows_commit():
    if sys.platform!='win32':return None
    class Info(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong)]+[(name,ctypes.c_size_t) for name in (
            'CommitTotal','CommitLimit','CommitPeak','PhysicalTotal','PhysicalAvailable',
            'SystemCache','KernelTotal','KernelPaged','KernelNonpaged','PageSize')]+[
            ('HandleCount',ctypes.c_ulong),('ProcessCount',ctypes.c_ulong),('ThreadCount',ctypes.c_ulong)]
    value=Info();value.cb=ctypes.sizeof(value)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(value),value.cb):return None
    return {k:int(getattr(value,k)*value.PageSize) for k in ('CommitTotal','CommitLimit','CommitPeak')}

def exclusive(queue,prompt_id,graph):
    rows=queue.get('queue_running') if isinstance(queue,dict) else None
    return (queue.get('queue_pending')==[] and isinstance(rows,list) and len(rows)==1
            and isinstance(rows[0],list) and len(rows[0])>=3 and rows[0][1]==prompt_id and rows[0][2]==graph)

def idle_qwen(health, expected_sha):
    return (isinstance(health,dict) and health.get('service')=='video-factory-h3ir-qwen4b-v1'
            and health.get('profile')=='h3ir-qwen4b-32k' and health.get('model_id')==QWEN_ID
            and health.get('script_sha256')==expected_sha and health.get('offline') is True
            and health.get('busy') is False and health.get('loaded') is False
            and health.get('inference_phase')=='idle' and (
                health.get('gpu_release_status')=='not_used' or (
                    health.get('gpu_release_status')=='confirmed' and
                    health.get('last_gpu_cleanup',{}).get('released') is True)))

def safe_evidence(config, raw):
    path=Path(raw)
    root=Path(config['output_root']).resolve()
    if not path.is_absolute() or '..' in path.parts or path.suffix!='.json':
        raise ValueError('Evidence must identify a bounded native JSON output')
    resolved=path.resolve()
    if not resolved.is_relative_to(root) or resolved==root:
        raise ValueError('Evidence is outside this runtime output root')
    if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in (path,*path.parents)):
        raise ValueError('Evidence links/junctions are forbidden')
    relative=resolved.relative_to(root)
    if relative.parts[0] not in ('h3_local_backend_profile_v3','h3_smd_sampling_audit','h3_local_vlm_guard',
                                  'h3_sampling_baseline_observer'):
        raise ValueError('Evidence namespace not permitted')
    if not resolved.is_file() or not 0<resolved.stat().st_size<=4*1024*1024:
        raise ValueError('Evidence missing or exceeds 4 MiB')
    return resolved

class QwenOwner:
    def __init__(self,config):
        self.config=config;self.root=Path(config['runtime_root']);self.process=None
        base=(HERE/'bundle/serve_screenplay_qwen.py').read_bytes()
        wrapper=(HERE/'bundle/serve_h3ir_qwen4b.py').read_bytes()
        self.expected_sha=hashlib.sha256(b'video-factory-qwen-profile-v1\0'+base+b'\0'+wrapper).hexdigest()
        self.lock=threading.RLock()

    def health(self):
        return request_json('http://127.0.0.1:8772','/health')

    def owned_process(self,health):
        import psutil
        record=load(self.root/'qwen-owned-process.json')
        if (record.get('script_sha256')!=self.expected_sha or record.get('entry')!=str(HERE/'bundle/serve_h3ir_qwen4b.py')
                or record.get('config_sha256')!=hashlib.sha256(json.dumps(self.config,sort_keys=True).encode()).hexdigest()):
            raise ValueError('Qwen ownership configuration changed; no process stopped')
        parent=psutil.Process(record['pid'])
        if parent.create_time()!=record['created'] or Path(parent.exe()).resolve()!=Path(record['executable']).resolve():
            raise ValueError('Qwen original process identity changed')
        candidate=psutil.Process(health['pid'])
        if candidate.pid!=parent.pid and candidate.pid not in {p.pid for p in parent.children(recursive=True)}:
            raise ValueError('Qwen listener is not the owned process or its venv child')
        command=candidate.cmdline()
        if len(command)!=4 or command[1:3]!=['-B','-u'] or Path(command[3]).resolve()!=HERE/'bundle/serve_h3ir_qwen4b.py':
            raise ValueError('Qwen listener command does not match the managed profile')
        listeners=[c for c in candidate.net_connections(kind='inet') if c.status=='LISTEN' and c.laddr.port==8772]
        if len(listeners)!=1 or listeners[0].laddr.ip!='127.0.0.1':
            raise ValueError('Qwen must have exactly one verified loopback listener')
        return parent,candidate

    def start(self):
        import psutil
        with self.lock:
            try:
                health=self.health()
            except Exception:health=None
            if health is not None:
                if not idle_qwen(health,self.expected_sha):raise ValueError('Existing Qwen is busy, changed, or not this bundle')
                self.owned_process(health)
                return health
            with socket.socket() as listener:
                if listener.connect_ex(('127.0.0.1',8772))==0:
                    raise ValueError('Port 8772 is occupied without verified health; no duplicate started')
            args=[self.config['qwen_python'],'-B','-u',str(HERE/'bundle/serve_h3ir_qwen4b.py')]
            stamp=uuid.uuid4().hex
            with (self.root/f'logs/qwen-{stamp}.stdout.log').open('xb') as stdout,(self.root/f'logs/qwen-{stamp}.stderr.log').open('xb') as stderr:
                self.process=subprocess.Popen(args,cwd=self.root,env=environment(self.config),stdout=stdout,stderr=stderr,
                                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            parent=psutil.Process(self.process.pid)
            write(self.root/'qwen-owned-process.json',{'pid':parent.pid,'created':parent.create_time(),
                'executable':parent.exe(),'entry':args[-1],'script_sha256':self.expected_sha,
                'config_sha256':hashlib.sha256(json.dumps(self.config,sort_keys=True).encode()).hexdigest()})
            deadline=time.monotonic()+105
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise ValueError('Managed Qwen exited; inspect native runtime logs')
                try:
                    health=self.health()
                    if idle_qwen(health,self.expected_sha):
                        self.owned_process(health)
                        return health
                except Exception:pass
                time.sleep(1)
            raise ValueError('Qwen did not reach verified idle health; process retained for diagnosis')

    def stop_idle(self):
        with self.lock:
            first=self.health()
            if not idle_qwen(first,self.expected_sha):raise ValueError('Qwen is not verified idle/released; no process stopped')
            parent,listener=self.owned_process(first)
            second=self.health()
            if not idle_qwen(second,self.expected_sha) or second['pid']!=first['pid']:
                raise ValueError('Qwen changed during stop verification')
            listener.terminate();listener.wait(timeout=10)
            if parent.pid!=listener.pid:
                try:parent.wait(timeout=10)
                except Exception:raise ValueError('Qwen venv parent did not exit; no unrelated process was stopped')
            return {'status':'VERIFIED_IDLE_PROCESS_STOPPED','pid':first['pid'],'at':now()}

class Runtime:
    def __init__(self,config,installation):
        self.config=config;self.installation=installation;self.qwen=QwenOwner(config)
        self.active={};self.admit=threading.Lock();self.stop=threading.Event();self.heartbeat=0
        self.monitor=threading.Thread(target=self.monitor_loop,daemon=True)

    def comfy(self,route,body=None,timeout=5):
        return request_json(self.config['comfy_origin'],route,body,timeout)

    def monitor_loop(self):
        while not self.stop.is_set():
            self.heartbeat=time.monotonic();self.stop.wait(.5)

    def status(self):
        checks=[];health=None;queue=None
        try:
            queue=self.comfy('/queue');queue_idle(queue)
            checks.append({'kind':'comfy_queue','ok':True})
            nodes=self.comfy('/object_info',timeout=15)
            required=set(load(MANIFEST)['required_nodes'])
            checks.append({'kind':'registered_nodes','ok':required.issubset(nodes),'missing':sorted(required-set(nodes))})
            stats=self.comfy('/system_stats')
            checks.append({'kind':'nvidia_cuda','ok':any(d.get('type')=='cuda' for d in stats.get('devices',[]))})
            argv=stats.get('system',{}).get('argv',[])
            flags=('--disable-async-offload','--disable-pinned-memory','--disable-comfy-compiler','--cache-none')
            def configured_path(flag,expected):
                return (flag in argv and argv.index(flag)+1<len(argv)
                        and Path(argv[argv.index(flag)+1]).resolve()==Path(expected).resolve())
            startup_ok=(all(f in argv for f in flags)
                        and configured_path('--input-directory',self.config['input_root'])
                        and configured_path('--output-directory',self.config['output_root']))
            checks.append({'kind':'comfy_runtime_paths_and_memory_flags','ok':startup_ok})
        except Exception as error:checks.append({'kind':'comfy','ok':False,'error':str(error)})
        try:
            health=self.qwen.health()
            checks.append({'kind':'qwen','ok':idle_qwen(health,self.qwen.expected_sha)})
            self.qwen.owned_process(health)
            checks.append({'kind':'qwen_owned_process','ok':True})
        except Exception as error:checks.append({'kind':'qwen_ownership','ok':False,'error':str(error)})
        watcher=self.monitor.is_alive() and time.monotonic()-self.heartbeat<3
        checks.append({'kind':'supervisor','ok':watcher})
        commit=windows_commit()
        checks.append({'kind':'windows_commit','ok':bool(commit and commit['CommitLimit']>0
                       and 0<=commit['CommitTotal']<=commit['CommitLimit']
                       and commit['CommitLimit']-commit['CommitTotal']>=2*1024**3),'metrics':commit})
        known=all(c['ok'] for c in self.installation['checks'])
        owned=any(c['kind']=='qwen_owned_process' and c['ok'] for c in checks)
        watched=any(item.get('thread') is not None and item['thread'].is_alive()
                    for item in list(self.active.values()))
        memory='runtime_owned' if sys.platform=='win32' and known and watcher and (owned or watched) else 'unverified'
        idle=queue is not None and queue_idle(queue) and not self.active
        return {'schema_version':1,'profile_id':PROFILE,'service':'video-factory-native-context-runtime-v1',
                'ready':known and all(c['ok'] for c in checks) and idle,'busy':not idle,
                'memory_policy':memory,'watcher_running':watcher,'qwen_health':health,
                'source_baseline':{'ok':known,'comfy_revision':load(MANIFEST)['comfy_revision'],
                                   'manifest_sha256':sha(MANIFEST)},
                'comfyui':{'origin':self.config['comfy_origin'],
                           'queue_running':len(queue['queue_running']) if queue else None,
                           'queue_pending':len(queue['queue_pending']) if queue else None},
                'active_prompts':list(self.active),'checks':checks,
                'installation_check':self.installation,'generation_tested':False,
                'startup_diagnostic':getattr(self,'startup_diagnostic',None)}

    def submit(self,body):
        if not self.admit.acquire(blocking=False):raise ValueError('Native submission already in progress')
        try:
            status=self.status()
            if not status['ready']:raise ValueError('Native runtime is not ready; inspect /video_factory/runtime')
            graph=body.get('prompt');client=body.get('client_id')
            if not isinstance(graph,dict) or not re.fullmatch(r'job_[a-f0-9]{32}',str(client)):
                raise ValueError('Native Context submissions require the original immutable job client_id')
            if graph.get('4',{}).get('class_type')!='VideoFactoryLocalH3IRCompile':
                raise ValueError('This proxy accepts the audited Context-IR profile only')
            if not all(node.get('class_type') in load(MANIFEST)['allowed_graph_nodes'] for node in graph.values()):
                raise ValueError('Graph contains a node outside the reviewed Context-IR native profile')
            case=Path(self.config['runtime_root'])/'jobs'/client
            if case.exists():raise ValueError('Job submission already recorded; poll it, never blindly re-submit')
            case.mkdir(parents=True)
            scope_root=Path(self.config['output_root'])/'h3_local_backend_profile_v3'
            previous={p.name for p in scope_root.iterdir()} if scope_root.is_dir() else set()
            write(case/'submitted_prompt.json',body)
            write(case/'scopes-before.json',sorted(previous))
            write(case/'submission-intent.json',{'at':now(),'retry_allowed':False})
            # Exactly one upstream POST. Unknown outcome is intentionally not retried.
            try:accepted=self.comfy('/prompt',body,timeout=120)
            except HTTPError as error:
                rejected=None
                try:
                    raw=error.read(4*1024*1024+1)
                    payload=json.loads(raw) if len(raw)<=4*1024*1024 else None
                    known={'invalid_prompt_id','no_prompt','missing_node_type','prompt_no_outputs','prompt_outputs_failed_validation'}
                    if (error.code==400 and isinstance(payload,dict) and isinstance(payload.get('node_errors'),dict)
                            and isinstance(payload.get('error'),dict) and payload['error'].get('type') in known
                            and isinstance(payload['error'].get('message'),str)):
                        queue=self.comfy('/queue');queue_idle(queue)
                        rows=queue['queue_running']+queue['queue_pending']
                        if all(isinstance(row,list) and len(row)>=4 and isinstance(row[3],dict) for row in rows):
                            if not any(row[3].get('client_id')==client or row[2]==graph for row in rows):
                                rejected={'response':payload,'queue_after_rejection':queue,
                                          'response_sha256':hashlib.sha256(raw).hexdigest()}
                except Exception:pass
                if rejected is not None:
                    write(case/'submission-rejected.json',rejected)
                    write(case/'supervisor-result.json',{'terminal_verified':True,'rejected_before_queue':True,
                                                         'prompt_id':None,'retry_allowed':False})
                    raise PromptRejected(rejected['response']) from error
                self.unknown(case,client,error)
            except Exception as error:self.unknown(case,client,error)
            write(case/'submission_response.json',accepted)
            prompt_id=accepted.get('prompt_id') if isinstance(accepted,dict) else None
            try:valid=isinstance(prompt_id,str) and str(uuid.UUID(prompt_id))==prompt_id
            except ValueError:valid=False
            if not valid:self.unknown(case,client,ValueError('Upstream did not return a valid prompt ID'))
            self.active[prompt_id]={'case':str(case),'unresolved':False}
            thread=threading.Thread(target=self.watch,args=(case,prompt_id,graph,client,previous),daemon=True)
            self.active[prompt_id]['thread']=thread;thread.start()
            return accepted
        finally:self.admit.release()

    def unknown(self,case,client,error):
        write(case/'submission-unknown.json',{'error':str(error),'retry_allowed':False})
        self.active['unknown:'+client]={'case':str(case),'unresolved':True}
        raise SubmissionUnknown('Upstream submission outcome unknown; inspect native queue/history before retry') from error

    def matching_scope(self,case,graph,previous):
        root=Path(self.config['output_root'])/'h3_local_backend_profile_v3'
        if not root.is_dir():return None
        candidates=[p for p in root.iterdir() if p.name not in previous and SCOPE_RE.fullmatch(p.name) and not p.is_symlink()]
        if len(candidates)!=1:return None
        scope=candidates[0];compiler=scope/'compiler-001'
        names=[compiler/'source-gate.json',compiler/'reference-binding.final.json',scope/'reference-contract.json',
               compiler/'brief.json',scope/'profile.json']
        if not all(p.is_file() for p in names):return None
        source,binding,declarations,brief,profile=map(load,names)
        body_hash=source.get('body_sha256')
        if (source.get('status')!='PASS' or source.get('source')!='written' or source.get('fallback_reason')
                or not isinstance(body_hash,str) or not re.fullmatch('[a-f0-9]{64}',body_hash)
                or binding.get('status')!='PASS' or binding.get('body_sha256')!=body_hash):return None
        inputs=graph['4']['inputs'];expected={k:inputs[k] for k in ('reference_groups','required_references','motion_targets')}
        if declarations.get('raw_declarations')!=expected or profile.get('seed')!=inputs['seed']:return None
        tray=json.loads(graph['2']['inputs']['tray']);assets=[]
        input_root=Path(self.config['input_root']).resolve();job_root=input_root/'video_factory/context_ir'/case.name
        for item in tray:
            raw=item['file'];relative,marker,tail=raw.rpartition(' [input]')
            if not marker or tail or Path(relative).is_absolute() or '..' in Path(relative).parts:return None
            path=(input_root/relative).resolve()
            if not path.is_relative_to(job_root):return None
            assets.append((item['role'],path))
        if [(x['role'],Path(x['path']).resolve()) for x in brief['assets']]!=assets:return None
        receipts=sorted(scope.glob('request-*/receipt.json'))
        if not receipts or load(receipts[-1]).get('factory_receipt',{}).get('gpu_cleanup',{}).get('released') is not True:return None
        return scope

    def watch(self,case,prompt_id,graph,client,previous):
        released=False;terminal=False;interrupted=False;deadline=time.monotonic()+7200
        try:
            while time.monotonic()<deadline:
                try:
                    history=self.comfy('/history/'+prompt_id).get(prompt_id)
                    if history and (history.get('status',{}).get('completed') is True or
                                    history.get('status',{}).get('status_str') in ('error','failed','cancelled','interrupted')):
                        recorded=history.get('prompt',[])
                        if len(recorded)<4 or recorded[1]!=prompt_id or recorded[2]!=graph or recorded[3].get('client_id')!=client:
                            raise ValueError('Terminal history differs from this exact accepted prompt/graph/client')
                        write(case/'terminal-history.json',history);terminal=True;break
                    queue=self.comfy('/queue')
                    commit=windows_commit()
                    with (case/'memory.jsonl').open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps({'at':now(),'commit':commit})+'\n')
                    if (commit and 0<=commit['CommitLimit']-commit['CommitTotal']<2*1024**3 and not interrupted
                            and exclusive(queue,prompt_id,graph) and exclusive(self.comfy('/queue'),prompt_id,graph)):
                        interrupted=True
                        write(case/'interrupt-intent.json',{'at':now(),'prompt_id':prompt_id,'retry_allowed':False})
                        try:write(case/'interrupt-response.json',self.comfy('/interrupt',{'prompt_id':prompt_id}))
                        except Exception as error:write(case/'interrupt-unknown.json',{'error':str(error),'retry_allowed':False})
                    if not released and not interrupted and exclusive(queue,prompt_id,graph):
                        scope=self.matching_scope(case,graph,previous)
                        if scope and exclusive(self.comfy('/queue'),prompt_id,graph):
                            # Record intent before the one stop attempt. Never retry an unknown stop.
                            released=True
                            write(case/'qwen-release-intent.json',{'at':now(),'scope':str(scope),'source_gate_sha256':sha(scope/'compiler-001/source-gate.json')})
                            result=self.qwen.stop_idle();write(case/'qwen-release.json',result)
                except Exception as error:
                    with (case/'watch-errors.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps({'at':now(),'error':str(error)})+'\n')
                time.sleep(1)
        finally:
            restored=False
            if released and terminal:
                try:self.qwen.start();restored=True
                except Exception as error:write(case/'qwen-restore-error.json',{'error':str(error)})
            write(case/'supervisor-result.json',{'prompt_id':prompt_id,'terminal_verified':terminal,
                                                'qwen_stop_attempted':released,'qwen_restored':restored})
            if terminal:self.active.pop(prompt_id,None)
            else:self.active[prompt_id]['unresolved']=True

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,fmt,*args):
        print(json.dumps({'at':now(),'client_ip':self.client_address[0],'method':self.command,
                          'path':urlsplit(self.path).path,'status':args[1] if len(args)>1 else ''}),flush=True)

    def json_response(self,status,body):
        raw=json.dumps(body,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)

    def handle_route(self):
        runtime=self.server.runtime;route=urlsplit(self.path)
        try:
            if self.headers.get('Origin') is not None or not client_permitted(runtime.config,self.client_address[0]):
                self.close_connection=True
                return self.json_response(403,{'error':'Client IP or browser Origin is not permitted by this native runtime'})
            if self.command=='GET' and route.path=='/video_factory/runtime':
                return self.json_response(200,runtime.status())
            if self.command=='GET' and route.path=='/video_factory/evidence':
                values=parse_qs(route.query)
                if set(values)!={'path'} or len(values['path'])!=1:raise ValueError('One native evidence path is required')
                path=safe_evidence(runtime.config,values['path'][0]);before=path.stat()
                with path.open('rb') as stream:raw=stream.read(4*1024*1024+1)
                after=path.stat()
                if (len(raw)>4*1024*1024 or len(raw)!=before.st_size
                        or (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino)):
                    raise ValueError('Evidence changed while reading or exceeded its byte bound')
                return self.json_response(200,{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),
                                               'bytes':len(raw),'raw_base64':base64.b64encode(raw).decode('ascii'),
                                               'content':json.loads(raw)})
            if self.command=='POST' and route.path=='/prompt':
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=10*1024*1024:raise ValueError('Bounded JSON prompt length is required')
                return self.json_response(200,runtime.submit(json.loads(self.rfile.read(length))))
            allowed_get=route.path in ('/queue','/system_stats','/object_info','/view','/history') or route.path.startswith(('/history/','/object_info/'))
            allowed_post=route.path=='/upload/image'
            if (self.command=='GET' and allowed_get) or (self.command=='POST' and allowed_post):
                return self.proxy()
            return self.json_response(404,{'error':'Route not exposed by the native Context-IR proxy'})
        except PromptRejected as error:return self.json_response(400,error.payload)
        except SubmissionUnknown as error:return self.json_response(502,{'error':str(error),'retry_allowed':False})
        except Exception as error:return self.json_response(409,{'error':str(error),'retry_allowed':False})

    def proxy(self):
        # Stream file uploads/downloads; do not buffer a video in proxy RAM.
        target=urlsplit(self.server.runtime.config['comfy_origin'])
        connection=http.client.HTTPConnection(target.hostname,target.port,timeout=120)
        try:
            connection.putrequest(self.command,self.path)
            length=int(self.headers.get('Content-Length','0'))
            if length<0 or length>4*1024**3:raise ValueError('Upload length exceeds 4 GiB')
            for key in ('Content-Type','Range','If-None-Match','If-Modified-Since'):
                if self.headers.get(key):connection.putheader(key,self.headers[key])
            if self.command=='POST':connection.putheader('Content-Length',str(length))
            connection.endheaders()
            remaining=length
            while remaining:
                block=self.rfile.read(min(1024*1024,remaining))
                if not block:raise ValueError('Truncated upload')
                connection.send(block);remaining-=len(block)
            response=connection.getresponse()
            if 300<=response.status<400:raise ValueError('Upstream redirects are not followed')
            self.send_response(response.status)
            for key in ('Content-Type','Content-Length','Content-Range','Accept-Ranges','ETag','Last-Modified'):
                value=response.getheader(key)
                if value:self.send_header(key,value)
            if not response.getheader('Content-Length'):self.send_header('Connection','close');self.close_connection=True
            self.end_headers()
            while True:
                block=response.read(1024*1024)
                if not block:break
                self.wfile.write(block)
        finally:connection.close()

    do_GET=handle_route
    do_POST=handle_route

def serve(config):
    validate_listener(config)
    installation=check(config)
    if not installation['ready']:raise ValueError('Native installation checks failed; no models or services started')
    # Do not adopt a live queue whose submission/ownership was not recorded here.
    if not queue_idle(request_json(config['comfy_origin'],'/queue')):
        raise ValueError('Comfy is busy; native supervisor will not adopt or re-submit existing work')
    root=Path(config['runtime_root'])
    for case in (root/'jobs').glob('*'):
        if (case/'submission-intent.json').exists() and not (case/'supervisor-result.json').exists():
            raise ValueError('An earlier native submission is unresolved; inspect its saved queue/history before restarting supervisor')
        result=load(case/'supervisor-result.json') if (case/'supervisor-result.json').exists() else {}
        if result and result.get('terminal_verified') is not True:
            raise ValueError('Earlier native prompt lacks verified terminal history; inspect it manually, never re-submit blindly')
    runtime=Runtime(config,installation)
    try:runtime.qwen.start()
    except Exception as error:
        # Keep the read-only diagnostic endpoint reachable. A foreign or busy
        # Qwen is never adopted/killed; status remains not-ready and /prompt blocks.
        runtime.startup_diagnostic={'code':'qwen_start_or_ownership_blocked','error':str(error),
                                    'action':'Use the original owner to stop its idle service, then restart this proxy'}
    runtime.monitor.start()
    server=ThreadingHTTPServer((config.get('listen','127.0.0.1'),int(config.get('port',8788))),Handler)
    server.runtime=runtime
    print(json.dumps({'service':'video-factory-native-context-runtime-v1','listen':config.get('listen'),
                      'port':config.get('port',8788),'weights_downloaded':False}),flush=True)
    try:server.serve_forever(poll_interval=.5)
    finally:
        runtime.stop.set();server.server_close()
        # Closing the proxy does not kill model services or erase job evidence.
    return 0
