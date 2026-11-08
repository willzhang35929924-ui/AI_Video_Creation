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

afterEach(() => { cleanup(); immediateEdit.value=null; vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const response = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
const video = { id: 'v1', project_id: 'p1', shot_id: 's1', revision: 1, relative_path: 'original.mp4',
  review_status: 'pending', metadata_json: { mode: 'cloud', provider: 'cloud_lk888_h3_v2' } };

function captureToastExpiry() {
  const callbacks: Array<() => void> = [];
  const setTimeout = window.setTimeout.bind(window);
  vi.spyOn(window, 'setTimeout').mockImplementation(((handler: TimerHandler, delay?: number, ...args: unknown[]) => {
    if (delay === 4500 && typeof handler === 'function') {
      callbacks.push(() => handler(...args));
      return 4500;
    }
    return setTimeout(handler, delay, ...args);
  }) as typeof window.setTimeout);
  return async () => { await act(async () => { callbacks.splice(0).forEach(callback => callback()); }); };
}

it('keeps preview validation errors beside the retained instruction after the toast expires and clears them on edit', async () => {
  const expireToasts = captureToastExpiry();
  const message = '此镜头参考与修改约束冲突，请核对原图';
  const fetcher = vi.fn(async (url: string) => {
    const path = new URL(url, 'http://localhost').pathname;
    if (path === '/api/projects') return response([{ id: 'p1', name: '项目' }]);
    if (path === '/api/projects/p1/videos') return response([video]);
    if (path === '/api/videos/v1') return response(video);
    if (path.endsWith('/revision-preview')) return new Response(JSON.stringify({ error: { message, code: 'revision_product_identity_conflict' } }), { status: 409, headers: { 'Content-Type': 'application/json' } });
    throw new Error(path);
  });
  vi.stubGlobal('fetch', fetcher);
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={['/review?project=p1']}><ToastProvider><ReviewPage /></ToastProvider></MemoryRouter>);
  const instruction = await screen.findByLabelText('本次修改要求');
  await user.clear(instruction);
  await user.type(instruction, '保持绿色产品，镜头缓慢向前');
  await user.click(screen.getByRole('button', { name: '基于当前版本重新生成' }));
  const failure = await screen.findByRole('alert', { name: '修订未完成' });
  await expireToasts();
  expect(screen.getByRole('region', { name: '操作通知' })).toBeEmptyDOMElement();
  expect(failure).toHaveTextContent(message);
  expect(failure).toHaveTextContent('尚未提交生成任务');
  expect(instruction).toHaveValue('保持绿色产品，镜头缓慢向前');
  expect(instruction).toHaveAttribute('aria-describedby', 'revision-error');
  expect(screen.getByRole('link', { name: '检查项目人物与产品参考设置' })).toHaveAttribute('href', '/?project=p1&shot=s1');
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/revise'))).toBe(false);
  expect(screen.queryByText('修订任务正在执行，旧版本与原片继续保留')).not.toBeInTheDocument();
  await user.type(instruction, '，不改变产品颜色');
  expect(screen.queryByRole('alert', { name: '修订未完成' })).not.toBeInTheDocument();
});

it('shows a persistent submission error outside the preview dialog and allows a fresh two-step retry', async () => {
  const expireToasts = captureToastExpiry();
  const message = '本地生成服务暂时不可用';
  let previews = 0, submissions = 0;
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const path = new URL(url, 'http://localhost').pathname;
    if (path === '/api/projects') return response([{ id: 'p1', name: '项目' }]);
    if (path === '/api/projects/p1/videos') return response([video]);
    if (path === '/api/videos/v1') return response(video);
    if (path.endsWith('/revision-preview')) { previews++; return response({ prompt: '完整真实预览' }); }
    if (path.endsWith('/revise')) { submissions++; return new Response(JSON.stringify({ error: { message } }), { status: 503, headers: { 'Content-Type': 'application/json' } }); }
    throw new Error(path);
  }));
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={['/review?project=p1']}><ToastProvider><ReviewPage /></ToastProvider></MemoryRouter>);
  const instruction = await screen.findByLabelText('本次修改要求');
  await user.clear(instruction);
  await user.type(instruction, '保持产品原色');
  await user.click(screen.getByRole('button', { name: '基于当前版本重新生成' }));
  await user.click(await screen.findByRole('button', { name: '提交真实修订' }));
  const failure = await screen.findByRole('alert', { name: '修订未完成' });
  await expireToasts();
  expect(failure).toHaveTextContent(message);
  expect(failure).toHaveTextContent('修订提交未成功确认');
  expect(screen.queryByRole('dialog', { name: '修订完整提示词' })).not.toBeInTheDocument();
  expect(instruction).toHaveValue('保持产品原色');
  expect(screen.queryByText('修订生成已提交；旧视频仍保留在版本历史中')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '基于当前版本重新生成' }));
  await screen.findByRole('dialog', { name: '修订完整提示词' });
  expect(screen.queryByRole('alert', { name: '修订未完成' })).not.toBeInTheDocument();
  expect({ previews, submissions }).toEqual({ previews: 2, submissions: 1 });
});

