import {useEffect,useRef,useState} from 'react';
import {ApiError,request} from '../lib/api';
import {FormField} from './ui';
import './AudioDurationResolution.css';

type Action='extend_video'|'shorten_dialogue'|'adjust_speed';
type Capability={available:boolean;reason?:string};
type SpeedCapability=Capability&{min?:number;max?:number;languages?:string[];recommended_speed?:number};
type Dialogue={id:string;text:string;original_text?:string;audio_id?:string;duration_seconds?:number;speed?:number;language?:string;provider?:string;runtime_family?:string;capabilities?:{adjust_speed?:SpeedCapability}};
export type AudioDurationContext={
  project_id:string;run_id:string;node_id:string;source_fingerprint:string;
  shot:{id:string;title:string;order:number;revision:number};
  video:{id:string;duration_seconds:number;sha256?:string};audio_duration_seconds:number;
  dialogues:Dialogue[];
  capabilities:{
    extend_video:Capability&{recommended_duration_seconds?:number};shorten_dialogue:Capability;
    adjust_speed:SpeedCapability;
  };
};
type Payload={expected_run_id:string;source_fingerprint:string;action:Action;client_request_id:string;target_duration_seconds?:number;dialogue_line_id?:string;text?:string;speed?:number;language?:string};
export type AudioDurationPreview={preview_token:string;action:Action;summary:string;affected_nodes:string[];warnings:string[];parameters:Record<string,unknown>};
export type AudioDurationResult={run_id:string;node_id:string;status:string;affected_nodes:string[];replayed:boolean;notice:string};
const labels:Record<Action,string>={extend_video:'延长镜头',shorten_dialogue:'缩短台词',adjust_speed:'调整语速'};
const actions:Action[]=['extend_video','shorten_dialogue','adjust_speed'];
const duration=(seconds:number|undefined)=>Number.isFinite(seconds)?`${seconds!.toFixed(3)} 秒`:'未测得';
const requestId=()=>globalThis.crypto?.randomUUID?.()||`audio-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const lineSpeedCapability=(data:AudioDurationContext,line?:Dialogue)=>line?.capabilities?.adjust_speed??data.capabilities.adjust_speed;

function checkedContext(value:AudioDurationContext,projectId:string,nodeId:string){
  if(value.project_id!==projectId||value.node_id!==nodeId||!value.run_id||!value.source_fingerprint||!value.shot?.id
    ||!Number.isFinite(value.video?.duration_seconds)||!Number.isFinite(value.audio_duration_seconds)||!Array.isArray(value.dialogues)
    ||actions.some(action=>typeof value.capabilities?.[action]?.available!=='boolean')){
    throw new Error('音频时长诊断与当前项目或任务不一致，未应用。');
  }
  return value;
}

export function AudioDurationResolution({projectId,nodeId,disabled=false,onApplied,onBusyChange}:{
  projectId:string;nodeId:string;disabled?:boolean;onApplied?:(result:AudioDurationResult)=>void|Promise<void>;onBusyChange?:(busy:boolean)=>void;
}){
  const [data,setData]=useState<AudioDurationContext|null>(null),[action,setAction]=useState<Action|''>('');
  const [seconds,setSeconds]=useState(''),[dialogueId,setDialogueId]=useState(''),[text,setText]=useState(''),[speed,setSpeed]=useState(''),[language,setLanguage]=useState('');
  const [loading,setLoading]=useState(true),[working,setWorking]=useState<'preview'|'apply'|''>(''),[error,setError]=useState(''),[refreshError,setRefreshError]=useState(''),[reload,setReload]=useState(0);
  const [preview,setPreview]=useState<{value:AudioDurationPreview;payload:Payload}|null>(null),[result,setResult]=useState<AudioDurationResult|null>(null);
  const [applyUncertain,setApplyUncertain]=useState(false);
  const epoch=useRef(0),locked=useRef(false),readScope=useRef(''),callbacks=useRef({onApplied,onBusyChange});callbacks.current={onApplied,onBusyChange};
  const path=`/api/studio/projects/${encodeURIComponent(projectId)}/audio-duration/${encodeURIComponent(nodeId)}`;
  const busy=loading||Boolean(working),dialogue=data?.dialogues.find(line=>line.id===dialogueId);
  const speedLimits:SpeedCapability=data?lineSpeedCapability(data,dialogue):{available:false};
  function actionCapability(value:Action):Capability{
    if(!data)return {available:false};
    if(value!=='adjust_speed')return data.capabilities[value];
    if(dialogue||data.dialogues.length<2)return speedLimits;
    const available=data.dialogues.some(line=>lineSpeedCapability(data,line).available);
    return {...speedLimits,available,reason:available?undefined:speedLimits.reason};
  }
  useEffect(()=>{callbacks.current.onBusyChange?.(Boolean(working));},[working]);
  useEffect(()=>()=>callbacks.current.onBusyChange?.(false),[]);
  useEffect(()=>{
    const controller=new AbortController(),token=++epoch.current;
    if(readScope.current!==path)setError('');readScope.current=path;
    locked.current=false;setLoading(true);setWorking('');setData(null);setAction('');setPreview(null);setResult(null);setApplyUncertain(false);setRefreshError('');
    void request<AudioDurationContext>(path,{signal:controller.signal}).then(value=>{
      const checked=checkedContext(value,projectId,nodeId);if(controller.signal.aborted||epoch.current!==token)return;
      setData(checked);const first=checked.dialogues.length===1?checked.dialogues[0]:undefined;
      setDialogueId(first?.id||'');setText(first?.text||'');setLanguage(first?.language||'');
      setSpeed(String(lineSpeedCapability(checked,first).recommended_speed??first?.speed??1));
      setSeconds(String(checked.capabilities.extend_video.recommended_duration_seconds??Math.ceil(checked.audio_duration_seconds+.25)));
    }).catch(e=>{if(!controller.signal.aborted&&epoch.current===token)setError('无法读取音频时长诊断：'+String(e));})
      .finally(()=>{if(!controller.signal.aborted&&epoch.current===token)setLoading(false);});
    return()=>{controller.abort();epoch.current++;};
  },[path,projectId,nodeId,reload]);
  function edit(change:()=>void){change();setPreview(null);setError('');setRefreshError('');}
  function refresh(){if(locked.current)return;setError('');setReload(value=>value+1);}
  function payload():Payload{
    if(!data||!action||!actionCapability(action).available)throw new Error('请选择当前可用的处理方式。');
    const base:Payload={expected_run_id:data.run_id,source_fingerprint:data.source_fingerprint,action,client_request_id:requestId()};
    if(action==='extend_video'){
      const target=Number(seconds);if(!Number.isFinite(target)||target<=data.video.duration_seconds)throw new Error('新的镜头时长必须大于当前原片时长。');
      return {...base,target_duration_seconds:target};
    }
    if(!dialogue)throw new Error('请明确选择要重新处理的台词。');
    if(action==='shorten_dialogue'){
      if(!text.trim()||text.trim()===dialogue.text.trim())throw new Error('请填写修改后的精简台词。');
      if(speedLimits.languages?.length&&language&&!speedLimits.languages.includes(language))throw new Error('请明确选择当前服务支持的配音语言。');
      return {...base,dialogue_line_id:dialogue.id,text:text.trim(),...(speedLimits.languages?.length&&language?{language}:{})};
    }
    const value=Number(speed),limits=speedLimits;
    if(!Number.isFinite(value)||value<(limits.min??.5)||value>(limits.max??2))throw new Error('语速超出当前配音服务支持范围。');
    if(limits.languages?.length&&(!language||!limits.languages.includes(language)))throw new Error('请明确选择当前服务支持的配音语言。');
    return {...base,dialogue_line_id:dialogue.id,speed:value,...(limits.languages?.length?{language}:{})};
  }
  async function submit(kind:'preview'|'apply'){
    if(locked.current||disabled||loading||result||kind==='preview'&&applyUncertain)return;
    const token=epoch.current;locked.current=true;setWorking(kind);setError('');setRefreshError('');
    try{
      if(kind==='preview'){
        const sent=payload(),value=await request<AudioDurationPreview>(`${path}/preview`,{method:'POST',body:JSON.stringify(sent)});
        if(epoch.current!==token)return;
        if(!value.preview_token||value.action!==sent.action||!Array.isArray(value.affected_nodes)||!Array.isArray(value.warnings))throw new Error('预览回执无效，未提交重新处理。');
        setPreview({value,payload:sent});
      }else{
        if(!preview)throw new Error('请先预览并核对处理影响。');
        const value=await request<AudioDurationResult>(`${path}/apply`,{method:'POST',body:JSON.stringify({...preview.payload,preview_token:preview.value.preview_token})});
        if(epoch.current!==token)return;
        if(value.node_id!==nodeId||!value.run_id||!value.status)throw new Error('处理回执与当前任务不一致，请刷新核对真实任务，勿重复提交。');
        setResult(value);setApplyUncertain(false);
        try{await callbacks.current.onApplied?.(value);}catch(e){if(epoch.current===token)setRefreshError('处理已接收，但刷新任务状态失败，请刷新工作台核对：'+String(e));}
      }
    }catch(e){
      if(epoch.current!==token)return;
      if(e instanceof ApiError&&e.status===409){setError('镜头、音频或编排状态已变化，未覆盖新数据。已重新读取，请重新选择并预览后再确认。');setReload(value=>value+1);}
      else {
        const uncertain=kind==='apply'&&(!(e instanceof ApiError)||e.status===0||e.status>=500);
        if(uncertain)setApplyUncertain(true);
        setError((kind==='preview'?'处理预览失败：':uncertain?'提交结果尚未确认；可再次确认以同一请求编号核对，未核对前不可更换方案：':'处理未提交：')+String(e));
      }
    }finally{if(epoch.current===token){locked.current=false;setWorking('');}}
  }
  return <section className="audio-duration-resolution" aria-label="音频超长处理">
    <h3>音频超长处理</h3>
    {loading?<p role="status">正在读取原片、配音与冻结任务依据…</p>:null}
    {data?<>
      <p>第 {data.shot.order} 镜 · {data.shot.title}：实际原片 <strong>{duration(data.video.duration_seconds)}</strong>，配音 <strong>{duration(data.audio_duration_seconds)}</strong>，超出 <strong>{duration(Math.max(0,data.audio_duration_seconds-data.video.duration_seconds))}</strong>。</p>
      <p className="form-hint">以下处理会新建受影响的素材版本，原片、原配音和历史任务保留。预览不会提交模型；确认后才由程序重新处理。</p>
      <div className="audio-duration-originals">{data.dialogues.map(line=><div key={line.id}><p><strong>原剧本台词</strong>：{line.original_text??line.text}</p><p><strong>本轮实际配音台词</strong>：{line.text}</p><small>配音 {duration(line.duration_seconds)} · 实际语言 {line.language||'未记录'} · 实际语速 {line.speed??'未记录'}{line.runtime_family?` · ${line.runtime_family}`:''}</small></div>)}</div>
      <div className="studio-actions" aria-label="选择音频超长处理方式">{actions.map(value=><button key={value} type="button" className={`button ${action===value?'button-primary':'button-secondary'}`} aria-pressed={action===value} disabled={disabled||busy||Boolean(result)||applyUncertain||!actionCapability(value).available} title={actionCapability(value).reason} onClick={()=>edit(()=>setAction(value))}>{labels[value]}</button>)}</div>
      {actions.filter(value=>!actionCapability(value).available).map(value=><p className="form-hint" key={value}>{labels[value]}不可用：{actionCapability(value).reason||'当前服务不支持。'}</p>)}
      {action?<fieldset className="audio-duration-fields" disabled={disabled||busy||Boolean(result)||applyUncertain}><legend>{labels[action]}参数</legend>
        {action==='extend_video'?<FormField label="新的镜头时长（秒）" hint={`建议 ${data.capabilities.extend_video.recommended_duration_seconds??Math.ceil(data.audio_duration_seconds+.25)} 秒；会重新生成本镜视频，再重新混音及处理画质，保留原配音。`}><input type="number" min={data.video.duration_seconds+.01} step="0.1" value={seconds} onChange={event=>edit(()=>setSeconds(event.target.value))}/></FormField>:<>
          <FormField label="需要重新处理的台词"><select value={dialogueId} onChange={event=>edit(()=>{const line=data.dialogues.find(item=>item.id===event.target.value);setDialogueId(line?.id||'');setText(line?.text||'');setLanguage(line?.language||'');setSpeed(String(lineSpeedCapability(data,line).recommended_speed??line?.speed??1));})}><option value="">请选择台词</option>{data.dialogues.map((line,index)=><option value={line.id} key={line.id}>台词 {index+1}：{line.text}</option>)}</select></FormField>
          {action==='shorten_dialogue'?<FormField label="修改后的精简台词" hint="只覆盖本轮所选台词的配音，不改原 Word 或剧本；重做该句配音及后续混音、画质，保留原片。实际长度仍需重新测量。"><textarea rows={3} value={text} onChange={event=>edit(()=>setText(event.target.value))}/></FormField>:<>
            <FormField label="新的配音语速" hint={`支持 ${speedLimits.min??.5}–${speedLimits.max??2} 倍；${speedLimits.recommended_speed?`建议 ${speedLimits.recommended_speed} 倍。`:''}加速可能影响自然度，完成后需试听。`}><input type="number" disabled={!speedLimits.available} min={speedLimits.min??.5} max={speedLimits.max??2} step="0.01" value={speed} onChange={event=>edit(()=>setSpeed(event.target.value))}/></FormField>
          </>}
          {Boolean(speedLimits.languages?.length)?<FormField label="重新配音的语言" hint="当前选择继承原配音的实际语言。请按台词明确选择，系统不会自动改语言。"><select value={language} onChange={event=>edit(()=>setLanguage(event.target.value))}><option value="">请选择配音语言</option>{speedLimits.languages!.map(value=><option key={value} value={value}>{value}</option>)}</select></FormField>:<p className="form-hint">当前配音服务不接收独立的语言选项；将保留该服务原有的语言识别方式。</p>}
          {dialogue&&/[a-z]{3,}/i.test(dialogue.text)&&dialogue.language==='ZH'&&speedLimits.languages?.includes('EN')?<p className="inline-notice warning">原台词包含英文，原配音实际语言是 ZH。需要英文配音时，请在上方明确选择 EN；不会暗中替你更改。</p>:null}
        </>}
      </fieldset>:null}
      {action&&!result?<div className="studio-actions"><button type="button" className="button button-secondary" disabled={disabled||busy||applyUncertain||!actionCapability(action).available} onClick={()=>void submit('preview')}>{working==='preview'?'正在核对处理影响…':'预览处理影响'}</button></div>:null}
      {preview&&!result?<div className="audio-duration-preview" role="region" aria-label="待确认的音频处理预览"><strong>{preview.value.summary}</strong><p>将重新处理：{preview.value.affected_nodes.join(' → ')||'以服务端回执为准'}</p>{preview.value.warnings.map((warning,index)=><p className="inline-notice warning" key={index}>{warning}</p>)}<p className="form-hint">尚未开始重新生成。确认将按以上选择调用已配置的服务，云端服务可能产生费用。</p><button type="button" className="button button-primary" disabled={disabled||busy} onClick={()=>void submit('apply')}>{working==='apply'?'正在提交重新处理…':'确认并开始重新处理'}</button></div>:null}
    </>:null}
    {result?<div role="status"><strong>{result.notice||'程序已接收处理。'}</strong><p>真实编排状态：{result.status} · {result.run_id}{result.replayed?' · 已返回原请求结果，未重复创建任务':''}</p><p className="form-hint">实际进度、错误和输出以工作台任务记录为准；生成结束后仍需试听并审核。</p></div>:null}
    {error?<p role="alert" className="inline-notice warning">{error}</p>:null}{refreshError?<p role="alert" className="inline-notice warning">{refreshError}</p>:null}
    <button type="button" className="button button-ghost button-small" disabled={disabled||busy} onClick={refresh}>重新读取时长与处理状态</button>
  </section>;
}
