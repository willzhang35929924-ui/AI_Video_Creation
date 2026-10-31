"""Deterministic, source-preserving DOCX import. No external AI or invented facts."""
import hashlib
import json
import re
from difflib import SequenceMatcher
from io import BytesIO
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from sqlalchemy import select, func
from app.core.errors import AppError
from app.services.imports import parse_uploaded_document
from app.models import (Character, CharacterVersion, CharacterAlias, WordImport, Project, ScriptRevision,
    SourceDocument, Scene, StoryboardShot, ShotDetail, ShotCharacter, DialogueLine, AudioEvent)
from app.schemas import ShotCreate
from app.schemas_product import IdentityProfile


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def read_blocks(data, limit):
    parse_uploaded_document('source.docx',data,limit)  # zip expansion/signature/content limits
    doc = Document(BytesIO(data))
    blocks = []
    for index, element in enumerate(doc.iter_inner_content()):
        if isinstance(element,Paragraph):
            blocks.append({'id':f'paragraph:{index}','kind':'paragraph','style':element.style.name if element.style else '',
                           'text':element.text,'heading_level':int(element.style.name[-1]) if element.style and re.match(r'Heading [1-9]$',element.style.name) else None})
        elif isinstance(element,Table):
            blocks.append({'id':f'table:{index}','kind':'table','rows':[[c.text for c in r.cells] for r in element.rows]})
    return blocks


def block_lines(blocks):
    for block in blocks:
        if block['kind']=='paragraph':
            for text in block['text'].splitlines():
                yield text.strip(),block['id']
        else:
            rows = block['rows']
            headers = rows[0] if rows else []
            matrix = '人物' in headers and any(v in headers for v in ['动作','台词','外观'])
            for n,values in enumerate(rows):
                if matrix:
                    if n==0: continue
                    record = dict(zip(headers,values))
                    name = record.get('人物','').strip()
                    if '外观' in record:
                        yield '人物：'+name, f"{block['id']}:row:{n}"
                        for key,value in record.items():
                            if key!='人物': yield key+'：'+value, f"{block['id']}:row:{n}:column:{headers.index(key)}"
                    elif record.get('台词'):
                        yield f"对白：{name}：{record['台词']}",f"{block['id']}:row:{n}"
                    else:
                        yield '表演：'+name+'｜'+'；'.join(k+'='+v for k,v in record.items() if k!='人物' and v.strip()),f"{block['id']}:row:{n}"
                elif len(values)>=2:
                    yield values[0].strip().rstrip('：:')+'：'+' '.join(v.strip() for v in values[1:]),f"{block['id']}:row:{n}"
                else:
                    yield ' '.join(values).strip(),f"{block['id']}:row:{n}"


