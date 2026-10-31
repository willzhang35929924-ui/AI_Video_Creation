// Isolated HTTP/UI contracts; these fixtures are not real generated media.
import {act,cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {Link,MemoryRouter,useLocation} from 'react-router-dom';
import {afterEach,beforeEach,describe,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {AudioReferencePicker} from './AudioReferencePicker';
import {ProjectVoicePreparation} from './ProjectVoicePreparation';
import type {ProjectVoiceState} from './ProjectVoicePreparation';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const base='/api/studio/projects/p1/voices';
const endpoint=base+'?allow_cloud_costs=false&allow_paid_fallback=false';
const group=()=>({character_id:'person-1',name:'角色甲',version_id:'version-1',version_number:1,version_state:'draft',participant_ids:['p-a','p-b'],shot_ids:['s1','s2'],dialogue_count:2,audio_id:null as string|null,mixed:false,expected_token:'a'.repeat(64),tts_enabled:true,requires_reference_audio:true,ready:false,issues:['尚未绑定参考音频。']});
const overview=()=>({project_id:'p1',audio_policy:'auto',groups:[group()],services:[{shot_id:'s1',enabled:true,service_id:'tts-local',name:'本地 CosyVoice3',provider:'local_runtime',model_id:'cosyvoice3',requires_reference_audio:true,issues:[]}],tts_enabled:true,ready_to_start:false,blockers:['角色甲：尚未绑定参考音频。'],summary:'角色配音准备尚未完成。'});
const asset=(id='audio-existing')=>({id,project_id:'p1',character_version_id:'version-1',relative_path:`audio/${id}.wav`,sha256:'b'.repeat(64),metadata:{actual:{duration:5},human_approved:false}});
const puts=()=>mocked.mock.calls.filter(([,init])=>init?.method==='PUT');
const posts=()=>mocked.mock.calls.filter(([,init])=>init?.method==='POST');
function LocationProbe(){const location=useLocation();return <output aria-label="当前测试路径">{location.pathname}</output>;}
let data:ReturnType<typeof overview>;
beforeEach(()=>{
  data=overview();
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/settings/ai-services')return [];
    if(url.startsWith('/api/model-services/routes/project/'))return {scope:'project',target_id:url.split('/')[5],slot:'dialogue_generation',revision:0,settings:{choices:[]}};
    if(url.startsWith(base)&&!init?.method)return structuredClone(data);
    if(url.startsWith('/api/model-services/audio-assets?'))return [asset()];
    if(url===base+'/person-1?allow_cloud_costs=false&allow_paid_fallback=false'&&init?.method==='PUT'){
      const body=JSON.parse(String(init.body));
      data={...data,ready_to_start:Boolean(body.audio_id),blockers:body.audio_id?[]:['缺少参考音频'],groups:[{...data.groups[0],audio_id:body.audio_id,ready:Boolean(body.audio_id),expected_token:'c'.repeat(64),issues:body.audio_id?[]:['缺少参考音频']}]};
      return structuredClone(data);
    }
    if(url==='/api/model-services/audio-assets'&&init?.method==='POST')return asset('audio-uploaded');
    throw new Error('Unexpected contract URL '+url);
  });
});
afterEach(()=>{cleanup();mocked.mockReset();vi.restoreAllMocks();});