it.each([
  ['model', { error_message: 'H3 模型执行失败：显存不足' }, 'H3 模型执行失败：显存不足'],
  ['postprocessing', { parameters_json: { revision_postprocess: { state: 'failed', error: { message: '修订原片已保存；SeedVR 处理失败' } } } }, '修订原片已保存；SeedVR 处理失败'],
] as const)('retains a terminal %s failure after polling and toast expiry without claiming a complete revision', async (_stage, failureFields, message) => {
  const expireToasts = captureToastExpiry();
  let tick: (() => Promise<void>) | undefined;
  vi.spyOn(window, 'setInterval').mockImplementation(((fn: () => Promise<void>, delay: number) => {
    if (delay === 2500) tick = fn;
    return 123;
  }) as typeof window.setInterval);
  vi.spyOn(window, 'clearInterval').mockImplementation(() => {});
  const raw = { ...video, metadata_json: { ...video.metadata_json, revision_postprocess: { job_id: 'j1', state: 'processing' } } };
  const fetcher = vi.fn(async (url: string) => {
    const path = new URL(url, 'http://localhost').pathname;
    if (path === '/api/projects') return response([{ id: 'p1', name: '项目' }]);
    if (path === '/api/projects/p1/videos') return response([raw]);
    if (path === '/api/videos/v1') return response(raw);
    if (path === '/api/jobs/j1') return response({ id: 'j1', shot_id: 's1', status: 'failed', provider: 'local', ...failureFields });
    throw new Error(path);
  });
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter initialEntries={['/review?project=p1']}><ToastProvider><ReviewPage /></ToastProvider></MemoryRouter>);
  const instruction = await screen.findByLabelText('本次修改要求');
  await waitFor(() => expect(tick).toBeDefined());
  const retainedInstruction = (instruction as HTMLTextAreaElement).value;
  await act(async () => { await tick?.(); });
  const failure = await screen.findByRole('alert', { name: '修订未完成' });
  await expireToasts();
  expect(screen.getByRole('region', { name: '操作通知' })).toBeEmptyDOMElement();
  expect(failure).toHaveTextContent(message);
  expect(instruction).toHaveValue(retainedInstruction);
  expect(screen.getByRole('button', { name: '基于当前版本重新生成' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '通过为候选成片' })).toBeDisabled();
  expect(screen.queryByText('修订任务已完成，请检查最新完整版本的画面与声音')).not.toBeInTheDocument();
  expect(fetcher.mock.calls.some(([url]) => String(url).endsWith('/revise'))).toBe(false);
});

