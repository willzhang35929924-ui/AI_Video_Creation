import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from '../lib/api';
import type { QualitySettings, QualityProfileDTO, QualityRenderDTO } from '../lib/quality-contract';
import { useSearchParams } from 'react-router-dom';
import { api } from '../lib/api';
import { FormField } from './ui';

type Row = Record<string, any>;
type ModeUse = 'generation' | 'cloud' | 'provider' | 'encoding';
const qualityModes = [
  {value:'high', label:'高质量', steps:30, workflow:'H3 Quality'},
  {value:'standard', label:'标准', steps:20, workflow:'H3 Quality'},
  {value:'fast', label:'Fast', steps:4, workflow:'H3 SMD4 Fast'},
] as const;
export const qualityDefaults: Required<QualitySettings> = {resolution:'720p',mode:'high',aspect_ratio:'16:9',processing:'lanczos'};
export const normalizedQuality = (value?: QualitySettings): Required<QualitySettings> => ({...qualityDefaults,...value});
const outputResolutions = [
  {value:'720p',label:'720p',width:1280,height:720},
  {value:'1080p',label:'1080p',width:1920,height:1080},
  {value:'2k',label:'2K / QHD',width:2560,height:1440},
  {value:'4k',label:'4K UHD',width:3840,height:2160},
] as const;
function outputDimensions(resolution:Required<QualitySettings>['resolution'],aspect:Required<QualitySettings>['aspect_ratio']) {
  const size=outputResolutions.find(item=>item.value===resolution)!;
  return aspect==='9:16'?`${size.height}×${size.width}`:aspect==='1:1'?`${size.height}×${size.height}`:`${size.width}×${size.height}`;
}
export function qualitySubmissionSupported(settings:QualitySettings,capabilities:Row|null){
  const q=normalizedQuality(settings),sr=capabilities?.ai_super_resolution,seed=capabilities?.seedvr2;
  const seedTarget=Array.isArray(seed?.verified_targets)
    ? seed.verified_targets.some((target:Row)=>target.resolution===q.resolution&&target.aspect_ratio===q.aspect_ratio)
    : q.resolution==='720p'&&q.aspect_ratio==='16:9'&&seed?.verified_resolutions?.includes('720p')&&seed?.verified_aspect_ratios?.includes('16:9');
  return q.processing==='lanczos'||Boolean(q.processing==='ai_super_resolution'&&q.resolution==='4k'&&sr?.state==='verified'&&sr?.verified_resolutions?.includes('4k')&&sr?.verified_aspect_ratios?.includes(q.aspect_ratio))||Boolean(q.processing==='seedvr2'&&seed?.state==='verified'&&seedTarget);
}
function useQualityCapabilities(){
  const [capabilities,setCapabilities]=useState<Row|null>(null),[capabilityError,setCapabilityError]=useState('');
  const refreshCapabilities=useCallback(async()=>{try{setCapabilities(await request<Row>('/api/quality/capabilities'));setCapabilityError('');}catch(e){setCapabilities(null);setCapabilityError(String(e));}},[]);
  useEffect(()=>{let alive=true;void request<Row>('/api/quality/capabilities').then(result=>{if(alive){setCapabilities(result);setCapabilityError('');}}).catch(e=>{if(alive)setCapabilityError(String(e));});return()=>{alive=false;};},[]);
  return {capabilities,capabilityError,refreshCapabilities};
}

