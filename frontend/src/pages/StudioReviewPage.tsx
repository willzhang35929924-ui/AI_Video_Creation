import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Check, CheckCheck, ChevronLeft, ChevronRight, Download, Film, RefreshCw, RotateCcw, X } from 'lucide-react';
import { api, request } from '../lib/api';
import type { ComposeInput, ComposeResult, DecisionInput, StudioShot } from '../lib/studio-contract';
import { EmptyState, FormField, LoadingState, PageHeader } from '../components/ui';
import { useStudio, StudioProjectPicker, StudioStatus, recordText, shotTime, studioVideoDuration } from '../lib/studio-ui';
import '../studio.css';
import { QualityDurationWarning, QualityMediaReport, QualityPostprocess, useQualityProfile } from '../components/QualitySettings';
import { reducesResolution, sourceCanvas, videoResolution } from '../lib/assembly-resolution';

const defaultOptions:ComposeInput={transition:'cut',transition_duration:.5,width:1280,height:720,
  resolution_mode:'source',allow_downscale:false,fps:24,audio_policy:'preserve',subtitles:false};

export function StudioReviewPage() {
  const studio = useStudio();
  const { projectId, data, projects } = studio;
  const projectQuality=useQualityProfile(projectId?'project':'system',projectId||'default');
  const [params, setParams] = useSearchParams();
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [trimStart, setTrimStart] = useState('0'), [trimEnd, setTrimEnd] = useState(''), [position, setPosition] = useState(1), [instruction, setInstruction] = useState('');
  const [promptEnabled,setPromptEnabled]=useState(false),[promptText,setPromptText]=useState(''),[promptLoading,setPromptLoading]=useState(false),[promptReferences,setPromptReferences]=useState<string[]>([]);
  const [options, setOptions] = useState<ComposeInput>(defaultOptions);
  const outputInitialized=useRef(''),outputTouched=useRef(false);
  useEffect(()=>{outputInitialized.current='';outputTouched.current=false;setOptions(defaultOptions);},[projectId]);
  useEffect(()=>{if(!projectId||projectQuality.profile?.scope!=='project'||projectQuality.profile.target_id!==projectId||outputInitialized.current===projectId)return;
    const target=projectQuality.profile.plan.requested||{};
    if(!outputTouched.current)setOptions(old=>({...old,width:Number(target.width)||1280,height:Number(target.height)||720}));
    outputInitialized.current=projectId;
  },[projectId,projectQuality.profile]);
  const [confirmed, setConfirmed] = useState(false);
  const [confirmedDecisions,setConfirmedDecisions]=useState<Record<string,number>|null>(null);
  const player = useRef<HTMLVideoElement | null>(null);
  const [versions,setVersions]=useState<Record<string,any>[]>([]),[versionError,setVersionError]=useState('');
  useEffect(()=>{let alive=true,inflight=false;setVersions([]);setVersionError('');if(!projectId)return;
    async function load(){if(inflight)return;inflight=true;try{const rows=await request<Record<string,any>[]>(`/api/projects/${projectId}/videos`);if(alive){setVersions(rows);setVersionError('');}}catch(e){if(alive)setVersionError(String(e));}finally{inflight=false;}}
    void load();const timer=window.setInterval(()=>void load(),4000);return()=>{alive=false;window.clearInterval(timer);};
  },[projectId]);
  const shots = data?.shots || [];
  const ordered = [...shots].sort((a,b) => (a.decision.position ?? a.order) - (b.decision.position ?? b.order) || a.order-b.order);
  const savedSelected = shots.find(shot => shot.id === params.get('shot')) || ordered[0];
  const selectedVersionId=params.get('video')||savedSelected?.video?.id||'';
  const selectedVideo=selectedVersionId===savedSelected?.video?.id?savedSelected?.video:versions.find(video=>video.id===selectedVersionId&&video.shot_id===savedSelected?.id);
  const selected=savedSelected?{...savedSelected,video:selectedVideo||null}:undefined;
  const availableVersions=savedSelected?[...(savedSelected.video?[savedSelected.video]:[]),...versions.filter(video=>video.shot_id===savedSelected.id&&video.id!==savedSelected.video?.id)]:[];
  const previewingVersion=Boolean(savedSelected&&selectedVersionId!==(savedSelected.video?.id||''));
  const selectedIndex = selected ? ordered.findIndex(shot => shot.id === selected.id) : -1;
  const kept = ordered.filter(shot => shot.decision.action === 'keep'), excluded = ordered.filter(shot => shot.decision.action === 'exclude');
  const sources=kept.map(shot=>videoResolution(shot.video?.metadata_json));
  const followsSource=options.resolution_mode==='source';
  const canvas=followsSource?sourceCanvas(sources):{width:options.width??0,height:options.height??0};
  const downscale=Boolean(!followsSource&&canvas&&reducesResolution(canvas,sources));
  const exportBlocked=downscale&&!options.allow_downscale;
  const finalRenders=(data?.renders||[]).filter(render=>render.kind!=='preview');
  const undecided = shots.filter(shot => !['keep','exclude'].includes(shot.decision.action || 'pending'));
  const decisionFingerprint = shots.map(shot => `${shot.id}:${shot.video?.id}:${shot.decision.revision}:${shot.decision.action}:${shot.decision.position}:${shot.decision.trim_start}:${shot.decision.trim_end}`).join('|');
  const draftKey = selected ? `${selected.id}:${selected.video?.id}:${selected.decision.revision}` : '';
  const promptRequest = useRef<{key:string;abort:AbortController}|null>(null),currentDraftKey=useRef(draftKey);currentDraftKey.current=draftKey;
  const unsavedDecision = Boolean(selected && (
    previewingVersion ||
    Number(trimStart) !== (selected.decision.trim_start || 0) ||
    (trimEnd.trim() ? Number(trimEnd) : null) !== (selected.decision.trim_end ?? null) ||
    position !== (selected.decision.position ?? selected.order) ||
    instruction.trim() !== (selected.decision.instruction || '').trim() || promptEnabled
  ));
  useEffect(() => {
    setTrimStart(previewingVersion?'0':String(selected?.decision.trim_start ?? 0));
    setTrimEnd(previewingVersion||selected?.decision.trim_end == null ? '' : String(selected.decision.trim_end));
    setPosition(selected?.decision.position ?? selected?.order ?? 1); setInstruction(selected?.decision.instruction || '');
    setPromptEnabled(false);setPromptText('');setPromptReferences([]);
    promptRequest.current?.abort.abort();promptRequest.current=null;setPromptLoading(false);
    player.current?.pause();
  // Polling must not overwrite text the user is editing; only saved revision / selected output changes reset the draft.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);
  useEffect(()=>()=>{promptRequest.current?.abort.abort();promptRequest.current=null;},[]);
  useEffect(() => { setConfirmed(false); }, [projectId, decisionFingerprint, options, unsavedDecision]);
  useEffect(()=>setOptions(old=>old.allow_downscale?{...old,allow_downscale:false}:old),[decisionFingerprint]);
  const selectShot = useCallback((shotId: string) => {
    if(shotId===selected?.id)return;
    if(unsavedDecision&&!window.confirm('当前镜头有未保存的版本选择、裁剪或修改说明，仍要丢弃草稿并切换镜头吗？'))return;
    setParams(current => { const next = new URLSearchParams(current); next.set('shot', shotId);next.delete('video');next.delete('quality_render');return next; }, { replace: true });
  }, [setParams,selected?.id,unsavedDecision]);
  const navigate = useCallback((offset: number) => {
    const next = ordered[selectedIndex + offset]; if (next) selectShot(next.id);
  }, [ordered, selectedIndex, selectShot]);
  async function perform(label: string, work: () => Promise<unknown>) {
    setBusy(label); setError(''); setNotice('');
    try { await work(); await studio.refreshAfterChange(); await studio.refreshList(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(''); }
  }
  function decisionPayload(shot: StudioShot, action: DecisionInput['action'], useDraft = true): DecisionInput {
    const start = useDraft ? Number(trimStart) : shot.decision.trim_start || 0;
    const end = useDraft ? (trimEnd.trim() ? Number(trimEnd) : null) : shot.decision.trim_end ?? null;
    const duration = Number(studioVideoDuration(shot.video) || shot.duration_seconds);
    if (action === 'keep' && (!Number.isFinite(start) || start < 0 || start >= duration || (end != null && (!Number.isFinite(end) || end <= start || end > duration+.05)))) throw new Error('裁剪范围应在实际视频内，结束时间必须大于开始时间。');
    return { ...(useDraft && promptEnabled && (action==='modify'||action==='regenerate') ? {prompt_override:promptText} : {}), action, video_id: shot.video?.id ?? null, trim_start: start, trim_end: end, position: useDraft ? position : shot.decision.position ?? shot.order, instruction: useDraft ? instruction.trim() : shot.decision.instruction || '', expected_revision: shot.decision.revision ?? null };
  }
  async function save(action: DecisionInput['action']) {
    if (!selected || busy) return;
    if (action === 'modify' && !instruction.trim()) { setError('请填写希望修改的内容，再提交修改。'); return; }
    if ((action==='modify'||action==='regenerate') && promptEnabled && !promptText.trim()) { setError('自定义提示词不能为空。'); return; }
    await perform(action, async () => {
      await request(`/api/studio/projects/${projectId}/shots/${selected.id}/decision`, { method: 'POST', body: JSON.stringify(decisionPayload(selected, action)) });
      if(action==='regenerate'||action==='modify')setParams(current=>{const next=new URLSearchParams(current);next.delete('video');next.delete('quality_render');return next;},{replace:true});
      setNotice(action === 'regenerate' || action === 'modify' ? '已请求仅重生成此镜头。其他镜头及历史视频保留，结果将以实际任务为准。' : '审片决定已保存，刷新后仍保留。');
    });
  }
  async function loadPrompt() {
    if(!selected || promptLoading)return;
    if(promptEnabled&&!window.confirm('重新读取会替换尚未提交的提示词编辑，继续吗？'))return;
    const pending={key:draftKey,abort:new AbortController()};promptRequest.current=pending;setPromptLoading(true);setError('');
    try { const result=await request<{prompt:string;reference_order?:string[]}>(`/api/shots/${selected.id}/prompt-preview`,{signal:pending.abort.signal});
      if(pending.abort.signal.aborted||currentDraftKey.current!==pending.key)return;setPromptText(result.prompt);setPromptReferences(result.reference_order||[]);setPromptEnabled(true);
    } catch(e){if(!pending.abort.signal.aborted&&currentDraftKey.current===pending.key)setError(String(e));} finally {if(promptRequest.current===pending){promptRequest.current=null;setPromptLoading(false);}}
  }
  useEffect(() => {
    function keyboard(e: KeyboardEvent) {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || busy || !selected) return;
      const target = e.target as HTMLElement;
      if (target.closest('input,textarea,select,video,[contenteditable="true"]')) return;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); navigate(e.key === 'ArrowLeft' ? -1 : 1); }
      if (e.key.toLowerCase() === 'k' && selected.video?.relative_path) { e.preventDefault(); void save('keep'); }
      if (e.key.toLowerCase() === 'x') { e.preventDefault(); void save('exclude'); }
      if (e.code === 'Space' && player.current && !target.closest('button,a,summary')) { e.preventDefault(); if (player.current.paused) void player.current.play().catch(() => setError('浏览器未能播放该文件，请检查视频或下载核对。')); else player.current.pause(); }
    }
    window.addEventListener('keydown', keyboard); return () => window.removeEventListener('keydown', keyboard);
  });
  async function move(shot: StudioShot, nextPosition: number) {
    await perform('order', async () => {
      const payload = decisionPayload(shot, shot.decision.action || 'pending', false); payload.position = nextPosition;
      await request(`/api/studio/projects/${projectId}/shots/${shot.id}/decision`, { method: 'POST', body: JSON.stringify(payload) });
    });
  }
  async function compose(e: React.FormEvent) {
    e.preventDefault(); if (!confirmed || undecided.length || !kept.length || busy || unsavedDecision || exportBlocked || projectQuality.loading || !projectQuality.profile) return;
    await perform('compose', async () => {
      const result = await request<ComposeResult>(`/api/studio/projects/${projectId}/compose`, { method: 'POST', body: JSON.stringify({...options,...canvas,expected_decisions:confirmedDecisions}) });
      setNotice(`已创建独立成片版本 ${result.render_id}，${result.status === 'generated' ? '输出已生成' : '等待实际合成'}。`); setConfirmed(false);
    });
  }
  const keptDuration = kept.reduce((sum, shot) => sum + Math.max(0, (shot.decision.trim_end ?? Number(studioVideoDuration(shot.video) || shot.duration_seconds)) - (shot.decision.trim_start || 0)), 0);
  const estimatedDuration = Math.max(0, keptDuration - (options.transition === 'crossfade' ? Math.max(0,kept.length-1) * (options.transition_duration || 0) : 0));
  return <div className="studio-workspace">
    <PageHeader eyebrow="REVIEW & FINISH" title="审片与成片" description="播放实际生成的视频，一次决定保留、修改或排除。按已保存的顺序、裁剪和声音设置合成独立 MP4 版本。" actions={<Link className="button button-secondary" to={projectId ? `/?project=${projectId}` : '/'}><ArrowLeft size={16}/>项目工作台</Link>}/>
    <div className="studio-toolbar"><StudioProjectPicker projects={projects} projectId={projectId} selectedName={data?.project.name} disabled={Boolean(busy)} onChange={id=>{if(unsavedDecision&&!window.confirm('当前镜头有未保存的审片草稿，仍要切换项目吗？'))return;studio.selectProject(id);}}/><button className="button button-secondary" disabled={Boolean(busy)} onClick={() => { void studio.refresh(); void studio.refreshList(); }}><RefreshCw size={15}/>刷新实际状态</button></div>
    {(error || studio.error || versionError) ? <p role="alert" className="inline-notice warning">{error || studio.error || versionError}</p> : null}{notice ? <p className="inline-notice" role="status">{notice}</p> : null}
    {studio.loading ? <LoadingState label="读取实际分镜视频与已保存的审片决定…"/> : null}
    {!projectId ? <EmptyState title="选择要审片的项目" description="项目制作记录会保留在本机，选择后即可继续审核。" action={<Link className="button button-primary" to="/">上传剧本并制作</Link>}/> : null}
    {data && !shots.length ? <EmptyState title="尚无可审片的分镜" description="回到项目工作台上传剧本，自动建立人物、场景和分镜。" action={<Link className="button button-primary" to={`/?project=${projectId}`}>打开项目工作台</Link>}/> : null}
    {data && shots.length ? <>
      <div className="studio-review-summary"><span><strong>{kept.length}</strong> 保留</span><span><strong>{excluded.length}</strong> 排除</span><span><strong>{undecided.length}</strong> 待决定</span><span className="studio-muted">快捷键：← → 切镜 · 空格播放 · K 保留 · X 排除</span></div>
      <section className="studio-review-layout">
        <aside className="card studio-shot-browser" aria-label="分镜视频列表">{ordered.map(shot => <button type="button" key={shot.id} aria-current={selected?.id === shot.id ? 'true' : undefined} className={`studio-shot-option${selected?.id === shot.id ? ' selected' : ''}`} onClick={() => selectShot(shot.id)}>
          <div className="studio-shot-thumbnail">{shot.video?.relative_path ? <video src={api.mediaUrl(shot.video.relative_path)} preload="metadata" muted aria-hidden="true"/> : <Film size={28} aria-hidden="true"/>}<span>#{shot.order}</span></div><div><strong>{shot.title || shot.scene_name}</strong><small>{shot.scene_name}</small><StudioStatus status={shot.decision.action === 'keep' || shot.decision.action === 'exclude' ? shot.decision.action : shot.status}/><small>{shotTime(studioVideoDuration(shot.video) || shot.duration_seconds)}</small></div>
        </button>)}</aside>
        {selected ? <article className="card studio-review-player">
          <div className="studio-section-heading"><div><h2>#{selected.order} · {selected.title || selected.scene_name}</h2><span className="studio-muted">{selected.scene_name} · {selected.cast?.map(person => person.name).join('、') || '未关联人物'}</span></div><div className="studio-actions"><button type="button" className="button button-secondary button-small" aria-label="上一个镜头" disabled={selectedIndex<=0} onClick={() => navigate(-1)}><ChevronLeft size={16}/></button><span>{selectedIndex+1} / {shots.length}</span><button type="button" className="button button-secondary button-small" aria-label="下一个镜头" disabled={selectedIndex>=shots.length-1} onClick={() => navigate(1)}><ChevronRight size={16}/></button></div></div>
          <FormField label="本镜预览与采用版本"><select value={selectedVersionId} disabled={Boolean(busy)} onChange={e=>{setConfirmed(false);setParams(current=>{const next=new URLSearchParams(current);next.set('video',e.target.value);next.delete('quality_render');return next;},{replace:true});}}>{!availableVersions.some(video=>video.id===selectedVersionId)?<option value={selectedVersionId}>等待读取所选实际版本</option>:null}{availableVersions.map(video=><option key={video.id} value={video.id}>v{video.revision??'当前'} · {video.metadata_json?.quality_render_id?'后处理独立版本':'模型输出'} · {video.metadata_json?.width??'—'}×{video.metadata_json?.height??'—'} · {video.id}</option>)}</select></FormField><p className="studio-muted">选择只改变预览，不改已保存决定。明确点击「保留／保存裁剪」后才采用该版本合成；切换会重置裁剪。已保存版本：{savedSelected?.decision.video_id||savedSelected?.video?.id||'尚无'}。</p>
          {selected.video?.relative_path ? <video key={selected.video.id} ref={player} data-testid="studio-review-video" src={api.mediaUrl(selected.video.relative_path)} controls preload="metadata" onError={() => setError('实际视频加载失败；文件可能缺失或格式不可播放，请在项目异常中检查。')}/> : <div className="studio-video-empty"><Film size={36}/><StudioStatus status={selected.status}/><p>{selectedVersionId?'所选版本尚未读取、不可用或不属于此镜头。':'此镜尚无可播放的视频文件。'}</p><Link to={`/?project=${projectId}`}>查看前置素材与生成任务</Link></div>}
          <div className="studio-player-meta"><StudioStatus status={previewingVersion?'pending':selected.decision.action || 'pending'}/><span>实际时长 {shotTime(studioVideoDuration(selected.video))}</span>{selected.video?.relative_path ? <a href={api.mediaUrl(selected.video.relative_path)} download><Download size={14}/>下载当前预览版本</a> : null}</div>
          <QualityDurationWarning metadata={selected.video?.metadata_json} videoId={selected.video?.id}/>
          <div className="studio-shot-script"><p>{selected.action}</p>{selected.cast?.filter(person=>person.performance_json?.action).map(person=><p key={person.character_id}><strong>{person.name}：</strong>{recordText(person.performance_json.action)}{person.performance_json.emotion?` · ${recordText(person.performance_json.emotion)}`:''}</p>)}{selected.dialogues?.map((line, index) => <p key={line.id || index}><strong>{recordText(line.speaker_name || line.speaker || line.character_name || selected.cast?.find(person=>person.id===line.participant_id)?.name || line.character_id)}：</strong>{recordText(line.text || line.content)}</p>)}</div>
          <div className="studio-trim-fields"><FormField label="裁剪起点（秒）"><input type="number" min="0" step="0.04" value={trimStart} onChange={e => setTrimStart(e.target.value)} disabled={Boolean(busy)||!selected.video}/></FormField><FormField label="裁剪终点（秒）" hint="留空保留完整音画；合成按实际音画时长补齐"><input type="number" min="0" step="0.04" max={studioVideoDuration(selected.video)} value={trimEnd} placeholder={String(studioVideoDuration(selected.video) || '')} onChange={e => setTrimEnd(e.target.value)} disabled={Boolean(busy)||!selected.video}/></FormField><FormField label="时间线位置"><input type="number" min="1" max={shots.length} step="1" value={position} onChange={e => setPosition(Number(e.target.value))} disabled={Boolean(busy)}/></FormField></div>
          <div className="studio-decision-actions"><button className="button button-primary" disabled={Boolean(busy)||!selected.video?.relative_path} onClick={() => void save('keep')}><Check size={17}/>保留／保存裁剪</button><button className="button button-secondary" disabled={Boolean(busy)} onClick={() => void save('exclude')}><X size={17}/>排除此镜</button><button className="button button-secondary" disabled={Boolean(busy)||selected.decision.action==='pending'} onClick={() => void save('pending')}>撤回决定</button></div>
          <details className="studio-modify"><summary>修改此镜头／重生成</summary><FormField label="希望修改的内容" hint="描述动作、表演、服装或环境问题。仅重生成本镜，已有其他镜头继续保留。"><textarea value={instruction} rows={3} onChange={e => setInstruction(e.target.value)} placeholder="例如：人物先停稳再抬头；记录本全程保持合上。"/></FormField><div className="studio-actions"><button type="button" className="button button-secondary" disabled={Boolean(busy)||promptLoading} onClick={()=>void loadPrompt()}>{promptLoading?'读取提示词…':promptEnabled?'重新读取原始提示词':'编辑本次提示词'}</button></div>{promptEnabled?<div><FormField label="本次生成提示词" hint="可以删除或改写文字；这只影响本次生成，不修改原剧本或其他镜头。请保留需要的人物、动作和参考图编号。"><textarea value={promptText} rows={14} onChange={e=>setPromptText(e.target.value)}/></FormField><p role="status">提示词 {[...promptText].length} 字；修改要求 {[...instruction].length} 字。{promptReferences.length?`参考图编号：${promptReferences.join('、')}。`:''}</p><p>本地视频服务没有应用层字符上限；云端按接口限制在提交前检查：零度 H3 为 7000 字，万相 2.6 为提示词 1500 字、负面提示词 500 字；自定义云端接口按服务档案中填写的上限。超限会明确提示，不会自动截断。</p><button type="button" className="button button-ghost" onClick={()=>{setPromptEnabled(false);setPromptText('');}}>取消提示词编辑</button></div>:null}<div className="studio-actions"><button className="button button-secondary" disabled={Boolean(busy)||!instruction.trim()||(promptEnabled&&!promptText.trim())} onClick={() => void save('modify')}>提交修改并重生成此镜</button><button className="button button-secondary" disabled={Boolean(busy)} onClick={() => void save('regenerate')}><RotateCcw size={15}/>按原设定重生成</button></div><p className="studio-muted">使用项目已配置服务；云端任务沿用该项目授权。生成完成后，本镜需要重新审片。</p></details>
          <QualityMediaReport metadata={selected.video?.metadata_json}/>{selected.video?.id?<QualityPostprocess key={'quality:'+selected.video.id} shotId={selected.id} videoId={selected.video.id}/>:null}
          <details className="studio-modify"><summary>实际生成信息与技术检查</summary><pre className="source-blocks">{JSON.stringify({ shot_id:selected.id,video_id:selected.video?.id,model:selected.video?.model,service:selected.video?.service,quality:selected.video?.quality_summary || selected.video?.checks || selected.video?.metadata_json,decision:selected.decision },null,2)}</pre><p>技术检查不等于人物、表演、口型或画面真实感已审核。</p></details>
        </article> : null}
      </section>
      <section className="card studio-panel"><div className="studio-section-heading"><div><h2>最终时间线</h2><p>移动顺序会保存到数据库。排除保留原文件，合成时跳过。</p></div><span>{kept.length} 镜纳入 · 预计 {shotTime(estimatedDuration)}</span></div><ol className="studio-timeline">{ordered.map((shot,index) => <li key={shot.id} className={shot.decision.action==='exclude' ? 'studio-timeline-excluded' : ''}><span className="studio-timeline-position">{index+1}</span><button type="button" className="studio-text-button" onClick={() => selectShot(shot.id)}>#{shot.order} · {shot.title || shot.scene_name}</button><StudioStatus status={shot.decision.action || 'pending'}/><span>{shot.decision.trim_start || 0} → {shot.decision.trim_end ?? studioVideoDuration(shot.video) ?? '结尾'} 秒</span><div className="studio-actions"><button className="button button-secondary button-small" aria-label={`上移镜头 ${shot.order}`} disabled={Boolean(busy)||unsavedDecision||index===0} onClick={() => void move(shot,index)}><ArrowLeft size={14}/></button><button className="button button-secondary button-small" aria-label={`下移镜头 ${shot.order}`} disabled={Boolean(busy)||unsavedDecision||index===ordered.length-1} onClick={() => void move(shot,index+2)}><ArrowRight size={14}/></button></div></li>)}</ol></section>
      <form className="card studio-panel" onSubmit={compose}><h2>一次确认合成成片</h2><p className="studio-muted">默认跟随已保留版本的实际尺寸。混合尺寸时使用可容纳所有原片的画布，保持各镜头比例。</p>{projectQuality.loading?<p role="status">读取项目自定义尺寸…</p>:null}{projectQuality.error?<p role="alert">{projectQuality.error}<button type="button" onClick={()=>void projectQuality.load()}>重新读取项目尺寸</button></p>:null}<div className="studio-output-grid">
        <FormField label="输出尺寸模式"><select value={options.resolution_mode??'source'} onChange={e=>{
          const mode=e.target.value as 'source'|'custom';
          setOptions(old=>({...old,...(mode==='custom'?canvas:undefined),resolution_mode:mode,allow_downscale:false}));
        }}><option value="source">跟随源片</option><option value="custom">自定义分辨率</option></select></FormField>
        <FormField label="转场"><select value={options.transition} onChange={e => setOptions(value => ({...value,transition:e.target.value as 'cut'|'crossfade'}))}><option value="cut">硬切</option><option value="crossfade">交叉淡化</option></select></FormField>
        <FormField label="转场时长（秒）"><input type="number" min="0.1" max="2" step="0.1" disabled={options.transition !== 'crossfade'} value={options.transition_duration} onChange={e => setOptions(value => ({...value,transition_duration:Number(e.target.value)}))}/></FormField>
        <FormField label="输出宽度"><input type="number" min="128" max="3840" step="2" value={canvas?.width??''} disabled={followsSource||projectQuality.loading||!projectQuality.profile} onChange={e => {outputTouched.current=true;setOptions(value => ({...value,width:Number(e.target.value),allow_downscale:false}));}}/></FormField>
        <FormField label="输出高度"><input type="number" min="128" max="3840" step="2" value={canvas?.height??''} disabled={followsSource||projectQuality.loading||!projectQuality.profile} onChange={e => {outputTouched.current=true;setOptions(value => ({...value,height:Number(e.target.value),allow_downscale:false}));}}/></FormField>
        <FormField label="输出帧率"><select value={options.fps} onChange={e => setOptions(value => ({...value,fps:Number(e.target.value) as 24|25|30}))}>{[24,25,30].map(fps => <option key={fps} value={fps}>{fps} fps</option>)}</select></FormField>
        <FormField label="声音处理"><select value={options.audio_policy} onChange={e => setOptions(value => ({...value,audio_policy:e.target.value as 'preserve'|'mute'}))}><option value="preserve">保留原镜头音轨</option><option value="mute">静音成片</option></select></FormField>
      </div><label className="studio-check"><input type="checkbox" checked={options.subtitles} onChange={e => setOptions(value => ({...value,subtitles:e.target.checked}))}/><span>嵌入可选中文字幕轨。按镜头区间使用剧本对白，未做逐句语音对齐。</span></label>
      {!followsSource?<label className="studio-check"><input type="checkbox" checked={options.allow_downscale} onChange={e=>setOptions(old=>({...old,allow_downscale:e.target.checked}))}/><span>允许降低源片分辨率</span></label>:null}
      {downscale?<p role="status" className="inline-notice warning">这个尺寸会缩小已审核的源片。</p>:null}
      <div className="studio-compose-summary"><p>保留 {kept.length} 镜，排除 {excluded.length} 镜，待决定 {undecided.length} 镜。输出 {canvas?`${canvas.width} × ${canvas.height}`:'按实际源片测量'} · {options.fps} fps · H.264 MP4；{options.audio_policy==='preserve' ? '保留原音轨' : '静音'}，{options.transition==='cut' ? '硬切' : `${options.transition_duration}秒交叉淡化`}。</p><p>成片生成独立版本，保留原始视频及历史成片。预计时长仅为剪辑计算，实际结果以合成后检查为准。</p></div>
      {unsavedDecision?<p role="status" className="inline-notice warning">当前镜头有未保存的版本选择、裁剪、顺序或修改说明。请先保存审片决定或提交修改，再确认成片。</p>:null}
      <label className="studio-check"><input type="checkbox" checked={confirmed} disabled={Boolean(undecided.length)||!kept.length||unsavedDecision||exportBlocked||projectQuality.loading||!projectQuality.profile} onChange={e => {setConfirmed(e.target.checked);if(e.target.checked)setConfirmedDecisions(Object.fromEntries(shots.map(shot=>[shot.id,shot.decision.revision??1])));}}/><span>我已审核所有镜头，并确认上述顺序、裁剪和输出设置。</span></label><button className="button button-primary" disabled={Boolean(busy)||!confirmed||Boolean(undecided.length)||!kept.length||unsavedDecision||exportBlocked||projectQuality.loading||!projectQuality.profile}><CheckCheck size={18}/>{busy==='compose' ? '正在提交真实合成…' : '按审核结果合成成片'}</button>{undecided.length ? <p className="studio-muted">还有 {undecided.length} 个镜头未作决定，可保留或排除后合成。</p> : null}
      </form>
      <section className="card studio-panel"><h2>成片版本与导出</h2>{finalRenders.length ? <div className="studio-render-grid">{finalRenders.map(render => <article key={render.id}><div className="studio-section-heading"><strong>{render.snapshot_json?.automatic_draft ? '自动生成草稿（待人工审核） · ' : ''}{render.created_at ? recordText(render.created_at).replace('T',' ').slice(0,19) : '成片版本'}</strong><StudioStatus status={render.status}/></div>{render.relative_path && render.status === 'generated' ? <><video controls preload="metadata" src={api.mediaUrl(render.relative_path)} data-testid="studio-final-video"/><a className="button button-secondary" href={api.mediaUrl(render.relative_path)} download><Download size={16}/>下载真实 MP4</a></> : <p>{render.status === 'failed' ? recordText(render.error_message) || '合成失败，已保留错误记录。' : '等待真实合成输出，完成后可播放与下载。'}</p>}<details><summary>本次合成参数与检查</summary><pre className="source-blocks">{JSON.stringify({render_id:render.id,snapshot:render.snapshot_json,metadata:render.metadata_json},null,2)}</pre></details></article>)}</div> : <p className="studio-muted">还没有成片版本。按审核结果提交合成后，实际输出会显示在这里。</p>}</section>
    </> : null}
  </div>;
}
