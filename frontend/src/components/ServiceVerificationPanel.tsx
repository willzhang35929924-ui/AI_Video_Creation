import {useEffect,useRef,useState} from 'react';
import {Link,useSearchParams} from 'react-router-dom';
import {api,ApiError,request} from '../lib/api';
import type {ModelServicesOverview,ServiceGenerationTest,UnifiedTask} from '../lib/model-services-contract';
import type {StudioProject} from '../lib/studio-contract';
import {FormField} from './ui';

const taskLabels:Record<UnifiedTask['status'],string>={queued:'等待实际执行',running:'正在实际执行',succeeded:'实际任务成功',failed:'实际任务失败',unknown:'结果待核对',cancelled:'已取消',blocked:'前置条件未满足',skipped:'已跳过'};
const pending=(task:UnifiedTask|null)=>Boolean(task&&['queued','running','unknown'].includes(task.status));
const choiceKey=(service:string,model:string)=>JSON.stringify([service,model]);
const textError=(error:unknown)=>error instanceof Error?error.message:String(error);

export function ServiceVerificationPanel({overview,projects,projectId,onProjectChange,onTask,onBusyChange,onConfigure}:{
  overview:ModelServicesOverview|null;
  projects:readonly Pick<StudioProject,'id'|'name'>[];
  projectId:string;
  onProjectChange:(id:string)=>void;
  onTask?:(task:UnifiedTask)=>void;
  onBusyChange?:(busy:boolean)=>void;
  onConfigure?:()=>void;
}){
  const [params,setParams]=useSearchParams();
  const resumeId=params.get('service_test_task')||'';
  const choices=(overview?.slots.find(item=>item.slot==='dialogue_generation')?.candidates||[])
    .filter(item=>item.provider==='openai_tts'&&item.enabled&&item.model_id.trim());
  const [selected,setSelected]=useState(''),[text,setText]=useState('记录已核对。'),[costs,setCosts]=useState(false);
  const [task,setTask]=useState<UnifiedTask|null>(null),[submitting,setSubmitting]=useState(false),[reading,setReading]=useState(false);
  const [error,setError]=useState(''),[uncertainSubmission,setUncertainSubmission]=useState(false),[restoring,setRestoring]=useState(Boolean(resumeId));
  const alive=useRef(true),submittingRef=useRef(false),readingRef=useRef<number|null>(null),readSequence=useRef(0),taskId=useRef(resumeId),epoch=useRef(0);
  const onTaskRef=useRef(onTask);onTaskRef.current=onTask;
  const candidate=choices.find(item=>choiceKey(item.service_id,item.model_id)===selected)||(!selected?choices[0]:undefined);
  const locked=submitting||restoring||uncertainSubmission||pending(task);
  const canSubmit=Boolean(candidate&&projects.some(project=>project.id===projectId)&&text.trim()&&costs&&!locked&&!reading);
  useEffect(()=>{alive.current=true;readingRef.current=null;return()=>{alive.current=false;epoch.current++;};},[]);
  useEffect(()=>{onBusyChange?.(submitting||uncertainSubmission);return()=>onBusyChange?.(false);},[submitting,uncertainSubmission,onBusyChange]);

  async function readTask(id:string){
    if(!id||readingRef.current!==null)return;
    const token=epoch.current,sequence=++readSequence.current;readingRef.current=sequence;setReading(true);
    try{
      const result=await request<UnifiedTask>(`/api/model-services/tasks/${encodeURIComponent(id)}`);
      if(result.id!==id)throw new Error('任务响应与当前测试ID不符');
      if(alive.current&&token===epoch.current&&taskId.current===id){setTask(result);setError('');setRestoring(false);onTaskRef.current?.(result);}
    }catch(cause){if(alive.current&&token===epoch.current){setError(`读取实际任务失败：${textError(cause)}。未重新提交。`);}}
    finally{if(readingRef.current===sequence){readingRef.current=null;if(alive.current&&token===epoch.current)setReading(false);}}
  }
  useEffect(()=>{
    if(!resumeId||resumeId===task?.id)return;
    epoch.current++;readingRef.current=null;taskId.current=resumeId;setTask(null);setRestoring(true);void readTask(resumeId);
  },[resumeId]);
  useEffect(()=>{
    if(!task||!['queued','running'].includes(task.status))return;
    const id=task.id;const timer=window.setInterval(()=>void readTask(id),3000);
    return()=>window.clearInterval(timer);
  },[task?.id,task?.status]);

  async function submit(event:React.FormEvent){
    event.preventDefault();if(!canSubmit||!candidate||submittingRef.current)return;
    submittingRef.current=true;setSubmitting(true);setError('');setCosts(false);epoch.current++;
    const token=epoch.current;
    const body:ServiceGenerationTest={slot:'dialogue_generation',project_id:projectId,model_id:candidate.model_id,
      inputs:{text:text.trim()},parameters:{},allow_cloud_costs:true};
    try{
      const result=await request<UnifiedTask>(`/api/model-services/services/${encodeURIComponent(candidate.service_id)}/generation-test`,{method:'POST',body:JSON.stringify(body)});
      if(alive.current&&token===epoch.current){
        taskId.current=result.id;setTask(result);setUncertainSubmission(false);onTaskRef.current?.(result);
        setParams(current=>{const next=new URLSearchParams(current);next.set('service_test_task',result.id);return next;},{replace:true});
      }
    }catch(cause){
      if(alive.current&&token===epoch.current){
        const definiteRejection=cause instanceof ApiError&&cause.status>=400&&cause.status<500&&cause.status!==408;
        setUncertainSubmission(!definiteRejection);
        setError(textError(cause)+(definiteRejection?'':'；提交结果无法确认。请先刷新统一任务记录核对，不要再次提交同一测试。'));
      }
    }finally{submittingRef.current=false;if(alive.current&&token===epoch.current)setSubmitting(false);}
  }
  const relative=task?.status==='succeeded'&&typeof task.output?.relative_path==='string'?task.output.relative_path:'';
  return <section className="card studio-panel" aria-label="云端服务实际生成测试"><h2>云端TTS实际生成测试</h2>
    <p>单独检查所选服务能否实际产出音频。提交只创建本次测试任务；不会保存生产路由、自动回退或发布角色声线。</p>
    {!overview?<p role="status">读取实际服务档案…</p>:!choices.length?<p>尚无已启用且已选择模型的云端TTS档案。请先在{onConfigure?<button type="button" onClick={onConfigure}>打开服务档案</button>:<Link to="/settings/ai-services">服务档案</Link>}配置支持的OpenAI TTS服务；连接检查与真实生成分别验证。</p>:<form onSubmit={submit}>
      <div className="workspace-grid" style={{minWidth:0}}><FormField label="本次测试服务与模型"><select style={{width:'100%',maxWidth:'100%',minWidth:0}} disabled={locked} value={candidate?choiceKey(candidate.service_id,candidate.model_id):''} onChange={event=>{setSelected(event.target.value);setCosts(false);}}>{choices.map(item=><option key={choiceKey(item.service_id,item.model_id)} value={choiceKey(item.service_id,item.model_id)}>{item.name} · {item.model_id} · {item.verification==='verified'?'已有真实测试':'尚未验证'}</option>)}</select></FormField>
        <FormField label="本次云测试项目"><select style={{width:'100%',maxWidth:'100%',minWidth:0}} disabled={locked} value={projectId} onChange={event=>{onProjectChange(event.target.value);setCosts(false);}}><option value="">选择实际项目</option>{projects.map(project=><option key={project.id} value={project.id}>{project.name}</option>)}</select></FormField></div>
      <FormField label="本次测试文本"><textarea rows={2} maxLength={2000} disabled={locked} value={text} onChange={event=>{setText(event.target.value);setCosts(false);}}/></FormField>
      <label className="studio-check"><input type="checkbox" checked={costs} disabled={locked} onChange={event=>setCosts(event.target.checked)}/><span>我授权本次云端TTS测试产生供应商费用</span></label>
      <p className="form-hint">只授权这一条明确测试；每次新测试都须重新勾选。费用以供应商实际账单为准。</p>
      {!projects.length?<p>当前没有可选实际项目，请先到<Link to="/">项目工作台</Link>创建或导入。</p>:null}
      <button type="submit" className="button button-primary" disabled={!canSubmit}>{submitting?'正在提交这一次测试…':'提交本次云端TTS测试'}</button>
    </form>}
    {error?<p role="alert">{error}</p>:null}
    {uncertainSubmission?<p role="status">提交结果待核对，未取得可信任务结果。此处已阻止重复提交；请通过上方统一任务记录核查服务端。</p>:null}
    {task||resumeId?<div className="workspace-details" aria-label="本次真实测试任务">
      {task?<><p role="status">{taskLabels[task.status]} · {task.model_id||task.provider}</p><p style={{overflowWrap:'anywhere'}}>实际任务ID：{task.id} · 项目：{task.project_id||'服务端未提供'}</p>
        {task.status==='unknown'?<p>结果未知：仅查询现有任务，不会再次提交或改用其他服务。请先核对供应商结果。</p>:null}
        {task.error?<pre className="source-blocks" role="alert">{JSON.stringify(task.error,null,2)}</pre>:null}
        {relative?<><audio controls preload="metadata" src={api.mediaUrl(relative)} data-testid="service-verification-audio"/><a href={api.mediaUrl(relative)} download>下载本次实际音频</a></>:task.status==='succeeded'?<p>服务端记录成功，但尚未提供可播放路径；请检查实际输出记录。</p>:null}
        <details><summary>本次冻结输入与实际结果</summary><pre className="source-blocks">{JSON.stringify({snapshot:task.snapshot,output:task.output,error:task.error},null,2)}</pre></details></>:<p role="status">读取已有测试任务…</p>}
      <button type="button" className="button button-secondary" disabled={submitting||reading} onClick={()=>void readTask(taskId.current||resumeId)}>{reading?'正在读取实际任务…':'刷新本次任务状态（只读）'}</button>
    </div>:null}
  </section>;
}
