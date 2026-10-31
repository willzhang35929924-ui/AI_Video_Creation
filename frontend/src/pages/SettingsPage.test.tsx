// UI contract tests only. They do not invoke models or modify real settings.
import {render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {beforeEach,afterEach,it,expect,vi} from 'vitest';
import {request} from '../lib/api';
import {SettingsPage} from './SettingsPage';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
vi.mock('../components/ToastProvider',()=>({useToast:()=>({notify:vi.fn()})}));
const mocked=vi.mocked(request);
const settings={resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'};
const profile={scope:'system',target_id:'default',revision:2,settings,plan:{method:'lanczos',label:'ordinary',diagnostic:'hidden-plan-json'}};
let services:any[];
beforeEach(()=>{
  services=[{id:'image',module:'character_generation',provider:'comfyui',name:'图像配置',default_model:'klein',
    base_url:'http://127.0.0.1:8189',enabled:true,is_default:true,models:[],settings:{},capabilities:{reference_image:{state:'verified',source:'hidden-evidence'}},
    has_api_key:false,revision:9,last_success_at:'2026-01-01T00:00:00Z',last_test:{status:'connected',http_status:200,elapsed_ms:123,version:'hidden-server-version',notice:'模型列表不代表生成能力'}},
    {id:'video',module:'video_generation',provider:'comfyui',name:'视频配置',default_model:'h3',base_url:'http://127.0.0.1:8189',
      enabled:true,is_default:true,models:[],settings:{},capabilities:{},has_api_key:false,revision:4,last_success_at:null,last_test:{}}];
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/settings/ai-services'&&init?.method==='POST'){
      const body=JSON.parse(String(init.body));const created={...services[0],...body,id:'new-service',is_default:false,last_test:{},capabilities:{}};
      services=[...services,created];return created;
    }
    if(url==='/api/settings/ai-services')return services;
    if(url==='/api/quality/capabilities')return {ai_super_resolution:{state:'not_configured'},seedvr2:{state:'not_configured'}};
    if(url==='/api/quality/profiles/system/default')return init?.method==='PUT'?{...profile,revision:3,settings:JSON.parse(String(init.body)).settings}:profile;
    throw new Error('Unexpected settings request: '+url);
  });
});
afterEach(()=>mocked.mockReset());
function mount(){render(<MemoryRouter initialEntries={['/settings']}><SettingsPage/></MemoryRouter>);}

it('opens service profiles and quality directly without diagnostic navigation or history requests',async()=>{
  mount();
  expect(await screen.findByRole('heading',{name:'图像配置'})).toBeVisible();
  expect(screen.getByRole('heading',{name:'视频配置'})).toBeVisible();
  expect(screen.getByRole('heading',{name:'服务档案'})).toBeVisible();
  expect(screen.getByRole('heading',{name:'制作画质'})).toBeVisible();
  expect(screen.getByLabelText('默认输出规格')).toHaveValue('720p');
  expect(screen.queryByRole('navigation',{name:'设置分区'})).not.toBeInTheDocument();
  expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  for(const label of ['运行诊断','模型对比实验','质量模式与实际验证证据','hidden-server-version','hidden-plan-json'])expect(screen.queryByText(label)).not.toBeInTheDocument();
  expect(mocked.mock.calls.every(([url])=>['/api/settings/ai-services','/api/quality/capabilities','/api/quality/profiles/system/default'].includes(url))).toBe(true);
});

it('adds the selected service module from the flat all-services view',async()=>{
  const user=userEvent.setup();mount();await screen.findByRole('heading',{name:'图像配置'});
  await user.click(screen.getByRole('button',{name:'添加服务'}));
  const dialog=screen.getByRole('dialog',{name:'配置 AI 服务'});
  await user.selectOptions(within(dialog).getByLabelText('服务用途'),'dialogue_generation');
  expect(within(dialog).getByLabelText('服务提供商')).toHaveValue('local_runtime');
  expect(within(dialog).getByLabelText('固定本地运行时')).toHaveValue('cosyvoice3');
  await user.type(within(dialog).getByLabelText('服务名称'),'本地配音配置');
  await user.click(within(dialog).getByRole('button',{name:'保存配置'}));
  expect(await screen.findByRole('heading',{name:'本地配音配置'})).toBeVisible();
  const saved=mocked.mock.calls.find(([,init])=>init?.method==='POST')!;
  expect(JSON.parse(String(saved[1]?.body))).toMatchObject({module:'dialogue_generation',provider:'local_runtime',default_model:'cosyvoice3-0.5b-2512',settings:{runtime_family:'cosyvoice3'}});
  expect(screen.getByRole('heading',{name:'视频配置'})).toBeVisible();
});

