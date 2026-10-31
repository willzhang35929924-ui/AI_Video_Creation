"""Load the authored rule source and immutable bundle snapshots."""
import hashlib
import copy
import json
from pathlib import Path
import yaml
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError

RULE_PATH=PROJECT_ROOT/'schemas/cinematic-production-rules.yaml'


def rules_bundle():
    raw=RULE_PATH.read_bytes();rules=yaml.safe_load(raw)
    if not isinstance(rules,dict) or set(rules.get('versions',{}))!={'production_rules_version','screenplay_schema_version','template_version','parser_version','video_prompt_compiler_version'}:
        raise AppError(503,'rules_invalid','制作规范源无效')
    return {**rules,'source_sha256':hashlib.sha256(raw).hexdigest()}


def generation_rules(snapshot=None):
    """Migrate only a known generated age assumption for future requests.

    The original script snapshot stays intact. User-authored age descriptions
    and custom rules are preserved, as are all historical generation snapshots.
    """
    current=rules_bundle()
    result=copy.deepcopy(snapshot or current)
    legacy='A believable adult with the referenced facial geometry, natural asymmetry, pores and age cues. Preserve identity across costume and emotional changes. Do not beautify, remodel facial proportions, invent markings, or smooth all skin texture.'
    if result.get('visual_language',{}).get('identity')==legacy:
        result['visual_language']['identity']=current['visual_language']['identity']
        result['legacy_rule_migration']={'policy':'optional_character_age','original_source_sha256':result.get('source_sha256')}
        result['source_sha256']=current['source_sha256']
        result['versions']={**result['versions'],**{key:current['versions'][key] for key in ('production_rules_version','video_prompt_compiler_version')}}
    return result


def fingerprint(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode('utf-8')).hexdigest()


def field_provenance(value,sources,state='explicit',reason='原文明确字段'):
    return {'state':state,'source_ids':list(sources),'confidence':1.0 if state in ('explicit','reviewed') else .5 if state=='suggested' else 0,'reason':reason}


def versions_for_script(script):
    return script.parsed_json.get('versions',{}) if script else {}


def check_generated_contract():
    """Drift is a visible failure; changing YAML alone cannot look synchronized."""
    bundle=rules_bundle();manifest=PROJECT_ROOT/'schemas/cinematic-generated-manifest.json'
    if not manifest.exists():return {'status':'failed','reason':'规范派生文件尚未生成'}
    data=json.loads(manifest.read_text(encoding='utf-8'));issues=[]
    if data.get('source_sha256')!=bundle['source_sha256']:issues.append('机器规则已修改，模板/Schema/提示词需重新构建')
    for name,expected in data.get('artifacts',{}).items():
        path=PROJECT_ROOT/name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:issues.append(name+' 不同步或缺失')
    return {'status':'passed' if not issues else 'failed','versions':bundle['versions'],'source_sha256':bundle['source_sha256'],'issues':issues}
