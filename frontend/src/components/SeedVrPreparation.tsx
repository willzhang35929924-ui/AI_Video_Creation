import { useEffect, useRef, useState } from 'react';
import { request } from '../lib/api';
import type { QualityRenderDTO, QualitySettings } from '../lib/quality-contract';
import { normalizedQuality, qualitySubmissionSupported } from './QualitySettings';
import { FormField } from './ui';

type Row = Record<string, any>;
type SourceVideo = {id:string;shot_id:string;generation_job_id?:string;relative_path?:string;revision?:number;created_at?:string;metadata_json?:Row};
type Shot = {id:string;order?:number;title?:string};
const activeStates=new Set(['queued','running','deferred','submitting','submitted']);
const stateNames:Record<string,string>={queued:'排队中',running:'正在处理完整视频',deferred:'等待本机空闲',submitting:'正在提交',submitted:'已提交',generated:'真实处理已完成',failed:'验证失败',unknown:'结果待核对',closed_unknown:'结果待核对',cancelled:'已取消'};

export function SeedVrPreparation({projectId,settings,capabilities,shots=[],disabled=false,onVerified}:{projectId:string;settings:QualitySettings;capabilities:Row|null;shots?:Shot[];disabled?:boolean;onVerified:()=>Promise<void>}) {
  const q=normalizedQuality(settings);
  const supported=qualitySubmissionSupported({...q,processing:'seedvr2'},capabilities);
  const canValidate=capabilities?.seedvr2?.state==='verified'&&!supported&&['720p','1080p'].includes(q.resolution);
  const [videos,setVideos]=useState<SourceVideo[]>([]),[sourceId,setSourceId]=useState('');
  const [loading,setLoading]=useState(false),[posting,setPosting]=useState(false),[error,setError]=useState(''),[pollError,setPollError]=useState('');
  const [job,setJob]=useState<QualityRenderDTO|null>(null),[frozen,setFrozen]=useState<Required<QualitySettings>|null>(null);
  const [sourceRefresh,setSourceRefresh]=useState(0);
  const projectRef=useRef(projectId),epoch=useRef(0),postingRef=useRef(false);
  projectRef.current=projectId;
  const shotNames=new Map(shots.map(shot=>[shot.id,shot]));
  const [ratioWidth,ratioHeight]=q.aspect_ratio.split(':').map(Number);
  const matchingSources=videos.filter(video=>{
    const meta=video.metadata_json||{},width=Number(meta.width),height=Number(meta.height);
    return !(width>0&&height>0)||width*ratioHeight===height*ratioWidth;
  }).sort((left,right)=>(shotNames.get(right.shot_id)?.order||0)-(shotNames.get(left.shot_id)?.order||0)||(right.revision||0)-(left.revision||0)||(right.created_at||'').localeCompare(left.created_at||''));
  const selectedSource=matchingSources.find(video=>video.id===sourceId)||matchingSources[0];
  const uncertain=job?.status==='unknown'||job?.status==='closed_unknown';
  const active=Boolean(job&&activeStates.has(job.status));
  const native=job?.snapshot?.native_execution;
  const resumable=Boolean(job&&['failed','unknown'].includes(job.status)&&native?.inference_submitted===true&&native?.prompt_id&&!['error','failed','cancelled','canceled'].includes(native?.terminal_status?.status_str));

  useEffect(()=>{
    epoch.current++;postingRef.current=false;setPosting(false);setJob(null);setFrozen(null);setError('');setPollError('');setVideos([]);setSourceId('');
  },[projectId]);

  useEffect(()=>{
    if(!canValidate)return;
    let alive=true;setLoading(true);setError('');
    void request<SourceVideo[]>(`/api/projects/${encodeURIComponent(projectId)}/videos`).then(result=>{
      if(!alive)return;
      setVideos(Array.isArray(result)?result.filter(video=>video.generation_job_id&&video.relative_path&&!video.metadata_json?.quality_render_id&&!video.metadata_json?.processing_kind&&video.metadata_json?.playable_video!==false):[]);
    }).catch(reason=>{if(alive)setError('读取验证源视频失败：'+String(reason));}).finally(()=>{if(alive)setLoading(false);});
    return()=>{alive=false;};
  },[projectId,canValidate,sourceRefresh]);

  useEffect(()=>{
    if(!job||!activeStates.has(job.status))return;
    let alive=true;let timer:ReturnType<typeof setTimeout>|undefined;
    const identifier=job.id;
    const poll=async()=>{
      try{
        const result=await request<QualityRenderDTO>(`/api/quality/renders/${encodeURIComponent(identifier)}`);
        if(!alive)return;
        if(result.id!==identifier)throw new Error('验证任务响应不匹配，保留原任务。');
        setJob(result);setPollError('');
        if(result.status==='generated')await onVerified();
        else if(activeStates.has(result.status))timer=setTimeout(()=>void poll(),1500);
      }catch(reason){if(alive){setPollError('暂时无法读取验证结果；保留原任务并继续查询，未重新提交。'+String(reason));timer=setTimeout(()=>void poll(),3000);}}
    };
    timer=setTimeout(()=>void poll(),1500);
    return()=>{alive=false;if(timer)clearTimeout(timer);};
  },[projectId,job?.id,job?.status,onVerified]);

  async function start(){
    if(!canValidate||!selectedSource||disabled||postingRef.current||active||uncertain||resumable)return;
    const target=projectId,token=epoch.current,config={...q,processing:'seedvr2' as const},source=selectedSource.id;
    postingRef.current=true;setPosting(true);setError('');setPollError('');
    try{
      const result=await request<QualityRenderDTO>('/api/quality/seedvr2/load-test',{method:'POST',body:JSON.stringify({video_id:source,settings:config})});
      if(projectRef.current!==target||epoch.current!==token)return;
      if(!result.id||result.source_video_id!==source)throw new Error('验证请求已返回，但来源不匹配；请核对原任务，未自动重提。');
      setFrozen(config);setJob(result);
      if(result.status==='generated')await onVerified();
    }catch(reason){if(projectRef.current===target&&epoch.current===token)setError(String(reason));}
    finally{if(projectRef.current===target&&epoch.current===token){postingRef.current=false;setPosting(false);}}
  }

  async function resume(){
    if(!job||!resumable||disabled||postingRef.current||active)return;
    const target=projectId,token=epoch.current,identifier=job.id,originalSource=job.source_video_id;
    postingRef.current=true;setPosting(true);setError('');setPollError('');
    try{
      const result=await request<QualityRenderDTO>(`/api/quality/renders/${encodeURIComponent(identifier)}/resume-native`,{method:'POST',body:'{}'});
      if(projectRef.current!==target||epoch.current!==token)return;
      if(result.id!==identifier||result.source_video_id!==originalSource)throw new Error('取回响应与原任务不匹配；原记录保留，未提交新推理。');
      setJob(result);
      if(result.status==='generated')await onVerified();
    }catch(reason){if(projectRef.current===target&&epoch.current===token)setError(String(reason));}
    finally{if(projectRef.current===target&&epoch.current===token){postingRef.current=false;setPosting(false);}}
  }

  if(!canValidate&&!job)return null;
  const actual=job?.result?.final_actual;
  return <section aria-label="SeedVR2 当前规格验证" className="studio-seedvr-preparation">
    <p>{supported?'当前规格已具备 SeedVR2 完整视频验证，可在上方选择对应处理方式。':<>当前规格尚无 SeedVR2 完整视频验证。可使用本项目原片实测 {q.resolution} · {q.aspect_ratio}，验证通过后开放对应选项。</>}原片保留。</p>
    {canValidate?<><FormField label="SeedVR2 验证源视频"><select value={selectedSource?.id||''} onChange={event=>setSourceId(event.target.value)} disabled={disabled||loading||posting||active||Boolean(uncertain)||resumable}>
      {!matchingSources.length?<option value="">没有匹配当前画幅的分镜原片</option>:matchingSources.map(video=>{const shot=shotNames.get(video.shot_id);return <option key={video.id} value={video.id}>第 {shot?.order||'?'} 镜 · {shot?.title||video.shot_id} · 原片 v{video.revision||1}</option>;})}
    </select></FormField><div className="studio-actions"><button type="button" className="button button-secondary" disabled={disabled||loading||posting||active||Boolean(uncertain)||resumable||!selectedSource} onClick={()=>void start()}>{posting&&!resumable?'正在创建验证任务…':'验证当前规格的 SeedVR2'}</button><button type="button" className="button button-ghost button-small" disabled={disabled||loading||posting||active} onClick={()=>setSourceRefresh(value=>value+1)}>刷新验证原片</button></div>{!loading&&!matchingSources.length?<p className="form-hint">先生成至少一条匹配当前画幅的真实分镜视频，再刷新原片。</p>:null}</>:null}
    {loading?<p role="status">读取本项目原片…</p>:null}
    {job?<p role="status">SeedVR2 验证：{stateNames[job.status]||job.status} · {frozen?.resolution} · {frozen?.aspect_ratio}。{job.status==='generated'?<>实际输出 {actual?.width??'—'}×{actual?.height??'—'} · {actual?.frames??'—'} 帧。请在上方选择已开放的 SeedVR2，再保存并应用。</>:uncertain?'请先核对该任务结果，不会自动重提。':'以后台真实状态为准。'}<small> 验证任务 {job.id}</small></p>:null}
    {resumable?<button type="button" className="button button-secondary" style={{whiteSpace:'normal'}} disabled={disabled||posting||active} onClick={()=>void resume()}>{posting?'正在继续取回原任务结果…':'继续取回已提交的 SeedVR2 结果'}</button>:null}
    {job?.error_message?<p role="alert" className="form-hint">{job.error_message}</p>:null}
    {error?<p role="alert" className="form-hint">{error}</p>:null}
    {pollError?<p role="status" className="form-hint">{pollError}</p>:null}
  </section>;
}
