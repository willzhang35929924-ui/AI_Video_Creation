"""Deterministic review hints, never a fabricated story or automatic approval."""
import re

def review_warnings(ir,input_mode):
    if not ir:return []
    project=ir.get('project',{});shots=ir.get('shots',[]);result=[]
    total=sum(float(shot.get('duration_seconds',0)) for shot in shots)
    value=project.get('target_duration',project.get('target_duration_seconds'))
    if value is not None:
        match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(秒|s|sec|seconds?|分钟|min|minutes?)?\s*',str(value),re.I)
        if match:
            amount=float(match[1])*(60 if (match[2] or '').lower() in {'分钟','min','minute','minutes'} else 1)
            explicit_seconds=(isinstance(value,(int,float)) and not isinstance(value,bool)) or ('target_duration' not in project and 'target_duration_seconds' in project)
            if (not match[2] and not explicit_seconds) or abs(amount-total)>.1:
                result.append(f'模型目标时长“{value}”的单位或数值需核对；实际分镜合计{total:g}秒，字数不代表片长。')
    for index,shot in enumerate(shots,1):
        if str(shot.get('photography',{}).get('shot_size','')).strip() in {'16:9','9:16','1:1','4:3','2.39:1'}:
            result.append(f'分镜{index}把画幅写入景别，请确认应为近景、中景或远景等实际景别。')
        production=shot.get('production',{});end=str(production.get('end_state',''))
        for prop in production.get('prop_states',[]):
            if not isinstance(prop,dict):continue
            description=' '.join(str(v) for v in prop.values())
            if any(name in description for name in ('记录本','本子')) and '合上' in description and any(word in end for word in ('打开','翻开')) and not any(word in end for word in ('未打开','不打开','未翻开')):
                result.append(f'分镜{index}的记录本道具状态与镜头结束状态开合不一致，请核对连续性。')
    if input_mode=='text' and any('原视频' in str(value) for value in ir.get('uncertainties',[])):
        result.append('本次只有文字输入，未读取图像或原视频；模型提及的“原视频”属于无对应来源的推测。')
    if input_mode in {'image','video'}:
        result.append('人物姓名、关系和扩展故事是创作设定；模型对素材的视觉理解也需逐项核对，不能当作已核实事实。')
    return list(dict.fromkeys(result))
