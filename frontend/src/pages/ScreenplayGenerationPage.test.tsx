// Isolated UI/API contracts; these fixtures do not stand in for real model tests.
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ScreenplayGenerationPage} from './ScreenplayGenerationPage';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const ir={project:{name:' 原样项目 '},characters:[{key:'a',name:'记录员',profile:{age:32,description:'原创成年记录员'}}],scenes:[{key:'room',name:'控制室',environment:{lighting:'自然顶灯'}}],shots:[{key:'s1',scene_key:'room',title:'核对记录',duration_seconds:5,participants:[{character_key:'a',performance:{action:'查看记录'}}],dialogues:[{character_key:'a',text:'记录已核对。'}]}]};
const baseJob=()=>({id:'job-1',project_name:' 原样项目 ',input_mode:'text',status:'awaiting_review',stage:'awaiting_review',revision:1,creative_prompt:'写实短片',story_text:'记录员核对记录。',screenplay:ir,counts:{characters:1,scenes:1,shots:1,dialogues:1},documents:[{id:'doc-1',version:1,download_url:'/api/screenplay/documents/doc-1/download',review_state:'pending',sha256:'a'.repeat(64)}],latest_document_id:'doc-1',project_id:null,uncertainties:[]});
let eligible:boolean,job:any;
function options(){return {modes:['text','image','video'].map(mode=>({input_mode:mode,slot:'screenplay_from_'+mode,name:mode,candidates:[{service_id:'local',model_id:'qwen',name:'本地视觉语言模型',provider:'local_llm',module:'language_processing',enabled:true,is_cloud:false,eligible,verification:eligible?'verified':'not_tested',reasons:eligible?[]:['没有该模式真实输出']}],default_service_id:'local',limits:{max_bytes:20*1024**2,max_duration_seconds:60,formats:mode==='image'?['PNG','JPEG','WebP']:['MP4','WebM']}})),services:[{id:'local',name:'本地视觉语言模型',enabled:true,provider:'local_llm',default_model:'qwen'}]};}
beforeEach(()=>{eligible=true;job=baseJob();mocked.mockImplementation(async(url,init)=>{
  if(url==='/api/screenplay/options')return options();
  if(url==='/api/screenplay/jobs'&&!init?.method)return [];
  if(url==='/api/screenplay/jobs/job-1'&&!init?.method)return structuredClone(job);
  if(url==='/api/screenplay/assets')return {id:'image-1',input_mode:'image',filename:'room.png',relative_path:'inputs/room.png',media_url:'/api/media/inputs/room.png',sha256:'b'.repeat(64),metadata:{}};
  if((url==='/api/screenplay/jobs'||url==='/api/screenplay/capability-tests')&&init?.method==='POST')return structuredClone(job);
  if(url==='/api/screenplay/jobs/job-1/approve'){
    const body=JSON.parse(String(init?.body));job={...job,revision:job.revision+1,status:'approved',stage:'approved',project_id:body.create_project?'p-new':null};return structuredClone(job);
  }
  throw new Error('Unexpected screenplay contract '+url);
});});
afterEach(()=>{cleanup();mocked.mockReset();});
function mount(search=''){render(<MemoryRouter initialEntries={['/screenplay-generation'+search]}><ScreenplayGenerationPage/></MemoryRouter>);}
async function fillText(user:ReturnType<typeof userEvent.setup>){await user.type(screen.getByLabelText('项目备注 / 项目名称'),' 原样项目 ');await user.type(screen.getByLabelText('创作提示词'),'写实短片');await user.type(screen.getByLabelText('故事梗概 / 创意素材'),'记录员核对记录。');}