export function useQualityProfile(scope: 'system'|'project'|'shot', targetId: string) {
  const capability=useQualityCapabilities();
  const [profile,setProfile]=useState<QualityProfileDTO|null>(null);
  const [settings,setSettings]=useState<Required<QualitySettings>>(qualityDefaults);
  const [loading,setLoading]=useState(true),[error,setError]=useState(''),[saving,setSaving]=useState(false);
  const path=`/api/quality/profiles/${scope}/${encodeURIComponent(targetId)}`;
  const pathRef=useRef(path),epoch=useRef(0),mounted=useRef(true),writes=useRef(new Map<string,Promise<QualityProfileDTO>>());
  pathRef.current=path;
  useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;epoch.current++;};},[]);
  const current=(token:number,target:string)=>mounted.current&&epoch.current===token&&pathRef.current===target;
  const matches=(result:QualityProfileDTO)=>result.scope===scope&&result.target_id===targetId;
  const load=useCallback(async()=>{
    const token=++epoch.current;setLoading(true);setError('');
    try {const result=await request<QualityProfileDTO>(path);if(current(token,path)){if(!matches(result))throw new Error('画质配置响应与当前对象不一致，未应用。');setProfile(result);setSettings(normalizedQuality(result.settings));}}
    catch(e){if(current(token,path)){setError(String(e));setProfile(null);}}finally{if(current(token,path))setLoading(false);}
  },[path]);
  useEffect(()=>{setProfile(null);setSaving(false);void load();return()=>{epoch.current++;};},[load]);
  const selectedProfile=profile&&matches(profile)?profile:null;
  const dirty=Boolean(selectedProfile)&&JSON.stringify(settings)!==JSON.stringify(normalizedQuality(selectedProfile!.settings));
  async function save():Promise<QualityProfileDTO> {
    if(!selectedProfile)throw new Error('画质配置尚未读取，不能提交生成。');
    if(!qualitySubmissionSupported(settings,capability.capabilities))throw new Error('当前分辨率或画幅没有已验证的超分能力；请明确选择受支持配置，不会自动降级。');
    const active=writes.current.get(path);if(active)return active;
    const token=++epoch.current;setSaving(true);setError('');
    const work=(async()=>{try {const result=await request<QualityProfileDTO>(path,{method:'PUT',body:JSON.stringify({settings,expected_revision:selectedProfile.revision})});
      if(!current(token,path)||!matches(result))throw new Error('已离开该配置对象，旧保存结果未应用到当前项目。');
      setProfile(result);setSettings(normalizedQuality(result.settings));return result;
    }catch(e){if(current(token,path))setError(String(e));throw e;}finally{writes.current.delete(path);if(current(token,path)){setSaving(false);setLoading(false);}}})();
    writes.current.set(path,work);return work;
  }
  async function inherit() {
    if(scope!=='shot')return;
    const token=++epoch.current;setSaving(true);setError('');
    try{const result=await request<QualityProfileDTO>(path,{method:'DELETE'});if(current(token,path)&&matches(result)){setProfile(result);setSettings(normalizedQuality(result.settings));}}
    catch(e){if(current(token,path))setError(String(e));}finally{if(current(token,path)){setSaving(false);setLoading(false);}}
  }
  return {profile:selectedProfile,settings,setSettings,loading,error,saving,dirty,load,save,inherit,...capability,supported:qualitySubmissionSupported(settings,capability.capabilities)};
}

