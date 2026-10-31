"""Generate shared docs, task prompts and JSON Schema from the authored bundle."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from app.services.cinematic_rules import rules_bundle
from app.schemas_cinematic import ScreenplayIR


def build(stamp=False):
    rules=rules_bundle();versions=rules['versions'];header='<!-- GENERATED from schemas/cinematic-production-rules.yaml; source-sha256: '+rules['source_sha256']+' -->\n'
    versiontext='\n'.join('- '+key+': `'+value+'`' for key,value in versions.items())
    standard=header+'# 电影级视觉制作规范\n\n本规范驱动 Word、兼容解析、中间数据、人物与制作设定、视觉与视频提示词、连续性预检及真实编排。电影质量须审核，文字规范不保证模型完全服从。\n\n'+versiontext+'\n\n'
    (ROOT/'prompts').mkdir(exist_ok=True)
    generated=[]
    for name,section in rules['sections'].items():
        body='## '+section['title']+'\n\n'+'\n\n'.join(section['instructions'])+'\n'
        standard+=body+'\n'
        p=ROOT/'prompts'/f'{name}.md';p.write_text(header+'# '+section['title']+'\n\n'+versiontext+'\n\n'+'\n\n'.join(section['instructions'])+'\n\n'+ '\n\n'.join(rules['visual_language'].values())+'\n',encoding='utf-8');generated.append(p)
    for kind,fields in rules['fields'].items():
        standard+='## '+kind+' 字段\n\n'+', '.join(rules['field_labels'].get(f,f)+' (`'+f+'`)' for f in fields)+'\n\n'
    standard+='## 来源与兼容\n\n原文、建议、继承、已审核、未知与冲突分别记录。没有版本标识的 Word 进入通用兼容模式，未确定的语义和主体保留待审核；没有经过验证的语义模型时，不宣称任意文档已被完全理解。\n\n历史人物、镜头与任务快照不会因当前规范变化自动重写。新生成冻结规则、模型、工作流和参考哈希。\n\n## 实际控制边界\n\n本项目 H3 工作流是参考生视频，最多9图，24fps和17n+5帧；宽高、时长、采样步数和种子有实际节点映射。关键画面和场景图属于参考，不能冒充精确首尾帧；摄影术语属于视觉意图。原生音频不等于精确口型或声音克隆。\n\n场景候选使用所选本地图像模型的实际文生图路径，Klein用独立ReferenceLatent接入人物与场景图；多图效果须以真实候选审核结果为准。检测仅认证实际解码/规格，身份、手部、动作与电影观感保留人工审核。\n\n## 同步构建\n\n使用 bundled Python 运行 scripts/build_screenplay_templates.py；检查用 .venv/Scripts/python.exe scripts/build_cinematic_contracts.py --check。修改规范后必须重新构建和测试，运行时的同步检查会报告源、模板、Schema或消费者不一致。\n'
    p=ROOT/'docs/cinematic-production-standard.md';p.write_text(standard,encoding='utf-8');generated.append(p)
    schema=ScreenplayIR.model_json_schema();schema.update({'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'urn:video-factory:'+versions['screenplay_schema_version'],'x-production-versions':versions,'x-rules-source-sha256':rules['source_sha256'],'x-production-field-definitions':rules['fields'],'x-default-policy':rules['defaults']})
    p=ROOT/'schemas/screenplay-ir.schema.json';p.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');generated.append(p)
    (ROOT/'schemas/cinematic-rules.snapshot.json').write_text(json.dumps(rules,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');generated.append(ROOT/'schemas/cinematic-rules.snapshot.json')
    if stamp:
        required=[ROOT/'docs/templates/剧本导入模板.docx',ROOT/'docs/templates/剧本导入示例.docx']
        from docx import Document
        for p in required:
            text='\n'.join(x.text for x in Document(p).paragraphs)
            if any(key+': '+value not in text for key,value in versions.items()):raise RuntimeError('Word version mismatch: '+str(p))
        consumers=['backend/app/schemas_cinematic.py','backend/app/services/cinematic_import.py','backend/app/services/cinematic_quality.py','backend/app/services/cinematic_rules.py','backend/app/services/screenplay.py','backend/app/services/word_screenplay.py','backend/app/services/cinematic_visuals.py','backend/app/services/production_orchestrator.py','backend/app/api/cinematic.py','backend/app/api/routes.py','frontend/src/pages/ProductionPage.tsx','frontend/src/components/ProductionShotEditor.tsx','frontend/src/components/ProductionDesignForm.tsx','frontend/src/components/ProductionVisualUpload.tsx','frontend/src/components/WordImportPanel.tsx','tests/backend/test_cinematic.py','scripts/build_screenplay_templates.py']
        consumers+=['backend/app/services/screenplay_generation.py','backend/app/services/screenplay_document.py','backend/app/services/screenplay_review_warnings.py','backend/app/services/screenplay_production_bindings.py']
        allfiles=generated+required+[ROOT/p for p in consumers]
        manifest={'source_sha256':rules['source_sha256'],'versions':versions,'artifacts':{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in allfiles}}
        (ROOT/'schemas/cinematic-generated-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'versions':versions,'generated':len(generated),'stamped':stamp}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--stamp',action='store_true');parser.add_argument('--check',action='store_true');args=parser.parse_args()
    if args.check:
        from app.services.cinematic_rules import check_generated_contract
        result=check_generated_contract();print(json.dumps(result,ensure_ascii=False));sys.exit(0 if result['status']=='passed' else 1)
    print(json.dumps(build(args.stamp),ensure_ascii=False))