it('registers IndexTTS 2.5 as a separate fixed local voice runtime',async()=>{
  const user=userEvent.setup();mount();await screen.findByRole('heading',{name:'图像配置'});
  await user.click(screen.getByRole('button',{name:'添加服务'}));
  const dialog=screen.getByRole('dialog',{name:'配置 AI 服务'});
  await user.selectOptions(within(dialog).getByLabelText('服务用途'),'dialogue_generation');
  await user.selectOptions(within(dialog).getByLabelText('固定本地运行时'),'indextts25');
  await user.type(within(dialog).getByLabelText('服务名称'),'IndexTTS 2.5 本地配音');
  await user.click(within(dialog).getByRole('button',{name:'保存配置'}));
  expect(await screen.findByRole('heading',{name:'IndexTTS 2.5 本地配音'})).toBeVisible();
  const saved=mocked.mock.calls.find(([,init])=>init?.method==='POST')!;
  expect(JSON.parse(String(saved[1]?.body))).toMatchObject({module:'dialogue_generation',provider:'local_runtime',default_model:'indextts-2.5',settings:{runtime_family:'indextts25'}});
});

it('shows the video API contract and provider limits before advanced settings',async()=>{
  const user=userEvent.setup();mount();await screen.findByRole('heading',{name:'图像配置'});
  await user.click(screen.getByRole('button',{name:'添加服务'}));
  const dialog=screen.getByRole('dialog',{name:'配置 AI 服务'});
  await user.selectOptions(within(dialog).getByLabelText('服务用途'),'video_generation');
  await user.selectOptions(within(dialog).getByLabelText('服务提供商'),'video_api');
  expect(within(dialog).getByText(/支持零度 MiniMax H3、千问万相 2.6 单首帧及明确实现 video-jobs-v1/)).toBeVisible();
  expect(within(dialog).getByLabelText('视频任务契约')).toBeVisible();
  await user.selectOptions(within(dialog).getByLabelText('视频任务契约'),'video-jobs-v1');
  expect(within(dialog).getByLabelText('视频任务契约')).toHaveValue('video-jobs-v1');
  await user.selectOptions(within(dialog).getByLabelText('服务提供商'),'cloud_draft');
  expect(within(dialog).getByText(/此类型仅保存供应商资料/)).toBeVisible();
});

it('shows connection HTTP status and time directly in the service profile',async()=>{
  mount();await screen.findByRole('heading',{name:'图像配置'});
  expect(screen.getByText('HTTP 200 · 123 ms')).toBeVisible();
  expect(screen.getByText('模型列表不代表生成能力')).toBeVisible();
});

it('saves quality with its revision without exposing the technical plan',async()=>{
  const user=userEvent.setup();mount();await screen.findByLabelText('默认输出规格');
  await waitFor(()=>expect(screen.getByRole('button',{name:'保存画质设置'})).toBeEnabled());
  await user.selectOptions(screen.getByLabelText('默认输出规格'),'1080p');
  await user.selectOptions(screen.getByLabelText('默认质量模式'),'high');
  await user.click(screen.getByRole('button',{name:'保存画质设置'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(true));
  const saved=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
  expect(saved[0]).toBe('/api/quality/profiles/system/default');
  expect(JSON.parse(String(saved[1]?.body))).toMatchObject({expected_revision:2,settings:{resolution:'1080p',mode:'high',processing:'lanczos'}});
  expect(screen.queryByText('hidden-plan-json')).not.toBeInTheDocument();
});


it('offers an optional provider prompt ceiling only for the custom cloud video contract',async()=>{
  const user=userEvent.setup();mount();await screen.findByRole('heading',{name:'图像配置'});
  await user.click(screen.getByRole('button',{name:'添加服务'}));
  const dialog=screen.getByRole('dialog',{name:'配置 AI 服务'});
  await user.selectOptions(within(dialog).getByLabelText('服务用途'),'video_generation');
  expect(within(dialog).queryByLabelText('云端提示词字数上限（可选）')).not.toBeInTheDocument();
  await user.selectOptions(within(dialog).getByLabelText('服务提供商'),'video_api');
  await user.selectOptions(within(dialog).getByLabelText('视频任务契约'),'video-jobs-v1');
  expect(within(dialog).getByLabelText('云端提示词字数上限（可选）')).toHaveValue(null);
  await user.type(within(dialog).getByLabelText('云端提示词字数上限（可选）'),'9000');
  await user.type(within(dialog).getByLabelText('服务名称'),'自定义云端视频');
  await user.type(within(dialog).getByLabelText('服务地址 base_url'),'https://example.invalid/v1');
  await user.click(within(dialog).getByRole('button',{name:'保存配置'}));
  await screen.findByRole('heading',{name:'自定义云端视频'});
  const saved=mocked.mock.calls.find(([,init])=>init?.method==='POST')!;
  expect(JSON.parse(String(saved[1]?.body))).toMatchObject({provider:'video_api',settings:{video_contract:'video-jobs-v1',max_prompt_characters:9000}});
});