export function QualitySettingsFields({value,onChange,disabled=false,prefix='制作',capabilities,capabilityError,onRefreshCapabilities,showTechnicalDetails=true,modeUse='generation'}:{value:QualitySettings;onChange:(value:Required<QualitySettings>)=>void;disabled?:boolean;prefix?:string;capabilities?:Row|null;capabilityError?:string;onRefreshCapabilities?:()=>Promise<void>;showTechnicalDetails?:boolean;modeUse?:ModeUse}) {
  const q=normalizedQuality(value);
  const workflow=qualityModes.find(mode=>mode.value===(q.mode==='preview'?'fast':q.mode));
  const sr=capabilities?.ai_super_resolution;
  const processingLimits=q.processing==='seedvr2'?capabilities?.seedvr2?.limits:q.processing==='ai_super_resolution'?sr?.limits:[];
  const aiAvailable=qualitySubmissionSupported({...q,processing:'ai_super_resolution'},capabilities||null);
  const seedAvailable=qualitySubmissionSupported({...q,processing:'seedvr2'},capabilities||null);
  const valid=qualitySubmissionSupported(q,capabilities||null);
  const update=(key:keyof QualitySettings,value:string)=>onChange({...q,[key]:value});
  return <><div className="workspace-grid quality-fields">
    <FormField label={`${prefix}输出规格`}><select disabled={disabled} value={q.resolution} onChange={e=>update('resolution',e.target.value)}>
      {outputResolutions.map(size=><option key={size.value} value={size.value}>{size.label} · {outputDimensions(size.value,q.aspect_ratio)}{qualitySubmissionSupported({...q,resolution:size.value},capabilities||null)?'':' · 当前处理方式未验证'}</option>)}
    </select></FormField>
    {modeUse==='generation'||modeUse==='encoding'?<FormField label={`${prefix}质量模式`}><select disabled={disabled} value={q.mode} onChange={e=>update('mode',e.target.value)}>
      {qualityModes.map(mode=><option key={mode.value} value={mode.value}>{modeUse==='generation'?`${mode.label}（${mode.steps} 步）`:mode.value==='fast'?'快速编码':mode.label}</option>)}
      {q.mode==='preview'?<option value="preview" disabled>已保存的旧预览</option>:null}
    </select></FormField>:null}
    <FormField label={`${prefix}画幅`}><select disabled={disabled} value={q.aspect_ratio} onChange={e=>update('aspect_ratio',e.target.value)}><option value="16:9">16:9 横向</option><option value="9:16">9:16 竖向</option><option value="1:1">1:1 方形</option></select></FormField>
    <FormField label={`${prefix}处理方式`}><select disabled={disabled} value={q.processing} onChange={e=>update('processing',e.target.value)}><option value="lanczos">普通 Lanczos 缩放（已接入）</option><option value="native" disabled>原生目标分辨率（尚未验证）</option><option value="ai_super_resolution" disabled={!aiAvailable}>{aiAvailable?'4K输出（Real-ESRGAN逐帧超分）':sr?.state==='detected_not_tested'?'AI 视频超分（已检测，未实测）':sr?.state==='verified'?'AI 视频超分（此档位或画幅未验证）':sr?.state==='not_configured'?'AI 视频超分（尚未配置）':'AI 视频超分（能力尚未读取）'}</option><option value="seedvr2" disabled={!seedAvailable}>{seedAvailable?`${q.resolution}输出（SeedVR2时序视频超分）`:capabilities?.seedvr2?.state==='verified'?'SeedVR2（当前档位或画幅未验证）':'SeedVR2（尚未通过完整视频实测）'}</option><option value="reconstruction" disabled>模型高分辨率重建（尚未配置）</option></select></FormField>
  </div><p className="form-hint" data-testid="quality-output-dimensions">当前输出：{q.aspect_ratio} · {outputDimensions(q.resolution,q.aspect_ratio)}。{modeUse!=='encoding'?'制作前请在「镜头画幅」中保存实际镜头方向。':''}</p>
    <details className="workspace-details"><summary>横屏与竖屏输出尺寸</summary><table className="quality-evidence-table"><thead><tr><th>规格</th><th>16:9 横屏</th><th>9:16 竖屏</th></tr></thead><tbody>{outputResolutions.map(size=><tr key={size.value}><td>{size.label}</td>{(['16:9','9:16'] as const).map(aspect=><td key={aspect}>{outputDimensions(size.value,aspect)} · {qualitySubmissionSupported({...q,resolution:size.value,aspect_ratio:aspect},capabilities||null)?'当前处理方式可用':'当前处理方式未验证'}</td>)}</tr>)}</tbody></table></details>
    {modeUse==='generation'&&workflow?<p className="form-hint" data-testid="h3-quality-route">本地 H3 工作流 {workflow.workflow} · {workflow.steps} 步。Context-IR：已启用（自动进行素材与提示词一致性分析）。{q.mode==='preview'?'旧预览设置保留，新的本地 H3 任务按 Fast 执行。':''}</p>:null}
    {modeUse==='cloud'?<p className="form-hint">云端服务使用已配置的模型与接口参数，采样和工作流由供应商决定。这里调整成片输出与后处理；不会将本地 4／20／30 步配方发给云端。</p>:null}
    {modeUse==='provider'?<p className="form-hint">当前使用服务档案中的普通本地工作流。这里调整输出与后处理；切换为 Context-IR 服务后才启用三档采样。</p>:null}
    {!valid?<p role="alert" className="inline-notice warning">{showTechnicalDetails?'当前处理方式没有此分辨率／画幅的真实验证。请先验证所需规格，或明确选择普通Lanczos缩放；保存、启动和输出已阻止，不会静默降级。':'此处理方式不支持当前分辨率或画幅，请调整设置。'}</p>:null}
    <p className="form-hint">{showTechnicalDetails?<>{q.processing==='seedvr2'?'SeedVR2采用时序分块处理完整视频，仅开放已有完整视频验证的规格组合；仍需检查边界闪烁、面容和细节。':q.processing==='ai_super_resolution'?'Real-ESRGAN使用图像模型逐帧处理完整视频，不保证时序一致性；放大后面部、细节伪造和闪烁仍需人工审片。':'普通 Lanczos 是整个视频的缩放，不是 AI 超分。'} 原始版本保留；4K文件尺寸不代表原生4K细节。输出画幅不会自动修改剧本中的实际镜头画幅；请在镜头画幅中明确保存。</>:q.processing==='seedvr2'?'SeedVR2时序超分，仅开放已验证的规格组合。':q.processing==='ai_super_resolution'?'Real-ESRGAN逐帧超分，当前支持已验证的4K画幅。':'普通缩放不增加原生细节。'}</p>
    {showTechnicalDetails?processingLimits?.map((line:string,index:number)=><p className="form-hint" key={index}>{line}</p>):null}
    {capabilityError?<p role="status" className="form-hint">超分验证状态未能读取，AI处理保持禁用：{capabilityError}</p>:null}
    {onRefreshCapabilities&&(showTechnicalDetails||capabilityError)?<button type="button" className="button button-ghost button-small" disabled={disabled} onClick={()=>void onRefreshCapabilities()}>{showTechnicalDetails?'重新读取超分验证状态（不启动模型）':'重新读取处理方式'}</button>:null}</>;
}

