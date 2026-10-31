import {useCallback,useEffect,useRef,useState} from 'react';
import {api,ApiError,request} from '../lib/api';
import type {CharacterReferenceAsset,CharacterReferenceOverview,CharacterReferencePurpose,CharacterReferenceRow} from '../lib/character-reference-contract';
import {Badge,FormField} from './ui';
import '../screenplay-generation.css';

const purposes:{id:CharacterReferencePurpose;name:string}[]=[{id:'identity_front',name:'正面身份'},{id:'identity_side',name:'侧面身份'},{id:'full_body',name:'全身比例'},{id:'wardrobe',name:'服装造型'}];
const states:Record<string,string>={no_reference:'无参考图，使用剧本描述',none:'无参考图，使用剧本描述',saved:'已保存参考图，等待后续生成',ready:'已保存参考图，等待后续生成',applied:'参考图已应用到任务',succeeded:'素材生成成功',generated:'素材生成成功',needs_review:'素材已生成，等待审核',approved:'素材已审核',failed:'生成失败，需要处理',unsupported:'当前模型不支持参考图',reference_limit:'参考图数量超出模型额度，请调整',queued:'参考图任务等待执行',running:'正在使用参考图生成'};
type State={busy:boolean;dirty:boolean};
type Props={projectId:string;refreshKey?:string|number;disabled?:boolean;onStateChange?:(state:State)=>void;onChanged?:()=>void|Promise<void>};
function CharacterCard({projectId,row,limit,disabled,onChanged,onStateChange}:{projectId:string;row:CharacterReferenceRow;limit:number;disabled:boolean;onChanged:()=>void|Promise<void>;onStateChange:(state:State)=>void}){
  const [baseline,setBaseline]=useState(row),[selected,setSelected]=useState<CharacterReferenceAsset[]>(row.references),[drafts,setDrafts]=useState(row.drafts),[files,setFiles]=useState<File[]>([]),[purpose,setPurpose]=useState<CharacterReferencePurpose>('identity_front'),[source,setSource]=useState(''),[busy,setBusy]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState(''),[conflict,setConflict]=useState(false);
  const [fileKey,setFileKey]=useState(0),[uploadRows,setUploadRows]=useState<{name:string;state:string;error?:string}[]>([]);
  const file=files[0];
  const maximum=baseline.max_images??4;
  const alive=useRef(true),busyRef=useRef(false),current=useRef(projectId);current.current=projectId;
  const dirty=Boolean(file)||JSON.stringify(selected.map(item=>[item.id,item.purpose]))!==JSON.stringify(baseline.references.map(item=>[item.id,item.purpose]));
  const dirtyRef=useRef(dirty);dirtyRef.current=dirty;
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;onStateChange({busy:false,dirty:false});};},[]);
  useEffect(()=>{if(!dirtyRef.current&&!busyRef.current){setBaseline(row);setSelected(row.references);setDrafts(row.drafts);}},[row]);
  useEffect(()=>{onStateChange({busy:Boolean(busy),dirty});},[busy,dirty,onStateChange]);
  const endpoint=`/api/studio/projects/${encodeURIComponent(projectId)}/character-references/${encodeURIComponent(row.character_id)}`;
  async function upload(){
    if(!files.length||disabled||busyRef.current)return;
    if(selected.length+files.length>maximum){setError(`此人物当前最多保存 ${maximum} 张项目参考图，请减少本次选择或先移除当前参考。`);return;}
    const destination=projectId;busyRef.current=true;setBusy('upload');setError('');setUploadRows(files.map(file=>({name:file.name,state:'等待上传'})));const failed:File[]=[];
    try{for(let index=0;index<files.length;index++){const file=files[index];if(!alive.current||current.current!==destination)break;setUploadRows(rows=>rows.map((row,n)=>n===index?{...row,state:'正在上传并校验'}:row));try{
      if(file.size>limit)throw new Error(`图片超过 ${Math.round(limit/1024**2)} MB 限制`);const form=new FormData();form.set('file',file);form.set('purpose',purpose);form.set('source_note',source);const asset=await request<CharacterReferenceAsset>(endpoint+'/uploads',{method:'POST',body:form});
      if(alive.current&&current.current===destination){setDrafts(items=>[...items.filter(item=>item.id!==asset.id),asset]);setSelected(items=>items.some(item=>item.id===asset.id)?items:[...items,{...asset,purpose,order:items.length+1}]);setUploadRows(rows=>rows.map((row,n)=>n===index?{...row,state:'已保存到此人物草稿'}:row));}
    }catch(error){failed.push(file);if(alive.current&&current.current===destination)setUploadRows(rows=>rows.map((row,n)=>n===index?{...row,state:'上传失败',error:String(error)}:row));}}
      if(alive.current&&current.current===destination){setFiles(failed);setFileKey(value=>value+1);setNotice(failed.length?'部分图片上传失败，可点击上传按钮重试失败文件。':'图片已上传到此人物草稿。点击保存后才会用于后续生成。');}
    }finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  async function save(){
    if(disabled||busyRef.current||file||conflict)return;const destination=projectId;busyRef.current=true;setBusy('save');setError('');
    try{const response=await request<CharacterReferenceOverview>(endpoint,{method:'PUT',body:JSON.stringify({expected_revision:baseline.revision,references:selected.map((asset,index)=>({asset_id:asset.id,purpose:asset.purpose,order:index+1}))})});
      const updated=response.characters.find(item=>item.character_id===row.character_id);if(!updated)throw new Error('保存响应缺少当前人物');
      if(alive.current&&current.current===destination){setBaseline(updated);setSelected(updated.references);setDrafts(updated.drafts);setNotice(selected.length?'参考图已保存，仅用于此人物后续新任务。':'参考关系已解除，历史图片与生成记录保留。');await onChanged();}
    }catch(e){if(alive.current&&current.current===destination){setConflict(e instanceof ApiError&&e.status===409);setError(String(e));}}finally{busyRef.current=false;if(alive.current)setBusy('');}
  }
  function discard(){setSelected(row.references);setBaseline(row);setDrafts(row.drafts);setFiles([]);setFileKey(value=>value+1);setConflict(false);setError('');setNotice('已放弃未保存的选择；上传草稿未绑定到生成任务。');void onChanged();}
  function move(index:number,by:number){setSelected(items=>{const next=[...items],other=index+by;if(other<0||other>=next.length)return items;[next[index],next[other]]=[next[other],next[index]];return next;});}
  const recent=row.last_application;
  const availableDrafts=drafts.filter(item=>!selected.some(asset=>asset.id===item.id));
  return <article className="character-reference-card" aria-label={`${row.name}人物参考图`}><div className="character-reference-heading"><div><h3>{row.name}</h3><p className="form-hint">剧本描述：{row.description||'按当前人物档案生成'}</p></div><Badge tone={recent?.status==='failed'?'warning':selected.length?'neutral':'neutral'}>{states[baseline.state]|| (baseline.references.length?'已保存参考图':'无参考图，使用剧本描述')}</Badge></div>
    {baseline.limit_reason?<p className="form-hint">{baseline.limit_reason}</p>:null}
    {selected.length?<div className="character-reference-images">{selected.map((asset,index)=><div className="character-reference-image" key={asset.id}><img src={api.mediaUrl(asset.media_url)} alt={`${row.name}参考图 ${index+1}`}/><div className="character-reference-image-controls"><FormField label={`${row.name}参考图 ${index+1} 用途`}><select disabled={disabled||!!busy||conflict} value={asset.purpose} onChange={e=>setSelected(items=>items.map(item=>item.id===asset.id?{...item,purpose:e.target.value as CharacterReferencePurpose}:item))}>{purposes.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></FormField><small>{asset.width} × {asset.height} · {asset.state==='draft'?'未保存草稿':'已保存参考'}</small><div className="character-reference-order"><button type="button" className="button button-secondary button-small" disabled={disabled||!!busy||index===0||conflict} onClick={()=>move(index,-1)} aria-label={`${row.name}参考图 ${index+1} 上移`}>上移</button><button type="button" className="button button-secondary button-small" disabled={disabled||!!busy||index===selected.length-1||conflict} onClick={()=>move(index,1)} aria-label={`${row.name}参考图 ${index+1} 下移`}>下移</button><button type="button" className="button button-ghost-danger button-small" disabled={disabled||!!busy||conflict} onClick={()=>setSelected(items=>items.filter(item=>item.id!==asset.id))}>移除此参考</button></div></div></div>)}</div>:<p className="form-hint">没有参考图也可以制作。系统继续使用此人物的剧本描述和已锁定身份。</p>}
    {availableDrafts.length?<FormField label={`${row.name}已有未绑定草稿`}><select value="" disabled={disabled||!!busy||conflict||selected.length>=maximum} onChange={e=>{const asset=availableDrafts.find(item=>item.id===e.target.value);if(asset)setSelected(items=>[...items,asset]);}}><option value="">选择草稿加入本次参考列表</option>{availableDrafts.map(item=><option key={item.id} value={item.id}>{purposes.find(p=>p.id===item.purpose)?.name} · {item.id}</option>)}</select></FormField>:null}
    <div className="character-reference-upload" onDragOver={event=>event.preventDefault()} onDrop={event=>{event.preventDefault();if(!disabled&&!busy&&!conflict)setFiles(Array.from(event.dataTransfer.files));}}><FormField label={`上传${row.name}人物参考图`} hint={`多选或拖放 PNG、JPEG、WebP · 每张最大 ${Math.round(limit/1024**2)} MB · 最多 ${maximum} 张`}><input key={fileKey} type="file" multiple accept=".png,.jpg,.jpeg,.webp" disabled={disabled||!!busy||conflict} onChange={event=>setFiles(Array.from(event.target.files||[]))}/></FormField><FormField label={`${row.name}新图用途`}><select value={purpose} disabled={disabled||!!busy||conflict} onChange={e=>setPurpose(e.target.value as CharacterReferencePurpose)}>{purposes.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></FormField><FormField label={`${row.name}图片来源说明`}><input value={source} disabled={disabled||!!busy||conflict} placeholder="原创人物、授权照片或其他来源" onChange={e=>setSource(e.target.value)}/></FormField><button type="button" className="button button-secondary button-small" disabled={disabled||!!busy||!file||conflict||selected.length>=maximum} onClick={()=>void upload()}>{busy==='upload'?'正在上传…':'上传并加入人物草稿'}</button>{files.length>1?<p className="form-hint">本次选择 {files.length} 张图片，仅归属到 {row.name}。</p>:null}</div>{uploadRows.length?<ul className="form-hint">{uploadRows.map((row,index)=><li key={index}>{row.name} · {row.state}{row.error?<span role="alert"> · {row.error}</span>:null}</li>)}</ul>:null}
    {dirty?<p className="form-hint">有未保存的参考选择。当前制作仍只读取已保存参考，不会使用这份草稿。</p>:null}
    {error?<p role="alert" className="inline-notice warning">{error}{conflict?' 此人物参考已被其他窗口修改。请放弃草稿后刷新，再决定新的参考。':''}</p>:null}{notice?<p role="status" className="form-hint">{notice}</p>:null}
    <div className="screenplay-actions"><button type="button" className="button button-primary button-small" disabled={disabled||!!busy||!dirty||Boolean(file)||conflict||selected.length>maximum} onClick={()=>void save()}>{busy==='save'?'正在保存…':`保存${row.name}人物参考`}</button>{dirty||conflict?<button type="button" className="button button-secondary button-small" disabled={!!busy} onClick={discard}>放弃未保存修改</button>:null}</div>
    {recent?<p className="character-reference-status">实际应用任务：{recent.job_id} · {states[recent.status]||recent.status} · 使用 {recent.reference_ids.length} 张此人物参考{recent.error_message?` · ${recent.error_message}`:''}</p>:null}
  </article>;
}

export function ProjectCharacterReferences({projectId,refreshKey='',disabled=false,onStateChange,onChanged}:Props){
  const [data,setData]=useState<CharacterReferenceOverview|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(false),[open,setOpen]=useState(false),[reload,setReload]=useState(0),[statesByRole,setStatesByRole]=useState<Record<string,State>>({});
  const current=useRef(projectId),alive=useRef(true);current.current=projectId;const callbacks=useRef({onStateChange,onChanged});callbacks.current={onStateChange,onChanged};
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;callbacks.current.onStateChange?.({busy:false,dirty:false});};},[]);
  const dirty=Object.values(statesByRole).some(item=>item.dirty),busy=Object.values(statesByRole).some(item=>item.busy);
  useEffect(()=>{setData(null);setError('');setStatesByRole({});},[projectId]);
  useEffect(()=>{
    if(!open)return;const controller=new AbortController(),destination=projectId;let timer:ReturnType<typeof setTimeout>|undefined;setLoading(true);
    async function fetchOverview(){try{
      const value=await request<CharacterReferenceOverview>(`/api/studio/projects/${encodeURIComponent(destination)}/character-references`,{signal:controller.signal});
      if(!controller.signal.aborted&&alive.current&&current.current===destination){if(value.project_id!==destination)throw new Error('参考图响应不属于当前项目');setData(value);setError('');}
    }catch(e){if(!controller.signal.aborted&&alive.current&&current.current===destination)setError(String(e));}
    finally{if(!controller.signal.aborted&&alive.current&&current.current===destination){setLoading(false);timer=setTimeout(()=>void fetchOverview(),10000);}}}
    void fetchOverview();return()=>{controller.abort();if(timer)clearTimeout(timer);};
  },[projectId,open,reload,refreshKey]);
  useEffect(()=>{callbacks.current.onStateChange?.({busy,dirty});},[busy,dirty]);
  const changed=useCallback(async()=>{setReload(value=>value+1);await callbacks.current.onChanged?.();},[]);
  const stateCallbacks=useRef(new Map<string,(state:State)=>void>());
  function reportState(id:string){if(!stateCallbacks.current.has(id))stateCallbacks.current.set(id,state=>{if(alive.current)setStatesByRole(current=>({...current,[id]:state}));});return stateCallbacks.current.get(id)!;}
  const ownedData=data?.project_id===projectId?data:null;
  return <details className="character-project-references" open={open} onToggle={event=>setOpen(event.currentTarget.open)}><summary>人物角色参考图（可选）</summary><p className="form-hint">只为需要的角色上传。保存后用于该角色后续新生成任务；不会改写已审核素材。未保存的草稿不参与制作。</p>{loading&&!data?<p role="status">读取人物参考图…</p>:null}{error?<p role="alert">{error}<button type="button" className="button button-secondary button-small" onClick={()=>setReload(value=>value+1)}>重新读取</button></p>:null}{ownedData?<div className="character-reference-list">{ownedData.characters.map(row=><CharacterCard key={`${projectId}:${row.character_id}`} projectId={projectId} row={row} limit={ownedData.limits.max_upload_bytes} disabled={disabled} onChanged={changed} onStateChange={reportState(row.character_id)}/>)}{!ownedData.characters.length?<p>项目建立人物后，可在这里选择性上传参考图。</p>:null}</div>:null}</details>;
}
