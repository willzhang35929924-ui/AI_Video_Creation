import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ToastProvider';
import { ReviewPage } from './ReviewPage';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const response = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
const video = { id: 'v1', project_id: 'p1', shot_id: 's1', revision: 1, relative_path: 'original.mp4',
  review_status: 'pending', metadata_json: { mode: 'cloud', provider: 'cloud_lk888_h3_v2' } };

it('re-previews a cloud revision on explicit local opt-in and submits precisely the reviewed input', async () => {
  const preview = { prompt: '此处显示实际修订提示词', character_id: null, character_version_id: null };
  const calls: { path: string; body: unknown; signal?: AbortSignal | null }[] = [];
  let finishFirstPreview: ((value: Response) => void) | undefined;
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    const path = new URL(url, 'http://localhost').pathname;
    if (path === '/api/projects') return response([{ id: 'p1', name: '项目' }]);
    if (path === '/api/projects/p1/videos') return response([video]);
    if (path === '/api/videos/v1') return response(video);
    calls.push({ path, body: JSON.parse(String(init.body)), signal: init.signal });
    if (path.endsWith('/revision-preview')) {
      if (calls.length === 1) return new Promise<Response>(resolve => { finishFirstPreview = resolve; });
      return response(preview);
    }
    if (path.endsWith('/revise')) return response({});
    throw new Error(path);
  }));
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={['/review?project=p1']}><ToastProvider><ReviewPage/></ToastProvider></MemoryRouter>);
  const local = await screen.findByRole('checkbox', { name: '使用本项目的本地 H3 服务' });
  const instruction = screen.getByLabelText('本次修改要求');
  const start = screen.getByRole('button', { name: '基于当前版本重新生成' });
  const dialog = () => screen.queryByRole('dialog', { name: '修订完整提示词' });
  expect(local).not.toBeChecked();
  expect(screen.getByRole('link', { name: '查看项目视频服务与画质' })).toHaveAttribute('href', '/video-production?project=p1&shot=s1');
  await user.clear(instruction);
  await user.type(instruction, '镜头缓慢向前');
  await user.click(start);
  await waitFor(() => expect(finishFirstPreview).toBeDefined());
  await user.click(local);
  expect(calls[0]).toMatchObject({ path: '/api/videos/v1/revision-preview', body: { instruction: '镜头缓慢向前', use_project_video_service: false } });
  expect(calls[0].signal?.aborted).toBe(true);
  finishFirstPreview?.(response(preview));
  await waitFor(() => expect(start).toBeEnabled());
  expect(dialog()).toBeNull();

  await user.click(start);
  expect(await screen.findByRole('dialog', { name: '修订完整提示词' })).toHaveTextContent('生成服务 · 本项目的本地 H3');
  await user.click(screen.getByRole('button', { name: '提交真实修订' }));
  await waitFor(() => expect(calls).toHaveLength(3));
  expect(calls.slice(1).map(({ path, body }) => ({ path, body }))).toEqual([
    { path: '/api/videos/v1/revision-preview', body: { instruction: '镜头缓慢向前', use_project_video_service: true } },
    { path: '/api/videos/v1/revise', body: { instruction: '镜头缓慢向前', use_project_video_service: true } },
  ]);
});