def parse_blocks(blocks):
    preview = {'project':{'name':'未命名剧本','aspect_ratio':'16:9','style':'','synopsis':''},
               'characters':[],'scenes':[],'shots':[],'unrecognized':[],'parser':'local_template_rules_v1'}
    person = scene = shot = None
    section = ''
    name_map = {};declared_names=set();ambiguous_names=set()
    def ensure_person(name,source,declaration=False):
        name = name.strip()
        if declaration and name in declared_names:
            ambiguous_names.add(name)
            item={'key':'person_'+digest(name+source)[:12],'name':name,'aliases':[],'profile':{},'source':[source],'decision':'pending','match_character_id':None,'duplicate_declared_name':True}
            preview['characters'].append(item);name_map[name]=item;return item
        if declaration:declared_names.add(name)
        if name not in name_map:
            item={'key':'person_'+digest(name)[:12],'name':name,'aliases':[],'profile':{},'source':[source], 'decision':'new','match_character_id':None}
            preview['characters'].append(item); name_map[name]=item
        return name_map[name]
    def participate(name,source):
        stable=re.match(r'^.+?[\[（(]([A-Za-z0-9_-]+)[\]）)]$',name.strip())
        if stable and stable[1] in name_map:name=stable[1]
        if name.strip() in ambiguous_names:
            original=name.strip();name=original+'（同名说话人待确认）'
            if name not in name_map:
                item=ensure_person(name,source);item['decision']='pending';item['unresolved_name']=original
        p=ensure_person(name,source)
        if not any(x['character_key']==p['key'] for x in shot['participants']):
            shot['participants'].append({'character_key':p['key'],'performance':{},'source':[source]})
        return next(x for x in shot['participants'] if x['character_key']==p['key'])
    env_fields={'地点':'location','时间':'time','天气':'weather','内外景':'interior_exterior','空间布局':'layout','环境':'description','氛围':'atmosphere','灯光':'lighting','主色调':'palette','关键道具':'props','连续性':'continuity'}
    perf_fields={'称谓':'scene_name','动作':'action','动作顺序':'action_order','姿态':'posture','路线':'route','互动':'interaction','表情':'expression','情绪':'emotion','情绪变化':'emotion_change','视线':'eyeline','位置':'position','朝向':'orientation','前后景':'depth','声音':'voice_profile','语速':'speed','音高':'pitch','发声状态':'vocal_state','服装':'wardrobe','发型':'hair','妆容':'makeup','道具':'props','连续性':'continuity'}
    camera_fields={'景别':'shot_size','机位':'camera_position','角度':'angle','构图':'composition','焦段':'lens','开始状态':'start_state','结束状态':'end_state','转场':'transition','衔接':'continuity'}
    from app.services.cinematic_rules import rules_bundle
    cinematic_rules=rules_bundle()
    perf_fields.update({label:key for key,label in cinematic_rules['field_labels'].items() if key in cinematic_rules['fields']['action'] or key in cinematic_rules['performance_state_fields']})
    env_fields.update({label:key for key,label in cinematic_rules['field_labels'].items() if key in cinematic_rules['fields']['scene']})
    titles={b['id']:b.get('style','') for b in blocks if b.get('style') in ('Title','Subtitle')}
    for text,source in block_lines(blocks):
        text=text.strip()
        if not text: continue
        if source in titles:
            if titles[source]=='Title' and preview['project']['name']=='未命名剧本':preview['project']['name']=text[:200]
            else:preview['project']['notes']=preview['project'].get('notes','')+'\n'+text
            continue
        if text in ('项目基本信息','项目资料','人物设定','场景设定','正文剧情','分镜正文','音频信息','连续性要求','额外约束','全剧梗概','本集梗概','梗概','编写说明','制作资料'):
            section=text; person=None; shot=None; continue
        found=re.match(r'^(?:人物|角色)(?:\s*[:：]\s*|\s+)(.+)$',text)
        if found and section not in ('正文剧情','分镜正文'):
            person=ensure_person(found[1],source,True); shot=None; continue
        found=re.match(r'^场景\s*([A-Za-z\d一二三四五六七八九十]+)[\s：:.、-]*(.*)$',text)
        if found:
            scene={'key':'scene_'+str(len(preview['scenes'])+1),'name':found[2] or '场景'+found[1],'order':len(preview['scenes'])+1,'environment':{},'source':[source]}
            preview['scenes'].append(scene); person=None; shot=None; continue
        found=re.match(r'^(?:分镜|镜头)\s*([A-Za-z\d]+)[\s：:.、-]*(.*)$',text)
        if found:
            if not scene:
                scene={'key':'scene_1','name':'待确认场景','order':1,'environment':{},'source':[], 'suggested':['name']};preview['scenes'].append(scene)
            shot={'key':'shot_'+str(len(preview['shots'])+1),'scene_key':scene['key'],'title':found[2] or '分镜'+found[1], 'visual_prompt':'','action':'','camera_move':'固定','duration_seconds':5,'participants':[],'dialogues':[],'audio_events':[],'photography':{},'source':[source],'suggested':['duration_seconds','camera_move']}
            preview['shots'].append(shot);person=None;continue
        parts=re.split(r'[:：]',text,maxsplit=1)
        key,value=(parts[0].strip(),parts[1].strip()) if len(parts)==2 else ('',text)
        if key in ('项目名称','标题','题材','目标时长','画幅','风格','补充说明') and not shot and not person:
            mapped={'项目名称':'name','标题':'name','题材':'genre','目标时长':'target_duration','画幅':'aspect_ratio','风格':'style','补充说明':'notes'}[key]
            preview['project'][mapped]=value;continue
        if section in ('全剧梗概','本集梗概','梗概','连续性要求','额外约束') and not shot:
            field='synopsis' if '梗概' in section else 'constraints'
            preview['project'][field]=preview['project'].get(field,'')+'\n'+text;continue
        if person and key:
            field={'年龄':'age','年龄范围':'age_range','身份':'description','外观':'facial_features','性格':'personality','声音':'voice_profile','声音特征':'voice_profile','服装':'wardrobe','服装基准':'wardrobe','发型':'hair','肤质':'skin','风格':'style','人物关系':'relationships','人物ID':'character_id','眼睛':'eyes','眉毛':'eyebrows','鼻部':'nose','嘴唇':'lips','下颌':'jaw','发际线':'hairline','体态':'body','肩部':'shoulders','手部':'hands','稳定识别特征':'marks','动作习惯':'movement_style','固定身份特征':'immutable_traits'}.get(key)
            if key=='别名':
                person['aliases']=[x.strip() for x in re.split('[、,，/]',value) if x.strip()]
                for alias in person['aliases']: name_map[alias]=person
            elif field:
                if field=='age' and re.fullmatch(r'\d+\s*岁?',value): value=int(re.search(r'\d+',value)[0])
                person['profile'][field]=value
                if field=='character_id':name_map[value]=person
            else: preview['unrecognized'].append({'source':source,'text':text,'reason':'人物字段待确认'})
            continue
        if shot:
            if key in ('出场人物','参与人物'):
                for name in re.split('[、,，]',value):
                    if name.strip(): participate(name,source)
            elif key in ('画面','画面描述','视觉重点'): shot['visual_prompt']=value
            elif key=='动作': shot['action']=value
            elif key in ('运镜','镜头运动'):
                shot['camera_move']=value;shot['suggested']=[x for x in shot['suggested'] if x!='camera_move']
            elif key in ('时长','建议时长'):
                match=re.search(r'[\d.]+',value)
                if match: shot['duration_seconds']=float(match[0]);shot['suggested']=[x for x in shot['suggested'] if x!='duration_seconds']
            elif key in camera_fields: shot['photography'][camera_fields[key]]=value
            elif key=='表演':
                bits=re.split('[｜|]',value,maxsplit=1)
                if len(bits)==2:
                    p=participate(bits[0],source)
                    for pair in re.split('[；;｜|]',bits[1]):
                        kv=re.split('[=＝:：]',pair,maxsplit=1)
                        if len(kv)==2 and kv[0].strip() in perf_fields: p['performance'][perf_fields[kv[0].strip()]]=kv[1].strip()
                        elif pair.strip(): preview['unrecognized'].append({'source':source,'text':pair,'reason':'人物表演字段待确认'})
                else: preview['unrecognized'].append({'source':source,'text':text,'reason':'表演写法需为 人物｜动作=…；情绪=…'})
            elif key in ('环境声','音效','音乐'):
                shot['audio_events'].append({'kind':{'环境声':'ambience','音效':'sfx','音乐':'music'}[key],'description':value,'source':[source]})
            elif key=='对白' or key in name_map:
                spoken=re.split('[:：]',value,maxsplit=1) if key=='对白' else [key,value]
                if len(spoken)==2:
                    speaker=spoken[0].strip(); delivery={}
                    m=re.match(r'(.+?)[（(](.+)[）)]$',speaker)
                    if m: speaker=m[1];delivery['emotion']=m[2]
                    p=participate(speaker,source)
                    shot['dialogues'].append({'character_key':p['character_key'],'text':spoken[1].strip(),'delivery':delivery,'source':[source]})
                else: preview['unrecognized'].append({'source':source,'text':text,'reason':'对白缺少说话人'})
            else: preview['unrecognized'].append({'source':source,'text':text,'reason':'镜头正文待归类'})
            continue
        if scene and key in env_fields: scene['environment'][env_fields[key]]=value;continue
        if scene and key in ('环境声','音乐'):
            scene.setdefault('audio_events',[]).append({'kind':'ambience' if key=='环境声' else 'music','description':value,'source':[source]});continue
        if '剧本模板' in text or '剧本示例' in text: continue
        preview['unrecognized'].append({'source':source,'text':text,'reason':'未识别内容，确认前请核对'})
    return preview


