// API payload contracts; actual media/model acceptance is performed separately.
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ScreenplayGenerationPage} from './ScreenplayGenerationPage';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const products=['一','二'].map((name,index)=>({id:`p${index+1}`,name:`产品${name}`,brand:'自有品牌',category:'杯具',selling_points:['已确认：黑色'],images:[{id:`image-${index+1}`,asset_id:`asset-${index+1}`,filename:`产品${name}.png`,thumbnail_url:'/thumb.png',media_url:'/image.png'}]}));
const videos=['一','二'].map((name,index)=>({id:`v${index+1}`,name:`参考${name}`,category:'实拍',notes:'',metadata:{duration_seconds:10},latest_analysis:{status:'succeeded'}}));
const job=(id='job1')=>({id,project_name:'新产品广告',input_mode:'image',status:'awaiting_review',stage:'awaiting_review',revision:1,creative_prompt:'展示真实产品',documents:[],uncertainties:[]});
beforeEach(()=>mocked.mockImplementation(async(url,init)=>{
  if(url==='/api/screenplay/options')return {modes:['image','video','text'].map(input_mode=>({input_mode,default_service_id:'local',candidates:[{service_id:'local',model_id:'vision',eligible:true}],limits:{}})),services:[{id:'local',provider:'local_llm',default_model:'vision',enabled:true}]};
  if(url==='/api/screenplay/jobs'&&!init?.method)return [];
  if(url==='/api/product-library')return {items:products,limits:{max_generation_images:8}};
  if(url==='/api/reference-videos')return {items:videos,limits:{}};
  if(url==='/api/reference-videos/style-profiles')return {items:[]};
  if(url==='/api/screenplay/batches')return {jobs:[job('job1'),job('job2')]};
  if(url==='/api/screenplay/jobs'&&init?.method==='POST')return job();
  if(url==='/api/screenplay/jobs/job1')return job();
  throw new Error(`Unexpected contract ${url}`);
}));
afterEach(()=>{cleanup();mocked.mockReset();});
function mount(){render(<MemoryRouter><ScreenplayGenerationPage/></MemoryRouter>);}
async function common(user:ReturnType<typeof userEvent.setup>){await user.type(screen.getByLabelText('项目备注 / 项目名称'),'新产品广告');await user.type(screen.getByLabelText('创作提示词'),'展示真实产品');}
async function chooseProducts(user:ReturnType<typeof userEvent.setup>){await user.click(screen.getByLabelText('图片生剧本'));await user.selectOptions(screen.getByLabelText('图片素材来源'),'products');await user.click(await screen.findByLabelText(/产品一 · 自有品牌/));await user.click(screen.getByLabelText(/产品二 · 自有品牌/));}

