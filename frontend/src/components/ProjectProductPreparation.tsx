import {useCallback,useEffect,useRef,useState} from 'react';
import {api,request} from '../lib/api';
import type {ProductAsset,ProductLibraryOverview} from '../lib/asset-library-contract';
import {FormField} from './ui';
import {ProjectProductReferences} from './ProjectProductReferences';

type Selection={project_id:string;product:ProductAsset;expected_token:string;image_ids:string[];shot_ids:string[];bound:boolean;shots:{id:string;order:number;title:string;suggested:boolean}[];note:string};
type State={busy:boolean;dirty:boolean;overBudget:boolean};

/** Files are saved to the shared library; explicit shot choices drive both generators. */
export function ProjectProductPreparation({projectId,shots,disabled,onStateChange,onChanged}:{projectId:string;shots:{id:string;order:number;title:string}[];disabled:boolean;onStateChange?:(state:State)=>void;onChanged?:()=>void|Promise<void>}){
  const [open,setOpen]=useState(false),[library,setLibrary]=useState<ProductLibraryOverview|null>(null),[productId,setProductId]=useState('');
  const [selection,setSelection]=useState<Selection|null>(null),[imageIds,setImageIds]=useState<string[]>([]),[shotIds,setShotIds]=useState<string[]>([]);
  const [name,setName]=useState(''),[files,setFiles]=useState<File[]>([]),[fileKey,setFileKey]=useState(0),[uploadTarget,setUploadTarget]=useState('');
  const [busy,setBusy]=useState(''),[loading,setLoading]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState(''),[reload,setReload]=useState(0);
  const [detailState,setDetailState]=useState<State>({busy:false,dirty:false,overBudget:false});
  const onDetailState=useCallback((state:State)=>setDetailState(state),[]);
  const callbacks=useRef({onStateChange,onChanged});callbacks.current={onStateChange,onChanged};
  const onDetailChanged=useCallback(async()=>{setReload(n=>n+1);await callbacks.current.onChanged?.();},[]);
  const active=useRef(true),busyRef=useRef(false);useEffect(()=>{active.current=true;return()=>{active.current=false;callbacks.current.onStateChange?.({busy:false,dirty:false,overBudget:false});};},[]);
  const referenceDirty=Boolean(selection&&(JSON.stringify(imageIds)!==JSON.stringify(selection.image_ids)||JSON.stringify(shotIds)!==JSON.stringify(selection.shot_ids)));
  const dirty=Boolean(files.length||name.trim())||referenceDirty;
  useEffect(()=>{callbacks.current.onStateChange?.({busy:Boolean(busy)||detailState.busy,dirty:dirty||detailState.dirty,overBudget:detailState.overBudget});},[busy,dirty,detailState]);
  useEffect(()=>{if(!open)return;const abort=new AbortController();void request<ProductLibraryOverview>('/api/product-library?include_archived=true',{signal:abort.signal}).then(value=>{if(!abort.signal.aborted)setLibrary(value);}).catch(e=>{if(!abort.signal.aborted)setError(String(e));});return()=>abort.abort();},[open,reload]);
  useEffect(()=>{setSelection(null);setImageIds([]);setShotIds([]);if(!open||!productId)return;const abort=new AbortController();setLoading(true);void request<Selection>(`/api/product-library/projects/${encodeURIComponent(projectId)}/references?product_id=${encodeURIComponent(productId)}`,{signal:abort.signal}).then(value=>{
    if(abort.signal.aborted)return;if(value.project_id!==projectId||value.product.id!==productId)throw new Error('产品参考响应不属于当前项目或产品');
    setSelection(value);setImageIds(value.image_ids);setShotIds(value.shot_ids);
  }).catch(e=>{if(!abort.signal.aborted)setError(String(e));}).finally(()=>{if(!abort.signal.aborted)setLoading(false);});return()=>abort.abort();},[projectId,productId,open,reload]);
  const blocked=disabled||Boolean(busy)||detailState.busy||detailState.dirty;
  async function upload(){
    if(blocked||busyRef.current||!files.length||(!uploadTarget&&!name.trim()))return;
    if(files.length>32){setError('每批最多上传32张图片；不同产品请分组上传。');return;}
    if(referenceDirty&&!window.confirm('上传完成后将重新读取产品引用。放弃尚未保存的分镜与图片选择？'))return;
    busyRef.current=true;setBusy('upload');setError('');setNotice('');
    try{
      let target=uploadTarget;
      if(!target){const product=await request<ProductAsset>('/api/product-library',{method:'POST',body:JSON.stringify({name:name.trim(),category:'项目固定产品／道具'})});target=product.id;if(active.current){setUploadTarget(target);setName('');}}
      const form=new FormData();files.forEach(file=>form.append('files',file));form.set('purpose','other');form.set('notes','项目工作台批量上传的固定产品／道具参考');
      const result=await request<{product:ProductAsset;added:unknown[];duplicates:unknown[];errors:{filename:string;error:string}[]}>(`/api/product-library/${encodeURIComponent(target)}/images`,{method:'POST',body:form});
      if(!active.current)return;
      const failedNames=new Set(result.errors.map(item=>item.filename));setFiles(previous=>previous.filter(file=>failedNames.has(file.name)));setFileKey(value=>value+1);setProductId(target);setReload(value=>value+1);
      setNotice(`服务器已确认保存 ${result.added.length} 张，重复原图 ${result.duplicates.length} 张。请继续勾选制作图片与使用分镜，再保存项目引用。`);
      if(result.errors.length)setError(result.errors.map(item=>`${item.filename}：${item.error}`).join('；'));
    }catch(e){if(active.current)setError(String(e));}finally{busyRef.current=false;if(active.current)setBusy('');}
  }
  async function save(){
    if(blocked||busyRef.current||!selection||shotIds.length>0&&(!imageIds.length||imageIds.length>4))return;
    busyRef.current=true;setBusy('save');setError('');setNotice('');
    try{
      const value=await request<Selection>(`/api/product-library/projects/${encodeURIComponent(projectId)}/references`,{method:'PUT',body:JSON.stringify({product_id:productId,expected_token:selection.expected_token,image_ids:shotIds.length?imageIds:[],shot_ids:shotIds})});
      if(!active.current)return;setSelection(value);setImageIds(value.image_ids);setShotIds(value.shot_ids);setReload(n=>n+1);
      setNotice(`已保存：${shotIds.length} 个分镜使用此产品原图。后续关键画面和视频自动使用所选参考；旧视频保留。`);await callbacks.current.onChanged?.();
    }catch(e){if(active.current)setError(String(e));}finally{busyRef.current=false;if(active.current)setBusy('');}
  }
  function changeProduct(value:string){if(dirty&&!window.confirm('放弃当前尚未保存的图片与分镜选择？'))return;setFiles([]);setName('');setFileKey(n=>n+1);setProductId(value);setError('');setNotice('');}
  return <details className="workspace-details" open={open} onToggle={event=>{if(dirty||busy||detailState.dirty||detailState.busy){event.currentTarget.open=true;return;}setOpen(event.currentTarget.open);}}>
    <summary>固定产品／道具参考图（可选）</summary>
    <p className="form-hint">可以不配置。需要保持同款产品、包装或道具时，批量上传同一产品的照片，或从产品图库选择，然后明确用于哪些分镜。人物身份图与产品外观图分别传给模型；不同产品请分组保存。</p>
    {open?<>
      <fieldset className="asset-picker" disabled={blocked}><legend>批量上传到产品图库</legend>
        <FormField label="上传归属"><select value={uploadTarget} disabled={files.length>0&&Boolean(uploadTarget)} onChange={e=>{setUploadTarget(e.target.value);setName('');}}><option value="">新建产品／道具</option>{library?.items.filter(item=>item.state==='active').map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></FormField>
        {!uploadTarget?<FormField label="产品／道具名称" hint="使用剧本中的同一名称，便于匹配分镜。"><input value={name} maxLength={200} onChange={e=>setName(e.target.value)} placeholder="例如：捏捏乐礼盒"/></FormField>:null}
        <FormField label="批量选择产品／道具图" hint="PNG、JPEG、WebP，每批最多32张，单张10MB。上传后再选择本次制作需要的图片。"><input key={fileKey} type="file" multiple accept="image/png,image/jpeg,image/webp" onChange={e=>setFiles(Array.from(e.target.files||[]))}/></FormField>
        {files.length?<p>待上传 {files.length} 张：{files.map(file=>file.name).join('、')}</p>:null}
        <div className="asset-actions"><button type="button" className="button button-secondary" disabled={!files.length||(!uploadTarget&&!name.trim())} onClick={()=>void upload()}>{busy==='upload'?'正在上传并校验原图…':'上传并保存到图库'}</button>{files.length||name?<button type="button" className="button button-ghost" onClick={()=>{setFiles([]);setName('');setFileKey(n=>n+1);}}>清空上传草稿</button>:null}</div>
      </fieldset>
      <fieldset className="asset-picker" disabled={blocked}><legend>从产品图库选择并绑定分镜</legend>
        <FormField label="固定产品／道具"><select value={productId} onChange={e=>changeProduct(e.target.value)}><option value="">请选择产品／道具</option>{library?.items.map(item=><option key={item.id} value={item.id}>{item.name}{item.state!=='active'?'（不可新用，可解除引用）':''}</option>)}</select></FormField>
        <p className="form-hint">可以依次配置多个产品。切换产品只切换编辑对象；已保存的其他产品引用继续保留。</p>
        <button type="button" className="button button-ghost" onClick={()=>{if(dirty&&!window.confirm('放弃尚未保存的产品参考草稿并重新读取？'))return;setFiles([]);setName('');setFileKey(n=>n+1);setReload(n=>n+1);}}>刷新图库与项目引用</button>
        {loading?<p role="status">读取产品原图与项目分镜…</p>:null}
        {selection?<><p>本次制作选择 {imageIds.length}/4 张；同一包装版本请保持一致。人物与场景也占参考图预算，超出模型能力会明确提示。</p>
          <div className="asset-picker-images">{selection.product.images.map((image,index)=><label key={image.id}><input type="checkbox" aria-label={`${selection.product.name}参考图 ${index+1}`} checked={imageIds.includes(image.id)} disabled={!imageIds.includes(image.id)&&imageIds.length>=4} onChange={e=>setImageIds(ids=>e.target.checked?[...ids,image.id]:ids.filter(id=>id!==image.id))}/><img src={api.mediaUrl(image.thumbnail_url||image.media_url)} alt={`${selection.product.name}原图 ${index+1}`}/><span>{image.filename}</span></label>)}</div>
          <p>使用此产品的分镜（只影响勾选的镜头）</p>
          <button type="button" className="button button-secondary button-small" disabled={!selection.shots.some(shot=>shot.suggested)} onClick={()=>setShotIds(selection.shots.filter(shot=>shot.suggested).map(shot=>shot.id))}>按剧本同名内容选择分镜</button>
          <p className="form-hint">名称匹配只是建议，请核对。无法匹配时手动勾选；未勾选的分镜不会收到此产品图。取消全部分镜并保存，可解除此产品的制作引用。</p>
          {selection.shots.map(shot=><label className="studio-check" key={shot.id}><input type="checkbox" checked={shotIds.includes(shot.id)} onChange={e=>setShotIds(ids=>e.target.checked?[...ids,shot.id]:ids.filter(id=>id!==shot.id))}/><span>#{shot.order} · {shot.title}{shot.suggested?' · 同名匹配':''}</span></label>)}
          <button type="button" className="button button-primary" disabled={shotIds.length>0&&(!imageIds.length||imageIds.length>4)||(!dirty&&selection.bound)} onClick={()=>void save()}>{busy==='save'?'正在保存项目引用…':'保存产品／道具项目引用'}</button>
        </>:null}
      </fieldset>
      <ProjectProductReferences key={`${projectId}:${reload}`} projectId={projectId} shots={shots} disabled={disabled||Boolean(busy)||dirty} onStateChange={onDetailState} onChanged={onDetailChanged}/>
    </>:null}
    {error?<p role="alert" className="inline-notice warning">{error}</p>:null}{notice?<p role="status" className="inline-notice">{notice}</p>:null}
  </details>;
}