def match_characters(session,preview):
    characters=list(session.scalars(select(Character)))
    aliases=list(session.scalars(select(CharacterAlias)))
    for p in preview['characters']:
        labels=set([p['name'],*p.get('aliases',[])])
        explicit=p['profile'].get('character_id')
        matches=[c for c in characters if (c.id==explicit if explicit else c.display_name in labels or c.slug in labels or any(a.character_id==c.id and a.name in labels for a in aliases))]
        fuzzy=False
        if not matches and not explicit:
            matches=[c for c in characters if any(len(name)>=2 and SequenceMatcher(None,name,c.display_name).ratio()>=.75 for name in labels)]
            fuzzy=bool(matches)
        p['candidates']=[{'id':c.id,'name':c.display_name,'description':c.description,'approved_version_id':c.current_approved_version_id} for c in matches]
        if p.get('unresolved_name') or p.get('duplicate_declared_name'):p['decision']='pending'
        elif len(matches)==1 and not fuzzy: p['decision']='existing';p['match_character_id']=matches[0].id
        elif matches: p['decision']='pending'
        elif explicit:
            # An explicit *new* stable ID is legitimate; a misspelled ID beside
            # an existing name is an identity conflict, never a silent new face.
            conflicting=[c for c in characters if c.display_name in labels or c.slug in labels]
            p['decision']='new' if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',explicit) and not conflicting else 'pending'
            if conflicting:p['candidates']=[{'id':c.id,'name':c.display_name,'approved_version_id':c.current_approved_version_id} for c in conflicting]
        else: p['decision']='new'
        if p['decision']=='existing':
            candidate=matches[0]
            approved=session.get(CharacterVersion,candidate.current_approved_version_id) if candidate.current_approved_version_id else None
            known=(approved.identity_json.get('profile') or {}) if approved else {}
            if isinstance(p['profile'].get('age'),int) and known.get('age') and known['age']!=p['profile']['age']:
                p['decision']='pending';p['identity_conflict']='文档年龄与已批准身份不一致，请确认身份或剧中年龄'
    return preview


