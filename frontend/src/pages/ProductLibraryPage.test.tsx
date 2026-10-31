// UI/API state contracts; real image uploads and restore endpoints are tested in pytest.
import {act,cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import type {ProductAsset} from '../lib/asset-library-contract';
import {ProductLibraryPage} from './ProductLibraryPage';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
function product(overrides:Partial<ProductAsset>={}):ProductAsset{return {id:'p1',name:'待恢复空产品',brand:'',category:'',notes:'',selling_points:[],evidence_notes:'',target_audience:'',state:'archived',revision:2,cover_image_id:null,images:[],created_at:'2026-10-26',updated_at:'2026-10-26',...overrides};}
function pictures(count:number):ProductAsset['images']{return Array.from({length:count},(_,index)=>({id:`image${index}`,asset_id:`source${index}`,filename:`angle-${index}.png`,media_url:`/image-${index}.png`,thumbnail_url:`/thumb-${index}.png`,sha256:index.toString(16).padStart(64,'0'),width:96,height:96,bytes:100,purpose:'front',order:index+1,notes:''}));}
let items:ProductAsset[];
beforeEach(()=>{items=[product()];mocked.mockImplementation(async(url,init)=>{
  if(url==='/api/product-library'||url==='/api/product-library?include_archived=true')return {items:structuredClone(items.filter(item=>url.includes('include_archived')||item.state!=='archived')),limits:{max_images_per_product:32,max_generation_images:8,max_upload_bytes:10*1024**2,formats:['.png']}};
  if(url==='/api/product-library/p1/references')return {references:[]};
  if(url==='/api/product-library/p1/restore'&&init?.method==='POST'){items=items.map(item=>({...item,state:'active',revision:item.revision+2}));return structuredClone(items[0]);}
  throw new Error(`Unexpected product contract ${url}`);
});});
afterEach(()=>{cleanup();mocked.mockReset();});
function mount(){render(<MemoryRouter><ProductLibraryPage/></MemoryRouter>);}

it('exposes archived products even when the active library is empty and waits for actual restore before enabling upload',async()=>{
  let complete!:(value:ProductAsset)=>void;const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>url==='/api/product-library/p1/restore'?new Promise(resolve=>{complete=resolve;}):original(url,init));
  const user=userEvent.setup();mount();await screen.findByText('没有匹配产品。');expect(screen.getByLabelText('批量上传产品图片')).toBeDisabled();await user.click(screen.getByLabelText('显示已移出的产品（可恢复）'));await screen.findByRole('heading',{name:'待恢复空产品'});expect(screen.queryByRole('link',{name:'用此产品生成剧本'})).not.toBeInTheDocument();expect(screen.getByLabelText('批量上传产品图片')).toBeDisabled();await user.click(screen.getByRole('button',{name:'恢复到产品图库'}));expect(screen.getByLabelText('批量上传产品图片')).toBeDisabled();expect(screen.queryByText('空产品已恢复到图库，可直接在上方上传产品图片。')).not.toBeInTheDocument();
  await act(async()=>{items=[product({state:'active',revision:4})];complete(items[0]);});
  expect(await screen.findByText('空产品已恢复到图库，可直接在上方上传产品图片。')).toBeInTheDocument();await waitFor(()=>expect(screen.getByLabelText('批量上传产品图片')).toBeEnabled());expect(screen.getByRole('button',{name:'上传首批产品图片'})).toBeEnabled();expect(screen.queryByRole('button',{name:'恢复到产品图库'})).not.toBeInTheDocument();
  const writes=mocked.mock.calls.filter(([,init])=>init?.method);expect(writes).toHaveLength(1);expect(writes[0][0]).toBe('/api/product-library/p1/restore');expect(writes[0][1]?.method).toBe('POST');
});