it('loads the legacy recovery diagnosis from selected-video details and resumes only postprocessing',async()=>{
  const recovery={available:true,required:true,state:'available',reason:'旧配音和高清尚未接回'};
  const calls:string[]=[];
  const fetcher=vi.fn(async(url:string)=>{
    const path=new URL(url,'http://localhost').pathname;
    if(path==='/api/projects')return response([{id:'p1',name:'项目'}]);
    if(path==='/api/projects/p1/videos')return response([video]);
    if(path==='/api/videos/v1')return response({...video,metadata_json:{...video.metadata_json,revision_processing_recovery:recovery}});
    if(path==='/api/videos/v1/resume-revision-processing'){
      calls.push(path);return response({job:{id:'j1',status:'postprocessing'},provider_resubmitted:false,replayed:false});
    }
    throw new Error(path);
  });
  vi.stubGlobal('fetch',fetcher);
  render(<MemoryRouter initialEntries={['/review?project=p1']}><ToastProvider><ReviewPage/></ToastProvider></MemoryRouter>);
  const user=userEvent.setup();
  const resume=await screen.findByRole('button',{name:'恢复此修订的配音与画质'});
  expect(screen.getByRole('button',{name:'通过为候选成片'})).toBeDisabled();
  await user.click(resume);
  expect(await screen.findByText('正在补齐修订的配音与画质，完成后可审核最终版本')).toBeInTheDocument();
  expect(calls).toEqual(['/api/videos/v1/resume-revision-processing']);
  expect(fetcher.mock.calls.some(([url])=>String(url).endsWith('/revise'))).toBe(false);
});

it('continues polling postprocessing after refresh and does not unlock another generation',async()=>{
  let tick:(()=>Promise<void>)|undefined;
  vi.spyOn(window,'setInterval').mockImplementation(((fn:()=>Promise<void>,delay:number)=>{
    if(delay===2500)tick=fn;return 123;
  }) as typeof window.setInterval);
  vi.spyOn(window,'clearInterval').mockImplementation(()=>{});
  const raw={...video,metadata_json:{...video.metadata_json,revision_postprocess:{job_id:'j1',state:'processing'},
    revision_processing_recovery:{required:true,available:false,state:'processing',reason:'配音画质处理中'}}};
  let polls=0,done=false;
  const final={...video,id:'complete',revision:2,relative_path:'complete.mp4',metadata_json:{revision_postprocess:{job_id:'j1',state:'ready'}}};
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>{
    const path=new URL(url,'http://localhost').pathname;
    if(path==='/api/projects')return response([{id:'p1',name:'项目'}]);
    if(path==='/api/projects/p1/videos')return response(done?[final,raw]:[raw]);
    if(path==='/api/videos/v1')return response(raw);
    if(path==='/api/videos/complete')return response(final);
    if(path==='/api/jobs/j1'){polls++;return response({id:'j1',shot_id:'s1',status:done?'generated':'postprocessing',provider:'local',
      parameters_json:{revision_postprocess:{state:done?'ready':'processing',final_video_id:done?'complete':null}}});}
    throw new Error(path);
  }));
  render(<MemoryRouter initialEntries={['/review?project=p1']}><ToastProvider><ReviewPage/></ToastProvider></MemoryRouter>);
  await waitFor(()=>expect(tick).toBeDefined());
  await act(async()=>{await tick?.();});
  expect(polls).toBe(1);
  expect(screen.getByRole('button',{name:'基于当前版本重新生成'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'通过为候选成片'})).toBeDisabled();
  expect(screen.getByText('正在补齐修订的配音与画质，完成后可审核最终版本')).toBeInTheDocument();
  done=true;
  await act(async()=>{await tick?.();});
  expect(await screen.findByLabelText('镜头版本 2 视频播放器')).toHaveAttribute('src',expect.stringContaining('complete.mp4'));
  expect(screen.getByRole('button',{name:'通过为候选成片'})).toBeEnabled();
});

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
