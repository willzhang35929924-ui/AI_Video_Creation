import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { request } from '../lib/api';
import { VideoServiceSelector } from './VideoServiceSelector';

vi.mock('../lib/api', async original => ({ ...await original<typeof import('../lib/api')>(), request: vi.fn() }));
const mocked = vi.mocked(request);
afterEach(() => { cleanup(); mocked.mockReset(); });

const local = { id: 'local', name: '本机 H3', provider: 'comfyui', enabled: true, is_default: false,
  default_model: 'minimax_h3_w6a8', base_url: 'http://127.0.0.1:8189', models: [{ id: 'another_video_model' }], has_api_key: false };
const cloud = { ...local, id: 'cloud', name: '云端 H3', provider: 'video_api', is_default: true, base_url: 'https://example.com', settings: {video_contract:'lk888-h3-v2'} };
const insecure = { ...local, id: 'remote', name: '远程 ComfyUI', base_url: 'https://example.com' };
const routeUrl = '/api/model-services/routes/project/p1/video_generation';
const bindingUrl = '/api/projects/p1/ai-services';
const choice = { service_id: local.id, model_id: local.default_model };
const profile = { scope: 'project', target_id: 'p1', slot: 'video_generation', inherited_from: null };

it('preserves configured cloud and native choices while changing an explicit route without bypassing eligibility', async () => {
  let settings = { mode: 'cloud_only', choices: [{ service_id: cloud.id, model_id: cloud.default_model }], allow_paid_fallback: true };
  mocked.mockImplementation(async (url, init) => {
    if (url.startsWith('/api/settings/ai-services')) return [local, cloud, insecure];
    if (url === bindingUrl) return [{ module: 'video_generation', ...choice }];
    if (url === '/api/model-services') return { slots: [{ slot: 'video_generation', candidates: [
      { ...choice, eligible: true }, { service_id: cloud.id, model_id: cloud.default_model, eligible: true },
    ] }] };
    if (url === routeUrl) {
      if (init?.method === 'PUT') settings = JSON.parse(String(init.body)).settings;
      return { ...profile, revision: 2, settings };
    }
    throw new Error(url);
  });
  const user = userEvent.setup();
  render(<MemoryRouter><VideoServiceSelector projectId="p1" /></MemoryRouter>);
  expect(await screen.findByText(/当前使用云端视频服务/)).toBeVisible();
  expect(screen.getByRole('option', { name: /云端 H3.*minimax_h3_w6a8/ })).toBeEnabled();
  expect(screen.queryByRole('option', { name: /远程 ComfyUI/ })).toBeNull();
  await user.selectOptions(screen.getByLabelText('项目视频服务与模型'), JSON.stringify([local.id, local.default_model]));
  await user.click(screen.getByRole('button', { name: '保存为项目覆盖' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  const sent = mocked.mock.calls.filter(([, init]) => init?.method === 'PUT');
  expect(sent.map(([url]) => url)).toEqual([routeUrl]);
  expect(JSON.parse(String(sent[0][1]?.body))).toMatchObject({ expected_revision: 2, settings: {
    mode: 'local_only', choices: [choice], allow_fallback: false, allow_paid_fallback: false,
  } });
  expect(screen.getByLabelText('项目视频服务与模型')).toHaveValue(JSON.stringify([local.id, local.default_model]));
});

it('changes only the project module binding when cloud is the inherited module default', async () => {
  let binding: { module: string; service_id: string; model_id: string } | undefined;
  mocked.mockImplementation(async (url, init) => {
    if (url.startsWith('/api/settings/ai-services')) return [local, cloud];
    if (url === bindingUrl) return binding ? [binding] : [];
    if (url === routeUrl) return { ...profile, revision: 0, inherited_from: 'legacy_module_default', settings: { mode: 'manual', choices: [binding || { service_id: cloud.id }] } };
    if (url === `${bindingUrl}/video_generation` && init?.method === 'PUT') return binding = { module: 'video_generation', ...JSON.parse(String(init.body)) };
    throw new Error(url);
  });
  const user = userEvent.setup();
  render(<MemoryRouter><VideoServiceSelector projectId="p1" /></MemoryRouter>);
  expect(await screen.findByText(/当前使用云端视频服务/)).toBeVisible();
  await user.selectOptions(screen.getByLabelText('项目视频服务与模型'), JSON.stringify([local.id, local.default_model]));
  await user.click(screen.getByRole('button', { name: '保存为项目覆盖' }));
  await waitFor(() => expect(screen.getByText(/当前来源 项目服务绑定/)).toHaveTextContent('本机 H3'));
  expect(binding).toEqual({ module: 'video_generation', ...choice });
  expect(mocked.mock.calls.some(([url, init]) => url === routeUrl && init?.method === 'PUT')).toBe(false);
});

it('offers an explicitly confirmed private local endpoint and hides an unconfirmed private endpoint', async () => {
  const confirmed = { ...local, id: 'lan', name: '局域网推理节点', base_url: 'http://192.168.1.20:8788', trusted_local_endpoint: true };
  const unconfirmed = { ...confirmed, id: 'unknown', name: '未确认节点', trusted_local_endpoint: false };
  mocked.mockImplementation(async url => {
    if (url.startsWith('/api/settings/ai-services')) return [confirmed, unconfirmed];
    if (url === bindingUrl) return [];
    if (url === routeUrl) return { ...profile, revision: 0, inherited_from: 'legacy_module_default', settings: { mode: 'manual', choices: [] } };
    throw new Error(url);
  });
  render(<MemoryRouter><VideoServiceSelector projectId="p1" /></MemoryRouter>);
  expect(await screen.findByRole('option', { name: /局域网推理节点.*minimax_h3_w6a8/ })).toBeVisible();
  expect(screen.queryByRole('option', { name: /未确认节点/ })).toBeNull();
});

it('saves an eligible cloud choice as cloud_only and does not enable paid fallback', async () => {
  let settings = {mode:'local_only',choices:[choice]};
  mocked.mockImplementation(async(url,init)=>{
    if(url.startsWith('/api/settings/ai-services'))return [local,cloud];
    if(url===bindingUrl)return [];
    if(url==='/api/settings/ai-service-options')return {container:true};
    if(url==='/api/model-services')return {slots:[{slot:'video_generation',candidates:[{...choice,eligible:true},{service_id:cloud.id,model_id:cloud.default_model,eligible:true}]}]};
    if(url===routeUrl){if(init?.method==='PUT')settings=JSON.parse(String(init.body)).settings;return {...profile,revision:2,settings};}
    throw new Error(url);
  });
  const state=vi.fn(),user=userEvent.setup();
  render(<MemoryRouter><VideoServiceSelector projectId="p1" onStateChange={state}/></MemoryRouter>);
  await user.selectOptions(await screen.findByLabelText('项目视频服务与模型'),JSON.stringify([cloud.id,cloud.default_model]));
  await user.click(screen.getByRole('button',{name:'保存为项目覆盖'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(true));
  const saved=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
  expect(JSON.parse(String(saved[1]?.body)).settings).toMatchObject({mode:'cloud_only',choices:[{service_id:cloud.id,model_id:cloud.default_model}],allow_paid_fallback:false,allow_fallback:false});
  await waitFor(()=>expect(state.mock.lastCall?.[0]).toMatchObject({contextEnabled:false,hasCloud:true}));
});

it.each([
  [true,{},false],
  [false,{},true],
  [true,{workflow_profile:'context_ir',h3_context_ir:true},true],
  [false,{workflow_profile:'native_h3_r2v',h3_context_ir:false},false],
])('reports Context eligibility for container=%s and profile=%j',async(container,settings,expected)=>{
  const native={...local,is_default:true,settings};
  mocked.mockImplementation(async url=>{
    if(url.startsWith('/api/settings/ai-services'))return [native];
    if(url===bindingUrl)return [];
    if(url==='/api/settings/ai-service-options')return {container};
    if(url===routeUrl)return {...profile,revision:0,settings:{mode:'manual',choices:[]}};
    throw new Error(url);
  });
  const state=vi.fn();
  render(<MemoryRouter><VideoServiceSelector projectId="p1" onStateChange={state}/></MemoryRouter>);
  await waitFor(()=>expect(state.mock.lastCall?.[0]).toMatchObject({busy:false,contextEnabled:expected,hasCloud:false}));
});
