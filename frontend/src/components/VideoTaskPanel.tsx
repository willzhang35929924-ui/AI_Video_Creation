import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, request } from '../lib/api';
import { StudioStatus } from '../lib/studio-ui';
import type { StudioShot } from '../lib/studio-contract';
import { FormField } from './ui';
import { QualityMediaReport } from './QualitySettings';
type Row=Record<string,any>;

function JobWorkflow({job}:{job:Row}) {
  const parameters=job.parameters_json||{}, workflow=parameters.h3_workflow;
  const cloud=parameters.service_snapshot?.is_cloud===true||String(job.provider||'').startsWith('cloud_');
  const label=({high:'高质量',standard:'标准',fast:'Fast'} as Row)[workflow?.quality_mode];
  const compiled=Boolean(parameters.context_ir_execution?.compiler_receipt&&parameters.context_ir_execution?.compiled_prompt_sha256);
  return <>
    <p>{cloud?'云端采样参数以供应商记录为准 · ':<>
      冻结 seed {job.seed??'未记录'} · 采样 {workflow?.actual_steps??parameters.steps??'未记录'} 步 · </>}
      请求尺寸 {parameters.width??'—'}×{parameters.height??'—'}</p>
    {!cloud&&workflow?.workflow_filename?<p>任务工作流 {label?`${label} · `:''}{workflow.workflow_filename}</p>:null}
    {!cloud&&workflow?.context_ir_enabled===true?<p>Context-IR 已启用 · {workflow.context_ir_version||'版本未记录'} · {compiled?'已记录编译回执':'尚无编译回执'}</p>:null}
  </>;
}

export function VideoTaskPanel({projectId,shots}:{projectId:string;shots:StudioShot[]}) {
  const [jobs,setJobs]=useState<Row[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(''),[preview,setPreview]=useState<Row|null>(null);
  const [videos,setVideos]=useState<Row[]>([]);
  const [revision,setRevision]=useState(0),[previewShot,setPreviewShot]=useState('');
  useEffect(()=>{let alive=true,inflight=false;
    setJobs([]);setVideos([]);setPreview(null);setPreviewShot('');
    async function load(){if(inflight)return;inflight=true;try{const [rows,outputs]=await Promise.all([request<Row[]>(`/api/projects/${projectId}/jobs`),request<Row[]>(`/api/projects/${projectId}/videos`)]);if(alive){setJobs(rows);setVideos(outputs);setError('');}}catch(e){if(alive)setError(String(e));}finally{inflight=false;}}
    void load();const timer=window.setInterval(()=>void load(),4000);return()=>{alive=false;window.clearInterval(timer);};
  },[projectId,revision]);
  async function action(id:string,kind:'cancel'|'retry') {
    setBusy(id);setError('');try{await request(`/api/jobs/${id}/${kind}`,{method:'POST',body:'{}'});setRevision(n=>n+1);}catch(e){setError(String(e));}finally{setBusy('');}
  }
  async function showPrompt(id:string){setBusy('preview');setError('');try{setPreview(await request<Row>(`/api/shots/${id}/prompt-preview`));setPreviewShot(id);}catch(e){setError(String(e));}finally{setBusy('');}}
  return <section className="card studio-panel" id="tasks"><h2>实际生成任务与输入</h2><p>生成步骤由制作质量模式映射。seed、采样步数、参考顺序和工作流以任务冻结快照为准；模型服务与制作画质可在系统设置中调整。</p>
    {error?<p role="alert" className="inline-notice warning">{error}</p>:null}
    <div className="studio-actions"><FormField label="查看镜头完整输入"><select value={previewShot} onChange={e=>{setPreviewShot(e.target.value);if(e.target.value)void showPrompt(e.target.value);}} disabled={Boolean(busy)}><option value="">选择镜头预览</option>{shots.map(shot=><option value={shot.id} key={shot.id}>#{shot.order} · {shot.title}</option>)}</select></FormField><button type="button" className="button button-secondary" disabled={Boolean(busy)} onClick={()=>setRevision(n=>n+1)}>刷新任务历史</button></div>
    {preview?<details open className="workspace-details"><summary>完整提示词与实际参考映射</summary><div className="reference-preview">{(preview.assets||[]).map((asset:Row,index:number)=><figure key={asset.id||asset.asset_id||index}><img src={api.mediaUrl(asset.relative_path||asset.media_url)} alt={`参考 ${index+1} · ${asset.character_id||asset.asset_type||asset.role||'场景或关键画面'}`}/><figcaption>{preview.reference_order?.[index]||`图片 ${index+1}`} · {asset.character_id||asset.asset_type||asset.role} · {asset.character_version_id}</figcaption></figure>)}</div><pre className="source-blocks">{preview.prompt}</pre><details><summary>参与者、角色版本与来源哈希</summary><pre className="source-blocks">{JSON.stringify({participants:preview.participants,assets:preview.assets,cinematic:preview.cinematic},null,2)}</pre></details><p>以上为服务端编译的当前输入；已提交任务的依据以下方各自快照为准。</p></details>:null}
    {jobs.length?<div className="job-list">{[...jobs].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))).map(job=>{const parameters=job.parameters_json||{},shot=shots.find(item=>item.id===job.shot_id),outputs=videos.filter(video=>video.generation_job_id===job.id);return <article className="job-row" key={job.id} id={`job-${job.id}`}><div className="job-main"><strong>{shot?`#${shot.order} ${shot.title}`:job.shot_id}</strong><StudioStatus status={job.status}/><p>{job.id} · {job.provider} · Provider ID {job.provider_job_id||'尚未接收'}</p><JobWorkflow job={job}/>{job.error_message?<p role="alert">{job.error_code} · {job.error_message}</p>:null}<details className="workspace-details production-history-outputs"><summary>此任务实际输出与保留版本 · {outputs.length}</summary><p>此处仅播放和下载，不更改审片决定，也不自动采用新结果。</p>{outputs.length?outputs.map(video=><article key={video.id} className="production-history-version"><strong>v{video.revision} · {video.metadata_json?.quality_render_id?'独立后处理版本':'模型原始输出'}</strong><small>{video.id}</small><video controls preload="none" src={api.mediaUrl(video.relative_path)} data-testid="production-history-video"/><a href={api.mediaUrl(video.relative_path)} download>下载此实际版本</a><QualityMediaReport metadata={video.metadata_json}/></article>):<p>本任务尚无已保存的实际视频文件。</p>}</details><details><summary>任务提示词、参考与复现快照</summary><pre className="source-blocks">{JSON.stringify({prompt:job.prompt,workflow:job.workflow_version,seed:job.seed,parameters,references:job.reference_assets_json},null,2)}</pre></details></div><div className="job-actions">{['queued','running'].includes(job.status)?(job.provider==='cloud_lk888_h3_v2'?<small>零度任务提交后不可取消</small>:<button type="button" className="button button-secondary button-small" disabled={Boolean(busy)} onClick={()=>void action(job.id,'cancel')}>取消此实际任务</button>):null}{['failed','cancelled'].includes(job.status)?<button type="button" className="button button-secondary button-small" disabled={Boolean(busy)} onClick={()=>void action(job.id,'retry')}>{job.error_code==='revision_postprocess_failed'?'仅重试配音与画质后处理':'保留旧记录并新任务重试'}</button>:null}{job.status==='unknown'?<p>结果不明，先核对原服务请求；不会自动重提。</p>:null}<Link to={`/review?project=${projectId}&shot=${job.shot_id}`}>审片／修改此镜</Link></div></article>;})}</div>:<p>此项目尚无底层视频生成任务。</p>}
  </section>;
}