def validate_preview(session,p):
    issues=[]
    if p.get('versions'):
        from app.schemas_cinematic import ScreenplayIR
        try:ScreenplayIR.model_validate(p)
        except ValueError:return ['制作中间数据不符合Schema，请修正字段类型、动作时间与来源状态']
    if not isinstance(p.get('project'),dict) or not isinstance(p['project'].get('name'),str) or not 1<=len(p['project']['name'])<=200:return ['项目标题无效']
    for key,limit in [('characters',100),('scenes',300),('shots',1000)]:
        if not isinstance(p.get(key),list) or len(p[key])>limit or any(not isinstance(x,dict) or not isinstance(x.get('key'),str) for x in p[key]):return ['人物、场景或分镜结构无效或超过数量限制']
    if not p.get('scenes') or not p.get('shots'): issues.append('缺少场景或分镜，不能正式导入')
    people={x['key']:x for x in p.get('characters',[])}
    scenes={x['key'] for x in p.get('scenes',[])}
    if len(people)!=len(p.get('characters',[])) or len(scenes)!=len(p.get('scenes',[])): issues.append('人物或场景内部编号重复')
    designs=p.get('designs',[])
    if len({d['key'] for d in designs})!=len(designs):issues.append('制作设定内部编号重复，请为每个服装或道具使用唯一ID')
    for person in people.values():
        if not person.get('name','').strip(): issues.append('人物名称不能为空')
        if person.get('decision')=='pending': issues.append(person['name']+'：请确认人物匹配')
        if person.get('decision')=='existing' and not session.get(Character,person.get('match_character_id','')): issues.append(person['name']+'：关联人物不存在')
        if person.get('decision') not in ('new','existing','pending'): issues.append('人物匹配方式无效')
        if not isinstance(person.get('profile'),dict):issues.append('人物档案结构无效')
        elif any(not isinstance(value,(str,int)) for value in person['profile'].values()):issues.append('人物资料只接受自然语言字段和明确年龄')
        if not isinstance(person.get('aliases',[]),list):issues.append('人物别名结构无效')
    for scene in p.get('scenes',[]):
        if not isinstance(scene.get('name'),str) or not 1<=len(scene['name'])<=240:issues.append('场景名称无效')
        if not isinstance(scene.get('order'),int) or scene['order']<1:issues.append('场景顺序无效')
        if not isinstance(scene.get('environment',{}),dict):issues.append('场景环境结构无效')
    for shot in p.get('shots',[]):
        if not isinstance(shot.get('participants'),list) or not isinstance(shot.get('dialogues'),list) or len(shot['participants'])>30 or len(shot['dialogues'])>300:issues.append('分镜人物或对白结构无效');continue
        if shot.get('scene_key') not in scenes: issues.append('分镜引用场景无效')
        ids=[x['character_key'] for x in shot.get('participants',[])]
        if len(set(ids))!=len(ids): issues.append('分镜人物重复')
        if set(ids)-set(people): issues.append('分镜人物未定义')
        if any(x['character_key'] not in ids for x in shot.get('dialogues',[])): issues.append('对白说话人不在本镜人物列表')
        if 'duration_seconds' not in shot:issues.append('缺少镜头时长，请选择建议值')
        for participant in shot['participants']:
            if not isinstance(participant.get('performance',{}),dict) or any(not isinstance(v,str) for v in participant.get('performance',{}).values()):issues.append('人物表演字段无效')
        for line in shot['dialogues']:
            if not isinstance(line.get('text'),str) or not line['text'].strip() or len(line['text'])>20000:issues.append('对白文本无效')
        try: ShotCreate(order=1,scene_title=shot.get('title',''),duration_seconds=shot.get('duration_seconds',5),visual_prompt=shot.get('visual_prompt',''),camera_move=shot.get('camera_move',''))
        except ValueError: issues.append('分镜标题、时长或摄影参数无效')
    return issues


def summary(p):
    return {'characters':len(p.get('characters',[])),'scenes':len(p.get('scenes',[])),'shots':len(p.get('shots',[])),
        'dialogues':sum(len(s.get('dialogues',[])) for s in p.get('shots',[])),
        'suggestions':sum(len(s.get('suggested',[])) for s in p.get('shots',[])), 'unrecognized':len(p.get('unrecognized',[]))}


def canonical_profile(person):
    from app.services.identity_profiles import source_profile
    return source_profile(person.get('profile'),person['name']).model_dump()


def semantic_value(value):
    """Source locations and review bookkeeping do not change screenplay facts."""
    ignored={'source','source_ids','provenance','suggested','reviewed_edit','key','candidates','decision','match_character_id','visual_ids','require_visual_review'}
    if isinstance(value,dict):return {k:semantic_value(v) for k,v in value.items() if k not in ignored}
    if isinstance(value,list):return [semantic_value(v) for v in value]
    return value


def semantic_digest(value):return digest(json.dumps(semantic_value(value),ensure_ascii=False,sort_keys=True))