it('defaults multiple products to separate persisted jobs and reuses selected image IDs without uploading',async()=>{
  const user=userEvent.setup();mount();await common(user);await chooseProducts(user);expect(screen.getByLabelText('每个产品分别生成一份营销剧本（默认）')).toBeChecked();await user.click(screen.getByRole('button',{name:'生成剧本'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url==='/api/screenplay/batches')).toBe(true));const sent=mocked.mock.calls.find(([url])=>url==='/api/screenplay/batches')!;
  expect(JSON.parse(String(sent[1]?.body))).toMatchObject({input_mode:'image',product_mode:'separate',marketing_template:'showcase',product_selections:[{product_id:'p1',image_ids:['image-1']},{product_id:'p2',image_ids:['image-2']}]});expect(mocked.mock.calls.some(([url])=>url==='/api/screenplay/assets')).toBe(false);expect(await screen.findByText(/已为 2 个产品分别建立独立剧本任务/)).toBeInTheDocument();
});
it('only combines products after an explicit combined-marketing choice',async()=>{
  const user=userEvent.setup();mount();await common(user);await chooseProducts(user);await user.click(screen.getByLabelText('将所选产品放入同一条视频组合营销'));await user.click(screen.getByRole('button',{name:'生成剧本'}));
  const sent=mocked.mock.calls.find(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')!;expect(JSON.parse(String(sent[1]?.body))).toMatchObject({product_mode:'combined',product_selections:[{product_id:'p1',image_ids:['image-1']},{product_id:'p2',image_ids:['image-2']}]});expect(JSON.parse(String(sent[1]?.body))).not.toHaveProperty('asset_id');
});
it('requires a new theme for style creation and retains distinct reference video IDs with a primary reference',async()=>{
  const user=userEvent.setup();mount();await common(user);await user.click(screen.getByLabelText('视频生剧本'));await user.selectOptions(screen.getByLabelText('视频素材来源'),'library');await user.click(await screen.findByLabelText(/参考一 · 10.0 秒/));await user.click(screen.getByLabelText(/参考二 · 10.0 秒/));await user.selectOptions(screen.getByLabelText('主参考视频'),'v2');await user.click(screen.getByLabelText('风格迁移式创作：为新的产品、故事或主题创作'));expect(screen.getByRole('button',{name:'生成剧本'})).toBeDisabled();await user.type(screen.getByLabelText('新的产品、故事或主题'),'黑色水杯的日常使用');await user.click(screen.getByRole('button',{name:'生成剧本'}));
  const sent=mocked.mock.calls.find(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')!;expect(JSON.parse(String(sent[1]?.body))).toMatchObject({input_mode:'video',reference_video_ids:['v1','v2'],primary_reference_video_id:'v2',video_creation_mode:'style',new_theme:'黑色水杯的日常使用'});expect(JSON.parse(String(sent[1]?.body))).not.toHaveProperty('asset_id');
});
it('requires separate permission to send private library images even after cloud costs are authorized',async()=>{
  const original=mocked.getMockImplementation()!;mocked.mockImplementation(async(url,init)=>{const value=await original(url,init);if(url==='/api/screenplay/options'){const base=value as any;return {...base,services:base.services.map((service:any)=>({...service,provider:'openai_compatible'})),modes:base.modes.map((mode:any)=>({...mode,candidates:mode.candidates.map((candidate:any)=>({...candidate,is_cloud:true}))}))};}return value;});
  const user=userEvent.setup();mount();await common(user);await user.click(screen.getByLabelText('图片生剧本'));await user.selectOptions(screen.getByLabelText('图片素材来源'),'products');await user.click(await screen.findByLabelText(/产品一 · 自有品牌/));await user.click(screen.getByLabelText('允许本次按所选策略调用付费云端服务'));expect(screen.getByRole('button',{name:'生成剧本'})).toBeDisabled();const media=screen.getByLabelText(/允许将本次选中的图片、视频或派生参考分析发送到所选云端模型服务/);expect(media).not.toBeChecked();await user.click(media);await user.click(screen.getByRole('button',{name:'生成剧本'}));const sent=mocked.mock.calls.find(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')!;expect(JSON.parse(String(sent[1]?.body))).toMatchObject({allow_cloud_costs:true,allow_cloud_media_upload:true,allow_paid_fallback:false});
});

it.each(['products','images','videos'])('restores all saved %s sources when returning a historical task to edit',async(kind)=>{
  const original=mocked.getMockImplementation()!,imageAssets=[1,2].map(index=>({id:`upload-${index}`,input_mode:'image',filename:`angle-${index}.png`,media_url:'/image.png',metadata:{}}));
  const source=kind==='products'?{product_selections:[{product_id:'p1',image_ids:['image-1']}],product_mode:'single',marketing_template:'unboxing'}:kind==='images'?{asset_ids:['upload-1','upload-2']}:{reference_video_ids:['v1','v2'],style_profile_id:'style1',video_creation_mode:'style',new_theme:'新的产品故事',primary_reference_video_id:'v2'};
  mocked.mockImplementation(async(url,init)=>{if(url==='/api/screenplay/jobs/job1')return {...job(),input_mode:kind==='videos'?'video':'image',source_selection:source,input_assets:kind==='images'?imageAssets:[],products:kind==='products'?[products[0]]:[]};if(url==='/api/reference-videos/style-profiles')return {items:[{id:'style1',name:'保存的风格',video_ids:['v1','v2'],analysis_ids:['analysis1','analysis2'],primary_video_id:'v2',version:1,profile:{}}]};return original(url,init);});
  const user=userEvent.setup();render(<MemoryRouter initialEntries={['/screenplay-generation?job=job1']}><ScreenplayGenerationPage/></MemoryRouter>);await user.click(await screen.findByRole('button',{name:'返回修改创作输入'}));
  if(kind==='products'){await screen.findByLabelText(/产品一 · 自有品牌/);expect(screen.getByLabelText('图片素材来源')).toHaveValue('products');expect(screen.getByLabelText(/产品一 · 自有品牌/)).toBeChecked();expect(screen.getByLabelText('产品营销模板')).toHaveValue('unboxing');}
  if(kind==='images'){expect(screen.getAllByAltText(/已保存的创作素材/)).toHaveLength(2);expect(screen.getByLabelText('图片素材来源')).toHaveValue('upload');}
  if(kind==='videos'){await screen.findByLabelText(/参考一 · 10.0 秒/);expect(screen.getByLabelText(/参考一 · 10.0 秒/)).toBeChecked();expect(screen.getByLabelText(/参考二 · 10.0 秒/)).toBeChecked();expect(screen.getByLabelText('主参考视频')).toHaveValue('v2');expect(screen.getByLabelText('使用已有风格档案（可选）')).toHaveValue('style1');expect(screen.getByLabelText('新的产品、故事或主题')).toHaveValue('新的产品故事');}
  await user.click(screen.getByRole('button',{name:'生成剧本'}));const sent=mocked.mock.calls.find(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')!;expect(JSON.parse(String(sent[1]?.body))).toMatchObject(source);
});

it('shows actual failures when a product batch only creates some jobs',async()=>{
  const original=mocked.getMockImplementation()!;mocked.mockImplementation(async(url,init)=>url==='/api/screenplay/batches'?{jobs:[job()],errors:[{product_id:'p2',error:'原图无法读取'}],partial:true}:original(url,init));const user=userEvent.setup();mount();await common(user);await chooseProducts(user);await user.click(screen.getByRole('button',{name:'生成剧本'}));expect(await screen.findByRole('alert')).toHaveTextContent('p2：原图无法读取');expect(await screen.findByText(/已为 1 个产品分别建立独立剧本任务/)).toBeInTheDocument();
});

it.each(['image','video'])('requires cloud media permission for a single uploaded %s as well as library inputs',async(inputMode)=>{
  const original=mocked.getMockImplementation()!;mocked.mockImplementation(async(url,init)=>{if(url==='/api/screenplay/jobs/job1')return {...job(),input_mode:inputMode,input_asset:{id:'single-private',filename:inputMode==='image'?'private.png':'private.mp4',media_url:'/private-media',metadata:{}}};const value=await original(url,init);if(url==='/api/screenplay/options'){const base=value as any;return {...base,services:base.services.map((service:any)=>({...service,provider:'openai_compatible'})),modes:base.modes.map((mode:any)=>({...mode,candidates:mode.candidates.map((candidate:any)=>({...candidate,is_cloud:true}))}))};}return value;});
  const user=userEvent.setup();render(<MemoryRouter initialEntries={['/screenplay-generation?job=job1']}><ScreenplayGenerationPage/></MemoryRouter>);await user.click(await screen.findByRole('button',{name:'返回修改创作输入'}));await user.click(screen.getByLabelText('允许本次按所选策略调用付费云端服务'));expect(screen.getByRole('button',{name:'生成剧本'})).toBeDisabled();await user.click(screen.getByLabelText(/允许将本次选中的图片、视频或派生参考分析发送到所选云端模型服务/));await user.click(screen.getByRole('button',{name:'生成剧本'}));const sent=mocked.mock.calls.find(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')!;expect(JSON.parse(String(sent[1]?.body))).toMatchObject({input_mode:inputMode,asset_id:'single-private',allow_cloud_media_upload:true,allow_cloud_costs:true});
});
