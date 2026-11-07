import {useEffect,useRef,useState} from 'react';
import {ApiError,request} from '../lib/api';
import type {ShotFramingOverview,ShotFramingResult,ShotFramingUpdate} from '../lib/studio-contract';
import {FormField} from './ui';

export type ShotAspectRatio=ShotFramingUpdate['aspect_ratio'];
type Framing=ShotFramingOverview&Partial<Pick<ShotFramingResult,'updated_shot_ids'|'notice'>>;
type State={busy:boolean;dirty:boolean;requiresNewRun:boolean;ready:boolean};
const ratios:ShotAspectRatio[]=['16:9','9:16','1:1'];
const labels:Record<ShotAspectRatio,string>={'16:9':'16:9 横向','9:16':'9:16 竖向','1:1':'1:1 方形'};
const isAspectRatio=(value:string):value is ShotAspectRatio=>ratios.some(ratio=>ratio===value);

function checkedResponse(value:Framing,projectId:string){
  if(value.project_id!==projectId||!Array.isArray(value.shots)||!Array.isArray(value.supported_aspect_ratios)
    ||value.shots.some(shot=>!shot.id||!Number.isInteger(shot.revision)||typeof shot.editable!=='boolean'||!isAspectRatio(shot.aspect_ratio))){
    throw new Error('镜头画幅响应与当前项目不一致或内容无效，未应用。');
  }
  return value;
}