def shot_source_digest(ir,index,identities):
    shot=ir['shots'][index];scene=next(s for s in ir['scenes'] if s['key']==shot['scene_key'])
    people={person['key']:person for person in ir['characters']}
    designs={design['key']:design for design in ir.get('designs',[])}
    scenes={s['key']:s for s in ir['scenes']}
    def person_id(key):return identities.get(key,{}).get('character_id') or people.get(key,{}).get('profile',{}).get('character_id') or people.get(key,{}).get('name',key)
    def scene_facts(value):return {'name':value['name'],'environment':value.get('environment',{}),'audio_events':value.get('audio_events',[])}
    def design_facts(key):
        design=designs.get(key)
        if not design:return {'unresolved_design':key}
        return {'kind':design['kind'],'name':design['name'],'spec':design.get('spec',{}),
            'character_id':person_id(design['character_key']) if design.get('character_key') else None,
            'scene':scene_facts(scenes[design['scene_key']]) if design.get('scene_key') in scenes else None}
    def canonical(value):
        if isinstance(value,list):return [canonical(v) for v in value]
        if not isinstance(value,dict):return value
        result={}
        for key,v in value.items():
            if key in ('key','order','scene_key','source','source_ids','provenance','suggested','reviewed_edit','visual_ids','require_visual_review'):continue
            if key=='character_key':result['character_id']=person_id(v)
            elif key in ('scene_state_key','wardrobe_key','prop_key'):result[key.removesuffix('_key')]=canonical(design_facts(v)) if v else None
            elif key=='wardrobe_states':result[key]={person_id(k):canonical(item) for k,item in v.items()}
            else:result[key]=canonical(v)
        return result
    # Only an explicit handoff in the same complete scene state affects the next
    # shot. A hard cut to an independent scene must not invalidate its cache.
    previous=ir['shots'][index-1] if index else None
    incoming=None
    if previous and canonical(scene_facts(scenes[previous['scene_key']]))==canonical(scene_facts(scene)):
        before=previous.get('production',{})
        handoff={'end_state':before.get('end_state'),
            'photography_end':previous.get('photography',{}).get('end_state'),
            'prop_states':before.get('prop_states',[]),
            'wardrobe_states':{key:v for key,v in before.get('wardrobe_states',{}).items() if v.get('state')},
            'beat_endings':[{k:b.get(k) for k in ('character_key','end_state','contact_target')} for b in before.get('beats',[]) if b.get('end_state') or b.get('contact_target')]}
        incoming=canonical({key:v for key,v in handoff.items() if v}) or None
    fact_shot=canonical(shot)
    if re.fullmatch(r'(?:分镜|镜头)\s*\d+',fact_shot.get('title','')):fact_shot['title']='未命名镜头'
    used=list(dict.fromkeys(p['character_key'] for p in shot.get('participants',[])))
    return semantic_digest({'project':{k:v for k,v in ir['project'].items() if k not in ('name','target_duration')},
        'people':[{'character_id':person_id(key),'name':people[key]['name'],'aliases':people[key].get('aliases',[]),'profile':people[key]['profile']} for key in used],
        'scene':canonical(scene_facts(scene)),'shot':fact_shot,'incoming':incoming,'versions':ir.get('versions',{})})


def persisted_shot_digest(session,shot):
    """Do not reuse an imported row subsequently changed through an editor."""
    from app.models import ProductionEntity
    detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==shot.id))
    prod=(detail.source_json or {}).get('production',{}) if detail else {}
    participants=list(session.scalars(select(ShotCharacter).where(ShotCharacter.shot_id==shot.id).order_by(ShotCharacter.order)))
    by_id={p.id:p.character_id for p in participants}
    lines=list(session.scalars(select(DialogueLine).where(DialogueLine.shot_id==shot.id).order_by(DialogueLine.order)))
    scene=session.get(Scene,detail.scene_id) if detail and detail.scene_id else None
    entities=[prod.get('scene_state_id')]+[v.get('entity_id') for v in prod.get('wardrobe_states',{}).values()]+[v.get('entity_id') for v in prod.get('prop_states',[])]
    return semantic_digest({'shot':{k:getattr(shot,k) for k in ('order','scene_title','visual_prompt','action','camera_move','duration_seconds','aspect_ratio','location','lighting')},
        'scene':{'name':scene.name,'environment':scene.environment_json} if scene else None,
        'photography':detail.photography_json if detail else {},'production':prod,
        'people':[{'character_id':p.character_id,'performance':p.performance_json} for p in participants],
        'dialogues':[{'character_id':by_id.get(line.participant_id),'text':line.text,'delivery':line.delivery_json} for line in lines],
        'audio':[{'kind':a.kind,'description':a.description,'start':a.start_seconds,'end':a.end_seconds,'volume':a.volume,'path':a.relative_path} for a in session.scalars(select(AudioEvent).where(AudioEvent.shot_id==shot.id).order_by(AudioEvent.id))],
        'designs':[{'id':e.id,'spec':e.spec_json} for identifier in entities if identifier and (e:=session.get(ProductionEntity,identifier))]})


