"""Maintainer-only source verification against public GitHub/PyPI; no model files."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
from pathlib import Path
import urllib.request
import zipfile

def main():
    root=Path(__file__).resolve().parents[2]
    manifest=json.loads((root/'runtime-manifests/context-ir-native-v1.json').read_text(encoding='utf-8'))
    def read(url,limit):
        with urllib.request.urlopen(url,timeout=30) as response:raw=response.read(limit+1)
        if len(raw)>limit:raise ValueError('Source-only size bound exceeded')
        return raw
    def verify(item):
        relative,expected=item
        url=f"https://raw.githubusercontent.com/ruashots/ComfyUI-OpenH3-IR/{manifest['openh3_revision']}/{relative}"
        actual=hashlib.sha256(read(url,1024*1024)).hexdigest()
        return {'file':relative,'source':url,'sha256':actual,'expected':expected,'match':actual==expected}
    with ThreadPoolExecutor(max_workers=5) as pool:files=list(pool.map(verify,manifest['openh3_node_pins'].items()))
    server_url=f"https://raw.githubusercontent.com/Comfy-Org/ComfyUI/{manifest['comfy_revision']}/server.py"
    server_sha=hashlib.sha256(read(server_url,1024*1024)).hexdigest()
    metadata=json.loads(read('https://pypi.org/pypi/open-h3-ir/0.4.1/json',2*1024*1024))
    wheel=next(x for x in metadata['urls'] if x['filename'].endswith('.whl'))
    raw=read(wheel['url'],4*1024*1024)
    if hashlib.sha256(raw).hexdigest()!=wheel['digests']['sha256']:raise ValueError('PyPI wheel hash mismatch')
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        actual={name.removeprefix('h3ir/'):hashlib.sha256(archive.read(name)).hexdigest()
                for name in archive.namelist() if name.startswith('h3ir/') and name.endswith('.py')}
    result={'schema_version':1,'openh3_node_files':files,'openh3_all_match':all(x['match'] for x in files),
            'compiler_wheel':{'url':wheel['url'],'sha256':wheel['digests']['sha256']},
            'scoped_interrupt_server':{'source':server_url,'sha256':server_sha,
                                       'match':server_sha==manifest['core_pins']['server.py']},
            'compiler_all_sources_match':actual==manifest['compiler_source_pins'],
            'compiler_changed_files':sorted(k for k in set(actual)|set(manifest['compiler_source_pins'])
                                             if actual.get(k)!=manifest['compiler_source_pins'].get(k)),
            'weights_downloaded':False}
    (root/'runtime-manifests/upstream-compiler-verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('openh3_node_files','compiler_wheel')}))
    return 0 if result['openh3_all_match'] and result['compiler_all_sources_match'] and result['scoped_interrupt_server']['match'] else 2

if __name__=='__main__':raise SystemExit(main())