export function ProjectShotFraming({projectId,disabled=false,onStateChange,onSaved}:{projectId:string;disabled?:boolean;onStateChange?:(state:State)=>void;onSaved?:(ratio:ShotAspectRatio,uniform:boolean)=>void|Promise<void>}){
  const [data,setData]=useState<Framing|null>(null),[selected,setSelected]=useState<string[]>([]),[ratio,setRatio]=useState<ShotAspectRatio|''>('');
  const [loading,setLoading]=useState(true),[saving,setSaving]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[reload,setReload]=useState(0);
  const epoch=useRef(0),callbacks=useRef({onStateChange,onSaved});callbacks.current={onStateChange,onSaved};
  const path=`/api/studio/projects/${encodeURIComponent(projectId)}/shot-framing`;
  const editable=data?.shots.filter(shot=>shot.editable)||[];
  const targets=editable.filter(shot=>selected.includes(shot.id));
  const dirty=Boolean(ratio)&&targets.some(shot=>shot.aspect_ratio!==ratio),busy=loading||saving;
  const requiresNewRun=data?.requires_new_run===true;
  const ready=Boolean(data)&&!error;
  useEffect(()=>{callbacks.current.onStateChange?.({busy,dirty,requiresNewRun,ready});},[busy,dirty,requiresNewRun,ready]);
  useEffect(()=>()=>callbacks.current.onStateChange?.({busy:false,dirty:false,requiresNewRun:false,ready:false}),[]);
  useEffect(()=>{
    const abort=new AbortController(),token=++epoch.current;
    setLoading(true);setSaving(false);setData(null);setSelected([]);setRatio('');
    void request<ShotFramingOverview>(path,{signal:abort.signal}).then(value=>{
      const checked=checkedResponse(value,projectId);if(abort.signal.aborted||epoch.current!==token)return;
      setData(checked);setSelected(checked.shots.filter(shot=>shot.editable).map(shot=>shot.id));
      const current=new Set(checked.shots.filter(shot=>shot.editable).map(shot=>shot.aspect_ratio));
      const only=Array.from(current)[0];setRatio(current.size===1&&isAspectRatio(only)?only:'');
    }).catch(e=>{if(!abort.signal.aborted&&epoch.current===token)setError('无法读取实际镜头画幅：'+String(e));})
      .finally(()=>{if(!abort.signal.aborted&&epoch.current===token)setLoading(false);});
    return()=>{abort.abort();epoch.current++;};
  },[path,projectId,reload]);
  function refresh(){setError('');setNotice('');setLoading(true);setReload(value=>value+1);}
  async function save(){
    if(!data||!ratio||!dirty||busy||disabled||!targets.length)return;
    const token=epoch.current;setSaving(true);setError('');setNotice('');
    const payload:ShotFramingUpdate={aspect_ratio:ratio,shots:targets.map(shot=>({id:shot.id,expected_revision:shot.revision}))};
    try{
      const value=checkedResponse(await request<ShotFramingResult>(path,{method:'PUT',body:JSON.stringify(payload)}),projectId);
      if(epoch.current!==token)return;
      setData(value);setSelected(previous=>previous.filter(id=>value.shots.some(shot=>shot.id===id&&shot.editable)));
      setNotice(`实际镜头画幅已保存为 ${ratio}；未启动生成。已批准的视频保留，后续新视频需重新审核。`);
      await callbacks.current.onSaved?.(ratio,value.shots.length>0&&value.shots.every(shot=>shot.aspect_ratio===ratio));
    }catch(e){
      if(epoch.current!==token)return;
      if(e instanceof ApiError&&e.status===409){
        setError('镜头版本或任务状态已变化，未覆盖新数据。已重新读取，请重新选择镜头和画幅后保存。');setLoading(true);setReload(value=>value+1);
      }else setError('实际镜头画幅未保存：'+String(e));
    }finally{if(epoch.current===token)setSaving(false);}
  }
  return <section className="project-shot-framing" aria-label="实际镜头画幅">
    <h3>实际镜头画幅</h3><p className="form-hint">修改选中镜头后续生成使用的真实横竖画幅，历史素材和视频保留。已有批准视频不会因此变成新画幅，新生成的视频需要重新审核。</p>
    {loading?<p role="status">正在读取实际镜头画幅…</p>:null}
    {data?<><fieldset disabled={disabled||busy}><legend>选择需要修改画幅的镜头</legend>
      <label className="studio-check"><input type="checkbox" checked={editable.length>0&&targets.length===editable.length} disabled={!editable.length} onChange={event=>setSelected(event.target.checked?editable.map(shot=>shot.id):[])}/><span>选择全部可编辑镜头</span></label>
      {data.shots.map(shot=><div key={shot.id}><label className="studio-check"><input type="checkbox" aria-label={`选择镜头 #${shot.order} · ${shot.title}`} disabled={!shot.editable} checked={selected.includes(shot.id)} onChange={event=>setSelected(previous=>event.target.checked?[...previous,shot.id]:previous.filter(id=>id!==shot.id))}/><span>#{shot.order} · {shot.title} · 当前 {shot.aspect_ratio}</span></label>{!shot.editable?<p className="form-hint">{shot.block_reason||'已有活动任务，当前不可修改画幅。'}</p>:null}</div>)}
    </fieldset><FormField label="选中镜头的实际画幅"><select value={ratio} disabled={disabled||busy||!targets.length} onChange={event=>setRatio(event.target.value as ShotAspectRatio)}><option value="" disabled>请选择实际画幅</option>{ratios.filter(value=>data.supported_aspect_ratios.includes(value)).map(value=><option key={value} value={value}>{labels[value]}</option>)}</select></FormField>
    {!data.shots.length?<p className="form-hint">当前有效剧本尚无镜头。</p>:null}</>:null}
    <div className="toolbar"><button type="button" className="button button-secondary" disabled={disabled||busy||!dirty} onClick={()=>void save()}>{saving?'正在保存实际镜头画幅…':'保存选中镜头画幅'}</button><button type="button" className="button button-ghost" disabled={disabled||busy} onClick={refresh}>重新读取镜头画幅</button></div>
    {dirty?<p role="status">实际镜头画幅尚未保存，请先保存或重新读取以放弃草稿，再开始制作。</p>:null}
    {requiresNewRun?<p role="status" className="inline-notice warning">{data?.notice||'实际镜头画幅与旧编排冻结的输入不同。'}请点击「开始／增量续作」或「按当前设置新建编排」，旧编排不能通过「继续制作」应用新画幅。</p>:null}
    {error?<p role="alert" className="inline-notice warning">{error}</p>:null}{notice?<p role="status">{notice}</p>:null}
  </section>;
}
