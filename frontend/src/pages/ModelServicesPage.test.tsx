// HTTP contract fixtures test UI state only; they are not model verification.
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ModelServicesPage} from './ModelServicesPage';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const settings={mode:'local_only',choices:[],parameters:{},quality_mode:'standard',allow_fallback:false,allow_paid_fallback:false,max_retries:0};
const profile={scope:'system',target_id:'default',slot:'dialogue_generation',revision:7,settings};
afterEach(()=>mocked.mockReset());
describe('task model policies',()=>{
  it('locks navigation during edits and sends revision plus separate fallback consent',async()=>{
    const user=userEvent.setup();
    mocked.mockImplementation(async (url,init)=>url==='/api/studio/projects'?[]:url==='/api/model-services'?{slots:[{slot:'dialogue_generation',name:'角色配音',allowed_modules:['dialogue_generation'],default_route:profile,candidates:[]}]}:init?.method==='PUT'?{...profile,revision:8,settings:JSON.parse(String(init.body)).settings}:profile);
    render(<MemoryRouter><ModelServicesPage/></MemoryRouter>);
    const paid=await screen.findByLabelText('允许付费云端回退（还需本次任务授权）');
    expect(paid).toBeDisabled();
    await user.click(screen.getByLabelText('允许确定失败后的候选回退'));
    await user.click(paid);
    expect(screen.getByLabelText('策略配置范围')).toBeDisabled();
    await user.click(screen.getByRole('button',{name:'保存模型策略'}));
    await waitFor(()=>expect(mocked.mock.calls.some(([,v])=>v?.method==='PUT')).toBe(true));
    const save=mocked.mock.calls.find(([,v])=>v?.method==='PUT')!;
    expect(save[0]).toBe('/api/model-services/routes/system/default/dialogue_generation');
    expect(JSON.parse(String(save[1]?.body))).toMatchObject({expected_revision:7,settings:{allow_fallback:true,allow_paid_fallback:true}});
    await waitFor(()=>expect(screen.getByLabelText('策略配置范围')).toBeEnabled());
  });
  it('keeps unverified candidates visible without allowing them to be added',async()=>{
    mocked.mockImplementation(async url=>url==='/api/studio/projects'?[]:url==='/api/model-services'?{slots:[{slot:'dialogue_generation',name:'角色配音',allowed_modules:['dialogue_generation'],default_route:profile,candidates:[{service_id:'s1',model_id:'model',name:'待测本地模型',provider:'local_runtime',module:'dialogue_generation',is_cloud:false,enabled:true,eligible:false,verification:'not_tested',reasons:['未完成真实音频推理'],evidence:{}}]}]}:profile);
    render(<MemoryRouter><ModelServicesPage/></MemoryRouter>);
    expect(await screen.findByRole('button',{name:'添加已验证候选'})).toBeDisabled();
    expect(screen.getByText('未完成真实音频推理')).toBeInTheDocument();
    expect(mocked.mock.calls.some(([,v])=>v?.method==='POST')).toBe(false);
  });
  it('preserves the full long project provenance and reads the matching route without writing',async()=>{
    const id='prj_example_123';
    const scoped={...profile,scope:'project',target_id:id};
    mocked.mockImplementation(async url=>url==='/api/studio/projects'?[{id,name:'长项目来源回归'}]:url==='/api/model-services'?{slots:[{slot:'dialogue_generation',name:'角色配音',allowed_modules:['dialogue_generation'],default_route:scoped,candidates:[]}]}:String(url).endsWith('/shots')?[]:scoped);
    render(<MemoryRouter initialEntries={[`/settings/ai-services?project=${id}`]}><ModelServicesPage/></MemoryRouter>);
    const provenance=await screen.findByText(/生效来源：project\//);
    expect(provenance).toHaveTextContent(`生效来源：project/${id}`);
    expect(screen.getByLabelText('策略配置范围')).toHaveValue('project');
    expect(mocked).toHaveBeenCalledWith(`/api/model-services/routes/project/${id}/dialogue_generation`);
    expect(mocked.mock.calls.every(([,init])=>!init?.method||init.method==='GET')).toBe(true);
  });
});