it('preserves the exact project name and sends only the current mutually exclusive image input',async()=>{
  const user=userEvent.setup();mount();await screen.findByRole('option',{name:/本地视觉语言模型 · qwen/});await fillText(user);
  await user.click(screen.getByLabelText('图片生剧本'));
  expect(screen.getByLabelText('创作提示词')).toHaveValue('写实短片');expect(screen.queryByLabelText('故事梗概 / 创意素材')).not.toBeInTheDocument();
  expect(screen.getByRole('button',{name:'生成剧本'})).toBeDisabled();
  const file=new File(['unit image bytes'],'room.png',{type:'image/png'});await user.upload(screen.getByLabelText('上传创作图片'),file);
  await user.click(screen.getByRole('button',{name:'上传并保存创作素材'}));await screen.findByAltText('已保存的创作素材');
  const upload=mocked.mock.calls.find(([url])=>url==='/api/screenplay/assets')!;expect((upload[1]?.body as FormData).get('file')).toBe(file);
  await user.click(screen.getByRole('button',{name:'生成剧本'}));
  const sent=mocked.mock.calls.find(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')!;
  expect(JSON.parse(String(sent[1]?.body))).toMatchObject({project_name:' 原样项目 ',creative_prompt:'写实短片',input_mode:'image',asset_id:'image-1',allow_cloud_costs:false,allow_paid_fallback:false});
  expect(JSON.parse(String(sent[1]?.body))).not.toHaveProperty('story_text');
  expect(JSON.parse(String(sent[1]?.body))).not.toHaveProperty('service_id');
  expect(JSON.parse(String(sent[1]?.body))).not.toHaveProperty('model_id');
});

it('requires explicit review and keeps project creation unchecked until the user chooses it',async()=>{
  const user=userEvent.setup();mount('?job=job-1');await screen.findByRole('heading',{name:'原样项目'});
  expect(screen.getByLabelText('审核通过后创建项目')).not.toBeChecked();expect(screen.getByRole('button',{name:'仅保存审核结果'})).toBeDisabled();
  await user.click(screen.getByLabelText('剧本内容审核通过'));await user.click(screen.getByRole('button',{name:'仅保存审核结果'}));
  await screen.findByText('审核已保存，仅归档剧本和 Word；尚未创建项目。');
  const first=mocked.mock.calls.filter(([url])=>url.endsWith('/approve'))[0];expect(JSON.parse(String(first[1]?.body))).toEqual({expected_revision:1,document_id:'doc-1',approve:true,create_project:false,reference_targets:[]});
  await user.click(screen.getByLabelText('剧本内容审核通过'));await user.click(screen.getByLabelText('审核通过后创建项目'));await user.click(screen.getByRole('button',{name:'确认审核并创建项目'}));
  await screen.findByRole('link',{name:/进入项目工作台/});
  const second=mocked.mock.calls.filter(([url])=>url.endsWith('/approve'))[1];expect(JSON.parse(String(second[1]?.body))).toMatchObject({expected_revision:2,create_project:true});
  expect(screen.queryByRole('button',{name:'确认审核并创建项目'})).not.toBeInTheDocument();
});

it('separates bootstrap capability testing from normal generation when the mode is unverified',async()=>{
  eligible=false;const user=userEvent.setup();mount();await screen.findByRole('option',{name:/本地视觉语言模型 · qwen/});await fillText(user);
  expect(screen.getByRole('button',{name:'生成剧本'})).toBeDisabled();
  await user.click(screen.getByRole('button',{name:'使用当前输入测试此模式能力'}));
  const sent=mocked.mock.calls.find(([url])=>url==='/api/screenplay/capability-tests')!;
  expect(JSON.parse(String(sent[1]?.body))).toMatchObject({service_id:'local',input_mode:'text',allow_cloud_costs:false});
  expect(mocked.mock.calls.some(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')).toBe(false);
});

it('prevents duplicate generation while the actual submission request is pending',async()=>{
  let release!:(value:any)=>void;const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>url==='/api/screenplay/jobs'&&init?.method==='POST'?new Promise(resolve=>{release=resolve;}):original(url,init));
  const user=userEvent.setup();mount();await screen.findByRole('option',{name:/本地视觉语言模型 · qwen/});await fillText(user);
  await user.dblClick(screen.getByRole('button',{name:'生成剧本'}));
  expect(mocked.mock.calls.filter(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')).toHaveLength(1);
  release(job);await waitFor(()=>expect(screen.getByRole('button',{name:'返回修改创作输入'})).toBeInTheDocument());
});

it('matches capability to the exact registered model version, including a system default override',async()=>{
  const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>{
    if(url==='/api/screenplay/options'){
      const base=options();return Promise.resolve({...base,services:base.services.map(service=>({...service,models:[{id:'qwen'},{id:'qwen-alt'}]})),modes:base.modes.map(item=>({...item,default_model_id:'qwen-alt',candidates:[{...item.candidates[0],eligible:false},{...item.candidates[0],model_id:'qwen-alt',eligible:true}]}))});
    }
    return original(url,init);
  });
  const user=userEvent.setup();mount();await screen.findByRole('option',{name:/本地视觉语言模型 · qwen/});await fillText(user);
  expect(screen.getByRole('button',{name:'生成剧本'})).toBeEnabled();
  await user.selectOptions(screen.getByLabelText('剧本模型'),'local');
  expect(screen.getByRole('button',{name:'生成剧本'})).toBeDisabled();
  await user.selectOptions(screen.getByLabelText('此服务的剧本模型版本'),'qwen-alt');
  await user.click(screen.getByRole('button',{name:'生成剧本'}));
  const sent=mocked.mock.calls.find(([url,init])=>url==='/api/screenplay/jobs'&&init?.method==='POST')!;
  expect(JSON.parse(String(sent[1]?.body))).toMatchObject({service_id:'local',model_id:'qwen-alt'});
});

it('exports a new Word version from saved IR without submitting another model request',async()=>{
  job={...job,project_id:'project-existing'};
  const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>{
    if(url==='/api/screenplay/jobs/job-1/export-new-version')return Promise.resolve({...job,revision:2,latest_document_id:'doc-2',documents:[...job.documents,{...job.documents[0],id:'doc-2',version:2,download_url:'/api/screenplay/documents/doc-2/download'}]});
    return original(url,init);
  });
  const user=userEvent.setup();mount('?job=job-1');await screen.findByRole('button',{name:'使用已有剧本导出新 Word 版本'});
  await user.click(screen.getByLabelText('剧本内容审核通过'));await user.click(screen.getByRole('button',{name:'使用已有剧本导出新 Word 版本'}));
  await screen.findByText('已使用保存的结构化剧本导出独立 Word 版本，没有重新调用创作模型。');
  expect(screen.getByLabelText('剧本内容审核通过')).not.toBeChecked();expect(screen.getByLabelText('本次审核的 Word 版本')).toHaveValue('doc-2');
  expect(screen.queryByLabelText('审核通过后创建项目')).not.toBeInTheDocument();
  expect(mocked.mock.calls.filter(([,init])=>init?.method==='POST').map(([url])=>url)).toEqual(['/api/screenplay/jobs/job-1/export-new-version']);
});

it('retries a determined failure as a new task with separate cloud authorization defaulting off',async()=>{
  job={...baseJob(),status:'failed',stage:'failed',screenplay:null,documents:[],latest_document_id:null};const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>url==='/api/screenplay/jobs/job-1/retry'?Promise.resolve({...baseJob(),id:'job-2'}):url==='/api/screenplay/jobs/job-2'?Promise.resolve({...baseJob(),id:'job-2'}):original(url,init));
  const user=userEvent.setup();mount('?job=job-1');await screen.findByRole('button',{name:'新建重试任务'});
  expect(screen.getByLabelText('允许此重试任务使用付费云端服务')).not.toBeChecked();
  await user.click(screen.getByRole('button',{name:'新建重试任务'}));
  const retry=mocked.mock.calls.find(([url])=>url.endsWith('/retry'))!;expect(JSON.parse(String(retry[1]?.body))).toMatchObject({expected_revision:1,allow_cloud_costs:false,allow_paid_fallback:false});
  expect(JSON.parse(String(retry[1]?.body)).idempotency_key).toBeTruthy();
  await screen.findByText('已建立独立重试任务，失败记录和已有文档保留。');
});

it('requires explicit acknowledgement before a new attempt after an unknown provider outcome',async()=>{
  job={...baseJob(),status:'unknown',stage:'unknown',screenplay:null,documents:[],latest_document_id:null,service_snapshot:{settings:{timeout:120}},receipt:{provider_request_submitted:true,elapsed_seconds:120.4,attempt_history:[{timeout_seconds:120}]}};const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>url==='/api/screenplay/jobs/job-1/retry'?Promise.resolve({...baseJob(),id:'job-2',status:'queued',stage:'queued'}):url==='/api/screenplay/jobs/job-2'?Promise.resolve({...baseJob(),id:'job-2',status:'queued',stage:'queued',screenplay:null,documents:[],latest_document_id:null}):original(url,init));
  const user=userEvent.setup();mount('?job=job-1');await screen.findByText(/本次等待已经结束/);expect(screen.getByText(/响应读取等待上限为 120 秒/)).toBeInTheDocument();expect(screen.getByText(/总等待时间约 120.4 秒/)).toBeInTheDocument();
  const acknowledge=screen.getByLabelText('我已核对服务商结果，或接受原请求可能已执行、重试可能重复生成和计费');
  const retryButton=screen.getByRole('button',{name:'确认并新建重试任务'});expect(acknowledge).not.toBeChecked();expect(retryButton).toBeDisabled();
  await user.click(acknowledge);await user.click(retryButton);
  const retry=mocked.mock.calls.find(([url])=>url.endsWith('/retry'))!;
  expect(JSON.parse(String(retry[1]?.body))).toMatchObject({expected_revision:1,confirm_unknown_retry:true,allow_cloud_costs:false,allow_paid_fallback:false});
  expect(JSON.parse(String(retry[1]?.body)).idempotency_key).toBeTruthy();
  await screen.findByText(/原结果未知记录保留/);
});

