"""Bundled-runtime DOCX authoring; rules and generated contracts share one hash."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from create_word_screenplay_templates import document,person,scene,shot,lines

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'docs/templates';FIXTURES=ROOT/'tests/fixtures/screenplays'
CONTRACT_PY=ROOT/('.venv/Scripts/python.exe' if sys.platform=='win32' else '.venv/bin/python')


def contracts(stamp=False):subprocess.run([str(CONTRACT_PY),str(ROOT/'scripts/build_cinematic_contracts.py'),*(['--stamp'] if stamp else [])],cwd=ROOT,check=True)


def metadata(d,rules):
    d.add_paragraph('按人物、场景和分镜提供自然语言资料。先核对来源、系统建议与连续性报告，再审核参考图和实际视频。未提供的核心事实保持未知，文字要求不代表模型已实现精确控制。')
    p=d.add_paragraph('\n'.join(k+': '+v for k,v in rules['versions'].items()))
    for run in p.runs:run.font.size=Pt(9)
    d.core_properties.comments='cinematic-rules-sha256:'+rules['source_sha256']


def polish(d):
    for table in d.tables:
        borders=OxmlElement('w:tblBorders')
        for edge in ['top','left','bottom','right','insideH','insideV']:
            el=OxmlElement('w:'+edge);el.set(qn('w:val'),'single');el.set(qn('w:sz'),'4');el.set(qn('w:color'),'D9D9D9');borders.append(el)
        table._tbl.tblPr.append(borders)
        for row in table.rows:row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        if len(table.columns)==4:
            table.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
            for cell in table.rows[0].cells:
                shade=OxmlElement('w:shd');shade.set(qn('w:fill'),'E8EEF3');cell._tc.get_or_add_tcPr().append(shade)
    return d


def design(d,kind,identifier,name,values):
    first=len(d.paragraphs)
    d.add_heading(f'{kind} {identifier} {name}',2)
    for k,v in values:lines(d,k+'：'+v)
    for p in d.paragraphs[first:-1]:p.paragraph_format.keep_with_next=True


def production_notes(d,*values):
    first=len(d.paragraphs)
    lines(d,*values)
    for p in d.paragraphs[max(0,first-1):-1]:p.paragraph_format.keep_with_next=True


def write_main(rules,example):
    d=document('电影制作剧本示例' if example else 'Word 电影制作剧本模板');metadata(d,rules)
    d.add_heading('项目基本信息',1)
    lines(d,'项目名称：'+('风雨值班室' if example else '填写片名'),'题材：现实职业短片','目标时长：约18秒','画幅：16:9','风格：自然成年肤质，可信空间与材质，动机明确的实景光源，克制表演')
    d.add_heading('梗概',1);lines(d,'三名工程师核对异常读数，前往档案室寻找校准记录。' if example else '填写人物、冲突与事件；不要把未经确认的身份或剧情补成确定事实。')
    d.add_heading('人物设定',1)
    cast=[('角色甲',34,'设备管理员','东亚成年女性，深棕色短发，宽肩','平稳中低音','浅灰棉质工作衫和墨蓝工装裤','character_example_a','甲工程师'),('角色丙',38,'值班设备工程师','原创成年男性，短黑发，自然成年肤质','略沙哑中音','深灰棉质工作衫','',''),('角色丁',32,'档案管理员','原创成年女性，黑色齐肩发，自然面部比例','清晰柔和，语速偏慢','米色棉衬衫和深色长裤','','')]
    if not example:cast=[('人物甲','填写明确年龄','填写身份','填写外观固定特征','填写声音特点','填写服装基准','','填写别名或删除此行'),('人物乙','填写明确年龄','填写身份','填写外观固定特征','填写声音特点','填写服装基准','','')]
    for values in cast:person(d,*values)
    d.add_heading('制作资料',1)
    design(d,'服装','W01','工作服',[('所属人物',cast[0][0]),('衣物层次','浅灰工作衫，袖口扣合'),('材质','中等厚度哑光棉布'),('织纹','可见细小斜纹'),('合身程度','肩部自然，活动余量适中'),('缝线','领口和袖口有可见车线'),('磨损','袖口轻微使用磨痕'),('固定特征','衣物层次与基础色彩'),('干湿状态','干燥')])
    design(d,'道具','P01','记录本',[('材质','硬纸封面与纸页'),('尺寸','约A5，作为创作设定可修改'),('颜色','深灰'),('磨损','封皮边缘轻微磨白')])
    d.add_heading('正文剧情',1);scene(d,1,'整理室','城市图书馆整理室','深夜','左前上方普通工作灯为主光，雨窗只有微弱冷色反光')
    if example:
        shot(d,1,'核对读数','眼平中景，三人站在磨损的设备桌旁，窗玻璃反射对应室内灯具。',[(c[0],a,e,pos) for c,a,e,pos in [(cast[0],'将记录本放在桌上','专注','画面左侧'),(cast[1],'指向仪表旁记录','克制担忧','画面中央'),(cast[2],'看向记录本','认真','画面右侧')]], [('角色甲','先核对原始记录。'),('角色丙','我来确认设备。'),('角色丁','我去找上一份校准表。')])
        production_notes(d,'造型状态：角色甲｜W01｜干湿状态=干燥','道具状态：P01｜持有人=角色甲｜持有手=右手｜位置=桌面｜开合状态=打开','动作节拍：0-2秒｜角色甲｜将记录本放在桌面｜初始状态=本子拿在右手｜结束状态=本子在桌面')
        shot(d,2,'确认分工','50mm等效中近景，目光在记录本与同事之间自然转移。',[('角色甲','合上记录本','平稳','左侧近景'),('角色丙','轻轻点头','理解','右侧后景')],[('角色甲','按原来的顺序查。'),('角色丙','明白。')],6)
        production_notes(d,'道具状态：P01｜持有人=角色甲｜持有手=右手｜位置=桌面｜开合状态=合上','状态变化原因：角色甲在本镜合上记录本，持有人和位置延续。')
        scene(d,2,'档案室','同一图书馆档案室','同一深夜','顶部普通照明，柜门投下柔和阴影')
        shot(d,3,'查找记录','固定中景，纸页在手指下自然弯曲，柜门结构保持稳定。',[('角色丁','从柜中取出档案夹','专注','画面中央')],[('角色丁','找到了，是这一份。')])
    else:
        shot(d,1,'填写镜头名称','填写景别、机位、画面目标、材质与光源。',[(cast[0][0],'填写一个主动作','填写情绪','填写站位'),(cast[1][0],'填写独立动作','填写情绪','填写站位')],[(cast[0][0],'填写原台词。'),(cast[1][0],'填写回应。')])
        production_notes(d,'造型状态：人物甲｜W01｜干湿状态=干燥','道具状态：P01｜持有人=人物甲｜持有手=右手｜位置=桌面｜开合状态=合上','动作节拍：0-2秒｜人物甲｜填写主要动作｜初始状态=填写起始状态｜结束状态=填写动作结果','状态变化原因：如服装、道具、时间或环境发生变化，在这里说明原因；无变化可删除此行。')
    d.add_heading('连续性要求',1);lines(d,'人物固定面部身份不随造型变化。记录本的持有人、左右手、位置和开合状态应衔接。光源与空间布局在同一场景中复用。')
    d.add_heading('额外约束',1);lines(d,'只使用已审核身份。关键画面属于视觉参考；只有实际工作流支持的参数才构成精确控制。对白和环境声逐项核对。')
    d.add_heading('编写说明',1)
    for section in rules['sections'].values():
        d.add_heading(section['title'],2)
        for instruction in section['instructions'][:2]:lines(d,instruction)
    polish(d).save(OUT/('剧本导入示例.docx' if example else '剧本导入模板.docx'))


def real_case(rules):
    d=document('书册整理 真实电影制作链路实测');metadata(d,rules)
    d.add_heading('项目基本信息',1);lines(d,'项目名称：书册整理 电影制作实测','画幅：16:9','风格：真人实拍的观察式短场景，克制表演，保留皮肤纹理和哑光棉布质感。')
    d.add_heading('人物设定',1)
    person(d,'角色甲',34,'设备管理员','东亚成年女性，深棕色短发，宽肩','平稳中低音','浅灰棉质工作衫、墨蓝工装裤','character_example_a')
    person(d,'角色乙',28,'图书馆研究助理','东亚成年女性，黑色齐肩发，保留既有标准脸特征','平静自然的中音','深蓝棉质外套、米白内搭','character_example_b')
    d.add_heading('制作资料',1)
    design(d,'服装','W_WEN','角色甲工作服',[('所属人物','角色甲'),('衣物层次','浅灰工作衫、墨蓝工装裤'),('材质','哑光中等厚度棉布'),('缝线','袖口与口袋有自然车线'),('磨损','袖口轻微磨白')])
    design(d,'服装','W_LAN','角色乙风衣',[('所属人物','角色乙'),('衣物层次','鼠尾草绿风衣、米白内搭'),('材质','哑光棉质斜纹'),('剪裁','宽松但肩线清楚'),('反光特性','低反光，雨滴只产生局部小高光')])
    design(d,'道具','P_BOOK','记录本',[('材质','硬纸封面和纸页'),('尺寸','A5'),('颜色','深灰'),('磨损','封面边缘轻微磨白')])
    d.add_heading('正文剧情',1)
    scene(d,1,'图书馆门厅','城市图书馆入口门厅下','入夜','门上方普通暖白灯照亮两人，门厅外的雨带只有少量反光')
    production_notes(d,'空间布局：门位于两人身后，门厅外地面湿润；镜头不穿墙。','主要材质：水泥地面、哑光金属门框、旧木长凳')
    shot(d,1,'停在门厅下','眼平稳定中景。角色甲在左，角色乙在右。两位成年人站在门厅内，衣物仅轻微潮湿。人物脸部保留自然比例和可见皮肤纹理。', [('角色甲','将合上的记录本稳稳靠在腰前','专注平静','左侧'),('角色乙','站在角色甲身旁看向记录本','安静倾听','右侧')],[('角色甲','清单在这里。')],5)
    production_notes(d,'景别：双人中景','机位：眼平固定，双脚与地面稳定接触','造型状态：角色甲｜W_WEN｜干湿状态=袖口微湿','造型状态：角色乙｜W_LAN｜干湿状态=肩部有少量雨滴','道具状态：P_BOOK｜持有人=角色甲｜持有手=右手｜位置=腰前｜开合状态=合上','动作节拍：0-2秒｜角色甲｜将本子靠稳在腰前｜初始状态=右手持本｜结束状态=保持本子不动')
    scene(d,2,'整理室','同一图书馆整理室设备桌旁','几分钟后的同一入夜','普通桌面工作灯从左前方照明，雨窗提供微弱冷色反射，背景不发光')
    production_notes(d,'空间布局：设备桌位于两人前方，雨窗在后方左侧，门在画面右后方。','主要材质：磨损桌面、哑光墙漆、玻璃和少量金属设备')
    shot(d,2,'低头核对','固定眼平双人中近景。角色甲在左，角色乙在右。两人的脸、发型、衣物层次与上一镜一致；服装仍微湿，没有突然变干。', [('角色甲','站稳并保持合上的记录本在腰前','平静专注','左侧'),('角色乙','低头看向记录本，轻微点头后保持','认真','右侧')],[('角色乙','我来整理。')],5)
    production_notes(d,'景别：双人中近景','机位：眼平固定','造型状态：角色甲｜W_WEN｜干湿状态=袖口微湿','造型状态：角色乙｜W_LAN｜干湿状态=肩部仍有少量雨滴','道具状态：P_BOOK｜持有人=角色甲｜持有手=右手｜位置=腰前｜开合状态=合上','状态变化原因：两人从门厅走入整理室，时间只过去几分钟，衣物仍湿；本镜不表现走路过程。','动作节拍：0-2秒｜角色乙｜低头看记录本｜初始状态=直立｜结束状态=目光落在本子上')
    d.add_heading('连续性要求',1);lines(d,'两人固定身份沿用已审核v1。服装潮湿状态延续，P_BOOK始终由角色甲右手持于腰前且合上。场景切换明确，镜头中不增加额外人物或复杂接触。')
    polish(d).save(FIXTURES/'cinematic_real_case.docx')


def fixture_documents():
    examples={
      'standard':'项目基本信息\n项目名称：标准测试\n人物设定\n人物 角色甲\n年龄：34\n正文剧情\n场景 1 整理室\n分镜 1 核对\n出场人物：角色甲\n画面：角色甲看向记录本。\n对白：角色甲：开始。',
      'free_narrative':'主要人物\n角色甲（34岁，工程师）\n剧情正文\n内景 整理室 - 夜\n角色甲把记录本放在桌上。\n角色甲说：先核对。',
      'ai_headings':'# 角色介绍\n角色：角色甲\n年龄：34\n# 剧本正文\nScene 1: Control room\nShot 1: Examine\nCast：角色甲\nVisual：角色甲看着记录本。\nDialogue：角色甲：清单在这里。',
      'multi_dialogue':'人物设定\n人物 角色甲\n年龄：34\n人物 角色乙\n年龄：28\n正文剧情\n场景 1 整理室\n分镜 1 双人\n出场人物：角色甲、角色乙\n对白：角色甲：开始。\n对白：角色乙：收到。\n对白：角色甲：继续。',
      'aliases':'人物设定\n人物 角色甲\n别名：甲工程师\n年龄：34\n正文剧情\n场景 1 整理室\n分镜 1 核对\n出场人物：甲工程师\n对白：甲工程师：开始。',
      'wardrobe_change':'人物设定\n人物 角色甲\n年龄：34\n服装基准：灰色棉衫\n正文剧情\n场景 1 门口\n分镜 1 雨中\n出场人物：角色甲\n表演：角色甲｜动作=站稳｜干湿状态=湿润\n分镜 2 室内\n出场人物：角色甲\n表演：角色甲｜动作=看向前方｜干湿状态=干燥',
      'scene_change':'人物设定\n人物 角色甲\n年龄：34\n正文剧情\n场次一 整理室\n时间：夜\n天气：大雨\n镜头一 核对\n出场人物：角色甲\n场次二 档案室\n时间：次日上午\n天气：晴\n镜头二 查找\n出场人物：角色甲',
      'incomplete':'一段没有人物表、镜头编号和场景标题的故事。她站在门边，看向远处，没有说明她是谁。\n未来才会补充这一段的说话人。',
      'contradiction':'人物设定\n人物 角色甲\n年龄：34\n制作资料\n道具 P01 记录本\n材质：纸\n正文剧情\n场景 1 整理室\n分镜 1 持本\n出场人物：角色甲\n道具状态：P01｜持有人=角色甲｜持有手=右手｜开合状态=打开\n分镜 2 持本\n出场人物：角色甲\n道具状态：P01｜持有人=角色乙｜持有手=左手｜开合状态=合上',
    }
    for name,text in examples.items():
        doc=document(name+' 剧本兼容测试')
        for line in text.splitlines():doc.add_paragraph(line)
        polish(doc).save(FIXTURES/(name+'.docx'))
    d=document('表格剧本兼容测试');d.add_heading('人物设定',1);person(d,'角色甲',34,'工程师','银色短发','平静中低音','浅灰棉衫')
    d.add_heading('正文剧情',1);table=d.add_table(rows=1,cols=6)
    for cell,value in zip(table.rows[0].cells,['Scene','Shot','Cast','Visual','Duration','Dialogue']):cell.text=value
    for values in [['整理室','核对记录','角色甲','角色甲低头看本子','5','角色甲：开始核对。'],['档案室','查找档案','角色甲','角色甲看向柜子','5','角色甲：找到了。']]:
        for cell,value in zip(table.add_row().cells,values):cell.text=value
    polish(d).save(FIXTURES/'table_layout.docx')


def main():
    OUT.mkdir(parents=True,exist_ok=True);FIXTURES.mkdir(parents=True,exist_ok=True);contracts()
    rules=json.loads((ROOT/'schemas/cinematic-rules.snapshot.json').read_text(encoding='utf-8'))
    write_main(rules,False);write_main(rules,True);real_case(rules);fixture_documents();contracts(True)
    print('Built 2 creator templates and 11 real DOCX fixtures; all stamped artifacts share '+rules['versions']['production_rules_version'])


if __name__=='__main__':main()
