"""User-facing natural-language templates, authored with the bundled runtime."""
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/templates';OUT.mkdir(parents=True,exist_ok=True)


def document(title):
    d=Document();s=d.sections[0];s.page_width=Inches(8.5);s.page_height=Inches(11)
    s.top_margin=s.bottom_margin=Inches(.7);s.left_margin=s.right_margin=Inches(.8)
    for name in ('Normal','Title','Heading 1','Heading 2','Heading 3'):
        style=d.styles[name];style.font.name='Microsoft YaHei';style.font.color.rgb=RGBColor(0,0,0)
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),'Microsoft YaHei')
        style.font.size=Pt(11 if name=='Normal' else 22 if name=='Title' else 15 if name=='Heading 1' else 12)
        style.paragraph_format.space_after=Pt(7)
        style.paragraph_format.space_before=Pt(0 if name in ('Normal','Title') else 10)
    for style in d.styles:
        for border in style.element.xpath('./w:pPr/w:pBdr'):
            border.getparent().remove(border)
    d.styles['Normal'].paragraph_format.line_spacing=1.1
    d.styles['Normal'].paragraph_format.space_after=Pt(5)
    p=d.add_heading(title,0)
    borders=OxmlElement('w:pBdr')
    for side in ('top','left','bottom','right','between'):
        border=OxmlElement('w:'+side);border.set(qn('w:val'),'nil');borders.append(border)
    p._p.get_or_add_pPr().append(borders)
    return d


def lines(d,*values):
    for v in values:d.add_paragraph(v)


def person(d,name,age,identity,appearance,voice,outfit,identifier='',alias=''):
    d.add_heading('人物 '+name,2)
    t=d.add_table(rows=0,cols=2);t.autofit=False;t.columns[0].width=Inches(1.15);t.columns[1].width=Inches(5.7)
    values=[('年龄',age),('身份',identity),('外观',appearance),('声音',voice),('服装基准',outfit)]
    if identifier:values.insert(0,('人物ID',identifier))
    if alias:values.insert(0,('别名',alias))
    for index,(key,value) in enumerate(values):
        cells=t.add_row().cells;cells[0].text=key;cells[1].text=str(value)
        for c in cells:
            for p in c.paragraphs:
                p.paragraph_format.space_after=Pt(0)
                p.paragraph_format.line_spacing=1
                p.paragraph_format.keep_with_next=index<len(values)-1
            tcpr=c._tc.get_or_add_tcPr();margins=OxmlElement('w:tcMar')
            for side in ('top','left','bottom','right'):
                e=OxmlElement('w:'+side);e.set(qn('w:w'),'45');e.set(qn('w:type'),'dxa');margins.append(e)
            tcpr.append(margins)
        trpr=t.rows[-1]._tr.get_or_add_trPr();trpr.append(OxmlElement('w:cantSplit'))
    d.add_paragraph()


def scene(d,n,name,location,time,light):
    first=len(d.paragraphs)
    d.add_heading(f'场景 {n} {name}',1)
    lines(d,'地点：'+location,'时间：'+time,'内外景：内景','天气：室外风雨，室内干燥','灯光：'+light,'环境声：窗外雨声，室内设备风扇低鸣')
    for p in d.paragraphs[first:-1]:p.paragraph_format.keep_with_next=True


def shot(d,n,title,description,performances,dialogues,duration=None):
    start=len(d.element.body)
    d.add_heading(f'分镜 {n} {title}',2)
    lines(d,'出场人物：'+'、'.join(p[0] for p in performances),'画面：'+description)
    if duration:lines(d,'时长：'+str(duration)+'秒','运镜：固定')
    t=d.add_table(rows=1,cols=4)
    t.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    for c,v in zip(t.rows[0].cells,['人物','动作','情绪','位置']):c.text=v
    for p in performances:
        for c,v in zip(t.add_row().cells,p):c.text=v
    for r in t.rows:r._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
    for speaker,text in dialogues:lines(d,'对白：'+speaker+'：'+text)
    lines(d,'音效：动作对应的轻微衣料和设备接触声')
    paragraphs=[]
    from docx.text.paragraph import Paragraph
    for block in list(d.element.body)[start-1:]:
        if block.tag==qn('w:p'):paragraphs.append(Paragraph(block,d._body))
        else:paragraphs.extend(Paragraph(p,d._body) for p in block.iter(qn('w:p')))
    for p in paragraphs:p.paragraph_format.keep_with_next=True
    if paragraphs:paragraphs[-1].paragraph_format.keep_with_next=False


