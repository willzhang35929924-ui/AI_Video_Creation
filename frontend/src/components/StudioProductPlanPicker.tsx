import {useEffect,useState} from 'react';
import {Link} from 'react-router-dom';
import {api,request} from '../lib/api';
import type {ProductLibraryOverview} from '../lib/asset-library-contract';
import type {StudioProductReferencePlan} from '../lib/studio-contract';
import '../asset-library.css';

export function StudioProductPlanPicker({enabled,onEnabled,value,onChange,disabled}:{enabled:boolean;onEnabled:(value:boolean)=>void;value:StudioProductReferencePlan;onChange:(value:StudioProductReferencePlan)=>void;disabled:boolean}){
  const [products,setProducts]=useState<ProductLibraryOverview|null>(null),[error,setError]=useState(''),[reload,setReload]=useState(0);
  useEffect(()=>{if(!enabled)return;const abort=new AbortController();setError('');setProducts(null);void request<ProductLibraryOverview>('/api/product-library',{signal:abort.signal}).then(data=>{if(!abort.signal.aborted)setProducts(data);}).catch(e=>{if(!abort.signal.aborted)setError(String(e));});return()=>abort.abort();},[enabled,reload]);
  const current=products?.items.find(item=>item.id===value.product_id&&item.state==='active');
  return <fieldset className="asset-picker" disabled={disabled}><legend>产品实物参考（可选）</legend>
    <label className="studio-check"><input type="checkbox" checked={enabled} onChange={e=>onEnabled(e.target.checked)}/><span>本次按所选产品原图自动配置相关镜头</span></label>
    {enabled?<><p className="form-hint">只选择希望本次使用的原始照片，例如同一语言的包装和托盘。程序依据剧本道具自动识别相关镜头；已有视频的镜头保持原输入，旧关键画面会按新参考重新制作。实际产品外观仍需审核。</p>
      <p><Link to="/assets/products">管理产品图库</Link> <button type="button" className="button button-secondary button-small" onClick={()=>setReload(n=>n+1)}>刷新产品图库</button></p>
      {error?<p role="alert">产品图库读取失败：{error}</p>:null}{!products&&!error?<p role="status">读取产品原图…</p>:null}
      <label>本次产品<select aria-label="本次产品" value={value.product_id} onChange={e=>onChange({...value,product_id:e.target.value,image_ids:[]})}><option value="">请选择产品</option>{products?.items.filter(item=>item.state==='active').map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      {current?<div className="asset-picker-images" aria-label="本次产品原图">{current.images.map(item=><label key={item.id}><input type="checkbox" checked={value.image_ids.includes(item.id)} disabled={!value.image_ids.includes(item.id)&&value.image_ids.length>=4} onChange={e=>onChange({...value,image_ids:e.target.checked?[...value.image_ids,item.id]:value.image_ids.filter(id=>id!==item.id)})}/><img src={api.mediaUrl(item.thumbnail_url||item.media_url)} alt=""/><span>{item.filename}</span></label>)}</div>:null}
      <p className="form-hint">已选择 {value.image_ids.length}/4 张。请确认照片属于同一产品和包装版本。</p>
      <label>选择说明<input aria-label="选择说明" value={value.selection_reason} maxLength={2000} onChange={e=>onChange({...value,selection_reason:e.target.value})} placeholder="例如：英文盒面与同款托盘"/></label>
      {!value.product_id||!value.image_ids.length||!value.selection_reason.trim()?<p role="status">请选择产品、至少一张原图并填写选择说明后开始。</p>:null}
    </>:null}
  </fieldset>;
}
