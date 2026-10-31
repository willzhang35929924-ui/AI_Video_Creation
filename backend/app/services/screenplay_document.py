"""Readable IR -> standard DOCX. No language model or lossy re-extraction here."""
from io import BytesIO
from datetime import datetime, timezone
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from app.services.screenplay_review_warnings import review_warnings

PROFILE = {'character_id':'人物ID','age':'年龄','description':'身份','facial_features':'外观',
    'hair':'头发','eyes':'眼睛','skin':'皮肤','marks':'稳定识别特征','body':'体态',
    'eyebrows':'眉毛','nose':'鼻部','hairline':'发际线','immutable_traits':'固定身份特征',
    'voice':'声音','voice_profile':'声音','wardrobe':'服装','style':'风格','relationships':'人物关系'}
ENV = {'location':'地点','time':'时间','weather':'天气','description':'环境','lighting':'灯光',
    'layout':'空间布局','palette':'主色调','props':'关键道具','continuity':'连续性','materials':'主要材质'}
PERF = {'action':'动作','position':'位置','expression':'表情','emotion':'情绪','eyeline':'视线',
    'orientation':'朝向','wardrobe':'服装','voice_profile':'声音','continuity':'连续性',
    'start_state':'开始状态','end_state':'结束状态','props':'道具'}
PHOTO = {'shot_size':'景别','camera_position':'机位','angle':'角度','composition':'构图','lens':'焦段',
    'start_state':'开始状态','end_state':'结束状态','transition':'转场','continuity':'衔接'}
AUDIO = {'ambience':'环境声','sfx':'音效','music':'背景音乐','voice':'声音说明'}

def readable(value):
    if isinstance(value,dict):return '；'.join(str(k)+'='+readable(v) for k,v in value.items())
    if isinstance(value,list):return '；'.join(readable(v) for v in value)
    return '' if value is None else str(value)

def field(document,label,value):
    if value is not None and value!='' and value!=[] and value!={}:document.add_paragraph(label+'：'+readable(value))

