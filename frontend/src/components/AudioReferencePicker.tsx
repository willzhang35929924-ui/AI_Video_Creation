import {useEffect,useRef,useState} from 'react';
import {request,api} from '../lib/api';
import type {AudioAssetDTO} from '../lib/model-services-contract';
import {FormField} from './ui';

export function AudioReferencePicker({projectId,versionId,name,value,onChange,disabled=false,inline=false,onBusyChange,onDraftChange}:{projectId:string;versionId:string;name:string;value:string;onChange:(id:string)=>void;disabled?:boolean;inline?:boolean;onBusyChange?:(busy:boolean)=>void;onDraftChange?:(dirty:boolean)=>void}){
  const [items,setItems]=useState<AudioAssetDTO[]>([]),[error,setError]=useState(''),[busy,setBusy]=useState(false),[file,setFile]=useState<File|null>(null),[source,setSource]=useState('ai_generated'),[declaration,setDeclaration]=useState('');
  const [fileKey,setFileKey]=useState(0);
  const identity=`${projectId}:${versionId}`,current=useRef(identity);current.current=identity;
  const callbacks=useRef({onBusyChange,onDraftChange});callbacks.current={onBusyChange,onDraftChange};
  useEffect(()=>{callbacks.current.onBusyChange?.(busy);},[busy]);
  useEffect(()=>{callbacks.current.onDraftChange?.(Boolean(file));},[file]);
  const alive=useRef(true);useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);
  useEffect(()=>{let active=true;setItems([]);setError('');setFile(null);setFileKey(v=>v+1);setDeclaration('');if(versionId)void request<AudioAssetDTO[]>(`/api/model-services/audio-assets?project_id=${encodeURIComponent(projectId)}&character_version_id=${encodeURIComponent(versionId)}`).then(v=>{if(active)setItems(v);}).catch(e=>{if(active)setError(String(e));});return()=>{active=false;};},[projectId,versionId]);
  async function upload(){if(!file||!versionId)return;setBusy(true);setError('');const destination=identity;
    try{const body=new FormData();body.set('file',file);body.set('project_id',projectId);body.set('character_version_id',versionId);body.set('source_type',source);body.set('declaration',declaration);
      const asset=await request<AudioAssetDTO>('/api/model-services/audio-assets',{method:'POST',body});
      if(alive.current&&current.current===destination){if(asset.project_id!==projectId||asset.character_version_id!==versionId)throw new Error('返回音频不属于当前人物版本');setItems(old=>[...old,asset]);onChange(asset.id);setFile(null);setFileKey(v=>v+1);}}
    catch(e){if(alive.current&&current.current===destination)setError(String(e));}finally{if(alive.current&&current.current===destination)setBusy(false);}}
  const selected=items.find(v=>v.id===value);
  const content=<><p>{inline?'上传或选择后，保存此角色配音，再开始制作。参考音频应为单人清晰说话，时长0.1至30秒。':'每个人物版本独立绑定参考音频。上传会校验实际音频，采用前仍须核对声音；选择后保存此人物表演才生效。'}</p>
    {!versionId?<p>先选择此人物的身份版本。</p>:<><FormField label={name+'参考音频'}><select disabled={disabled||busy} value={value} onChange={e=>onChange(e.target.value)}><option value="">未绑定（不自动套用其他人物音色）</option>{value&&!selected?<option value={value}>当前绑定尚未读取 · {value}</option>:null}{items.map(v=><option key={v.id} value={v.id}>{v.id} · {v.metadata?.actual?.duration??v.metadata?.duration_seconds??'时长待查看'}秒</option>)}</select></FormField>
    {selected?<><audio controls preload="metadata" src={api.mediaUrl(selected.relative_path)}/><p className="form-hint">SHA256 {selected.sha256} · {selected.metadata?.human_approved?'已人工审核':'待人工核对'}</p></>:null}
    <FormField label="上传本人物参考音频"><input key={fileKey} type="file" accept="audio/*" disabled={disabled||busy} onChange={e=>setFile(e.target.files?.[0]||null)}/></FormField><FormField label="音频来源"><select value={source} disabled={disabled||busy} onChange={e=>setSource(e.target.value)}><option value="ai_generated">原创合成人声</option><option value="user_authorized">已获授权的人声</option><option value="open_license">开放许可人声</option></select></FormField><FormField label="来源与授权说明"><input disabled={disabled||busy} value={declaration} onChange={e=>setDeclaration(e.target.value)} placeholder="说明来源、角色和允许用途"/></FormField><div className="form-actions"><button type="button" className="button button-secondary button-small" disabled={disabled||busy||!file||!declaration.trim()} onClick={()=>void upload()}>{busy?'实际校验并上传…':'上传并选入本人物草稿'}</button>{file?<button type="button" className="button button-ghost button-small" disabled={disabled||busy} onClick={()=>{setFile(null);setFileKey(v=>v+1);}}>取消本次上传</button>:null}</div></>}{error?<p role="alert">{error}</p>:null}</>;
  return inline?<div className="project-voice-picker">{content}</div>:<details className="workspace-details"><summary>{name} 配音参考音频</summary>{content}</details>;
}
