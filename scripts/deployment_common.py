"""Resumable verified acquisition from official HF models and PyPI wheels.

Only deployment plans choose destinations. Existing files are never overwritten;
progress is actual bytes written, not elapsed-time estimates.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit
PARALLEL_RANGE_BYTES=64*1024*1024
SOURCE_HOSTS={'huggingface.co','files.pythonhosted.org'}


def file_digest(path, algorithm='sha256'):
    p=Path(path);digest=hashlib.new('sha1' if algorithm=='git-sha1' else algorithm)
    if algorithm=='git-sha1':digest.update(f'blob {p.stat().st_size}\0'.encode())
    with p.open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def save_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp.json');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8');temporary.replace(path)


def verified(spec,path):
    p=Path(path)
    return p.is_file() and p.stat().st_size==spec['expected_bytes'] and file_digest(p,spec['hash_algorithm'])==spec['expected_hash']


def download_component(spec,progress=None,max_attempts=5,workers=1):
    if workers not in (1,2,4):raise ValueError('Only 1, 2 or 4 bounded transfer workers are supported')
    partial=Path(spec['partial']);partial.parent.mkdir(parents=True,exist_ok=True)
    with (partial.with_suffix(partial.suffix+'.lock')).open('a+b') as lock:
        lock.seek(0,2)
        if not lock.tell():lock.write(b'0');lock.flush()
        lock.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RuntimeError('Another process already owns this component download') from None
        try:
            if workers>1 or partial.with_name(partial.name+'.ranges.json').exists():
                return _download_parallel_unlocked(spec,progress,max_attempts,workers)
            return _download_component_unlocked(spec,progress,max_attempts)
        finally:
            lock.seek(0)
            if os.name=='nt':msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def _download_component_unlocked(spec,progress=None,max_attempts=5):
    url=spec['url'];parts=urlsplit(url)
    if parts.scheme!='https' or parts.hostname not in SOURCE_HOSTS or parts.username or parts.password:
        raise ValueError('Only pinned official Hugging Face/PyPI source URLs are accepted')
    target=Path(spec['target']);partial=Path(spec['partial'])
    if target.resolve()==partial.resolve():raise ValueError('Partial and published paths must be different')
    if target.exists():
        if not verified(spec,target):raise ValueError('Existing destination differs; preserved without overwrite')
        if progress:progress('verified_existing',target.stat().st_size)
        return target
    target.parent.mkdir(parents=True,exist_ok=True);partial.parent.mkdir(parents=True,exist_ok=True)
    expected=spec['expected_bytes']
    for attempt in range(max_attempts):
        written=partial.stat().st_size if partial.exists() else 0
        if written>expected:raise ValueError('Partial exceeds declared size; preserved for inspection')
        if written==expected:break
        request=urllib.request.Request(url,headers={'Range':f'bytes={written}-','Accept-Encoding':'identity','Cache-Control':'no-cache'})
        try:
            with urllib.request.urlopen(request,timeout=90) as response:
                final=urlsplit(response.geturl())
                if final.scheme!='https':raise ValueError('Insecure model redirect rejected')
                if written and (response.status!=206 or not response.headers.get('Content-Range','').startswith(f'bytes {written}-')):
                    raise ValueError('Server ignored resume range; existing partial preserved')
                if response.status not in (200,206):raise RuntimeError('Unexpected HTTP status')
                last_report=written
                with partial.open('ab' if written else 'wb') as stream:
                    while True:
                        chunk=response.read(8*1024*1024)
                        if not chunk:break
                        if written+len(chunk)>expected:raise ValueError('Response exceeds exact declared component size')
                        stream.write(chunk);written+=len(chunk)
                        if written-last_report>=64*1024*1024:
                            stream.flush()
                            if progress:progress('downloading',written)
                            last_report=written
                    stream.flush();os.fsync(stream.fileno())
            if written==expected:break
        except ValueError:raise
        except Exception as exc:
            if progress:progress('retry_'+type(exc).__name__,partial.stat().st_size if partial.exists() else 0)
            if attempt==max_attempts-1:raise RuntimeError('Component download failed after bounded retries; partial retained') from None
        time.sleep(min(2**attempt,16))
    if not verified(spec,partial):raise ValueError('Complete size/digest verification failed; source partial preserved')
    os.link(partial,target)  # Exclusive publication on the same managed E volume.
    partial.unlink()
    if progress:progress('verified',target.stat().st_size)
    return target


def _download_parallel_unlocked(spec,progress,max_attempts,workers):
    """Disjoint exact ranges; one lock owns prefix, parts and atomic publication.

    Counts are bytes actually present, never elapsed-time progress. Parts survive
    interruption and are merged only after all ranges have their exact lengths.
    """
    from concurrent.futures import ThreadPoolExecutor
    import threading
    parts_url=urlsplit(spec['url'])
    if parts_url.scheme!='https' or parts_url.hostname not in SOURCE_HOSTS or parts_url.username or parts_url.password:
        raise ValueError('Only pinned official Hugging Face/PyPI URLs are supported')
    target,partial=Path(spec['target']),Path(spec['partial'])
    if target.resolve()==partial.resolve():raise ValueError('Partial and target must differ')
    if target.exists():
        if not verified(spec,target):raise ValueError('Existing target differs; preserved')
        if progress:progress('verified_existing',target.stat().st_size)
        return target
    expected=spec['expected_bytes'];current_prefix=partial.stat().st_size if partial.exists() else 0
    if current_prefix>expected:raise ValueError('Existing prefix exceeds declared model size')
    target.parent.mkdir(parents=True,exist_ok=True)
    range_plan=partial.with_name(partial.name+'.ranges.json')
    identity={key:spec[key] for key in ('url','expected_bytes','expected_hash','hash_algorithm','target')}
    if range_plan.exists():
        saved=json.loads(range_plan.read_text('utf-8'))
        if saved.get('identity')!=identity:raise ValueError('Existing range plan belongs to different source/target')
        prefix=saved['initial_prefix'];chunk_size=saved['range_bytes']
        if not 0<=prefix<=current_prefix:raise ValueError('Model prefix shrank since range planning; preserve and inspect')
    else:
        prefix=current_prefix;chunk_size=min(PARALLEL_RANGE_BYTES,16*1024*1024 if expected<512*1024*1024 else PARALLEL_RANGE_BYTES)
        save_json(range_plan,{'identity':identity,'initial_prefix':prefix,'range_bytes':chunk_size})
    ranges=[]
    for start in range(prefix,expected,chunk_size):
        end=min(expected,start+chunk_size)-1
        part=partial.with_name(partial.name+f'.range-{start}-{end}.part')
        if part.exists() and part.stat().st_size>end-start+1:raise ValueError('Existing range part exceeds exact range')
        ranges.append((start,end,part))
    callback_lock=threading.Lock()
    def emit(stage):
        if progress:
            with callback_lock:
                progress(stage,prefix+sum(p.stat().st_size for _,_,p in ranges if p.exists()))
    def transfer(item):
        start,end,part=item;size=end-start+1
        if end<current_prefix:return  # Already merged before a prior interruption.
        for attempt in range(max_attempts):
            present=part.stat().st_size if part.exists() else 0
            if present==size:return
            offset=start+present
            request=urllib.request.Request(spec['url'],headers={'Range':f'bytes={offset}-{end}',
                'Accept-Encoding':'identity','Cache-Control':'no-cache'})
            try:
                with urllib.request.urlopen(request,timeout=60) as response:
                    if urlsplit(response.geturl()).scheme!='https' or response.status!=206 or response.headers.get('Content-Range')!=f'bytes {offset}-{end}/{expected}':
                        raise ValueError('Server did not honor exact disjoint range; existing parts preserved')
                    last=present
                    with part.open('ab' if present else 'wb') as stream:
                        while True:
                            block=response.read(min(1024*1024,size-present+1))
                            if not block:break
                            if present+len(block)>size:raise ValueError('Exact range overflow')
                            stream.write(block);present+=len(block)
                            if present-last>=8*1024*1024:
                                stream.flush();emit('downloading_parallel');last=present
                        stream.flush();os.fsync(stream.fileno())
                if present==size:emit('range_complete');return
                raise OSError('Range connection ended before declared byte count')
            except ValueError:raise
            except Exception as exc:
                emit('range_retry_'+type(exc).__name__)
                if attempt==max_attempts-1:raise RuntimeError('Bounded range retries failed; all partials preserved') from None
                time.sleep(min(2**attempt,16))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(transfer,ranges):pass
    emit('ranges_complete_merging')
    # Resume a crash during merging by comparing the current prefix offset to
    # each fixed disjoint part; never append the same already merged bytes twice.
    import shutil
    with partial.open('ab' if partial.exists() else 'wb') as stream:
        for start,end,part in ranges:
            current=stream.tell()
            if current>end:continue
            if not start<=current<=end:raise ValueError('Unexpected gap in exact-range merge')
            with part.open('rb') as source:
                source.seek(current-start);shutil.copyfileobj(source,stream,1024*1024)
        stream.flush();os.fsync(stream.fileno())
    if not verified(spec,partial):raise ValueError('Merged model failed full upstream hash; all data preserved')
    os.link(partial,target)
    partial.unlink()
    # Only exact files created for this managed transfer are removed after hash.
    for _,_,part in ranges:
        if part.exists():part.unlink()
    range_plan.unlink()
    if progress:progress('verified',target.stat().st_size)
    return target
