import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, FileUp, RefreshCw, Plus, Play, Pause, FolderOpen, Wrench } from 'lucide-react';
import { ApiError, api, request } from '../lib/api';
import type { PipelineStart, StudioOverview } from '../lib/studio-contract';
import { PageHeader, EmptyState, LoadingState, FormField } from '../components/ui';
import { useStudio, StudioProjectPicker, StudioStatus, recordText, shotTime, studioAssetPath, studioVideoDuration } from '../lib/studio-ui';
import { WordImportPanel } from '../components/WordImportPanel';
import { ProjectProductPreparation } from '../components/ProjectProductPreparation';
import { ProjectProductionModels } from '../components/ProjectProductionModels';
import { ProjectAudioPolicy } from '../components/ProjectAudioPolicy';
import { ProjectVoicePreparation } from '../components/ProjectVoicePreparation';
import { ProjectCharacterReferences } from '../components/ProjectCharacterReferences';
import { QualitySettingsFields, QualityPlan, useQualityProfile } from '../components/QualitySettings';
import { SeedVrPreparation } from '../components/SeedVrPreparation';
import type { QualitySettings } from '../lib/quality-contract';
import '../studio.css';
import '../asset-library.css';

const nodeLabels: Record<string, string> = { character_reference: '人物参考素材', character: '人物参考素材', scene_reference: '场景参考', scene: '场景参考', keyframe: '镜头关键画面', video: '分镜视频', tts: '角色配音', tts_optional: '可选配音', audio_mix: '对白混音', lip_sync: '口型同步', quality: '画质处理', quality_check: '视频技术检查', parse: 'Word 解析' };
const repairStatusLabels:Record<string,string>={applied:'已执行',waiting:'等待',blocked:'受阻',skipped:'未执行',not_needed:'无需修复',unknown:'结果待核对',error:'诊断错误'};
const gpuReasonLabels:Record<string,string>={health_busy:'本地模型正占用GPU',health_idle:'本地模型空闲',runtime_port_refused:'本地模型服务未运行',health_unreachable_and_no_managed_runtime_process:'健康检查超时，但进程登记确认本地模型未运行',health_unreachable_while_managed_runtime_process_is_alive:'健康检查超时且受管模型进程仍在运行',health_unreachable_and_runtime_process_state_unverified:'健康检查超时，模型进程状态无法确认',unexpected_runtime_identity_or_health_schema:'本地模型服务身份或健康数据异常'};
type VoicePreparationState = { ready:boolean;dirty:boolean;loading:boolean;busy:boolean;blockingReasons:string[] };
type StudioRepairReport = { run_status:string; gpu_gate:{state:'idle'|'busy'|'unknown';reason:string}; actions:{code:string;status:string;message:string;visual_id?:string;provider_job_id?:string|null;references?:{shot_id:string;reference_count:number;roles:Record<string,number>}[]}[]; blockers:{node_id?:string;kind?:string;status?:string;code?:string;message:string}[] };
export function StudioPage() {
  const studio = useStudio();
  const { data, projectId, projects, loading } = studio;
  const quality=useQualityProfile(projectId?'project':'system',projectId||'default');
  const [file, setFile] = useState<File | null>(null), [cloudCosts, setCloudCosts] = useState(false),[paidFallback,setPaidFallback]=useState(false);
  const [fileInputKey,setFileInputKey] = useState(0);
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [repairReport,setRepairReport]=useState<StudioRepairReport|null>(null);
  const [repairError,setRepairError]=useState('');
  const [newProject, setNewProject] = useState(false), [name, setName] = useState('');
  const [newRevision, setNewRevision] = useState(false);
  const [pendingImport, setPendingImport] = useState<{id:string;projectId:string;allowCosts:boolean;allowPaidFallback:boolean;quality:QualitySettings;qualityRevision:number;issues:string[]}|null>(null);
  const [importReviewBusy,setImportReviewBusy]=useState(false);
  const pendingImportRef=useRef(pendingImport);pendingImportRef.current=pendingImport;
  useEffect(()=>()=>{pendingImportRef.current=null;},[]);
  const pageBusy=Boolean(busy)||importReviewBusy;
  const [voiceState,setVoiceState] = useState<VoicePreparationState & {projectId:string}>({projectId:'',ready:false,dirty:false,loading:true,busy:false,blockingReasons:[]});
  const onVoiceStateChange=useCallback((state:VoicePreparationState)=>setVoiceState({...state,projectId}),[projectId]);
  const voiceReady=voiceState.projectId===projectId&&voiceState.ready&&!voiceState.loading&&!voiceState.dirty&&!voiceState.busy;
  const [productReferenceState,setProductReferenceState]=useState({busy:false,dirty:false,overBudget:false});
  const onProductReferenceStateChange=useCallback((state:{busy:boolean;dirty:boolean;overBudget:boolean})=>setProductReferenceState(state),[]);
  const [modelState,setModelState]=useState({busy:false,dirty:false});
  const onModelStateChange=useCallback((state:{busy:boolean;dirty:boolean})=>setModelState(state),[]);
  const [audioPolicyRevision,setAudioPolicyRevision]=useState(0),[audioPolicyState,setAudioPolicyState]=useState({busy:false,dirty:false});
  const onAudioPolicyStateChange=useCallback((state:{busy:boolean;dirty:boolean})=>setAudioPolicyState(state),[]);
  const [referenceState,setReferenceState]=useState({busy:false,dirty:false});
  const onReferenceStateChange=useCallback((state:{busy:boolean;dirty:boolean})=>setReferenceState(state),[]);
  const voiceUnsettled=(voiceState.projectId===projectId&&(voiceState.busy||voiceState.dirty))||referenceState.busy||referenceState.dirty;
  const preparationUnsettled=voiceUnsettled||audioPolicyState.busy||audioPolicyState.dirty||modelState.busy||modelState.dirty||productReferenceState.busy||productReferenceState.dirty;
  const activeRun=Boolean(data?.run&&!['completed','cancelled','failed','completed_with_errors','waiting_review'].includes(data.run.status));
  const productInputsChanged=Boolean(data?.run?.snapshot_json?.product_references_changed);
  const [focusPreparation,setFocusPreparation]=useState('');
  useEffect(()=>{if(projectId&&focusPreparation===projectId&&data?.project.id===projectId){const target=document.getElementById('project-voice-preparation');if(target){target.scrollIntoView?.({behavior:'smooth',block:'start'});setFocusPreparation('');}}},[projectId,focusPreparation,data?.project.id]);
  useEffect(()=>{if(!importReviewBusy)return;const guard=(event:MouseEvent)=>{if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;const target=event.target instanceof Element?event.target.closest('a[href]'):null;if(target){event.preventDefault();event.stopPropagation();setNotice('正在确认并保存剧本，请稍候再切换页面。');}};document.addEventListener('click',guard,true);return()=>document.removeEventListener('click',guard,true);},[importReviewBusy]);
  useEffect(()=>{
    const busy=referenceState.busy||modelState.busy||audioPolicyState.busy||productReferenceState.busy,dirty=referenceState.dirty||modelState.dirty||audioPolicyState.dirty||productReferenceState.dirty;
    if((!busy&&!dirty)||voiceState.busy||voiceState.dirty)return;
    const unload=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue='';};
    const navigate=(event:MouseEvent)=>{if(event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;const link=event.target instanceof Element?event.target.closest<HTMLAnchorElement>('a[href]'):null;if(!link||link.hasAttribute('download')||(link.target&&link.target!=='_self'))return;const next=new URL(link.href,window.location.href),here=new URL(window.location.href);if(!['http:','https:'].includes(next.protocol)||next.origin===here.origin&&next.pathname===here.pathname&&next.search===here.search)return;if(busy){event.preventDefault();event.stopPropagation();setNotice('制作设置或人物参考图正在保存，请完成后再离开。');}else if(!window.confirm('制作设置或人物参考图有未保存的选择。确定放弃这些草稿并离开吗？')){event.preventDefault();event.stopPropagation();setNotice('已保留未保存的制作准备草稿。');}};
    document.addEventListener('click',navigate,true);window.addEventListener('beforeunload',unload);return()=>{document.removeEventListener('click',navigate,true);window.removeEventListener('beforeunload',unload);};
  },[referenceState,modelState,audioPolicyState,productReferenceState,voiceState.busy,voiceState.dirty]);
  async function perform(label: string, work: () => Promise<unknown>) {
    setBusy(label); setError(''); setNotice('');
    try { await work(); await studio.refreshList(); await studio.refreshAfterChange(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(''); }
  }
  async function importWord(e: FormEvent) {
    e.preventDefault(); if (pageBusy||pendingImport||!file||!quality.profile||quality.loading||!quality.supported) return;
    await perform('import', async () => {
      const form = new FormData(); form.set('file', file); form.set('auto_start', 'false');
      form.set('allow_cloud_costs', String(cloudCosts)); form.set('allow_paid_fallback',String(paidFallback)); if (projectId) form.set('project_id', projectId);
      form.set('new_revision', String(Boolean(projectId)&&newRevision));
      form.set('quality_json',JSON.stringify(quality.settings));
      const result = await request<{ project_id?: string; status: string; import_id:string; issues?:string[] }>('/api/studio/import', { method: 'POST', body: form });
      setFile(null);setFileInputKey(value=>value+1);
      if(result.status==='pending_review') {
        setPendingImport({id:result.import_id,projectId:projectId,allowCosts:cloudCosts,allowPaidFallback:paidFallback,quality:{...quality.settings},qualityRevision:projectId?quality.profile!.revision:0,issues:result.issues||[]});
        setNotice('文档中有需要核对的身份或解析歧义。请在下方修正并确认，再配置角色配音和制作画质。');
      } else {
        setPendingImport(null);if(result.project_id){studio.selectProject(result.project_id);setFocusPreparation(result.project_id);}
        setNotice('Word 已导入，尚未启动制作。请先在「角色配音」上传并保存参考音频，核对画质后点击「开始／增量续作」。');
      }
    });
  }
  async function control(action: 'start' | 'pause' | 'resume' | 'retry', nodeId?: string) {
    await perform(action, async () => {
      if(action==='start'&&(!quality.profile||!quality.supported))throw new Error('请先读取并核对制作画质配置。');
      if(action==='start'){
        if(productReferenceState.overBudget)throw new Error('此镜头产品参考超过模型预算，请在制作设置中明确减少对应图片。');
        if(productReferenceState.busy||productReferenceState.dirty)throw new Error('请先保存或放弃产品制作参考选择。');
        if(modelState.busy||modelState.dirty)throw new Error('请先保存或放弃项目模型选择。');
        if(audioPolicyState.busy||audioPolicyState.dirty)throw new Error('请先保存项目声音方式。');
        if(referenceState.busy||referenceState.dirty)throw new Error('请先保存或放弃角色参考图草稿。');
        if(!voiceReady)throw new Error(voiceState.dirty?'请先保存角色配音设置。':voiceState.blockingReasons.join('；')||'请先完成角色配音准备检查。');
        const voices=await request<{ready_to_start:boolean;blockers:string[]}>(`/api/studio/projects/${projectId}/voices?allow_cloud_costs=${cloudCosts}&allow_paid_fallback=${paidFallback}`);
        if(!voices.ready_to_start)throw new Error(voices.blockers.join('；')||'角色配音条件未完成，请先配置。');
      }
      if(action==='start'&&quality.dirty)await quality.save();
      const payload: PipelineStart | { node_id?: string } = action === 'start' ? { allow_cloud_costs: cloudCosts,allow_paid_fallback:paidFallback,quality:quality.settings } : nodeId ? { node_id: nodeId } : {};
      await request<StudioOverview>(`/api/studio/projects/${projectId}/${action}`, { method: 'POST', body: JSON.stringify(payload) });
      setNotice(action === 'pause' ? '已请求暂停后续任务；正在运行的模型请求以服务实际状态为准。' : '请求已提交，执行结果将在任务列表更新。');
    });
  }
  async function applyQuality(){
    if(!projectId||!data?.run||pageBusy||quality.loading||quality.saving||!quality.supported)return;
    await perform('quality-apply',async()=>{
      const saved=await quality.save();
      const result=await request<{applied:string[];skipped:{shot_id:string;reason:string}[];run_status:string}>(`/api/studio/projects/${encodeURIComponent(projectId)}/quality/apply`,{method:'POST',body:JSON.stringify({expected_profile_revision:saved.revision})});
      const skipped=Array.isArray(result.skipped)?result.skipped:[];
      setNotice(`已将保存的画质应用到 ${result.applied.length} 个未派发镜头。已接收任务与原视频保留。${skipped.length?`其余 ${skipped.length} 镜：${[...new Set(skipped.map(item=>item.reason))].join('；')}`:''}`);
    });
  }
  async function oneClickRepair() {
    if(!projectId)return;
    setRepairReport(null);
    setRepairError('');setError('');setNotice('');setBusy('repair');
    try {
      const report=await request<StudioRepairReport>(`/api/studio/projects/${projectId}/repair`,{method:'POST',body:'{}'});
      setRepairReport(report);
      const repaired=report.actions.some(action=>action.status==='applied');
      setNotice(repaired?'已完成针对性修复；后台保留原任务并继续处理。':'诊断已完成，结果与仍需处理的节点已显示在下方。');
      await studio.refreshList();
      await studio.refreshAfterChange();
    } catch(e) {
      setRepairError(e instanceof ApiError && e.status===404
        ? '当前本地后端尚未加载一键修复接口（HTTP 404）。请在保存该服务密钥的同一 Windows 用户上下文中重启后端，再重新点击。'
        : `一键修复请求失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy('');
    }
  }
  const shots = data?.shots || [], characters = data?.characters || [], scenes = data?.scenes || [];
  const nodes = Array.isArray(data?.run?.nodes_json) ? data.run.nodes_json as Record<string, unknown>[] : [];
  const videoCount = shots.filter(shot => shot.video?.relative_path).length;
  const finishedNodes = nodes.filter(node => ['succeeded', 'completed', 'reused', 'skipped'].includes(recordText(node.status))).length;
  return <div className="studio-workspace">
    <PageHeader eyebrow="WORD → 制作准备 → 真实视频 → 成片" title="项目工作台" description="导入剧本建立人物、场景和分镜，按顺序核对制作设置和角色设置，再开始自动制作。日常审核集中在「审片与成片」。" actions={<button type="button" className="button button-secondary" disabled={preparationUnsettled||pageBusy||Boolean(pendingImport)} onClick={() => setNewProject(value => !value)}><Plus size={16}/>新建项目</button>}/>
    <div className="studio-toolbar"><StudioProjectPicker projects={projects} projectId={projectId} selectedName={data?.project.name} disabled={pageBusy||preparationUnsettled||Boolean(pendingImport)||quality.saving||quality.loading} onChange={studio.selectProject}/><button className="button button-secondary" disabled={pageBusy||preparationUnsettled} onClick={() => { void studio.refreshList(); void studio.refresh(); }}><RefreshCw size={15}/>刷新</button>{projectId ? <Link className="button button-primary" to={`/studio-review?project=${projectId}`}>审片与成片<ArrowRight size={16}/></Link> : null}</div>
    {newProject ? <form className="card studio-panel studio-new-project" onSubmit={e => { e.preventDefault(); if (!name.trim()) return; void perform('create', async () => { const project = await api.createProject({ name: name.trim() }); studio.selectProject(project.id); setName(''); setNewProject(false); }); }}>
      <FormField label="项目名称" required><input value={name} maxLength={200} onChange={e => setName(e.target.value)} required/></FormField><button className="button button-primary" disabled={pageBusy||!name.trim()}>创建项目</button><button type="button" className="button button-secondary" onClick={() => setNewProject(false)}>取消</button>
    </form> : null}
    <section className="card studio-import studio-preparation-step" aria-label="第一步 导入 Word，准备制作"><p className="studio-step-label">第一步</p>
      <div className="studio-import-copy"><FileUp size={28} aria-hidden="true"/><div><h2>导入 Word，准备制作</h2><p>{projectId ? `导入至「${data?.project.name || projects.find(p => p.id === projectId)?.name || '当前项目'}」，相同文档按已有记录复用。` : '未选择项目时，根据剧本自动创建项目。'} 导入后先配置角色参考音频，再确认开始制作。</p></div></div>
      {data?.script?<div className="inline-notice" role="status"><strong>已加载保存的剧本</strong><p>{recordText(data.project.source_filename||data.script.filename||data.script.source_filename||data.script.title)||'已保存的结构化剧本和源文档'} · {scenes.length} 个场景 · {shots.length} 个分镜 · {characters.length} 位角色 · 解析状态：{recordText(data.script.status)||'已保存'}</p><p>已识别角色：{characters.map(character=>recordText(character.display_name||character.name)).join('、')||'暂无角色'}</p><p>已识别场景：{scenes.map(scene=>recordText(scene.name||scene.title)).join('、')||'暂无场景'}</p><p>此项目可直接继续制作，无需重新导入同一份 Word。下面仅用于导入新的文档或修订。</p></div>:null}
      <form onSubmit={importWord}>
        <FormField label="剧本文档（.docx）" hint="请选择 Word 实际保存的 .docx 文件。"><input key={fileInputKey} type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={e => setFile(e.target.files?.[0] || null)} disabled={pageBusy||preparationUnsettled||Boolean(pendingImport)}/></FormField>
        {projectId?<label className="studio-check"><input type="checkbox" checked={newRevision} onChange={e=>setNewRevision(e.target.checked)}/><span>将改动的 Word 保存为本项目新剧本修订。相同文档会复用已有结果，原人物身份、素材和历史视频保留。</span></label>:null}
        <label className="studio-check"><input type="checkbox" checked={cloudCosts} onChange={e => {setCloudCosts(e.target.checked);if(!e.target.checked)setPaidFallback(false);}} disabled={pageBusy||preparationUnsettled}/><span>允许本项目调用已配置的云端生成服务，并承担供应商实际费用。未勾选时仅执行获准的本地任务。</span></label>
        <label className="studio-check"><input type="checkbox" checked={paidFallback} onChange={e=>setPaidFallback(e.target.checked)} disabled={pageBusy||preparationUnsettled||!cloudCosts}/><span>允许本次制作在已保存策略许可时回退到付费云端；仅开启云端调用不会自动授权付费回退。</span></label><div className="studio-import-bottom"><span>导入仅建立制作数据；确认配音与画质后再派发生成任务。</span><button className="button button-primary" disabled={pageBusy||preparationUnsettled||Boolean(pendingImport)||!file||quality.loading||quality.saving||!quality.profile||!quality.supported}><FileUp size={16}/>{busy === 'import' ? '正在解析并创建制作数据…' : '导入并配置制作'}</button></div>
      </form>
    </section>
    <section className="card studio-panel studio-preparation-step" aria-label="第二步 制作设置"><p className="studio-step-label">第二步</p><h2>制作设置</h2><p>选择本次画质，导入后核对角色配音，再开始制作。{projectId?'未覆盖的镜头继承项目设置；已接收任务和旧视频保留原快照。':'新项目使用下方设置，不会更改系统默认。'}</p>
      {projectId?<ProjectProductionModels key={`models:${projectId}`} projectId={projectId} disabled={pageBusy||voiceUnsettled||Boolean(pendingImport)||(activeRun&&data?.run?.status!=='paused')} onStateChange={onModelStateChange} onSaved={studio.refreshAfterChange}/>:null}
      {projectId?<ProjectAudioPolicy key={`audio-policy:${projectId}`} projectId={projectId} onStateChange={onAudioPolicyStateChange} disabled={pageBusy||voiceUnsettled||Boolean(pendingImport)||(activeRun&&data?.run?.status!=='paused')} onSaved={async()=>{setAudioPolicyRevision(value=>value+1);await studio.refreshAfterChange();}}/>:null}
      {quality.loading?<p role="status">读取制作画质设置…</p>:null}{quality.error?<p role="alert" className="inline-notice warning">{quality.error}<button type="button" className="button button-secondary button-small" onClick={()=>void quality.load()}>重新读取画质</button></p>:null}
      {quality.profile?<><p>来源：{quality.profile.inherited_from||'项目独立配置'} · 修订 {quality.profile.revision}</p><QualitySettingsFields value={quality.settings} onChange={quality.setSettings} capabilities={quality.capabilities} capabilityError={quality.capabilityError} onRefreshCapabilities={quality.refreshCapabilities} disabled={pageBusy||quality.saving||quality.loading}/>{projectId?<button type="button" className="button button-secondary" disabled={pageBusy||quality.saving||quality.loading||!quality.supported} onClick={()=>void quality.save().then(()=>setNotice('项目制作画质已保存，后续未覆盖镜头继承此配置。')).catch(()=>{})}>保存项目制作设置</button>:null}{projectId&&data?.run?<button type="button" className="button button-primary" disabled={pageBusy||quality.saving||quality.loading||!quality.supported} onClick={()=>void applyQuality()}>保存并应用到未派发画质任务</button>:null}{projectId?<SeedVrPreparation key={`seedvr:${projectId}`} projectId={projectId} settings={quality.settings} capabilities={quality.capabilities} shots={shots.map(shot=>({id:shot.id,order:shot.order,title:shot.title}))} disabled={pageBusy||quality.saving||quality.loading} onVerified={quality.refreshCapabilities}/>:null}{quality.dirty?<p className="studio-muted">本次选择尚未保存；上传时会随文档原子传入，开始续作时先保存。下方显示已保存方案。</p>:null}<QualityPlan plan={quality.profile.plan}/></>:null}
    </section>
    <section className="card studio-panel studio-preparation-step studio-role-settings" aria-label="第三步 角色设置"><p className="studio-step-label">第三步</p><h2>角色设置</h2><p>为每个角色保存声音与可选的人物参考图。绑定跟随角色 ID，未上传参考图时继续使用剧本描述和已有身份。</p>
    {projectId&&data?.project.id===projectId?<div id="project-voice-preparation"><ProjectVoicePreparation projectId={projectId} refreshKey={`${data?.script?.id||''}:${audioPolicyRevision}`} allowCloudCosts={cloudCosts} allowPaidFallback={paidFallback} disabled={pageBusy||Boolean(pendingImport)||quality.saving||(activeRun&&data.run?.status!=='paused')} onStateChange={onVoiceStateChange} onChanged={studio.refreshAfterChange}/></div>:null}
    {projectId&&data?.project.id===projectId?<ProjectCharacterReferences key={projectId} projectId={projectId} refreshKey={data.script?.id||''} disabled={pageBusy||Boolean(pendingImport)||(activeRun&&data.run?.status!=='paused')} onStateChange={onReferenceStateChange} onChanged={studio.refreshAfterChange}/>:<p className="form-hint">先导入剧本建立角色，再配置声音与人物参考图。</p>}
    {projectId&&data?.project.id===projectId?<ProjectProductPreparation key={`products:${projectId}`} projectId={projectId} shots={shots.map(shot=>({id:shot.id,order:shot.order,title:shot.title}))} disabled={pageBusy||Boolean(pendingImport)||(activeRun&&data?.run?.status!=='paused')} onStateChange={onProductReferenceStateChange} onChanged={studio.refreshAfterChange}/>:null}
    </section>
    {(error || studio.error) ? <div role="alert" className="inline-notice warning">{error || studio.error}<Link to="/settings">检查系统设置</Link></div> : null}
    {notice ? <p className="inline-notice" role="status">{notice}</p> : null}
    {pendingImport?<WordImportPanel key={pendingImport.id} importId={pendingImport.id} studioMode initialProjectId={pendingImport.projectId} onBusyChange={setImportReviewBusy} onClose={()=>setPendingImport(null)} onImported={async id=>{
      const pending=pendingImport;
      if(pendingImportRef.current?.id!==pending.id)return;
      try{await request(`/api/quality/profiles/project/${id}`,{method:'PUT',body:JSON.stringify({settings:pending.quality,expected_revision:pending.qualityRevision})});}
      catch(e){if(pendingImportRef.current?.id===pending.id)setError('剧本已导入，但画质配置未保存，请核对当前项目制作设置：'+String(e));}
      if(pendingImportRef.current?.id!==pending.id)return;
      if(id===projectId)await quality.load();
      if(pendingImportRef.current?.id!==pending.id)return;
      await studio.refreshList();if(id===projectId)await studio.refreshAfterChange();if(pendingImportRef.current?.id!==pending.id)return;studio.selectProject(id);setFocusPreparation(id);setPendingImport(null);setNotice('剧本已确认，尚未启动制作。请先配置角色配音并核对画质，再开始制作。');
    }}/>:null}
    {loading ? <LoadingState label="读取项目与真实制作记录…"/> : null}
    {!projectId ? <section className="studio-projects"><h2>已有项目</h2>{projects.length ? <div className="studio-project-grid">{projects.map(project => <button key={project.id} type="button" className="card studio-project-card" onClick={() => studio.selectProject(project.id)}><FolderOpen size={22}/><strong>{project.name}</strong><StudioStatus status={project.status}/><span>{project.shot_count || 0} 镜 · {project.video_count || 0} 条视频{project.error_count ? ` · ${project.error_count} 项异常` : ''}</span></button>)}</div> : studio.listLoading ? <LoadingState label="读取已有项目…"/> : studio.error ? <p className="studio-muted">项目列表暂未读取成功。恢复连接后刷新即可继续查看已有记录。</p> : <EmptyState title="从第一份剧本开始" description="上传 Word 即可创建项目；已有项目会显示在这里。"/>}</section> : null}
    {data ? <>
      <section className="studio-metrics" aria-label="制作概览"><div><strong>{characters.length}</strong><span>已关联人物</span></div><div><strong>{scenes.length}</strong><span>结构化场景</span></div><div><strong>{shots.length}</strong><span>真实分镜记录</span></div><div><strong>{videoCount} / {shots.length}</strong><span>已有视频文件</span></div></section>
      <section className="card studio-panel"><div className="studio-section-heading"><div><h2>自动制作状态</h2><StudioStatus status={data.run?.status}/></div><div className="studio-actions">
        {!data.run || ['completed','cancelled','failed','completed_with_errors','waiting_review'].includes(data.run.status) || productInputsChanged&&['paused','exceptions'].includes(data.run.status) ? <button className="button button-primary" disabled={pageBusy||preparationUnsettled||productReferenceState.overBudget||!voiceReady||!shots.length||quality.loading||quality.saving||!quality.profile||!quality.supported} onClick={() => void control('start')}><Play size={15}/>开始／增量续作</button> : null}
        {data.run?.status === 'paused'&&!productInputsChanged ? <button className="button button-primary" disabled={pageBusy||preparationUnsettled} onClick={() => void control('resume')}><Play size={15}/>继续制作</button> : null}
        {data.run && !['completed','cancelled','paused','failed','completed_with_errors','waiting_review'].includes(data.run.status) ? <button className="button button-secondary" disabled={pageBusy} onClick={() => void control('pause')}><Pause size={15}/>暂停后续任务</button> : null}
        {data.run ? <button type="button" className="button button-secondary" disabled={pageBusy} onClick={() => void oneClickRepair()}><Wrench size={15}/>{busy==='repair'?'正在诊断并修复…':'一键修复'}</button> : null}
        <Link className="button button-secondary" to={`/studio-review?project=${projectId}`}>进入统一审片</Link></div></div>
        {repairError ? <p className="inline-notice warning" role="alert" aria-live="assertive">{repairError}</p> : null}
        {productInputsChanged?<p className="inline-notice">产品参考已更改。点击「开始／增量续作」使用新参考重新核对受影响镜头；已有视频保留。</p>:null}
        {repairReport ? <div className="inline-notice studio-repair-report" role="status" aria-live="polite"><strong>一键修复诊断结果</strong><p>任务状态：{repairReport.run_status} · GPU门控：{repairReport.gpu_gate.state==='idle'?'空闲':repairReport.gpu_gate.state==='busy'?'占用':'状态未知'}（{gpuReasonLabels[repairReport.gpu_gate.reason]||repairReport.gpu_gate.reason}）</p>{repairReport.actions.map((action,index)=><div key={`${action.code}:${index}`}><p>{repairStatusLabels[action.status]||action.status} · {action.message}{action.provider_job_id?` · 服务任务 ${action.provider_job_id}`:''}</p>{action.references?.map(reference=><p key={reference.shot_id}>第 {shots.find(shot=>shot.id===reference.shot_id)?.order||'?'} 镜：{reference.reference_count} 张原图已完整编译（人物 {reference.roles.identity||0}、场景 {reference.roles.scene||0}、产品 {reference.roles.product||0}），未删除参考图。生成进度以对应关键画面节点为准。</p>)}</div>)}{repairReport.blockers.map((blocker,index)=><p key={`${blocker.node_id||blocker.code}:${index}`}>仍需处理：{nodeLabels[blocker.kind||'']||blocker.kind||'任务'} · {blocker.code||blocker.status} · {blocker.message}</p>)}</div> : null}
        {!voiceReady&&!activeRun?<p className="studio-muted">{voiceState.dirty?'角色配音有未保存设置，请先保存。':voiceState.blockingReasons.join('；')||'正在检查制作前的角色配音配置…'}</p>:null}
        {data.run?.error_message ? <p className="inline-notice warning">{recordText(data.run.error_message)}</p> : null}
        {nodes.length ? <><p className="studio-muted">已结束 {finishedNodes} / {nodes.length} 个任务。以下为后端实际状态，等待参考素材的镜头会自动接续。</p><div className="studio-node-list">{nodes.map((node, index) => <div key={recordText(node.id)||index} className="studio-node"><strong>{nodeLabels[recordText(node.kind)] || recordText(node.kind)}<small>{recordText(node.target_name) || recordText(node.target_id)}</small></strong><StudioStatus status={recordText(node.status)}/><span>{recordText(node.error)}</span>{Number(node.attempts) > 0 ? <small>已尝试 {recordText(node.attempts)} 次</small> : null}</div>)}</div></> : <p className="studio-muted">暂无制作任务。上传 Word 或开始增量续作后，会在这里显示真实依赖与执行结果。</p>}
      </section>
      {Boolean(data.exceptions?.length) ? <section className="card studio-panel studio-exceptions"><h2>需要处理的异常</h2><p>任务失败或条件缺失会保留真实错误。结果不明的服务请求须先核对，避免重复提交。</p>{data.exceptions?.map((item, index) => <article key={item.node_id || index}><div><strong>{recordText(item.message) || recordText(item.error)}</strong><small>{recordText(item.code)}{item.target_id ? ` · ${recordText(item.target_id)}` : ''}</small></div>{item.node_id && ['failed','blocked','waiting_user','waiting_exception','exception'].includes(item.status) ? <button className="button button-secondary button-small" disabled={pageBusy} onClick={() => void control('retry', item.node_id)}>重试此任务</button> : null}<Link to={item.kind?.includes('character') ? `/characters/${item.target_id}?project=${encodeURIComponent(projectId)}` : `/video-production?project=${projectId}`}>查看并处理</Link></article>)}</section> : null}
      <div className="studio-two-column"><section className="card studio-panel"><h2>人物与复用身份</h2>{characters.length ? characters.map((character, index) => <article className="studio-asset-row" key={character.id || character.character_id || index}>{studioAssetPath(character) ? <img src={api.mediaUrl(studioAssetPath(character))} alt={recordText(character.display_name || character.name)}/> : <span className="studio-avatar-placeholder" aria-hidden="true">人</span>}<div><strong>{recordText(character.display_name || character.name) || recordText(character.character_id)}</strong><p>{recordText(character.description || character.identity || character.status)}</p><small>{recordText(character.character_id || character.id)}</small>{character.acceptance?.actor==='automation'&&character.current_approved_version_id!==character.version_id?<p className="studio-muted">自动技术采纳，待视频审片</p>:null}</div><Link to={`/characters/${character.character_id || character.id}?project=${encodeURIComponent(projectId)}`}>查看／修改</Link></article>) : <p>文档尚未建立人物关联。</p>}</section>
      <section className="card studio-panel"><h2>场景与环境素材</h2>{scenes.length ? scenes.map((scene, index) => <article className="studio-asset-row" key={scene.id || scene.scene_id || index}>{studioAssetPath(scene) ? <img src={api.mediaUrl(studioAssetPath(scene))} alt={recordText(scene.name || scene.title)}/> : null}<div><strong>{recordText(scene.name || scene.title || scene.scene_title)}</strong><p>{recordText(scene.description || scene.location || scene.status)}</p><small>{recordText(scene.scene_id || scene.id)}</small></div><Link to={`/video-production?project=${projectId}`}>查看／修改</Link></article>) : <p>文档尚未建立场景记录。</p>}</section></div>
      <section className="card studio-panel"><div className="studio-section-heading"><h2>分镜与实际输出</h2><Link to={`/studio-review?project=${projectId}`}>集中播放与审核 <ArrowRight size={14}/></Link></div>{shots.length ? <div className="studio-shot-table">{shots.map(shot => <article key={shot.id}><strong>#{shot.order} · {shot.title || shot.scene_name}</strong><span>{shot.scene_name} · {shot.cast?.map(person => person.name).join('、') || '未关联人物'}</span><span>{shotTime(studioVideoDuration(shot.video) || shot.duration_seconds)}</span><StudioStatus status={shot.status}/><Link to={`/studio-review?project=${projectId}&shot=${shot.id}`}>{shot.video?.relative_path ? '播放／审片' : '查看状态'}</Link></article>)}</div> : <EmptyState title="还没有分镜" description="上传包含场景与人物动作的 Word 剧本，自动建立分镜和关联。"/>}</section>
      <details className="workspace-details"><summary>项目剧本与高级编辑</summary><p>{recordText(data.script?.raw_text || data.project.description).slice(0,1200) || '可通过下方入口查看源文档、人物和逐镜完整信息。'}</p><div className="studio-actions"><Link to={`/storyboard?project=${projectId}`}>源剧本与分镜</Link><Link to={`/video-production?project=${projectId}`}>视频制作与生成依据</Link><Link to={`/video-production?project=${projectId}`}>生成任务记录</Link><Link to="/content">历史与内容管理</Link></div></details>
    </> : null}
  </div>;
}
