// Profile contracts only; these tests do not call model services.
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { request } from '../lib/api';
import { AIServicesPage } from './AIServicesPage';

vi.mock('../lib/api', async original => ({ ...await original<typeof import('../lib/api')>(), request: vi.fn() }));
vi.mock('../components/ToastProvider', () => ({ useToast: () => ({ notify: vi.fn() }) }));
const mocked = vi.mocked(request);
afterEach(() => { cleanup(); mocked.mockReset(); });

const deployment = { container: true, defaults: { comfyui: 'http://host.docker.internal:8189',
  local_llm: 'http://host.docker.internal:1234/v1', context_ir: 'http://host.docker.internal:8788' },
  video_workflows: [{ id: 'context_ir', label: 'Context-IR 三档：Fast 4 / Quality 20 / Quality 30' },
    { id: 'native_h3_r2v', label: '原生 H3 参考图工作流（20步）' }] };

it('uses the host gateway, saves explicit origin trust, and offers bundled workflows without host paths', async () => {
  mocked.mockImplementation(async (url, init) => {
    if (url === '/api/settings/ai-service-options') return deployment;
    if (url === '/api/settings/ai-services') return init?.method === 'POST' ? {} : [];
    throw new Error(url);
  });
  const user = userEvent.setup();
  render(<MemoryRouter><AIServicesPage embedded /></MemoryRouter>);
  await waitFor(() => expect(mocked).toHaveBeenCalledWith('/api/settings/ai-service-options'));
  await user.click(screen.getByRole('button', { name: '添加服务' }));
  const dialog = within(screen.getByRole('dialog', { name: '配置 AI 服务' }));
  await user.selectOptions(dialog.getByLabelText('服务用途'), 'video_generation');
  expect(dialog.getByLabelText('服务地址 base_url')).toHaveValue('http://host.docker.internal:8788');
  expect(dialog.getByLabelText('内置视频工作流')).toHaveValue('context_ir');
  expect(dialog.queryByLabelText('本地 R2V 工作流路径')).toBeNull();
  await user.type(dialog.getByLabelText('服务名称'), '我的本地视频节点');
  await user.click(dialog.getByLabelText('确认这是我管理的本地模型服务'));
  const address = dialog.getByLabelText('服务地址 base_url');
  await user.clear(address);
  await user.type(address, 'http://192.168.1.20:8788');
  expect(dialog.getByLabelText('确认这是我管理的本地模型服务')).not.toBeChecked();
  await user.click(dialog.getByLabelText('确认这是我管理的本地模型服务'));
  await user.click(dialog.getByRole('button', { name: '保存配置' }));
  const saved = mocked.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(JSON.parse(String(saved[1]?.body))).toMatchObject({ provider: 'comfyui', module: 'video_generation',
    base_url: 'http://192.168.1.20:8788', settings: { workflow_profile: 'context_ir', h3_context_ir: true,
      local_service_origin: 'http://192.168.1.20:8788' } });
});

it('shows reachable but incomplete native model checks in the simplified settings', async () => {
  mocked.mockImplementation(async url => {
    if (url === '/api/settings/ai-service-options') return deployment;
    if (url === '/api/settings/ai-services') return [{ id: 'node', name: '本地视频节点', module: 'video_generation',
      provider: 'comfyui', default_model: 'minimax_h3_w6a8', base_url: deployment.defaults.context_ir,
      enabled: true, models: [], settings: {}, capabilities: {}, last_test: { status: 'reachable',
        error: '本地推理节点尚未就绪', runtime: { checks: [{ name: 'H3模型', ok: false, detail: '缺少指定权重' }] } } }];
    throw new Error(url);
  });
  render(<MemoryRouter><AIServicesPage embedded /></MemoryRouter>);
  expect(await screen.findByText('接口可达 · 尚未就绪')).toBeVisible();
  expect(screen.getByLabelText('本地推理节点检查')).toHaveTextContent('H3模型：未就绪 · 缺少指定权重');
  expect(screen.getByRole('alert')).toHaveTextContent('本地推理节点尚未就绪');
});
