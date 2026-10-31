import {useEffect,useRef,useState} from 'react';
import {request} from '../lib/api';
import {FormField} from './ui';

type Policy='auto'|'native'|'tts'|'skip';
type Preferences={project_id:string;revision:number;dialogue_audio_policy:Policy};
export function ProjectAudioPolicy({projectId,disabled,onSaved,onStateChange}:{projectId:string;disabled:boolean;onSaved:()=>void|Promise<void>;onStateChange?:(state:{busy:boolean;dirty:boolean})=>void}){
  const [data,setData]=useState<Preferences|null>(null),[draft,setDraft]=useState<Policy>('auto'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[reload,setReload]=useState(0);
  const callbacks=useRef(onStateChange);callbacks.current=onStateChange;
  useEffect(()=>{callbacks.current?.({busy,dirty:Boolean(data)&&draft!==data?.dialogue_audio_policy});},[busy,draft,data]);
  useEffect(()=>()=>callbacks.current?.({busy:false,dirty:false}),[]);
  useEffect(()=>{const abort=new AbortController();setData(null);void request<Preferences>(`/api/studio/projects/${encodeURIComponent(projectId)}/production-preferences`,{signal:abort.signal}).then(value=>{if(value.project_id!==projectId)throw new Error('声音方式响应不属于当前项目');setData(value);setDraft(value.dialogue_audio_policy);setError('');}).catch(e=>{if(!abort.signal.aborted)setError(String(e));});return()=>abort.abort();},[projectId,reload]);
  async function save(){if(!data||busy)return;setBusy(true);setError('');try{const result=await request<Preferences>(`/api/studio/projects/${encodeURIComponent(projectId)}/production-preferences`,{method:'PUT',body:JSON.stringify({expected_revision:data.revision,dialogue_audio_policy:draft})});setData(result);await onSaved();}catch(e){setError(String(e));}finally{setBusy(false);}}
  return <div className="project-audio-policy"><FormField label="对白声音方式" hint="按项目保存。自动检查不会将缺少服务误标为已跳过。"><select value={draft} disabled={disabled||busy||!data} onChange={event=>setDraft(event.target.value as Policy)}><option value="auto">自动检查已配置的 TTS（缺少条件时等待配置）</option><option value="tts">使用独立角色配音</option><option value="native">明确沿用视频原声</option><option value="skip">明确跳过独立配音</option></select></FormField><button type="button" className="button button-secondary button-small" disabled={disabled||busy||!data||draft===data.dialogue_audio_policy} onClick={()=>void save()}>{busy?'正在保存声音方式…':'保存项目声音方式'}</button>{error?<p role="alert" className="form-hint">{error}<button type="button" className="button button-ghost button-small" onClick={()=>setReload(value=>value+1)}>重试读取声音方式</button></p>:null}</div>;
}