def commit_import(session,batch,payload):
    if batch.status=='completed': return batch.result_json
    if batch.status!='pending_review' or batch.revision!=payload.revision: raise AppError(409,'import_stale','预览已变化，请刷新后确认')
    p=batch.preview_json
    issues=validate_preview(session,p)
    if issues: raise AppError(422,'import_validation','请修正导入预览',{'issues':issues})
    if payload.duplicate_strategy=='compare':
        return {'status':'comparison','previous_imports':[{'id':x.id,'result':x.result_json} for x in session.scalars(select(WordImport).where(WordImport.sha256==batch.sha256,WordImport.id!=batch.id))]}
    if payload.duplicate_strategy=='new_revision':
        project=session.get(Project,payload.project_id) if payload.project_id else None
        if not project: raise AppError(422,'project_required','新建剧本版本必须选择已有项目')
    else:
        project=Project(name=p['project']['name'],source_type='docx',source_filename=batch.filename,source_text='\n'.join(text for text,_ in block_lines(batch.blocks_json)))
        session.add(project);session.flush()
    previous=session.scalar(select(ScriptRevision).where(ScriptRevision.project_id==project.id).order_by(ScriptRevision.revision.desc()).limit(1))
    batch.status='importing'
    revision=(session.scalar(select(func.max(ScriptRevision.revision)).where(ScriptRevision.project_id==project.id)) or 0)+1
    raw='\n'.join(text for text,_ in block_lines(batch.blocks_json))
    script=ScriptRevision(project_id=project.id,revision=revision,raw_text=raw,parsed_json={'word_import_id':batch.id,'project':p['project'],'summary':summary(p),'source_blocks':batch.blocks_json,'shots':p['shots'],
        'characters':p['characters'],'scenes':p['scenes'],'designs':p.get('designs',[]),**{k:p[k] for k in ('versions','rules_snapshot','source_versions','mode') if k in p}},status='draft')
    session.add(script);session.flush()
    session.add(SourceDocument(project_id=project.id,filename=batch.filename,original_text=raw,sha256=batch.sha256))
    cmap={};new=[]
    for person in p['characters']:
        if person['decision']=='existing': c=session.get(Character,person['match_character_id'])
        else:
            explicit=person['profile'].get('character_id','')
            identifier=explicit if re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}',explicit) else 'char_'+digest(batch.id+person['key'])[:24]
            if session.get(Character,identifier):raise AppError(409,'character_id_conflict','人物稳定ID已被使用，请刷新导入匹配')
            c=Character(id=identifier,slug=identifier,display_name=person['name'],description=person['profile'].get('description',''),status='needs_review')
            session.add(c);session.flush()
            profile=canonical_profile(person)
            v=CharacterVersion(character_id=c.id,version=1,identity_json={'profile':profile,'source_profile':person['profile'],'source':person.get('source',[]),'import_id':batch.id,'needs_confirmation':[]})
            session.add(v);session.flush();new.append({'character_id':c.id,'version_id':v.id,'profile_ready':bool(profile)})
        v=session.get(CharacterVersion,c.current_approved_version_id) if c.current_approved_version_id else session.scalar(select(CharacterVersion).where(CharacterVersion.character_id==c.id).order_by(CharacterVersion.version.desc()).limit(1))
        for alias in person.get('aliases',[]):
            if not session.scalar(select(CharacterAlias.id).where(CharacterAlias.character_id==c.id,CharacterAlias.name==alias)):
                session.add(CharacterAlias(character_id=c.id,name=alias));session.flush()
        cmap[person['key']]=(c,v)
    identities={key:{'character_id':c.id,'version_id':v.id if v else None} for key,(c,v) in cmap.items()}
    source_hashes=[shot_source_digest(p,n,identities) for n in range(len(p['shots']))]
    reused={};old_scene_candidates={}
    if previous:
        from collections import Counter,defaultdict
        old=previous.parsed_json;old_ids=old.get('explicit_shot_ids',[])
        # Recompute from preserved source so older fingerprint formats remain
        # readable without modifying any historical script or video snapshot.
        old_hashes=[shot_source_digest(old,n,old.get('identity_bindings',{})) for n in range(len(old['shots']))] if all(k in old for k in ('characters','scenes','shots','designs','identity_bindings')) else []
        candidates=defaultdict(list)
        for n,source_hash in enumerate(old_hashes):
            if n<len(old_ids):candidates[source_hash].append(old_ids[n])
        new_counts=Counter(source_hashes)
        for n,source_hash in enumerate(source_hashes):
            if len(candidates[source_hash])!=1 or new_counts[source_hash]!=1:continue
            candidate=session.get(StoryboardShot,candidates[source_hash][0])
            detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==candidate.id)) if candidate else None
            current_people=list(session.scalars(select(ShotCharacter).where(ShotCharacter.shot_id==candidate.id))) if candidate else []
            expected={cmap[person['character_key']][0].id:cmap[person['character_key']][1].id if cmap[person['character_key']][1] else None for person in p['shots'][n]['participants']}
            identity_match=all(expected.get(person.character_id)==person.character_version_id for person in current_people)
            if candidate and candidate.status!='deleted' and detail and identity_match and candidate.project_id==project.id and detail.source_json.get('import_semantic_hash')==persisted_shot_digest(session,candidate):
                reused[p['shots'][n]['key']]=candidate
        for old_id in old_ids:
            detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==old_id))
            scene=session.get(Scene,detail.scene_id) if detail and detail.scene_id else None
            if scene:old_scene_candidates[semantic_digest({'name':scene.name,'environment':scene.environment_json})]=scene.id
    smap={};scene_states={}
    for s in p['scenes']:
        scene_shots=[shot for shot in p['shots'] if shot['scene_key']==s['key']]
        if scene_shots and all(shot['key'] in reused for shot in scene_shots):
            detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==reused[scene_shots[0]['key']].id))
            smap[s['key']]=session.get(Scene,detail.scene_id);continue
        # Repeated declarations in one revision may be a screenplay returning to
        # exactly the same set. Different weather/time/light/audio stays distinct.
        location=s.get('environment',{}).get('location') or s['name']
        audio=[{k:v for k,v in a.items() if k!='source'} for a in s.get('audio_events',[])]
        signature=json.dumps([s['name'],location,s.get('environment',{}),audio],ensure_ascii=False,sort_keys=True)
        if signature in scene_states:
            smap[s['key']]=scene_states[signature]
            continue
        reference_scene_id=old_scene_candidates.get(semantic_digest({'name':s['name'],'environment':s.get('environment',{})}))
        scene=Scene(project_id=project.id,script_revision_id=script.id,order=s['order'],name=s['name'],environment_json=s.get('environment',{}),source_json={'blocks':s.get('source',[]),'import_id':batch.id,'suggested':s.get('suggested',[]),'reviewed_edit':s.get('reviewed_edit',False),**({'reference_scene_id':reference_scene_id} if reference_scene_id else {})})
        session.add(scene);session.flush();smap[s['key']]=scene;scene_states[signature]=scene
        for a in s.get('audio_events',[]): session.add(AudioEvent(scene_id=scene.id,kind=a['kind'],description=a['description'],source_json={'blocks':a.get('source',[])}))
    designs={};scene_designs={}
    from app.models import ProductionEntity
    for design in p.get('designs',[]):
        character=cmap.get(design.get('character_key'))
        scene=smap.get(design.get('scene_key'))
        signature=json.dumps([scene.id if scene else None,design.get('spec',{})],ensure_ascii=False,sort_keys=True)
        if design['kind']=='scene_state' and signature in scene_designs:
            designs[design['key']]=scene_designs[signature];continue
        old_design=next((e for e in session.scalars(select(ProductionEntity).where(ProductionEntity.project_id==project.id,ProductionEntity.logical_id==design['key'],ProductionEntity.kind==design['kind']).order_by(ProductionEntity.version.desc()))
            if e.character_id==(character[0].id if character else None) and e.scene_id==(scene.id if scene else None) and e.spec_json==design.get('spec',{})),None)
        if old_design:
            designs[design['key']]=old_design.id
            if design['kind']=='scene_state':scene_designs[signature]=old_design.id
            continue
        design_version=(session.scalar(select(func.max(ProductionEntity.version)).where(ProductionEntity.project_id==project.id,ProductionEntity.logical_id==design['key'])) or 0)+1
        item=ProductionEntity(project_id=project.id,logical_id=design['key'],version=design_version,kind=design['kind'],name=design['name'],character_id=character[0].id if character else None,scene_id=scene.id if scene else None,spec_json=design.get('spec',{}),source_json={'import_id':batch.id,'blocks':design.get('source',[]),'versions':p.get('versions',{})})
        session.add(item);session.flush();designs[design['key']]=item.id
        if design['kind']=='scene_state':scene_designs[signature]=item.id
    shot_ids=[]
    for order,s in enumerate(p['shots'],1):
        if s['key'] in reused:shot_ids.append(reused[s['key']].id);continue
        scene=smap[s['scene_key']]
        aspect=p['project'].get('aspect_ratio','16:9')
        shot=StoryboardShot(project_id=project.id,order=order,scene_title=s['title'],visual_prompt=s.get('visual_prompt',''),action=s.get('action',''),camera_move=s.get('camera_move',''),location=scene.environment_json.get('location',scene.name),lighting=scene.environment_json.get('lighting',''),duration_seconds=s['duration_seconds'],aspect_ratio=aspect if aspect in ['16:9','9:16','1:1'] else '16:9')
        session.add(shot);session.flush();shot_ids.append(shot.id)
        import copy
        production=copy.deepcopy(s.get('production',{}))
        if production:
            production['scene_state_id']=designs.get(production.get('scene_state_key'))
            production['wardrobe_states']={cmap[k][0].id:{**v,'entity_id':designs.get(v.get('wardrobe_key'))} for k,v in production.get('wardrobe_states',{}).items() if k in cmap}
            production['prop_states']=[{**v,'entity_id':designs.get(v.get('prop_key'))} for v in production.get('prop_states',[])]
            production['beats']=[{**beat,'character_id':cmap[beat['character_key']][0].id} for beat in production.get('beats',[]) if beat['character_key'] in cmap]
        session.add(ShotDetail(shot_id=shot.id,scene_id=scene.id,photography_json=s.get('photography',{}),source_json={'blocks':s.get('source',[]),'suggested':s.get('suggested',[]),'import_id':batch.id,'reviewed_edit':s.get('reviewed_edit',False),'provenance':s.get('provenance',{}),**({'production':production,'versions':p['versions']} if production and p.get('versions') else {})}))
        participants={}
        for n,person in enumerate(s.get('participants',[]),1):
            c,v=cmap[person['character_key']]
            from app.services.screenplay import approved_reference_ids
            refs=approved_reference_ids(session,v) if payload.reuse_approved else []
            item=ShotCharacter(shot_id=shot.id,character_id=c.id,character_version_id=v.id if v else None,order=n,reference_asset_ids_json=refs,performance_json=person.get('performance',{}),source_json={'blocks':person.get('source',[])})
            session.add(item);session.flush();participants[person['character_key']]=item
        for n,line in enumerate(s.get('dialogues',[]),1): session.add(DialogueLine(shot_id=shot.id,participant_id=participants[line['character_key']].id,order=n,text=line['text'],delivery_json=line.get('delivery',{}),source_json={'blocks':line.get('source',[])}))
        for a in s.get('audio_events',[]): session.add(AudioEvent(shot_id=shot.id,kind=a['kind'],description=a['description'],source_json={'blocks':a.get('source',[])}))
        session.flush()
        detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==shot.id))
        detail.source_json={**detail.source_json,'import_semantic_hash':persisted_shot_digest(session,shot)}
    script.parsed_json={**script.parsed_json,'explicit_shot_ids':shot_ids,'shot_source_hashes':source_hashes,
        'shot_source_hash_version':2,'identity_bindings':identities,'scene_bindings':{key:value.id for key,value in smap.items()},'reused_shot_ids':[s.id for s in reused.values()]}
    old_order=previous.parsed_json.get('explicit_shot_ids',[]) if previous else []
    old_positions={identifier:n for n,identifier in enumerate(old_order,1)}
    order_changed=bool(previous and (len(old_order)!=len(shot_ids) or any(old_positions[identifier]!=n for n,identifier in enumerate(shot_ids,1) if identifier in old_positions)))
    timeline_reordered=False
    if previous:
        from app.models import StudioDecision,AuditEvent
        positions={identifier:n for n,identifier in enumerate(shot_ids,1)}
        for decision in session.scalars(select(StudioDecision).where(StudioDecision.project_id==project.id,StudioDecision.shot_id.in_(shot_ids))):
            target=positions[decision.shot_id]
            if decision.position!=target:
                before=decision.position;decision.position=target;decision.revision+=1;timeline_reordered=True
                session.add(AuditEvent(object_type='studio_decision',object_id=decision.id,action='word_revision_order_applied',
                    details_json={'script_id':script.id,'previous_script_id':previous.id,'shot_id':decision.shot_id,'previous_position':before,'position':target,'revision':decision.revision,'review_action_preserved':decision.action,'video_id_preserved':decision.video_id}))
    actual_scene_ids=[session.scalar(select(ShotDetail.scene_id).where(ShotDetail.shot_id==identifier)) for identifier in shot_ids]
    batch.result_json={'project_id':project.id,'script_id':script.id,'shot_ids':shot_ids,
        'character_ids':list(dict.fromkeys(c.id for c,_ in cmap.values())),'new_characters':new,'asset_requests':[], 'summary':summary(p),'production_entity_ids':list(dict.fromkeys(designs.values())),'versions':p.get('versions',{}),
        'scene_ids':list(dict.fromkeys(identifier for identifier in actual_scene_ids if identifier)),
        'scene_bindings':{key:value.id for key,value in smap.items()},
        'reused_shot_ids':[s.id for s in reused.values()],'changed_shot_ids':[identifier for identifier in shot_ids if identifier not in {s.id for s in reused.values()}],
        'revision_diff':{'order_changed':order_changed,'timeline_reordered':timeline_reordered,'new_members':[identifier for identifier in shot_ids if identifier not in old_order],
            'removed_members':[identifier for identifier in old_order if identifier not in shot_ids],
            'timeline_policy':'New Word revision order is the new default timeline; existing keep/exclude decisions, video IDs and trims are preserved.'}}
    batch.status='completed'
    session.flush()
    return batch.result_json
