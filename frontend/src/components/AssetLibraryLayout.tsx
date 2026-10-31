import {type ReactNode,useState} from 'react';
import {NavLink,Link} from 'react-router-dom';
import {RefreshCw} from 'lucide-react';
import {PageHeader,FormField} from './ui';
import '../asset-library.css';

export type AssetReference={project_id:string;project_name?:string;character_id?:string;character_name?:string;reference_type?:string;id?:string;href?:string;binding_id?:string};
export function AssetLibraryLayout({title,description,search,onSearch,category,onCategory,categories=[],busy=false,onRefresh,children}:{title:string;description:string;search:string;onSearch:(value:string)=>void;category?:string;onCategory?:(value:string)=>void;categories?:string[];busy?:boolean;onRefresh:()=>void;children:ReactNode}){
  return <div className="asset-center"><PageHeader eyebrow="辅助与高级工具 · 本地资产中心" title={title} description={description} actions={<button type="button" className="button button-secondary" disabled={busy} onClick={onRefresh}><RefreshCw size={15}/>刷新图库</button>}/><nav className="asset-tabs" aria-label="资产库"><NavLink to="/assets/products">产品图库</NavLink><NavLink to="/assets/videos">参考视频库</NavLink><NavLink to="/assets/voices">配音库</NavLink></nav><div className="asset-toolbar"><FormField label={`搜索${title}`}><input type="search" value={search} onChange={event=>onSearch(event.target.value)} placeholder="名称、分类、标签或备注"/></FormField>{onCategory?<FormField label="分类筛选"><select value={category||''} onChange={event=>onCategory(event.target.value)}><option value="">全部分类</option>{categories.map(value=><option key={value} value={value}>{value}</option>)}</select></FormField>:null}</div>{children}</div>;
}

export function AssetReferences({references,onUnlink,busy=false}:{references:AssetReference[];onUnlink?:(reference:AssetReference)=>Promise<void>;busy?:boolean}){
  return <section className="asset-reference-list"><h3>引用关系</h3>{references.length?references.map((reference,index)=><article key={reference.id||`${reference.project_id}:${reference.character_id||index}`}><div>{reference.href||reference.project_id?<Link to={reference.href||`/?project=${encodeURIComponent(reference.project_id)}`}>{reference.project_name||reference.project_id}</Link>:<span>{reference.project_name||'历史资料引用'}</span>}{reference.character_name?<small> · {reference.character_name}</small>:null}{reference.reference_type?<small> · {reference.reference_type}</small>:null}</div>{onUnlink&&reference.binding_id?<button type="button" className="button button-secondary button-small" disabled={busy} onClick={()=>void onUnlink(reference)}>仅解除此引用</button>:null}</article>):<p className="form-hint">当前没有引用记录。</p>}</section>;
}

export function AssetRemovalOptions({name,onArchive,onPreview,onClose}:{name:string;onArchive?:()=>Promise<void>;onPreview:()=>void;onClose:()=>void}){
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  async function archive(){if(!onArchive||busy)return;setBusy(true);setError('');try{await onArchive();onClose();}catch(e){setError(String(e));}finally{setBusy(false);}}
  return <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-label="资产移除选项"><h2>移除「{name}」</h2>{onArchive?<><p>仅从图库移除会保留历史引用和磁盘文件。</p><button type="button" className="button button-secondary" disabled={busy} onClick={()=>void archive()}>{busy?'正在从图库移除…':'仅从图库移除，保留历史与文件'}</button></>:null}<p>如需删除记录或处理磁盘文件，请先预览实际引用、共享保护和影响范围。永久删除还需要单独确认。</p>{error?<p role="alert" className="inline-notice warning">{error}</p>:null}<div className="asset-actions"><button type="button" className="button button-ghost-danger" disabled={busy} onClick={onPreview}>预览删除记录与文件的影响</button><button type="button" className="button button-secondary" disabled={busy} onClick={onClose}>取消</button></div></section></div>;
}