it('never treats an already submitted request as safely cancellable and gates its replacement',async()=>{
  job={...baseJob(),status:'cancelled',stage:'cancelled',screenplay:null,documents:[],latest_document_id:null,receipt:{provider_request_submitted:true}};
  const original=mocked.getMockImplementation()!;
  mocked.mockImplementation((url,init)=>url==='/api/screenplay/jobs/job-1/resume'?Promise.resolve({...baseJob(),id:'job-2',status:'queued',stage:'queued'}):url==='/api/screenplay/jobs/job-2'?Promise.resolve({...baseJob(),id:'job-2',status:'queued',stage:'queued',screenplay:null,documents:[],latest_document_id:null}):original(url,init));
  const user=userEvent.setup();mount('?job=job-1');await screen.findByText(/本地中止不会停止已经提交给模型服务的请求/);
  const acknowledge=screen.getByLabelText('我已核对服务商结果，或接受原请求可能已执行、重试可能重复生成和计费');
  const resume=screen.getByRole('button',{name:'确认并新建重试任务'});expect(acknowledge).not.toBeChecked();expect(resume).toBeDisabled();
  await user.click(acknowledge);await user.click(resume);
  const sent=mocked.mock.calls.find(([url])=>url.endsWith('/resume'))!;
  expect(JSON.parse(String(sent[1]?.body))).toMatchObject({confirm_unknown_retry:true,allow_cloud_costs:false});
});