def export_screenplay(ir, *, job_id, document_id, version, input_source):
    document=Document()
    section=document.sections[0]
    section.page_width=Inches(8.5);section.page_height=Inches(11)
    section.top_margin=section.bottom_margin=Inches(.72)
    section.left_margin=section.right_margin=Inches(.78)
    for name in ('Normal','Title','Subtitle','Heading 1','Heading 2','Heading 3'):
        style=document.styles[name];style.font.name='Microsoft YaHei';style.font.color.rgb=RGBColor(0,0,0)
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),'Microsoft YaHei')
        style.paragraph_format.space_after=Pt(6)
        style.font.underline=False
        properties=style.element.get_or_add_pPr()
        for border in list(properties.findall(qn('w:pBdr'))):properties.remove(border)
    document.styles['Normal'].font.size=Pt(11)
    document.styles['Normal'].paragraph_format.line_spacing=1.2
    document.styles['Title'].font.size=Pt(22)
    for name,size in [('Heading 1',16),('Heading 2',13),('Heading 3',11)]:
        document.styles[name].font.size=Pt(size);document.styles[name].paragraph_format.keep_with_next=True
    title=document.add_paragraph(ir['project']['name'],style='Title')
    properties=title._p.get_or_add_pPr()
    for border in list(properties.findall(qn('w:pBdr'))):properties.remove(border)
    document.add_paragraph('本文件由已校验的结构化剧本生成，用于内容审核、角色与场景核对及后续视频制作。创作建议和原始素材中无法确认的信息在文末单独列出；审核通过不会自动开始模型生产。')
    field(document,'剧本任务',job_id);field(document,'文档版本',version)
    generated_at=datetime.now(timezone.utc)
    field(document,'文档生成时间',generated_at.isoformat())
    document.core_properties.created=generated_at.replace(tzinfo=None)
    document.core_properties.modified=generated_at.replace(tzinfo=None)
    for key,value in ir['versions'].items():document.add_paragraph(key+': '+str(value))
    document.core_properties.title=ir['project']['name']
    document.core_properties.subject='视频制作结构化剧本'
    document.core_properties.comments=f'screenplay_job={job_id}; document={document_id}; rules_sha256={ir["rules_snapshot"].get("source_sha256","")}'
    document.add_heading('项目基本信息',1)
    for key,label in [('name','项目名称'),('genre','题材'),('target_duration','目标时长'),('aspect_ratio','画幅'),('style','风格')]:field(document,label,ir['project'].get(key))
    field(document,'分镜合计时长（秒）',ir['project'].get('shot_total_duration_seconds',sum(float(s['duration_seconds']) for s in ir['shots'])))
    if ir['project'].get('duration_conflict'):document.add_paragraph('模型填写的目标时长存在单位或合计冲突，请以文末待确认内容核对，不将字数作为片长。')
    document.add_heading('梗概',1);document.add_paragraph(ir['project'].get('synopsis',''))
    if ir['project'].get('story_body'):
        document.add_heading('剧情正文',1);document.add_paragraph(ir['project']['story_body'])
    document.add_heading('人物设定',1)
    names={p['key']:p['name'] for p in ir['characters']}
    for person in ir['characters']:
        document.add_heading('人物 '+person['name'],2)
        for key,value in person['profile'].items():field(document,PROFILE.get(key,key),value)
        if person.get('aliases'):field(document,'别名',person['aliases'])
    if ir.get('designs'):
        document.add_heading('制作资料',1)
        for design in ir['designs']:
            kind={'wardrobe':'服装','prop':'道具','scene_state':'场景状态'}[design['kind']]
            document.add_heading(f"{kind} {design['key']} {design['name']}",2)
            if design.get('character_key'):field(document,'所属人物',names.get(design['character_key'],design['character_key']))
            for key,value in design.get('spec',{}).items():field(document,key,value)
    if ir.get('products'):
        document.add_heading('产品与营销依据',1)
        for product in ir['products']:
            document.add_heading('产品 '+product['name'],2)
            for key,label in [('id','产品编号'),('brand','品牌或系列'),('category','类别'),('selling_points','用户提供卖点（待核实）'),('evidence_notes','证据来源'),('target_audience','目标受众')]:field(document,label,product.get(key))
            for picture in product.get('images',[]):field(document,'所选图片',picture['id']+'｜'+picture.get('purpose','other')+'｜SHA256 '+picture['sha256'])
        field(document,'营销内容与行动号召',ir.get('marketing'))
        document.add_paragraph('价格、功效、规格与认证不得当作已验证事实；产品外观、Logo及包装文字须人工核对，可采用原图后期图层。')
    document.add_heading('正文剧情',1)
    order=0
    for scene in sorted(ir['scenes'],key=lambda x:x['order']):
        document.add_heading(f"场景 {scene['order']} {scene['name']}",2)
        field(document,'引用场景编号',scene['key'])
        for key,value in scene.get('environment',{}).items():field(document,ENV.get(key,key),value)
        for event in scene.get('audio_events',[]):field(document,AUDIO.get(event.get('kind'),'声音说明'),event.get('description'))
        for shot in (s for s in ir['shots'] if s['scene_key']==scene['key']):
            order+=1;document.add_heading(f"分镜 {order} {shot['title']}",3)
            field(document,'引用分镜编号',shot['key']);field(document,'时长',str(shot['duration_seconds'])+'秒')
            field(document,'出场人物','、'.join(names[p['character_key']] for p in shot['participants']))
            field(document,'画面',shot.get('visual_prompt'));field(document,'动作',shot.get('action'))
            for product in shot.get('products',[]):field(document,'本镜产品可见状态',product)
            field(document,'运镜',shot.get('camera_move'))
            for key,value in shot.get('photography',{}).items():field(document,PHOTO.get(key,key),value)
            for participant in shot['participants']:
                values='；'.join(PERF.get(k,k)+'='+v for k,v in participant.get('performance',{}).items())
                field(document,'表演',names[participant['character_key']]+'｜'+values)
            for line in shot['dialogues']:
                field(document,'对白',names[line['character_key']]+'：'+line['text'])
                if line.get('delivery'):field(document,'对白表演',readable(line['delivery']))
            for event in shot.get('audio_events',[]):field(document,AUDIO.get(event.get('kind'),'声音说明'),event.get('description'))
            production=shot.get('production',{})
            for person_key,state in production.get('wardrobe_states',{}).items():field(document,'造型状态',names.get(person_key,person_key)+'｜'+readable(state))
            for prop in production.get('prop_states',[]):field(document,'道具状态',readable(prop))
            for beat in production.get('beats',[]):
                field(document,'动作节拍',f"{beat['start_seconds']}-{beat['end_seconds']}秒｜{names.get(beat['character_key'],beat['character_key'])}｜{beat['action']}｜初始状态={beat.get('start_state','')}｜结束状态={beat.get('end_state','')}")
            for key,label in [('start_state','开始状态'),('end_state','结束状态'),('state_change_reason','状态变化原因')]:field(document,label,production.get(key))
    document.add_heading('连续性要求',1)
    document.add_paragraph(ir['project'].get('continuity','人物身份与造型分别管理。逐镜核对服装、道具、光线和动作开始结束状态。'))
    document.add_heading('来源与待确认内容',1)
    for key,value in input_source.items():field(document,key,value)
    text_only=input_source.get('输入方式')=='文生剧本'
    for observation in ir.get('observations',[]):field(document,'文字创作建议（并非图像或视频观察）' if text_only else '模型对素材的理解（待核对）',observation)
    for suggestion in ir.get('creative_suggestions',[]):field(document,'创作建议',suggestion)
    mode={'文生剧本':'text','图片生剧本':'image','视频生剧本':'video'}.get(input_source.get('输入方式'),'text')
    for item in dict.fromkeys(ir.get('uncertainties',[])+review_warnings(ir,mode)):field(document,'待确认',item)
    for warning in ir.get('parse_warnings',[]):field(document,'提示',warning.get('message',readable(warning)))
    if not ir.get('uncertainties'):document.add_paragraph('模型未列出额外待确认项；仍需人工审核剧情与素材的对应关系。')
    for table in document.tables:
        borders=OxmlElement('w:tblBorders')
        for edge in ('top','left','bottom','right','insideH','insideV'):
            el=OxmlElement('w:'+edge);el.set(qn('w:val'),'single');el.set(qn('w:sz'),'4');el.set(qn('w:color'),'D9D9D9');borders.append(el)
        table._tbl.tblPr.append(borders)
        for row in table.rows:
            for cell in row.cells:cell.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
    result=BytesIO();document.save(result);return result.getvalue()