describe('project voice preparation before production',()=>{
  it('binds a reusable library voice through stable project, character and version IDs after explicit save',async()=>{
    const original=mocked.getMockImplementation()!;mocked.mockImplementation(async(url,init)=>{
      if(url==='/api/voice-library')return {items:[{id:'voice-local',name:'本人授权录音',language:'zh',preview_url:'/api/media/voice.wav',actual:{duration:3},capabilities:{playback:true,voice_reference:true,tts:false,voice_clone:false,reason:'当前仅作为声音参考'}}]};
      if(url==='/api/voice-library/voice-local/bind'){data={...data,ready_to_start:true,blockers:[],groups:[{...data.groups[0],audio_id:'bound-audio',ready:true,issues:[]}]};return {...data,groups:[{...data.groups[0],library_asset_id:'voice-local',configuration_status:'ready',generation_status:'pending'}]};}
      return original(url,init);
    });
    const user=userEvent.setup();render(<MemoryRouter><ProjectVoicePreparation projectId="p1"/></MemoryRouter>);await screen.findByRole('option',{name:/本人授权录音/});await user.selectOptions(screen.getByLabelText('角色甲配音库声音'),'voice-local');expect(posts()).toHaveLength(0);expect(screen.getByText(/当前仅作为声音参考/)).toBeInTheDocument();await user.click(screen.getByRole('button',{name:'保存角色甲配音'}));await waitFor(()=>expect(posts()).toHaveLength(1));expect(posts()[0][0]).toBe('/api/voice-library/voice-local/bind');expect(JSON.parse(String(posts()[0][1]?.body))).toEqual({project_id:'p1',character_id:'person-1',version_id:'version-1',expected_token:'a'.repeat(64)});expect(puts()).toHaveLength(0);
  });
  it('shows upload and source controls without expanding details, and saves one role across its current shots',async()=>{
    const user=userEvent.setup(),states:ProjectVoiceState[]=[],changed=vi.fn();
    render(<MemoryRouter><ProjectVoicePreparation projectId="p1" onStateChange={state=>states.push(state)} onChanged={changed}/></MemoryRouter>);
    const card=await screen.findByRole('article',{name:'角色甲配音设置'});
    expect(screen.getByRole('heading',{name:'角色配音（开始制作前配置）'})).toBeInTheDocument();
    expect(within(card).getByLabelText('上传本人物参考音频')).toBeVisible();
    expect(within(card).getByLabelText('音频来源')).toBeVisible();
    expect(card.querySelector('details')).toBeNull();
    expect(within(card).getByText(/2 条台词 · 2 个镜头/)).toBeInTheDocument();
    expect(states.at(-1)?.ready).toBe(false);
    await within(card).findByRole('option',{name:/audio-existing/});
    await user.selectOptions(within(card).getByLabelText('角色甲参考音频'),'audio-existing');
    await waitFor(()=>expect(states.at(-1)?.dirty).toBe(true));
    expect(card.querySelector('audio')).toHaveAttribute('src',expect.stringContaining('audio/audio-existing.wav'));
    await user.click(within(card).getByRole('button',{name:'保存角色甲配音'}));
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:true,dirty:false,busy:false})));
    expect(JSON.parse(String(puts()[0][1]?.body))).toEqual({version_id:'version-1',audio_id:'audio-existing',expected_token:'a'.repeat(64)});
    expect(changed).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status')).toHaveTextContent('应用到本项目该角色版本的 2 个镜头');
    expect(posts()).toHaveLength(0);
  });

  it('uploads the actual selected file to the role version, then requires a separate save',async()=>{
    const user=userEvent.setup(),states:ProjectVoiceState[]=[];
    render(<MemoryRouter><ProjectVoicePreparation projectId="p1" onStateChange={state=>states.push(state)}/></MemoryRouter>);
    const card=await screen.findByRole('article',{name:'角色甲配音设置'});
    const file=new File(['isolated audio fixture'],'voice.wav',{type:'audio/wav'});
    await user.upload(within(card).getByLabelText('上传本人物参考音频'),file);
    await waitFor(()=>expect(states.at(-1)?.dirty).toBe(true));
    expect(screen.getByLabelText('本项目配音模型')).toBeDisabled();
    expect(within(card).getByRole('button',{name:'保存角色甲配音'})).toBeDisabled();
    await user.type(within(card).getByLabelText('来源与授权说明'),'原创测试声线，内部验证。');
    await user.click(within(card).getByRole('button',{name:'上传并选入本人物草稿'}));
    await waitFor(()=>expect(within(card).getByLabelText('角色甲参考音频')).toHaveValue('audio-uploaded'));
    const form=posts()[0][1]?.body as FormData;
    expect(form.get('project_id')).toBe('p1');expect(form.get('character_version_id')).toBe('version-1');
    expect(form.get('file')).toBe(file);expect(form.get('source_type')).toBe('ai_generated');
    expect(form.get('declaration')).toBe('原创测试声线，内部验证。');
    expect(puts()).toHaveLength(0);expect(states.at(-1)?.dirty).toBe(true);
    await user.click(within(card).getByRole('button',{name:'保存角色甲配音'}));
    await waitFor(()=>expect(states.at(-1)?.ready).toBe(true));
    expect(JSON.parse(String(puts()[0][1]?.body)).audio_id).toBe('audio-uploaded');
  });

  it('keeps explicitly selected native-audio mode usable without pretending a TTS profile has been configured',async()=>{
    data={...overview(),audio_policy:'native',tts_enabled:false,ready_to_start:true,services:[],blockers:[],summary:'用户已明确选择沿用视频模型原声。',groups:[{...group(),tts_enabled:false,requires_reference_audio:false,ready:true,issues:[]}]};
    const states:ProjectVoiceState[]=[];
    render(<MemoryRouter><ProjectVoicePreparation projectId="p1" onStateChange={state=>states.push(state)}/></MemoryRouter>);
    expect(await screen.findByText('本项目明确沿用视频原声')).toBeInTheDocument();
    await waitFor(()=>expect(states.at(-1)?.ready).toBe(true));
    expect(screen.getByRole('link',{name:'配置角色配音服务'})).toHaveAttribute('href','/settings');
    expect(puts()).toHaveLength(0);expect(posts()).toHaveLength(0);
  });

  it('blocks production for an unsaved model choice and refreshes actual readiness after saving',async()=>{
    data={...overview(),ready_to_start:true,blockers:[],groups:[{...group(),audio_id:'audio-existing',ready:true,issues:[]}]};
    let route={scope:'project',target_id:'p1',slot:'dialogue_generation',revision:3,settings:{choices:[{service_id:'cosy',model_id:'cosyvoice3-0.5b-2512'}]}};
    const original=mocked.getMockImplementation()!;
    mocked.mockImplementation(async(url,init)=>{
      if(url==='/api/settings/ai-services')return [{id:'cosy',name:'CosyVoice3',module:'dialogue_generation',enabled:true,provider:'local_runtime',default_model:'cosyvoice3-0.5b-2512',settings:{runtime_family:'cosyvoice3'}},{id:'index',name:'IndexTTS 2.5',module:'dialogue_generation',enabled:true,provider:'local_runtime',default_model:'indextts-2.5',settings:{runtime_family:'indextts25'}}];
      if(url==='/api/model-services/routes/project/p1/dialogue_generation'){
        if(init?.method==='PUT'){
          route={...route,revision:4,settings:JSON.parse(String(init.body)).settings};
          data={...data,services:[{...data.services[0],service_id:'index',name:'IndexTTS 2.5',model_id:'indextts-2.5'}]};
        }
        return route;
      }
      return original(url,init);
    });
    const user=userEvent.setup(),states:ProjectVoiceState[]=[],changed=vi.fn();
    render(<MemoryRouter><ProjectVoicePreparation projectId="p1" onStateChange={state=>states.push(state)} onChanged={changed}/></MemoryRouter>);
    await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('cosy'));
    await user.selectOptions(screen.getByLabelText('本项目配音模型'),'index');
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({dirty:true,ready:false})));
    expect(screen.getByLabelText('上传本人物参考音频')).toBeDisabled();
    await user.click(screen.getByRole('button',{name:'保存配音模型'}));
    await waitFor(()=>expect(changed).toHaveBeenCalledTimes(1));
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({dirty:false,busy:false,ready:true})));
    expect(await screen.findByText('IndexTTS 2.5 · indextts-2.5',{selector:'p'})).toBeVisible();
    expect(mocked.mock.calls.filter(([url])=>url===endpoint).length).toBeGreaterThan(1);
    expect(posts()).toHaveLength(0);
  });

  it('does not show an old project upload result as belonging to a newly selected project',async()=>{
    let complete!:(value:ReturnType<typeof asset>)=>void;
    const pending=new Promise<ReturnType<typeof asset>>(resolve=>{complete=resolve;});
    const original=mocked.getMockImplementation()!;
    mocked.mockImplementation(async(url,init)=>{
      if(url==='/api/model-services/audio-assets'&&init?.method==='POST')return pending;
      if(url.startsWith('/api/studio/projects/p2/voices'))return {...overview(),project_id:'p2',groups:[]};
      return original(url,init);
    });
    const user=userEvent.setup(),states:ProjectVoiceState[]=[];
    const view=render(<MemoryRouter><ProjectVoicePreparation projectId="p1" onStateChange={state=>states.push(state)}/></MemoryRouter>);
    await screen.findByRole('article',{name:'角色甲配音设置'});
    await user.upload(screen.getByLabelText('上传本人物参考音频'),new File(['fixture'],'voice.wav',{type:'audio/wav'}));
    await user.type(screen.getByLabelText('来源与授权说明'),'原创声线测试');
    await user.click(screen.getByRole('button',{name:'上传并选入本人物草稿'}));
    await waitFor(()=>expect(states.at(-1)?.busy).toBe(true));
    view.rerender(<MemoryRouter><ProjectVoicePreparation projectId="p2" onStateChange={state=>states.push(state)}/></MemoryRouter>);
    await screen.findByText('先导入 Word，建立人物与分镜后，即可在这里为每个角色上传参考音频。');
    await act(async()=>{complete(asset('late-audio'));await pending;});
    expect(screen.queryByRole('article',{name:'角色甲配音设置'})).not.toBeInTheDocument();
    expect(screen.queryByText(/late-audio/)).not.toBeInTheDocument();expect(puts()).toHaveLength(0);
  });

  it('preserves a selected file and its declaration across a same-project refresh until upload and save finish',async()=>{
    const user=userEvent.setup(),states:ProjectVoiceState[]=[],changed=vi.fn();
    const view=render(<MemoryRouter><ProjectVoicePreparation projectId="p1" refreshKey={1} onStateChange={s=>states.push(s)} onChanged={changed}/></MemoryRouter>);
    await screen.findByRole('article',{name:'角色甲配音设置'});
    await waitFor(()=>expect(states.at(-1)?.loading).toBe(false));
    const file=new File(['same-project audio fixture'],'voice.wav',{type:'audio/wav'});
    await user.upload(screen.getByLabelText('上传本人物参考音频'),file);
    await user.type(screen.getByLabelText('来源与授权说明'),'原创测试声线');
    await waitFor(()=>expect(states.at(-1)?.dirty).toBe(true));
    const reads=mocked.mock.calls.filter(([url])=>url===endpoint).length;
    view.rerender(<MemoryRouter><ProjectVoicePreparation projectId="p1" refreshKey={2} onStateChange={s=>states.push(s)} onChanged={changed}/></MemoryRouter>);
    await screen.findByText(/已保留当前音频草稿和保存操作/);
    expect((screen.getByLabelText('上传本人物参考音频') as HTMLInputElement).files?.[0]).toBe(file);
    expect(screen.getByLabelText('来源与授权说明')).toHaveValue('原创测试声线');
    expect(states.at(-1)).toEqual(expect.objectContaining({ready:false,dirty:true,loading:true,busy:false}));
    expect(mocked.mock.calls.filter(([url])=>url===endpoint)).toHaveLength(reads);
    expect(screen.getByRole('button',{name:'上传并选入本人物草稿'})).toBeEnabled();
    await user.click(screen.getByRole('button',{name:'上传并选入本人物草稿'}));
    await waitFor(()=>expect(screen.getByLabelText('角色甲参考音频')).toHaveValue('audio-uploaded'));
    await user.click(screen.getByRole('button',{name:'保存角色甲配音'}));
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:true,dirty:false,loading:false,busy:false})));
    expect((posts()[0][1]?.body as FormData).get('file')).toBe(file);
    expect(changed).toHaveBeenCalledTimes(1);
    expect(mocked.mock.calls.filter(([url])=>url===endpoint)).toHaveLength(reads+1);
  });

  it('does not unmount a dirty model selector or release production during an in-flight save and refresh',async()=>{
    data={...overview(),ready_to_start:true,blockers:[],groups:[{...group(),audio_id:'audio-existing',ready:true,issues:[]}]};
    let route={scope:'project',target_id:'p1',slot:'dialogue_generation',revision:3,settings:{choices:[{service_id:'cosy',model_id:'cosyvoice3-0.5b-2512'}]}};
    let finish!:(value:typeof route)=>void;
    const pending=new Promise<typeof route>(resolve=>{finish=resolve;}),original=mocked.getMockImplementation()!;
    mocked.mockImplementation(async(url,init)=>{
      if(url==='/api/settings/ai-services')return [{id:'cosy',name:'CosyVoice3',module:'dialogue_generation',enabled:true,provider:'local_runtime',default_model:'cosyvoice3-0.5b-2512',settings:{runtime_family:'cosyvoice3'}},{id:'index',name:'IndexTTS 2.5',module:'dialogue_generation',enabled:true,provider:'local_runtime',default_model:'indextts-2.5',settings:{runtime_family:'indextts25'}}];
      if(url==='/api/model-services/routes/project/p1/dialogue_generation')return init?.method==='PUT'?pending:structuredClone(route);
      return original(url,init);
    });
    const user=userEvent.setup(),states:ProjectVoiceState[]=[],changed=vi.fn();
    const view=render(<MemoryRouter><ProjectVoicePreparation projectId="p1" refreshKey={1} onStateChange={s=>states.push(s)} onChanged={changed}/></MemoryRouter>);
    await waitFor(()=>expect(screen.getByLabelText('本项目配音模型')).toHaveValue('cosy'));
    await user.selectOptions(screen.getByLabelText('本项目配音模型'),'index');
    const voiceReads=mocked.mock.calls.filter(([url])=>url===endpoint).length;
    const routeReads=mocked.mock.calls.filter(([url,init])=>url.includes('/routes/project/')&&!init?.method).length;
    view.rerender(<MemoryRouter><ProjectVoicePreparation projectId="p1" refreshKey={2} onStateChange={s=>states.push(s)} onChanged={changed}/></MemoryRouter>);
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:false,dirty:true,loading:true,busy:false})));
    expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index');
    expect(mocked.mock.calls.filter(([url])=>url===endpoint)).toHaveLength(voiceReads);
    await user.click(screen.getByRole('button',{name:'保存配音模型'}));
    await waitFor(()=>expect(states.at(-1)?.busy).toBe(true));
    view.rerender(<MemoryRouter><ProjectVoicePreparation projectId="p1" refreshKey={3} onStateChange={s=>states.push(s)} onChanged={changed}/></MemoryRouter>);
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:false,dirty:true,loading:true,busy:true})));
    expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index');
    expect(mocked.mock.calls.filter(([url])=>url===endpoint)).toHaveLength(voiceReads);
    expect(mocked.mock.calls.filter(([url,init])=>url.includes('/routes/project/')&&!init?.method)).toHaveLength(routeReads);
    route={...route,revision:4,settings:{choices:[{service_id:'index',model_id:'indextts-2.5'}]}};
    data={...data,services:[{...data.services[0],service_id:'index',name:'IndexTTS 2.5',model_id:'indextts-2.5'}]};
    await act(async()=>{finish(structuredClone(route));await pending;});
    await waitFor(()=>expect(changed).toHaveBeenCalledTimes(1));
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:true,dirty:false,loading:false,busy:false})));
    expect(screen.getByLabelText('本项目配音模型')).toHaveValue('index');
    expect(JSON.parse(String(puts()[0][1]?.body)).expected_revision).toBe(3);
    expect(mocked.mock.calls.filter(([url])=>url===endpoint)).toHaveLength(voiceReads+1);
    expect(posts()).toHaveLength(0);
  });

  it('keeps an in-flight audio save busy across same-project refreshes and accepts its result',async()=>{
    let finish!:(value:ReturnType<typeof overview>)=>void;
    const pending=new Promise<ReturnType<typeof overview>>(resolve=>{finish=resolve;}),original=mocked.getMockImplementation()!;
    mocked.mockImplementation(async(url,init)=>url.startsWith(base+'/person-1?')&&init?.method==='PUT'?pending:original(url,init));
    const user=userEvent.setup(),states:ProjectVoiceState[]=[],changed=vi.fn();
    const view=render(<MemoryRouter><ProjectVoicePreparation projectId="p1" refreshKey={1} onStateChange={s=>states.push(s)} onChanged={changed}/></MemoryRouter>);
    await screen.findByRole('option',{name:/audio-existing/});
    await waitFor(()=>expect(states.at(-1)?.loading).toBe(false));
    await user.selectOptions(screen.getByLabelText('角色甲参考音频'),'audio-existing');
    await user.click(screen.getByRole('button',{name:'保存角色甲配音'}));
    await waitFor(()=>expect(states.at(-1)?.busy).toBe(true));
    const reads=mocked.mock.calls.filter(([url])=>url===endpoint).length;
    view.rerender(<MemoryRouter><ProjectVoicePreparation projectId="p1" refreshKey={2} onStateChange={s=>states.push(s)} onChanged={changed}/></MemoryRouter>);
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:false,dirty:true,loading:true,busy:true})));
    expect(screen.getByRole('button',{name:'正在保存…'})).toBeDisabled();
    expect(screen.getByLabelText('角色甲参考音频')).toHaveValue('audio-existing');
    expect(mocked.mock.calls.filter(([url])=>url===endpoint)).toHaveLength(reads);
    data={...overview(),ready_to_start:true,blockers:[],groups:[{...group(),audio_id:'audio-existing',ready:true,issues:[]}]};
    await act(async()=>{finish(structuredClone(data));await pending;});
    await waitFor(()=>expect(changed).toHaveBeenCalledTimes(1));
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:true,dirty:false,loading:false,busy:false})));
    expect(screen.getByLabelText('角色甲参考音频')).toHaveValue('audio-existing');
    expect(mocked.mock.calls.filter(([url])=>url===endpoint)).toHaveLength(reads+1);
    expect(puts()).toHaveLength(1);expect(posts()).toHaveLength(0);
  });

  it('fails closed on unreadable configuration and passes cloud authorizations only to the read-only preflight',async()=>{
    mocked.mockRejectedValue(new Error('读取服务失败'));
    const states:ProjectVoiceState[]=[];
    render(<MemoryRouter><ProjectVoicePreparation projectId="p1" allowCloudCosts allowPaidFallback onStateChange={state=>states.push(state)}/></MemoryRouter>);
    expect(await screen.findByRole('alert')).toHaveTextContent('读取服务失败');
    await waitFor(()=>expect(states.at(-1)).toEqual(expect.objectContaining({ready:false,loading:false})));
    expect(mocked.mock.calls[0][0]).toBe(base+'?allow_cloud_costs=true&allow_paid_fallback=true');
    expect(puts()).toHaveLength(0);expect(posts()).toHaveLength(0);
  });

  it('preserves the old per-shot collapsed picker by default',async()=>{
    render(<AudioReferencePicker projectId="p1" versionId="version-1" name="角色甲" value="" onChange={vi.fn()}/>);
    const summary=screen.getByText('角色甲 配音参考音频');
    expect(summary.closest('details')).not.toHaveAttribute('open');
    expect(await screen.findByLabelText('上传本人物参考音频')).toBeInTheDocument();
  });

  it('keeps unsaved choices when navigation is cancelled, then allows navigation after saving',async()=>{
    const user=userEvent.setup(),confirm=vi.spyOn(window,'confirm').mockReturnValue(false);
    render(<MemoryRouter><ProjectVoicePreparation projectId="p1"/><LocationProbe/></MemoryRouter>);
    await screen.findByRole('article',{name:'角色甲配音设置'});
    await screen.findByRole('option',{name:/audio-existing/});
    await user.selectOptions(screen.getByLabelText('角色甲参考音频'),'audio-existing');
    const unloading=new Event('beforeunload',{cancelable:true});window.dispatchEvent(unloading);
    expect(unloading.defaultPrevented).toBe(true);
    await user.click(screen.getByRole('link',{name:'配置角色配音服务'}));
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('当前测试路径')).toHaveTextContent('/');
    expect(screen.getByText('已保留未保存的配音草稿，请保存或放弃后再离开。')).toBeInTheDocument();
    expect(screen.getByLabelText('角色甲参考音频')).toHaveValue('audio-existing');
    await user.click(screen.getByRole('button',{name:'保存角色甲配音'}));
    await screen.findByText(/配音参考已保存/);
    const cleanUnload=new Event('beforeunload',{cancelable:true});window.dispatchEvent(cleanUnload);
    expect(cleanUnload.defaultPrevented).toBe(false);
    await user.click(screen.getByRole('link',{name:'配置角色配音服务'}));
    expect(screen.getByLabelText('当前测试路径')).toHaveTextContent('/settings');
    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it('blocks sidebar navigation while uploading, but permits modified clicks and hash links',async()=>{
    const user=userEvent.setup(),confirm=vi.spyOn(window,'confirm').mockReturnValue(false);
    let finish!:(value:ReturnType<typeof asset>)=>void;
    const pending=new Promise<ReturnType<typeof asset>>(resolve=>{finish=resolve;});
    const original=mocked.getMockImplementation()!;
    mocked.mockImplementation(async(url,init)=>url==='/api/model-services/audio-assets'&&init?.method==='POST'?pending:original(url,init));
    render(<MemoryRouter><Link to="/characters">侧栏人物库</Link><ProjectVoicePreparation projectId="p1"/><LocationProbe/></MemoryRouter>);
    await screen.findByRole('article',{name:'角色甲配音设置'});
    await user.upload(screen.getByLabelText('上传本人物参考音频'),new File(['fixture'],'voice.wav',{type:'audio/wav'}));
    await user.type(screen.getByLabelText('来源与授权说明'),'原创测试音频');
    await user.click(screen.getByRole('button',{name:'上传并选入本人物草稿'}));
    await screen.findByRole('button',{name:'实际校验并上传…'});
    await user.click(screen.getByRole('link',{name:'侧栏人物库'}));
    expect(screen.getByText('音频正在上传或保存，请完成后再离开当前页面。')).toBeInTheDocument();
    expect(screen.getByLabelText('当前测试路径')).toHaveTextContent('/');
    expect(confirm).not.toHaveBeenCalled();
    // Native listeners run after document capture and prevent actual jsdom
    // navigation. Their receiving the click proves our guard allowed it through.
    const outside=document.createElement('a');outside.href='/other-page';document.body.append(outside);
    const passed=vi.fn((event:MouseEvent)=>event.preventDefault());outside.addEventListener('click',passed);
    fireEvent.click(outside,{ctrlKey:true});fireEvent.click(outside,{metaKey:true});fireEvent.click(outside,{button:1});
    outside.href=window.location.pathname+window.location.search+'#voice';fireEvent.click(outside);
    expect(passed).toHaveBeenCalledTimes(4);outside.remove();
    await act(async()=>{finish(asset('uploaded'));await pending;});
  });

  it('does not call the parent refresh when a save response arrives after unmounting',async()=>{
    const user=userEvent.setup(),changed=vi.fn();
    let finish!:(value:ReturnType<typeof overview>)=>void;
    const pending=new Promise<ReturnType<typeof overview>>(resolve=>{finish=resolve;});
    const original=mocked.getMockImplementation()!;
    mocked.mockImplementation(async(url,init)=>init?.method==='PUT'?pending:original(url,init));
    const view=render(<MemoryRouter><ProjectVoicePreparation projectId="p1" onChanged={changed}/></MemoryRouter>);
    await screen.findByRole('option',{name:/audio-existing/});
    await user.selectOptions(screen.getByLabelText('角色甲参考音频'),'audio-existing');
    await user.click(screen.getByRole('button',{name:'保存角色甲配音'}));
    await screen.findByRole('button',{name:'正在保存…'});
    view.unmount();
    await act(async()=>{finish({...overview(),ready_to_start:true,blockers:[]});await pending;});
    expect(changed).not.toHaveBeenCalled();
    const cleanUnload=new Event('beforeunload',{cancelable:true});window.dispatchEvent(cleanUnload);
    expect(cleanUnload.defaultPrevented).toBe(false);
  });
});
