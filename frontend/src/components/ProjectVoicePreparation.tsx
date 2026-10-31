import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {Link} from 'react-router-dom';
import {request,api} from '../lib/api';
import type {ProjectVoiceGroup as VoiceGroup,ProjectVoicePreparation as VoiceOverview} from '../lib/studio-voice-contract';
import {AudioReferencePicker} from './AudioReferencePicker';
import {ProjectTTSServiceSelector} from './ProjectTTSServiceSelector';
import type {ProjectTTSServiceState} from './ProjectTTSServiceSelector';
import {Badge} from './ui';
import {FormField} from './ui';
import type {VoiceLibraryAsset,VoiceLibraryOverview} from '../lib/asset-library-contract';
import './project-voice.css';

export type ProjectVoiceState={ready:boolean;dirty:boolean;loading:boolean;busy:boolean;blockingReasons:string[]};
export type ProjectVoicePreparationProps={projectId:string;disabled?:boolean;allowCloudCosts?:boolean;allowPaidFallback?:boolean;refreshKey?:string|number;onChanged?:()=>void|Promise<void>;onStateChange?:(state:ProjectVoiceState)=>void};
const keyFor=(group:VoiceGroup)=>`${group.character_id}:${group.version_id||'unbound'}`;

export function ProjectVoicePreparation({projectId,disabled=false,allowCloudCosts=false,allowPaidFallback=false,refreshKey,onChanged,onStateChange}:ProjectVoicePreparationProps){
  const [data,setData]=useState<VoiceOverview|null>(null),[loading,setLoading]=useState(true),[error,setError]=useState(''),[notice,setNotice]=useState(''),[navigationNotice,setNavigationNotice]=useState('');
  const [drafts,setDrafts]=useState<Record<string,string>>({}),[uploading,setUploading]=useState<Record<string,boolean>>({}),[uploadDrafts,setUploadDrafts]=useState<Record<string,boolean>>({}),[saving,setSaving]=useState(''),[reload,setReload]=useState(0);
  const [serviceState,setServiceState]=useState<ProjectTTSServiceState>({dirty:false,busy:false});
  const [library,setLibrary]=useState<VoiceLibraryAsset[]>([]),[libraryError,setLibraryError]=useState('');
  useEffect(()=>{if(data?.project_id!==projectId)return;const abort=new AbortController();void request<VoiceLibraryOverview>('/api/voice-library',{signal:abort.signal}).then(value=>{setLibrary(value.items);setLibraryError('');}).catch(error=>{if(!abort.signal.aborted)setLibraryError(String(error));});return()=>abort.abort();},[projectId,data?.project_id]);
  const onServiceStateChange=useCallback((state:ProjectTTSServiceState)=>setServiceState(state),[]);
  // Keep same-project editors mounted while an external refresh waits for them.
  const identity=`${projectId}:${allowCloudCosts}:${allowPaidFallback}`,current=useRef(identity);current.current=identity;
  const [appliedRefresh,setAppliedRefresh]=useState({identity,key:refreshKey}),[requestedRefresh,setRequestedRefresh]=useState(false);
  const pendingRefresh=requestedRefresh||(appliedRefresh.identity===identity&&appliedRefresh.key!==refreshKey);
  const loadedIdentity=useRef('');
  const alive=useRef(true);
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  const callbacks=useRef({onChanged,onStateChange});callbacks.current={onChanged,onStateChange};
  const base=`/api/studio/projects/${encodeURIComponent(projectId)}/voices`;
  const query=`?allow_cloud_costs=${allowCloudCosts}&allow_paid_fallback=${allowPaidFallback}`;
  useEffect(()=>{
    const controller=new AbortController();const destination=identity;
    const changedContext=loadedIdentity.current!==identity;loadedIdentity.current=identity;
    setLoading(true);setError('');setNotice('');setNavigationNotice('');setDrafts({});setUploading({});setUploadDrafts({});setSaving('');
    if(changedContext){setData(null);setServiceState({dirty:false,busy:false});}
    void request<VoiceOverview>(base+query,{signal:controller.signal}).then(next=>{
      if(!alive.current||controller.signal.aborted||current.current!==destination)return;
      if(next.project_id!==projectId)throw new Error('返回的配音设置不属于当前项目');
      setData(next);
    }).catch(e=>{if(alive.current&&!controller.signal.aborted&&current.current===destination)setError(String(e));}).finally(()=>{if(alive.current&&!controller.signal.aborted&&current.current===destination)setLoading(false);});
    return()=>controller.abort();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[identity,reload]);
  const voiceDirty=Object.keys(drafts).some(key=>{const group=data?.groups.find(g=>keyFor(g)===key);return group&&(drafts[key]!== (group.audio_id||'')||group.mixed);})||Object.values(uploadDrafts).some(Boolean);
  const voiceBusy=Boolean(saving)||Object.values(uploading).some(Boolean);
  const dirty=voiceDirty||serviceState.dirty,busy=voiceBusy||serviceState.busy;
  useEffect(()=>{
    if(appliedRefresh.identity!==identity){setAppliedRefresh({identity,key:refreshKey});setRequestedRefresh(false);return;}
    if(!pendingRefresh||dirty||busy)return;
    setAppliedRefresh({identity,key:refreshKey});setRequestedRefresh(false);setReload(value=>value+1);
  },[identity,refreshKey,appliedRefresh.identity,pendingRefresh,dirty,busy]);
  useEffect(()=>{
    if(!dirty&&!busy){setNavigationNotice('');return;}
    function beforeUnload(event:BeforeUnloadEvent){event.preventDefault();event.returnValue='';}
    function beforeNavigate(event:MouseEvent){
      if(event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
      const anchor=event.target instanceof Element?event.target.closest<HTMLAnchorElement>('a[href]'):null;
      if(!anchor||anchor.hasAttribute('download')||(anchor.target&&anchor.target.toLowerCase()!=='_self'))return;
      const next=new URL(anchor.href,window.location.href),here=new URL(window.location.href);
      if(!['http:','https:'].includes(next.protocol))return;
      if(next.origin===here.origin&&next.pathname===here.pathname&&next.search===here.search)return;
      if(busy){
        event.preventDefault();event.stopPropagation();
        setNavigationNotice('音频正在上传或保存，请完成后再离开当前页面。');
      }else if(!window.confirm('角色配音有未保存的选择或尚未上传的音频。确定放弃这些草稿并离开当前页面吗？')){
        event.preventDefault();event.stopPropagation();
        setNavigationNotice('已保留未保存的配音草稿，请保存或放弃后再离开。');
      }
    }
    document.addEventListener('click',beforeNavigate,true);
    window.addEventListener('beforeunload',beforeUnload);
    return()=>{document.removeEventListener('click',beforeNavigate,true);window.removeEventListener('beforeunload',beforeUnload);};
  },[dirty,busy]);
  const refreshPending=pendingRefresh||Boolean(serviceState.refreshPending);
  const blockingReasons=useMemo(()=>[...(error?['角色配音设置尚未读取成功，请重试。']:data?.blockers||[]),...(dirty?['有未保存的角色配音选择或尚未上传的音频。']:[]),...(refreshPending?['项目配置已变化，待保存或放弃配音草稿后刷新。']:[])],[data,error,dirty,refreshPending]);
  const state=useMemo<ProjectVoiceState>(()=>({ready:!loading&&!refreshPending&&!busy&&!dirty&&!error&&Boolean(data?.ready_to_start),dirty,loading:loading||refreshPending,busy,blockingReasons}),[data,loading,refreshPending,busy,dirty,error,blockingReasons]);
  useEffect(()=>{callbacks.current.onStateChange?.(state);},[state]);
  const services=useMemo(()=>[...new Map((data?.services||[]).filter(s=>s.enabled).map(s=>[`${s.service_id}:${s.model_id}`,s])).values()],[data]);
  function markUploading(key:string,value:boolean){setUploading(old=>old[key]===value?old:{...old,[key]:value});}
  function markUploadDraft(key:string,value:boolean){setUploadDrafts(old=>old[key]===value?old:{...old,[key]:value});}
  const serviceSaved=useCallback(async()=>{const destination=current.current;await callbacks.current.onChanged?.();if(alive.current&&current.current===destination)setRequestedRefresh(true);},[]);
  async function save(group:VoiceGroup){
    const key=keyFor(group),destination=identity;
    if(!group.version_id||disabled||busy||serviceState.dirty||drafts[key]===undefined)return;
    setSaving(key);setError('');setNotice('');setNavigationNotice('');
    try{
      const libraryId=drafts[key].startsWith('library:')?drafts[key].slice(8):'';
      const next=libraryId?await request<VoiceOverview>(`/api/voice-library/${encodeURIComponent(libraryId)}/bind`,{method:'POST',body:JSON.stringify({project_id:projectId,character_id:group.character_id,version_id:group.version_id,expected_token:group.expected_token})}):await request<VoiceOverview>(`${base}/${encodeURIComponent(group.character_id)}${query}`,{method:'PUT',body:JSON.stringify({version_id:group.version_id,audio_id:drafts[key]||null,expected_token:group.expected_token})});
      if(!alive.current||current.current!==destination)return;
      if(next.project_id!==projectId)throw new Error('保存结果不属于当前项目');
      setData(next);setDrafts(old=>{const remaining={...old};delete remaining[key];return remaining;});
      setNotice(`${group.name}的配音参考已保存，应用到本项目该角色版本的 ${group.shot_ids.length} 个镜头。`);
      await callbacks.current.onChanged?.();
    }catch(e){if(alive.current&&current.current===destination)setError(String(e));}
    finally{if(alive.current&&current.current===destination)setSaving('');}
  }
  return <section className="card studio-panel project-voice-preparation" aria-label="制作前角色配音设置">
    <div className="studio-section-heading"><div><h2>角色配音（开始制作前配置）</h2><p>从配音库选择或上传真实声音，试听并保存角色声线，再开始制作。</p></div><button type="button" className="button button-secondary button-small" disabled={disabled||loading||busy||dirty} onClick={()=>setRequestedRefresh(true)}>刷新配音设置</button></div>
    {loading?<p role="status">读取本项目角色与配音服务…</p>:null}
    {pendingRefresh?<p role="status" className="inline-notice warning">项目配置已变化，已保留当前音频草稿和保存操作。保存或放弃草稿后自动刷新，刷新完成前不能开始制作。</p>:null}
    {error?<p role="alert" className="inline-notice warning">{error}<button type="button" className="button button-secondary button-small" disabled={disabled||busy||dirty} onClick={()=>setRequestedRefresh(true)}>重新读取配音设置</button></p>:null}
    {data?<>
      <div className="project-voice-service"><strong>{(data as VoiceOverview&{audio_policy?:string}).audio_policy==='native'?'本项目明确沿用视频原声':(data as VoiceOverview&{audio_policy?:string}).audio_policy==='skip'?'本项目明确跳过独立配音':data.tts_enabled?'本次制作的配音服务':'独立配音服务待配置'}</strong><p>{data.summary}</p>{services.length?<p>{services.map(s=>`${s.name}${s.model_id?' · '+s.model_id:''}`).join('；')}</p>:null}<ProjectTTSServiceSelector projectId={projectId} refreshKey={refreshKey} disabled={disabled||loading||voiceDirty||voiceBusy} onStateChange={onServiceStateChange} onSaved={serviceSaved}/><Link to="/settings">配置角色配音服务</Link></div>
      {data.groups.length?<div className="project-voice-grid">{data.groups.map(group=>{
        const key=keyFor(group),value=drafts[key]??group.audio_id??'',changed=drafts[key]!==undefined&&(value!==(group.audio_id||'')||group.mixed),extra=group as VoiceGroup&{library_asset_id?:string;configuration_status?:string;generation_status?:string},libraryId=value.startsWith('library:')?value.slice(8):drafts[key]===undefined?extra.library_asset_id||'':'',libraryVoice=library.find(item=>item.id===libraryId);
        return <article key={key} className="project-voice-person" aria-label={`${group.name}配音设置`}>
          <div className="section-heading"><h3>{group.name}</h3><Badge tone={group.ready?'success':'warning'}>{extra.configuration_status==='native'?'明确沿用视频原声':extra.configuration_status==='skipped'?'用户已明确跳过':extra.configuration_status==='not_required'?'此角色不需要配音':group.mixed?'各镜头使用不同参考':group.audio_id?'已绑定参考音频':group.requires_reference_audio&&group.dialogue_count?'待配置参考音频':'尚未绑定参考音频'}</Badge></div>
          <p>{group.version_id?`人物 v${group.version_number??'—'} · ${group.version_state==='approved'?'已锁定版本':'当前人物版本'}`:'尚未绑定人物版本'} · {group.dialogue_count} 条台词 · {group.shot_ids.length} 个镜头</p>
          {group.issues.map((issue,i)=><p className="project-voice-issue" key={i}>{issue}</p>)}
          {group.mixed?<p>各镜头已保存的参考音频会继续分别使用。选择并保存后，统一为本项目这个人物版本的声线。</p>:null}
          <FormField label={`${group.name}配音库声音`} hint="可复用同一个声音；只有实际兼容的服务才能将其用于对白生成。"><select value={libraryId} disabled={disabled||loading||busy||serviceState.dirty||!group.version_id} onChange={event=>{if(event.target.value)setDrafts(old=>({...old,[key]:`library:${event.target.value}`}));else setDrafts(old=>({...old,[key]:''}));}}><option value="">未选择配音库声音</option>{library.map(item=><option key={item.id} value={item.id}>{item.name} · {item.language} · {item.capabilities.voice_reference?'可作声音参考':'仅试听 / 参考未就绪'}</option>)}</select></FormField>
          {libraryVoice?<div><audio controls preload="metadata" src={api.mediaUrl(libraryVoice.preview_url||libraryVoice.media_url)}/><p className="form-hint">{libraryVoice.actual.duration?.toFixed(2)??'—'} 秒 · {libraryVoice.capabilities.reason||'能力以当前服务实际校验为准'}</p></div>:null}
          <p className="form-hint"><Link to="/assets/voices">批量导入或管理配音库声音</Link> · 取消绑定可在下方参考音频中选择“未绑定”后保存。</p>{libraryError?<p className="form-hint">配音库暂未读取成功：{libraryError} <Link to="/assets/voices">打开配音库</Link></p>:null}
          {extra.configuration_status?<p className="form-hint">声音配置：{({not_required:'不需要配音',native:'明确沿用视频原声',skipped:'用户已明确跳过',unconfigured:'未配置',blocked:'条件不满足',ready:'已就绪'} as Record<string,string>)[extra.configuration_status]||extra.configuration_status} · 对白生成：{({pending:'等待生成',generating:'生成中',succeeded:'成功',failed:'失败',blocked:'条件不满足',unconfigured:'未配置',skipped:'已明确跳过',not_required:'不需要配音',native:'沿用视频原声'} as Record<string,string>)[extra.generation_status||'']||extra.generation_status||'未检查'}</p>:null}
          <AudioReferencePicker projectId={projectId} versionId={group.version_id||''} name={group.name} value={value.startsWith('library:')?group.audio_id||'':value} inline disabled={disabled||loading||busy||serviceState.dirty} onChange={id=>{setDrafts(old=>({...old,[key]:id}));setNotice('');}} onBusyChange={value=>markUploading(key,value)} onDraftChange={value=>markUploadDraft(key,value)}/>
          <div className="project-voice-actions"><button type="button" className="button button-primary button-small" disabled={disabled||busy||serviceState.dirty||loading||!group.version_id||!changed||Boolean(uploadDrafts[key])} onClick={()=>void save(group)}>{saving===key?'正在保存…':`保存${group.name}配音`}</button>{changed?<button type="button" className="button button-ghost button-small" disabled={disabled||busy} onClick={()=>setDrafts(old=>{const next={...old};delete next[key];return next;})}>放弃未保存选择</button>:null}</div>
          {changed?<p className="studio-muted">当前选择尚未保存，开始制作前请保存。</p>:null}
        </article>;
      })}</div>:<p>先导入 Word，建立人物与分镜后，即可在这里为每个角色上传参考音频。</p>}
      {data.blockers.length?<div className="inline-notice warning"><strong>开始配音前需要完成</strong><ul>{data.blockers.map((reason,i)=><li key={i}>{reason}</li>)}</ul></div>:null}
    </>:null}
    {notice?<p role="status" className="inline-notice">{notice}</p>:null}
    {navigationNotice?<p role="status" className="inline-notice warning">{navigationNotice}</p>:null}
  </section>;
}