export function QualityPlan({plan,title='已保存的实际处理方案',modeUse='generation'}:{plan?:Row;title?:string;modeUse?:ModeUse}) {
  if(!plan)return null;
  const {generation,...outputPlan}=plan;
  return <details className="workspace-details quality-plan"><summary>{title} · {String(plan.label||plan.method||'待解析')}</summary>
    <p>请求输出 {plan.requested?.width}×{plan.requested?.height}{modeUse==='generation'&&generation?` · 基础生成目标 ${generation.width}×${generation.height}`:''}</p>
    <p>处理方式 {plan.method==='lanczos'?'普通 Lanczos 缩放':plan.method==='seedvr2'?'SeedVR2时序视频超分':plan.method==='ai_super_resolution'?'Real-ESRGAN逐帧超分（无时序保证）':String(plan.method)} · {plan.supported?'此处理路径可提交':'此处理路径尚不支持'} · 视觉质量需人工审片</p>
    {Array.isArray(plan.limits)?plan.limits.map((line:string)=><p key={line}>{line}</p>):null}<pre className="source-blocks">{JSON.stringify(modeUse==='generation'?plan:outputPlan,null,2)}</pre>
  </details>;
}

export function QualityProfileEditor({scope,targetId,title,onSaved,onDirtyChange,showDetails=true,modeUse='generation'}:{scope:'system'|'project'|'shot';targetId:string;title:string;onSaved?:()=>void;onDirtyChange?:(dirty:boolean)=>void;showDetails?:boolean;modeUse?:ModeUse}) {
  const quality=useQualityProfile(scope,targetId),[notice,setNotice]=useState('');
  useEffect(()=>{onDirtyChange?.(quality.dirty);return()=>onDirtyChange?.(false);},[quality.dirty,onDirtyChange]);
  return <section className="card studio-panel"><h2>{title}</h2><p>{scope==='system'?'保存为之后项目的制作默认；已有项目与历史视频保持各自快照。':scope==='shot'?'继承项目配置；保存后仅覆盖此镜的后续生成。':'项目设置适用于未单独覆盖的镜头。'}</p>
    {quality.loading?<p role="status">读取画质配置…</p>:null}{quality.error?<p role="alert" className="inline-notice warning">{quality.error}</p>:null}
    {quality.profile?<>{showDetails?<p>配置来源：{quality.profile.inherited_from||'本层独立设置'} · 修订 {quality.profile.revision}</p>:null}<QualitySettingsFields prefix={scope==='shot'?'此镜':'默认'} value={quality.settings} onChange={quality.setSettings} capabilities={quality.capabilities} capabilityError={quality.capabilityError} onRefreshCapabilities={quality.refreshCapabilities} showTechnicalDetails={showDetails} modeUse={modeUse} disabled={quality.saving||quality.loading}/><div className="studio-actions"><button className="button button-primary" disabled={quality.saving||quality.loading||!quality.supported} onClick={()=>void quality.save().then(()=>{setNotice(showDetails?'实际画质配置已保存；已接收任务仍沿用原快照。':'画质设置已保存。');onSaved?.();}).catch(()=>{})}>保存{scope==='shot'?'此镜覆盖':'画质设置'}</button>{scope==='shot'?<button className="button button-secondary" disabled={quality.saving||quality.loading} onClick={()=>void quality.inherit().then(onSaved)}>恢复继承项目</button>:null}<button className="button button-secondary" disabled={quality.saving||quality.loading} onClick={()=>void quality.load()}>重新读取</button></div>{quality.dirty?<p>{showDetails?'有未保存设置；下方仍显示上次保存方案。':'有未保存的画质设置。'}</p>:null}{showDetails?<QualityPlan plan={quality.profile.plan} modeUse={modeUse}/>:null}</>:null}
    {notice?<p role="status">{notice}</p>:null}
  </section>;
}

