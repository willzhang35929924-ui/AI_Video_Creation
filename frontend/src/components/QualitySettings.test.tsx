// UI contract fixtures only; these tests do not claim model or media verification.
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {QualityProfileEditor, qualitySubmissionSupported} from './QualitySettings';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
afterEach(()=>mocked.mockReset());
describe('quality profiles',()=>{
  it('enables SeedVR2 only for its independently verified resolution and aspect',()=>{
    const q={resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'seedvr2'} as const;
    const caps={seedvr2:{state:'verified',verified_resolutions:['720p'],verified_aspect_ratios:['16:9']}};
    expect(qualitySubmissionSupported(q,caps)).toBe(true);
    expect(qualitySubmissionSupported({...q,resolution:'4k'},caps)).toBe(false);
    expect(qualitySubmissionSupported({...q,aspect_ratio:'9:16'},caps)).toBe(false);
    expect(qualitySubmissionSupported(q,{seedvr2:{...caps.seedvr2,state:'not_tested'}})).toBe(false);
    expect(qualitySubmissionSupported(q,{ai_super_resolution:{state:'verified',verified_resolutions:['4k'],verified_aspect_ratios:['16:9']}})).toBe(false);
  });
  it('requires a verified 4K result for the selected aspect before enabling framewise AI processing',()=>{
    const q={resolution:'4k',mode:'standard',aspect_ratio:'16:9',processing:'ai_super_resolution'} as const;
    expect(qualitySubmissionSupported(q,{ai_super_resolution:{state:'detected_not_tested',verified_resolutions:['4k'],verified_aspect_ratios:['16:9']}})).toBe(false);
    const caps={ai_super_resolution:{state:'verified',verified_resolutions:['4k'],verified_aspect_ratios:['16:9']}};
    expect(qualitySubmissionSupported(q,caps)).toBe(true);
    expect(qualitySubmissionSupported({...q,resolution:'1080p'},caps)).toBe(false);
    expect(qualitySubmissionSupported({...q,aspect_ratio:'9:16'},caps)).toBe(false);
    expect(qualitySubmissionSupported({...q,processing:'native'},caps)).toBe(false);
  });
  it('opens only exact SeedVR2 target pairs without implying their cross-product',()=>{
    const q={resolution:'1080p',mode:'high',aspect_ratio:'9:16',processing:'seedvr2'} as const;
    const caps={seedvr2:{state:'verified',verified_resolutions:['720p','1080p'],verified_aspect_ratios:['16:9','9:16'],verified_targets:[{resolution:'720p',aspect_ratio:'16:9'},{resolution:'1080p',aspect_ratio:'9:16'}]}};
    expect(qualitySubmissionSupported(q,caps)).toBe(true);
    expect(qualitySubmissionSupported({...q,resolution:'720p'},caps)).toBe(false);
    expect(qualitySubmissionSupported({...q,aspect_ratio:'16:9'},caps)).toBe(false);
    expect(qualitySubmissionSupported(q,{seedvr2:{...caps.seedvr2,verified_targets:[]}})).toBe(false);
    expect(qualitySubmissionSupported(q,{seedvr2:{...caps.seedvr2,verified_targets:undefined}})).toBe(false);
    expect(qualitySubmissionSupported(q,{seedvr2:{...caps.seedvr2,state:'not_tested'}})).toBe(false);
  });
  it('keeps a detected but untested image super-resolution model disabled for production',async()=>{
    mocked.mockImplementation(async url=>url==='/api/quality/capabilities'?{ai_super_resolution:{state:'detected_not_tested',model:'RealESRGAN_x4plus',verified_resolutions:[],verified_aspect_ratios:[]}}:{scope:'system',target_id:'default',revision:0,settings:{resolution:'4k',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'},plan:{}});
    render(<MemoryRouter><QualityProfileEditor scope="system" targetId="default" title="系统默认"/></MemoryRouter>);
    expect(await screen.findByRole('option',{name:'AI 视频超分（已检测，未实测）'})).toBeDisabled();
    expect(screen.getByLabelText('默认处理方式')).toHaveValue('lanczos');
    expect(mocked.mock.calls.some(([,init])=>init?.method==='POST')).toBe(false);
  });
  it('blocks a previously valid AI selection after the resolution changes without silently replacing the method',async()=>{
    const user=userEvent.setup();
    mocked.mockImplementation(async url=>url==='/api/quality/capabilities'?{ai_super_resolution:{state:'verified',verified_resolutions:['4k'],verified_aspect_ratios:['16:9']}}:{scope:'shot',target_id:'s1',revision:1,settings:{resolution:'4k',mode:'standard',aspect_ratio:'16:9',processing:'ai_super_resolution'},plan:{}});
    render(<MemoryRouter><QualityProfileEditor scope="shot" targetId="s1" title="镜头配置"/></MemoryRouter>);
    expect(await screen.findByRole('option',{name:'4K输出（Real-ESRGAN逐帧超分）'})).toBeEnabled();
    await user.selectOptions(screen.getByLabelText('此镜输出规格'),'1080p');
    expect(screen.getByRole('alert')).toHaveTextContent('不会静默降级');
    expect(screen.getByLabelText('此镜处理方式')).toHaveValue('ai_super_resolution');
    expect(screen.getByRole('button',{name:'保存此镜覆盖'})).toBeDisabled();
    expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(false);
    await user.selectOptions(screen.getByLabelText('此镜处理方式'),'lanczos');
    expect(screen.getByRole('button',{name:'保存此镜覆盖'})).toBeEnabled();
  });
  it('saves a shot override with concurrency revision and restores actual inheritance with DELETE',async()=>{
    const user=userEvent.setup();
    const settings={resolution:'720p',mode:'standard',aspect_ratio:'16:9',processing:'lanczos'};
    mocked.mockImplementation(async (_url,init)=>_url==='/api/quality/capabilities'?{ai_super_resolution:{state:'not_configured'}}:({scope:'shot',target_id:'s1',revision:init?.method==='PUT'?1:0,inherited_from:init?.method==='PUT'?null:'project:p1',settings:init?.method==='PUT'?JSON.parse(String(init.body)).settings:settings,plan:{supported:true,method:'lanczos'}}));
    render(<MemoryRouter><QualityProfileEditor scope="shot" targetId="s1" title="镜头配置"/></MemoryRouter>);
    await screen.findByText('配置来源：project:p1 · 修订 0');
    expect(screen.getByRole('option',{name:'AI 视频超分（尚未配置）'})).toBeDisabled();
    expect(screen.getByRole('option',{name:'原生目标分辨率（尚未验证）'})).toBeDisabled();
    await user.selectOptions(screen.getByLabelText('此镜输出规格'),'4k');
    await user.selectOptions(screen.getByLabelText('此镜质量模式'),'high');
    await user.click(screen.getByRole('button',{name:'保存此镜覆盖'}));
    await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(true));
    const save=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
    expect(save[0]).toBe('/api/quality/profiles/shot/s1');
    expect(JSON.parse(String(save[1]?.body))).toEqual({settings:{resolution:'4k',mode:'high',aspect_ratio:'16:9',processing:'lanczos'},expected_revision:0});
    await user.click(screen.getByRole('button',{name:'恢复继承项目'}));
    await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='DELETE')).toBe(true));
    expect(await screen.findByText('配置来源：project:p1 · 修订 0')).toBeInTheDocument();
  });
  it('does not offer a successful default when the real profile request fails',async()=>{
    mocked.mockRejectedValue(new Error('真实配置接口不可用'));
    render(<MemoryRouter><QualityProfileEditor scope="system" targetId="default" title="系统默认"/></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveTextContent('真实配置接口不可用');
    expect(screen.queryByRole('button',{name:'保存画质设置'})).not.toBeInTheDocument();
  });
});
