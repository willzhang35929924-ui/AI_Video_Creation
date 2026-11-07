import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { ToastProvider } from '../components/ToastProvider';
import { ReviewPage } from './ReviewPage';

const immediateEdit=vi.hoisted(()=>({value:null as string|null}));
vi.mock('../components/ui',async original=>{
  const actual=await original<typeof import('../components/ui')>();
  const React=await import('react');
  return {...actual,FormField:(props:React.ComponentProps<typeof actual.FormField>)=>{
    React.useLayoutEffect(()=>{
      if(props.label==='本次修改要求'&&immediateEdit.value!==null){
        const input=props.children as React.ReactElement<{onChange:(event:{target:{value:string}})=>void}>;
        if(React.isValidElement(input)&&input.type==='textarea'){
          input.props.onChange({target:{value:''}});input.props.onChange({target:{value:immediateEdit.value}});
        }
      }
    },[]);
    return <actual.FormField {...props}/>;
  }};
});

afterEach(() => { cleanup(); immediateEdit.value=null; vi.unstubAllGlobals(); });

const response = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
const video = { id: 'v1', project_id: 'p1', shot_id: 's1', revision: 1, relative_path: 'original.mp4',
  review_status: 'pending', metadata_json: { mode: 'cloud', provider: 'cloud_lk888_h3_v2' } };

it.each([
  ['video=v1&job=j2&shot=s2','v1'],
  ['job=j1&shot=s2','v1'],
  ['shot=s1','v3'],
  ['video=another-project-video&job=j1&shot=s2','v1'],
  ['video=another-project-video&shot=s1','v3'],
])('continues retained deep-link selection %s using only current-project versions',async(query,expected)=>{
  const versions=[
    {...video,generation_job_id:'j1',created_at:'2026-11-01T00:00:00Z'},
    {...video,id:'v2',shot_id:'s2',generation_job_id:'j2',revision:2,relative_path:'second.mp4',created_at:'2026-11-03T00:00:00Z'},
    {...video,id:'v3',generation_job_id:'j3',revision:3,relative_path:'third.mp4',created_at:'2026-11-02T00:00:00Z'},
  ];
  const fetcher=vi.fn(async(url:string)=>{
    const path=new URL(url,'http://localhost').pathname;
    if(path==='/api/projects')return response([{id:'p1',name:'项目'}]);
    if(path==='/api/projects/p1/videos')return response(versions);
    const detail=versions.find(item=>path===`/api/videos/${item.id}`);
    if(detail)return response(detail);
    throw new Error(path);
  });
  vi.stubGlobal('fetch',fetcher);
  render(<MemoryRouter initialEntries={[`/review?project=p1&${query}`]}><ToastProvider><ReviewPage/></ToastProvider></MemoryRouter>);
  const selected=versions.find(item=>item.id===expected)!;
  expect(await screen.findByLabelText(`镜头版本 ${selected.revision} 视频播放器`)).toHaveAttribute('src',expect.stringContaining(selected.relative_path));
  await waitFor(()=>expect(fetcher.mock.calls.some(([url])=>new URL(url,'http://localhost').pathname===`/api/videos/${expected}`)).toBe(true));
  expect(fetcher.mock.calls.some(([url])=>String(url).includes('another-project-video'))).toBe(false);
});

it('keeps the first edit made before selection effects flush and does not overwrite it with late video details',async()=>{
  // Mount-time editing deliberately precedes passive effects, reproducing the
  // reset race without timers, sleeps or relying on CI machine speed.
  immediateEdit.value='镜头缓慢向前';
  let finishDetail!:(value:Response)=>void;
  const detail=new Promise<Response>(resolve=>{finishDetail=resolve;});
  const submitted:unknown[]=[];
  vi.stubGlobal('fetch',vi.fn(async(url:string,init:RequestInit)=>{
    const path=new URL(url,'http://localhost').pathname;
    if(path==='/api/projects')return response([{id:'p1',name:'项目'}]);
    if(path==='/api/projects/p1/videos')return response([video]);
    if(path==='/api/videos/v1')return detail;
    if(path.endsWith('/revision-preview')){submitted.push(JSON.parse(String(init.body)));return response({prompt:'精确预览',character_id:null,character_version_id:null});}
    throw new Error(path);
  }));
  const user=userEvent.setup();
  render(<MemoryRouter initialEntries={['/review?project=p1']}><ToastProvider><ReviewPage/></ToastProvider></MemoryRouter>);
  const input=await screen.findByLabelText('本次修改要求');
  expect(input).toHaveValue('镜头缓慢向前');
  await act(async()=>{finishDetail(response({...video,metadata_json:{...video.metadata_json,detail_loaded:true}}));await detail;});
  expect(input).toHaveValue('镜头缓慢向前');
  await user.click(screen.getByRole('button',{name:'基于当前版本重新生成'}));
  await screen.findByRole('dialog',{name:'修订完整提示词'});
  expect(submitted).toEqual([{instruction:'镜头缓慢向前',use_project_video_service:false}]);
});

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
  expect(screen.getByRole('link', { name: '查看项目视频服务与画质' })).toHaveAttribute('href', '/?project=p1&shot=s1');
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