/** Show only recorded cloud-output deviations before a review decision. */
export function QualityDurationWarning({metadata,videoId}:{metadata?:Row|null;videoId?:string|null}) {
  const [checked,setChecked]=useState<{videoId:string;status:string;details:Row|null}|null>(null);
  const outputWarning=metadata?.quality_status==='warning' && Array.isArray(metadata?.quality_warnings) && metadata.quality_warnings.includes('cinematic_output_spec');
  useEffect(()=>{
    let alive=true; setChecked(null);
    if(outputWarning && videoId) void request<Row>(`/api/videos/${encodeURIComponent(videoId)}`).then(detail=>{
      const checks=Array.isArray(detail.quality_checks)?detail.quality_checks:[];
      const latest=checks.filter((item:Row)=>item.check_type==='cinematic_output_spec').at(-1);
      if(alive && latest) setChecked({videoId,status:latest.status,details:latest.details_json||null});
    }).catch(()=>{});
    return ()=>{alive=false;};
  },[outputWarning,videoId]);
  const current=checked?.videoId===videoId?checked:null;
  if(!outputWarning || (current && current.status!=='warning'))return null;
  const spec=current?.details;
  const number=(value:unknown)=>typeof value==='number' && Number.isFinite(value) && value>0?value:null;
  const actualDuration=number(spec?.actual?.duration)??number(metadata?.duration_seconds);
  const actualFrames=number(spec?.actual?.frames),expectedFrames=number(spec?.expected?.frames),expectedFps=number(spec?.expected?.fps);
  const expectedDuration=expectedFrames!==null && expectedFps!==null?expectedFrames/expectedFps:null;
  const actualWidth=number(spec?.actual?.width)??number(metadata?.width),actualHeight=number(spec?.actual?.height)??number(metadata?.height);
  const expectedWidth=number(spec?.expected?.width),expectedHeight=number(spec?.expected?.height);
  const dimensions=(width:number|null,height:number|null)=>width!==null && height!==null?`${width} × ${height}`:'未提供';
  const seconds=(value:number|null)=>value===null?'未提供':`${Number(value.toFixed(3))} 秒`;
  return <div className="inline-notice warning" role="status" data-testid="duration-warning">
    <strong>输出规格提示：</strong>当前视频的实际规格与请求不完全一致，请播放核对画面和声音后再决定是否保留。
    <p>请求：尺寸 {dimensions(expectedWidth,expectedHeight)}；时长 {seconds(expectedDuration)}；帧数 {expectedFrames??'未提供'}。</p>
    <p>实际：尺寸 {dimensions(actualWidth,actualHeight)}；时长 {seconds(actualDuration)}；帧数 {actualFrames??'未提供'}。</p>
    <p>合成时画面按输出尺寸等比缩放并补边，保留完整画面；导出会重新编码。该提示不表示规格完全一致。</p>
  </div>;
}

export function QualityMediaReport({metadata}:{metadata?:Row|null}) {
  if(!metadata)return null;
  const quality=metadata.quality||metadata.quality_report||metadata.quality_render||metadata;
  const stages=[['请求输出',quality.requested_resolution],['模型实际／源视频',quality.model_actual_resolution],['后处理',quality.postprocess_resolution],['最终文件',quality.final_actual]] as const;
  return <details className="workspace-details"><summary>实际文件规格与处理证据</summary><p>生成尺寸、后处理尺寸和最终尺寸分别以实际探测为准；解码通过不等于人物身份或真实感已审核。</p>{quality.final_actual?<table className="quality-evidence-table"><thead><tr><th>阶段</th><th>实际或请求尺寸</th><th>帧率／编码／时长</th></tr></thead><tbody>{stages.map(([label,value])=><tr key={label}><td>{label}</td><td>{value?.width??'未提供'} × {value?.height??'未提供'}</td><td>{[value?.fps,value?.codec,value?.duration?`${value.duration}秒`:null].filter(Boolean).join(' · ')||'—'}</td></tr>)}</tbody></table>:<p>实际媒体探测：{metadata.width??'—'} × {metadata.height??'—'} · {metadata.frame_rate||'帧率未提供'} · {metadata.duration_seconds??'—'} 秒</p>}<p>处理方法：{quality.method==='seedvr2_temporal_video'?'SeedVR2时序视频超分':quality.ai_super_resolution===true?'Real-ESRGAN图像模型逐帧超分（无时序保证）':quality.method==='ordinary_lanczos_scaling'?'普通 Lanczos 缩放':quality.method||'没有独立后处理记录'}。{quality.ai_super_resolution===false?'未使用 AI 超分。':''}</p><pre className="source-blocks">{JSON.stringify(quality,null,2)}</pre></details>;
}