def example():
    d=document('多人剧本导入示例')
    lines(d,'本例展示两处场景、三个人物和多人同镜对白。导入后先核对人物匹配与系统建议，再决定是否生成标准脸候选。角色甲使用稳定人物ID复用现有角色，另外两人创建待审核档案。')
    d.add_heading('项目基本信息',1)
    lines(d,'项目名称：风雨值班室','题材：现实职业短片','目标时长：约18秒','画幅：16:9','风格：真人实拍故事片，克制表演，可信的灯光和空间材质')
    d.add_heading('梗概',1);lines(d,'风雨夜，三名工程师核对异常读数，随后前往档案室寻找校准记录。')
    d.add_heading('人物设定',1)
    person(d,'角色甲',34,'设备管理员','东亚成年女性，深棕色短发，宽肩','平稳中低音，语速适中','浅灰工作衫和墨蓝工装裤','character_example_a','甲工程师')
    person(d,'角色丙',38,'值班设备工程师','原创成年男性，短黑发，自然成年皮肤，普通体态','略沙哑中音，表达简短','深灰棉质工作衫')
    person(d,'角色丁',32,'海洋档案管理员','原创成年女性，黑色齐肩发，自然面部比例','清晰柔和，语速偏慢','米色衬衫和深色长裤')
    d.add_heading('正文剧情',1)
    scene(d,1,'整理室','城市图书馆整理室','深夜','左前上方室内工作灯为主光，雨窗提供微弱冷色反光')
    shot(d,1,'核对读数','眼平中景，三人站在磨损的设备桌旁。设备与衣料比例自然，窗玻璃反射对应室内灯具。',[
        ('角色甲','将记录本放在桌上','专注','画面左侧'),('角色丙','指向仪表旁的记录','克制担忧','画面中央'),('角色丁','看向记录本','认真','画面右侧')],
        [('角色甲','先核对原始记录。'),('角色丙','我来确认设备。'),('角色丁','我去找上一份校准表。')])
    shot(d,2,'确认分工','50mm等效中近景，人物目光在记录本与同事之间自然转移。',[
        ('角色甲','合上记录本','平稳','左侧近景'),('角色丙','轻轻点头','理解','右侧后景')],[('角色甲','按原来的顺序查。'),('角色丙','明白。')],6)
    scene(d,2,'档案室','同一图书馆档案室','同一深夜','顶部普通照明，柜门投下柔和阴影')
    shot(d,3,'查找记录','固定中景，档案柜和桌面有正常使用痕迹，纸张在手指下自然弯曲。',[
        ('角色丁','从柜中取出档案夹','专注','画面中央')],[('角色丁','找到了，是这一份。')])
    d.add_heading('连续性要求',1);lines(d,'同一晚的衣着、发型和道具保持一致。整理室桌上的记录本由角色甲持有。档案夹由角色丁从档案室柜中取出。')
    d.add_heading('额外约束',1);lines(d,'所有新人物均为原创成年人。未填写的运镜和时长由系统给出可修改建议；对白保留原文，是否实际配音以生成任务能力和结果为准。')
    d.save(OUT/'剧本导入示例.docx')


def blank():
    d=document('Word剧本导入模板')
    lines(d,'填写方式：保留人物、场景和分镜的标题写法，用自然语言替换占位内容。项目与人物资料在前，正文按场景、分镜顺序排列。无需编写代码或提示词。可删除不需要的字段，未提供的镜头参数会标为建议。')
    d.add_heading('项目基本信息',1)
    lines(d,'项目名称：填写片名','题材：填写题材','目标时长：填写建议时长','画幅：16:9','风格：填写希望的摄影与表演风格')
    d.add_heading('梗概',1);lines(d,'填写主要人物、冲突与本集事件。')
    d.add_heading('人物设定',1)
    person(d,'人物甲','填写明确年龄','填写职业与人物身份','填写外观与固定特征','填写音色、语速和发声特点','填写服装基准',alias='填写别名或删除此行')
    person(d,'人物乙','填写明确年龄','填写职业与人物身份','填写外观与固定特征','填写音色与表达习惯','填写服装基准')
    d.add_heading('正文剧情',1)
    scene(d,1,'填写场景名称','填写具体地点','填写时间','填写主要光源的方向、强弱和颜色')
    lines(d,'空间布局：填写人物活动区域、设备与主要道具的位置','关键道具：填写道具名称与归属')
    shot(d,1,'填写镜头名称','填写景别、机位、画面内容与视觉重点。',[
        ('人物甲','填写独立动作','填写情绪','填写站位'),('人物乙','填写独立动作','填写情绪','填写站位')],
        [('人物甲','填写第一句台词。'),('人物乙','填写回应。')])
    lines(d,'开始状态：填写人物或道具的初始状态','结束状态：填写动作完成后的状态','转场：填写转场意图或删除此行','音乐：填写音乐意图或删除此行')
    d.add_heading('连续性要求',1);lines(d,'填写人物外观、道具、位置、衣着与剧情状态的衔接要求。')
    d.add_heading('额外约束',1);lines(d,'填写特殊要求。已有素材库人物可在人物资料表中增加“人物ID”一行，填入页面显示的稳定ID；不确定时在导入预览中选择，不要仅凭重名合并。')
    d.save(OUT/'剧本导入模板.docx')


if __name__=='__main__':
    from build_screenplay_templates import main
    main()
