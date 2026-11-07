"""Maintainer-only provenance probe. Reads small public metadata/source, never weights."""
from __future__ import annotations
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request

REV = '8010b48d2c1be986613f8e8c761527d205bd756f'

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    audit = Path(__file__).parent/'bundle/comfy_nodes/VideoFactorySamplingAudit/smd_audit.py'
    tree = ast.parse(audit.read_text(encoding='utf-8'))
    pins = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'CORE_PINS' for t in n.targets))
    def verify(item):
        relative, expected = item
        url = f'https://raw.githubusercontent.com/Comfy-Org/ComfyUI/{REV}/{relative}'
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read(4*1024*1024 + 1)
        if len(raw) > 4*1024*1024:
            raise ValueError('Source unexpectedly exceeds the source-only bound')
        actual = hashlib.sha256(raw).hexdigest()
        return {'file':relative,'url':url,'sha256':actual,'expected':expected,'match':actual == expected}
    with ThreadPoolExecutor(max_workers=5) as pool:
        files = list(pool.map(verify, pins.items()))
    cards = []
    for repo, revision in [('Comfy-Org/MiniMax-H3','e5eb578a89295337b8ff433a035929ce0279e0b6'),
                           ('zjwfufu/SMD-MiniMax-H3-Ref2VA-4step-ComfyUI','9544cef6ca00bcedb34a7139032634c4558ed4f8'),
                           ('Qwen/Qwen3-VL-4B-Instruct','main')]:
        url = f'https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true'
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.load(response)
        cards.append({'repo':repo,'revision':data.get('sha'),'url':url,'card_data':data.get('cardData'),
                      'files':data.get('siblings',[])})
    result = {'schema_version':1,'comfy_revision':REV,'all_core_pins_match':all(x['match'] for x in files),
              'core_files':files,'model_metadata':cards,'weights_downloaded':False}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'core_files':len(files),'all_core_pins_match':result['all_core_pins_match'],
                      'metadata_repositories':len(cards),'weights_downloaded':False}))
    return 0 if result['all_core_pins_match'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