export function QualityCapabilityEvidence(){
  const [data,setData]=useState<Row|null>(null),[error,setError]=useState('');
  useEffect(()=>{let alive=true;void request<Row>('/api/quality/capabilities').then(result=>{if(alive)setData(result);}).catch(e=>{if(alive)setError(String(e));});return()=>{alive=false;};},[]);
  return <section className="card studio-panel"><h2>质量模式与实际验证证据</h2>{error?<p role="alert">{error}</p>:null}{data?<><table className="quality-evidence-table"><thead><tr><th>模式</th><th>真实参数映射</th><th>实测状态</th></tr></thead><tbody>{Object.entries(data.modes||{}).map(([name,row])=>{const mode=row as Row;return <tr key={name}><td>{({preview:'预览',standard:'标准',high:'高质量'} as Row)[name]||name}</td><td>H3采样 {mode.steps} 步 · CRF {mode.crf} · {mode.preset}</td><td>{mode.verification}</td></tr>;})}</tbody></table><p>参数可提交、媒体文件可解码、视觉质量满意分别验证。以上H3参数不能视为所有云端模型通用参数。</p><details className="workspace-details"><summary>当前档位与原生输出证据</summary><pre className="source-blocks">{JSON.stringify(data,null,2)}</pre></details></>:<p>读取真实能力记录…</p>}</section>;
}

export function QualityPostprocess({shotId,videoId}:{shotId:string;videoId:string}) {
  const quality=useQualityProfile('shot',shotId),[params,setParams]=useSearchParams();
  const renderId=params.get('quality_render')||'';
  const [render,setRender]=useState<QualityRenderDTO|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{let alive=true,inflight=false;if(!renderId){setRender(null);return;}
    async function load(){if(inflight)return;inflight=true;try{const result=await request<QualityRenderDTO>(`/api/quality/renders/${renderId}`);if(alive){setRender(result);setError('');}}catch(e){if(alive)setError(String(e));}finally{inflight=false;}}
    void load();const timer=window.setInterval(()=>void load(),4000);return()=>{alive=false;window.clearInterval(timer);};
  },[renderId]);
  async function submit(){if(!quality.supported){setError('所选超分组合尚无真实验证，未提交任务。');return;}setBusy(true);setError('');try{const result=await request<QualityRenderDTO>('/api/quality/renders',{method:'POST',body:JSON.stringify({video_id:videoId,settings:quality.settings})});setRender(result);setParams(current=>{const next=new URLSearchParams(current);next.set('quality_render',result.id);return next;},{replace:true});}catch(e){setError(String(e));}finally{setBusy(false);}}
  const current=render?.source_video_id===videoId?render:null;
  return <details className="studio-modify"><summary>此视频输出另一画质版本</summary><p>按下方明确选择的方法处理完整视频，保留原文件并创建独立待审版本。本操作不会修改项目或镜头默认配置。</p>{quality.error||error?<p role="alert">{quality.error||error}</p>:null}<QualitySettingsFields modeUse="encoding" prefix="版本" value={quality.settings} onChange={quality.setSettings} capabilities={quality.capabilities} capabilityError={quality.capabilityError} onRefreshCapabilities={quality.refreshCapabilities} disabled={busy}/><button type="button" className="button button-secondary" disabled={busy||quality.loading||!quality.profile||!quality.supported||Boolean(current&&['queued','running'].includes(current.status))} onClick={()=>void submit()}>创建完整视频画质版本</button>{current?<><p role="status">处理记录 {current.id} · {current.status}</p>{current.error_message?<p role="alert">{current.error_message}</p>:null}{current.status==='generated'&&current.relative_path?<><video controls preload="metadata" src={api.mediaUrl(current.relative_path)} data-testid="quality-render-video"/><a href={api.mediaUrl(current.relative_path)} download>下载独立画质版本</a><QualityMediaReport metadata={{quality:current.result}}/></>:null}</>:null}</details>;
}
