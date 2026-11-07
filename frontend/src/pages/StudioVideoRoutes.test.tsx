// Controlled route/UI contracts only; these tests do not run a model or generate media.
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {useStudio} from '../lib/studio-ui';
import {StudioPage} from './StudioPage';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
vi.mock('../lib/studio-ui',async original=>({...await original<typeof import('../lib/studio-ui')>(),useStudio:vi.fn()}));
vi.mock('../components/ProjectVoicePreparation',async()=>{const React=await import('react');return {ProjectVoicePreparation:({onStateChange}:{onStateChange:(state:any)=>void})=>{React.useEffect(()=>onStateChange({ready:true,dirty:false,loading:false,busy:false,blockingReasons:[]}),[onStateChange]);return null;}};});
vi.mock('../components/ProjectAudioPolicy',()=>({ProjectAudioPolicy:()=>null}));
vi.mock('../components/ProjectProductPreparation',()=>({ProjectProductPreparation:()=>null}));
vi.mock('../components/ProjectCharacterReferences',()=>({ProjectCharacterReferences:()=>null}));
vi.mock('../components/SeedVrPreparation',()=>({SeedVrPreparation:()=>null}));

const mocked=vi.mocked(request);
const context={id:'local-context',name:'明确Context服务',module:'video_generation',provider:'comfyui',enabled:true,is_default:true,
  default_model:'minimax_h3_w6a8',base_url:'http://127.0.0.1:8788',has_api_key:false,models:[],settings:{workflow_profile:'context_ir',h3_context_ir:true}};
const provider={...context,id:'local-provider',name:'普通本地服务',settings:{workflow_profile:'provider',h3_context_ir:false}};
const cloud={...context,id:'cloud',name:'云端视频服务',provider:'video_api',default_model:'minimax-h3',base_url:'https://example.invalid/v1',settings:{video_contract:'video-jobs-v1'}};
let kind:'context'|'provider'|'cloud'|'mixed';
beforeEach(()=>{
  kind='context';
  vi.mocked(useStudio).mockReturnValue({projectId:'p1',projects:[{id:'p1',name:'项目'}],data:{project:{id:'p1',name:'项目'},characters:[],scenes:[],
    shots:[{id:'s1',order:1,title:'测试镜头',duration_seconds:5,status:'draft',cast:[],decision:{}}],run:null},loading:false,error:'',listLoading:false,
    refresh:vi.fn(),refreshAfterChange:vi.fn(),refreshList:vi.fn(),selectProject:vi.fn()} as any);
  mocked.mockImplementation(async(url,init)=>{
    const services=kind==='mixed'?[context,cloud]:[kind==='context'?context:kind==='provider'?provider:cloud];
    if(url==='/api/settings/ai-services?module=video_generation')return services;
    if(url==='/api/projects/p1/ai-services')return [];
    if(url==='/api/settings/ai-service-options')return {container:true};
    if(url==='/api/model-services/routes/project/p1/video_generation')return {scope:'project',target_id:'p1',slot:'video_generation',revision:kind==='mixed'?1:0,
      inherited_from:'legacy_module_default',settings:{mode:kind==='mixed'?'local_first':'local_only',choices:kind==='mixed'?services.map(service=>({service_id:service.id,model_id:service.default_model})):[],parameters:{}}};
    if(url==='/api/model-services')return {slots:[{slot:'video_generation',candidates:services.map(service=>({service_id:service.id,model_id:service.default_model,eligible:true,is_cloud:service.provider==='video_api',reasons:[]}))}]};
    if(url.endsWith('/shot-framing'))return {project_id:'p1',shots:[{id:'s1',order:1,title:'测试镜头',aspect_ratio:'16:9',revision:1,editable:true}],supported_aspect_ratios:['16:9','9:16','1:1'],requires_new_run:false};
    if(url.startsWith('/api/quality/profiles/'))return {scope:'project',target_id:'p1',revision:0,settings:init?.method==='PUT'?JSON.parse(String(init.body)).settings:{resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'},plan:{supported:true}};
    if(url==='/api/quality/capabilities')return {};
    if(url.includes('/voices?'))return {ready_to_start:true,blockers:[]};
    if(url==='/api/studio/projects/p1/start')return {};
    throw new Error(url);
  });
});
afterEach(()=>{cleanup();mocked.mockReset();});

function mount(){render(<MemoryRouter initialEntries={['/?project=p1']}><StudioPage/></MemoryRouter>);}

it.each(['cloud','mixed'] as const)('keeps %s video routes usable in the main workbench with explicit cost consent',async(route)=>{
  kind=route;const user=userEvent.setup();mount();
  await screen.findByText(/当前视频流程包含云端模型/);
  if(route==='mixed'){
    const mode=screen.getByLabelText('制作质量模式');await user.selectOptions(mode,'fast');
    expect(screen.getByTestId('h3-quality-route')).toHaveTextContent('4 步');
    expect(screen.getByText(/三档采样只用于本地 Context-IR/)).toBeInTheDocument();
  }else{
    expect(screen.queryByLabelText('制作质量模式')).toBeNull();
    expect(screen.queryByTestId('h3-quality-route')).toBeNull();
    expect(screen.getByText(/不会将本地 4／20／30 步配方发给云端/)).toBeInTheDocument();
  }
  const start=screen.getByRole('button',{name:'开始／增量续作'});
  expect(start).toBeDisabled();
  expect(mocked.mock.calls.some(([url])=>url.endsWith('/start'))).toBe(false);
  await user.click(screen.getByLabelText(/允许本项目调用已配置的云端生成服务/));
  await waitFor(()=>expect(start).toBeEnabled());
  await user.click(start);
  await waitFor(()=>expect(mocked.mock.calls.some(([url])=>url.endsWith('/start'))).toBe(true));
  const sent=mocked.mock.calls.find(([url])=>url.endsWith('/start'))!;
  expect(JSON.parse(String(sent[1]?.body))).toMatchObject({allow_cloud_costs:true,allow_paid_fallback:false,...(route==='mixed'?{quality:{mode:'fast'}}:{})});
});

it('keeps ordinary provider workflow parameters instead of labeling them Context-IR',async()=>{
  kind='provider';mount();
  await screen.findByText(/当前使用服务档案中的普通本地工作流/);
  expect(screen.queryByLabelText('制作质量模式')).toBeNull();
  expect(screen.queryByTestId('h3-quality-route')).toBeNull();
  await waitFor(()=>expect(screen.getByRole('button',{name:'开始／增量续作'})).toBeEnabled());
});

it('retains three verified Context-IR quality choices even when the model settings panel stays closed',async()=>{
  kind='context';const user=userEvent.setup();mount();
  const mode=await screen.findByLabelText('制作质量模式');
  await waitFor(()=>expect(mode).toBeEnabled());
  await user.selectOptions(mode,'fast');
  expect(screen.getByTestId('h3-quality-route')).toHaveTextContent('4 步');
  expect(screen.getByText('本项目图像与视频模型（继承或项目级覆盖）').closest('details')).not.toHaveAttribute('open');
});