it('retains the archived state and disabled uploads when the server rejects restoration',async()=>{
  const original=mocked.getMockImplementation()!;mocked.mockImplementation((url,init)=>url.endsWith('/restore')?Promise.reject(new ApiError('恢复条件未满足',409)):original(url,init));const user=userEvent.setup();mount();await user.click(screen.getByLabelText('显示已移出的产品（可恢复）'));await screen.findByRole('heading',{name:'待恢复空产品'});await user.click(screen.getByRole('button',{name:'恢复到产品图库'}));expect(await screen.findByRole('alert')).toHaveTextContent('恢复条件未满足');expect(screen.getByLabelText('批量上传产品图片')).toBeDisabled();expect(screen.getByRole('button',{name:'恢复到产品图库'})).toBeEnabled();expect(screen.queryByRole('button',{name:'上传首批产品图片'})).not.toBeInTheDocument();
});

it('keeps a needs-attention product available for explicitly owned repair uploads without claiming the old issue is fixed',async()=>{
  items=[product({name:'待修复产品',state:'needs_attention',images:[{id:'image1',asset_id:'source1',filename:'bad.png',media_url:'/image.png',thumbnail_url:'/thumb.png',sha256:'a'.repeat(64),width:800,height:800,bytes:100,purpose:'front',order:1,notes:''}],analysis:{version:1,status:'needs_attention',scope:'Local file check only',model_calls:0,images:[{image_id:'image1',status:'unavailable',error:'sha256_mismatch'}]}})];
  const user=userEvent.setup();mount();await screen.findByRole('heading',{name:'待修复产品'});expect(screen.getByLabelText('批量上传产品图片')).toBeEnabled();expect(screen.getByText(/上传不会自动忽略旧问题/)).toBeInTheDocument();expect(screen.queryByRole('link',{name:'用此产品生成剧本'})).not.toBeInTheDocument();await user.click(screen.getByRole('button',{name:'上传修复图片'}));expect(screen.getByLabelText('批量上传产品图片')).toHaveFocus();await user.upload(screen.getByLabelText('批量上传产品图片'),new File(['unit fixture'],'repair.png',{type:'image/png'}));expect(screen.getByLabelText('repair.png归属')).toHaveValue('p1');expect(screen.getByRole('option',{name:'待修复产品 · 文件需处理'})).toBeInTheDocument();expect(screen.getByText('本产品有文件需要处理')).toBeInTheDocument();expect(mocked.mock.calls.some(([,init])=>init?.method)).toBe(false);
});

it.each([9,32])('allows local file checking for all %i library images despite the separate eight-image generation limit',async count=>{
  items=[product({name:'多图文件检查',state:'active',images:pictures(count)})];
  const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>{
    if(url==='/api/product-library/p1/reanalyze'&&init?.method==='POST'){
      items=[{...items[0],revision:3,analysis:{version:1,status:'succeeded',scope:'Local file check only; no semantic model',model_calls:0,images:items[0].images.map(image=>({image_id:image.id,status:'verified',width:96,height:96}))}}];
      return Promise.resolve(structuredClone(items[0]));
    }
    return original(url,init);
  });
  const user=userEvent.setup();mount();
  const check=await screen.findByRole('button',{name:'重新检查本地产品文件'});
  expect(check).toBeEnabled();expect(screen.getByText(/可检查图库内最多 32 张图片/)).toBeInTheDocument();
  await user.click(check);
  expect(await screen.findByText(`检查 v1 · 文件检查通过 · 模型调用 0 次`)).toBeInTheDocument();
  expect(mocked.mock.calls.filter(([,init])=>init?.method)).toEqual([['/api/product-library/p1/reanalyze',{method:'POST'}]]);
  expect(screen.getByRole('heading',{name:`产品图片 · ${count} 张`})).toBeInTheDocument();
});

it.each([{count:33,limit:32},{count:13,limit:12}])('blocks local file checking only beyond the server library limit ($count/$limit)',async({count,limit})=>{
  items=[product({name:'超出文件检查上限',state:'active',images:pictures(count)})];
  const original=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>{
    const result=await original(url,init);
    return url==='/api/product-library'?{...(result as object),limits:{max_images_per_product:limit,max_generation_images:8,max_upload_bytes:10*1024**2,formats:['.png']}}:result;
  });
  mount();expect(await screen.findByRole('button',{name:'重新检查本地产品文件'})).toBeDisabled();
  expect(screen.getByText(new RegExp(`超出图库文件检查上限 ${limit} 张`))).toBeInTheDocument();
  expect(mocked.mock.calls.some(([,init])=>init?.method)).toBe(false);
});
