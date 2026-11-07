// UI contract tests only. Real model processing is validated separately in an isolated project.
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import {AudioDurationResolution,type AudioDurationContext,type AudioDurationPreview,type AudioDurationResult} from './AudioDurationResolution';

vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request),path='/api/studio/projects/p1/audio-duration/audio_mix%3As1';
const context=():AudioDurationContext=>({project_id:'p1',run_id:'run1',node_id:'audio_mix:s1',source_fingerprint:'frozen-inputs-1',shot:{id:'s1',title:'展示盲盒',order:1,revision:3},video:{id:'v1',duration_seconds:3.041688,sha256:'original-video'},audio_duration_seconds:5.18966,dialogues:[{id:'line1',text:'Mommy! Look what Santa left in a mini dim sum basket!',original_text:'Mommy! Look what Santa left in a mini dim sum basket!',audio_id:'a1',duration_seconds:5.18966,speed:1,language:'ZH',runtime_family:'indextts25'}],capabilities:{extend_video:{available:true,recommended_duration_seconds:6},shorten_dialogue:{available:true},adjust_speed:{available:true,min:.5,max:2,languages:['ZH','EN','JA'],recommended_speed:1.8}}});
const preview=(action:AudioDurationPreview['action']='extend_video'):AudioDurationPreview=>({preview_token:'checked-preview',action,summary:'保留原片，重新处理本镜',affected_nodes:['video:s1','audio_mix:s1','quality:s1'],warnings:['新输出需重新审核'],parameters:{}});
const result=():AudioDurationResult=>({run_id:'run1',node_id:'audio_mix:s1',status:'running',affected_nodes:['video:s1','audio_mix:s1','quality:s1'],replayed:false,notice:'已提交本镜重新处理'});
function fixture(){mocked.mockImplementation(async(url,init)=>url.endsWith('/preview')?preview(JSON.parse(String(init?.body)).action):url.endsWith('/apply')?result():context());}
afterEach(()=>{cleanup();mocked.mockReset();});
const calls=(ending:string)=>mocked.mock.calls.filter(([url])=>url.endsWith(ending));
const body=(ending:string,index=0)=>JSON.parse(String(calls(ending)[index][1]?.body));

it('shows measured durations, original/effective dialogue and three explicit choices without dispatch',async()=>{
  fixture();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await screen.findByRole('button',{name:'延长镜头'});
  expect(screen.getByRole('region',{name:'音频超长处理'})).toHaveTextContent('实际原片 3.042 秒，配音 5.190 秒，超出 2.148 秒');
  expect(screen.getByText('本轮实际配音台词')).toBeInTheDocument();expect(screen.getByText(/实际语言 ZH · 实际语速 1/)).toBeInTheDocument();
  for(const name of ['延长镜头','缩短台词','调整语速'])expect(screen.getByRole('button',{name})).toHaveAttribute('aria-pressed','false');
  expect(screen.queryByRole('button',{name:'确认并开始重新处理'})).toBeNull();expect(calls('/preview')).toHaveLength(0);expect(calls('/apply')).toHaveLength(0);
});

it('previews extension with frozen source CAS, then applies exactly that preview after explicit confirmation',async()=>{
  fixture();const applied=vi.fn(),user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1" onApplied={applied}/>);
  await user.click(await screen.findByRole('button',{name:'延长镜头'}));
  const seconds=screen.getByLabelText('新的镜头时长（秒）');expect(seconds).toHaveValue(6);await user.clear(seconds);await user.type(seconds,'7');
  await user.click(screen.getByRole('button',{name:'预览处理影响'}));
  await screen.findByRole('region',{name:'待确认的音频处理预览'});
  expect(body('/preview')).toMatchObject({expected_run_id:'run1',source_fingerprint:'frozen-inputs-1',action:'extend_video',target_duration_seconds:7,client_request_id:expect.any(String)});
  expect(calls('/apply')).toHaveLength(0);await user.click(screen.getByRole('button',{name:'确认并开始重新处理'}));
  await waitFor(()=>expect(applied).toHaveBeenCalledWith(result()));
  expect(body('/apply')).toEqual({...body('/preview'),preview_token:'checked-preview'});expect(screen.getByText(/真实编排状态：running/)).toHaveTextContent('run1');
});

it('shortens only selected effective speech while showing the frozen original separately',async()=>{
  const data=context();data.dialogues[0].text='Santa left a basket!';
  mocked.mockImplementation(async(url,init)=>url.endsWith('/preview')?preview(JSON.parse(String(init?.body)).action):url.endsWith('/apply')?result():data);
  const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'缩短台词'}));
  const input=screen.getByLabelText('修改后的精简台词');expect(input).toHaveValue('Santa left a basket!');
  expect(screen.getByText(/原剧本台词/).parentElement).toHaveTextContent('Mommy! Look what Santa left');
  await user.clear(input);await user.type(input,'Look! A basket!');await user.selectOptions(screen.getByLabelText('重新配音的语言'),'EN');await user.click(screen.getByRole('button',{name:'预览处理影响'}));
  await screen.findByRole('button',{name:'确认并开始重新处理'});
  expect(body('/preview')).toMatchObject({action:'shorten_dialogue',dialogue_line_id:'line1',text:'Look! A basket!',language:'EN'});
  expect(body('/preview')).not.toHaveProperty('target_duration_seconds');
  expect(screen.getByText(/只覆盖本轮所选台词的配音/)).toHaveTextContent('不改原 Word 或剧本');
  await user.click(screen.getByRole('button',{name:'确认并开始重新处理'}));await screen.findByText('已提交本镜重新处理');
});

it('does not silently change ZH on English speech and submits the explicitly selected EN and speed',async()=>{
  fixture();const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'调整语速'}));
  const language=screen.getByLabelText('重新配音的语言');expect(language).toHaveValue('ZH');expect(screen.getByText(/原台词包含英文，原配音实际语言是 ZH/)).toBeInTheDocument();
  await user.selectOptions(language,'EN');expect(screen.getByLabelText('新的配音语速')).toHaveValue(1.8);
  await user.click(screen.getByRole('button',{name:'预览处理影响'}));await screen.findByRole('button',{name:'确认并开始重新处理'});
  expect(body('/preview')).toMatchObject({action:'adjust_speed',dialogue_line_id:'line1',speed:1.8,language:'EN'});
  await user.click(screen.getByRole('button',{name:'确认并开始重新处理'}));await screen.findByText('已提交本镜重新处理');
});

it('can adjust speed on adapters without a separate language parameter',async()=>{
  const data=context();data.dialogues[0].runtime_family='cosyvoice';data.capabilities.adjust_speed.languages=[];
  mocked.mockImplementation(async(url,init)=>url.endsWith('/preview')?preview(JSON.parse(String(init?.body)).action):url.endsWith('/apply')?result():data);
  const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'调整语速'}));expect(screen.queryByLabelText('重新配音的语言')).toBeNull();
  await user.click(screen.getByRole('button',{name:'预览处理影响'}));await screen.findByRole('button',{name:'确认并开始重新处理'});
  expect(body('/preview')).toMatchObject({action:'adjust_speed',speed:1.8});expect(body('/preview')).not.toHaveProperty('language');
});

it('uses each selected dialogue adapter and resets ranges, language and suggested speed without leaking previous edits',async()=>{
  const data=context();data.dialogues[0].capabilities={adjust_speed:{available:true,min:.5,max:2,languages:['ZH','EN'],recommended_speed:1.7}};
  data.dialogues.push({...data.dialogues[0],id:'line2',text:'Another speaker',language:'EN',speed:1.1,runtime_family:'cosyvoice',capabilities:{adjust_speed:{available:true,min:.25,max:4,languages:[],recommended_speed:2.4}}});
  mocked.mockImplementation(async(url,init)=>url.endsWith('/preview')?preview(JSON.parse(String(init?.body)).action):data);
  const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'调整语速'}));const select=screen.getByLabelText('需要重新处理的台词');
  await user.selectOptions(select,'line1');expect(screen.getByLabelText('新的配音语速')).toHaveValue(1.7);expect(screen.getByLabelText('新的配音语速')).toHaveAttribute('max','2');
  await user.selectOptions(screen.getByLabelText('重新配音的语言'),'EN');await user.clear(screen.getByLabelText('新的配音语速'));await user.type(screen.getByLabelText('新的配音语速'),'1.6');
  await user.click(screen.getByRole('button',{name:'预览处理影响'}));await screen.findByRole('button',{name:'确认并开始重新处理'});
  await user.selectOptions(select,'line2');expect(screen.queryByRole('button',{name:'确认并开始重新处理'})).toBeNull();
  expect(screen.getByLabelText('新的配音语速')).toHaveValue(2.4);expect(screen.getByLabelText('新的配音语速')).toHaveAttribute('min','0.25');expect(screen.getByLabelText('新的配音语速')).toHaveAttribute('max','4');expect(screen.queryByLabelText('重新配音的语言')).toBeNull();
  await user.click(screen.getByRole('button',{name:'预览处理影响'}));await screen.findByRole('button',{name:'确认并开始重新处理'});
  expect(body('/preview',1)).toMatchObject({dialogue_line_id:'line2',speed:2.4});expect(body('/preview',1)).not.toHaveProperty('language');
  await user.selectOptions(select,'line1');expect(screen.getByLabelText('新的配音语速')).toHaveValue(1.7);expect(screen.getByLabelText('重新配音的语言')).toHaveValue('ZH');
  await user.click(screen.getByRole('button',{name:'缩短台词'}));await user.selectOptions(select,'line2');expect(screen.getByLabelText('修改后的精简台词')).toHaveValue('Another speaker');expect(screen.queryByLabelText('重新配音的语言')).toBeNull();
});

it('can select a supported speaker even when the first adapter cannot change speed and blocks an unsupported selected speaker',async()=>{
  const data=context();data.capabilities.adjust_speed={available:false,reason:'第一句服务不支持语速'};
  data.dialogues[0].capabilities={adjust_speed:{available:false,reason:'第一句服务不支持语速'}};
  data.dialogues.push({...data.dialogues[0],id:'line2',text:'Supported speaker',speed:1.2,language:'EN',capabilities:{adjust_speed:{available:true,min:1,max:3,languages:['EN'],recommended_speed:2}}});
  mocked.mockResolvedValue(data);const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  const action=await screen.findByRole('button',{name:'调整语速'});expect(action).toBeEnabled();await user.click(action);
  const select=screen.getByLabelText('需要重新处理的台词');await user.selectOptions(select,'line1');expect(action).toBeDisabled();expect(screen.getByRole('button',{name:'预览处理影响'})).toBeDisabled();expect(screen.getByLabelText('新的配音语速')).toBeDisabled();
  expect(screen.getByText(/调整语速不可用/)).toHaveTextContent('第一句服务不支持语速');
  await user.selectOptions(select,'line2');expect(action).toBeEnabled();expect(screen.getByRole('button',{name:'预览处理影响'})).toBeEnabled();expect(screen.getByLabelText('新的配音语速')).toHaveValue(2);expect(screen.getByLabelText('重新配音的语言')).toHaveValue('EN');
});

it('rejects unchanged text before any preview and removes approval whenever the chosen parameters change',async()=>{
  fixture();const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'缩短台词'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('请填写修改后的精简台词');expect(calls('/preview')).toHaveLength(0);
  await user.click(screen.getByRole('button',{name:'延长镜头'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));await screen.findByRole('button',{name:'确认并开始重新处理'});
  fireEvent.change(screen.getByLabelText('新的镜头时长（秒）'),{target:{value:'8'}});
  expect(screen.queryByRole('button',{name:'确认并开始重新处理'})).toBeNull();expect(calls('/apply')).toHaveLength(0);
});

it('requires explicit line selection when multiple dialogue clips exist and explains unsupported choices',async()=>{
  const data=context();data.dialogues.push({...data.dialogues[0],id:'line2',text:'Second line'});data.capabilities.extend_video={available:false,reason:'当前服务不能生成所需时长'};
  mocked.mockResolvedValue(data);const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  expect(await screen.findByRole('button',{name:'延长镜头'})).toBeDisabled();expect(screen.getByText(/延长镜头不可用/)).toHaveTextContent('当前服务不能生成所需时长');
  await user.click(screen.getByRole('button',{name:'调整语速'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('请明确选择要重新处理的台词');expect(calls('/preview')).toHaveLength(0);
});

it('blocks duplicate apply clicks while a real submission is pending',async()=>{
  let finish!:(value:AudioDurationResult)=>void;const pending=new Promise<AudioDurationResult>(resolve=>{finish=resolve;}),state=vi.fn();
  mocked.mockImplementation(async(url,init)=>url.endsWith('/preview')?preview(JSON.parse(String(init?.body)).action):url.endsWith('/apply')?pending:context());
  const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1" onBusyChange={state}/>);
  await user.click(await screen.findByRole('button',{name:'延长镜头'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));
  const confirm=await screen.findByRole('button',{name:'确认并开始重新处理'});fireEvent.click(confirm);fireEvent.click(confirm);
  expect(calls('/apply')).toHaveLength(1);expect(confirm).toBeDisabled();expect(screen.getByRole('button',{name:'调整语速'})).toBeDisabled();
  expect(state).toHaveBeenLastCalledWith(true);
  await act(async()=>{finish(result());await pending;});await screen.findByText('已提交本镜重新处理');expect(state).toHaveBeenLastCalledWith(false);
});

it('keeps an uncertain apply locked to the same idempotent request and allows safe receipt retry',async()=>{
  let attempts=0;mocked.mockImplementation(async(url,init)=>{if(url.endsWith('/preview'))return preview(JSON.parse(String(init?.body)).action);if(url.endsWith('/apply')){if(++attempts===1)throw new ApiError('连接中断',0);return {...result(),replayed:true};}return context();});
  const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'延长镜头'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));await user.click(await screen.findByRole('button',{name:'确认并开始重新处理'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('同一请求编号');expect(screen.getByRole('button',{name:'缩短台词'})).toBeDisabled();expect(screen.getByRole('button',{name:'预览处理影响'})).toBeDisabled();
  await user.click(screen.getByRole('button',{name:'确认并开始重新处理'}));await screen.findByText(/已返回原请求结果，未重复创建任务/);
  expect(body('/apply',1)).toEqual(body('/apply',0));
});

it('refreshes source CAS conflicts and requires a new choice without automatically applying',async()=>{
  let reads=0;mocked.mockImplementation(async(url)=>{if(url.endsWith('/preview'))throw new ApiError('输入已变化',409);reads++;return {...context(),source_fingerprint:`frozen-${reads}`};});
  const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'延长镜头'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));
  await waitFor(()=>expect(reads).toBe(2));expect(screen.getByRole('alert')).toHaveTextContent('请重新选择并预览后再确认');
  expect(screen.getByRole('button',{name:'延长镜头'})).toHaveAttribute('aria-pressed','false');expect(calls('/apply')).toHaveLength(0);
});

it('ignores late context responses from a previously selected project or node',async()=>{
  let finish!:(value:AudioDurationContext)=>void;const old=new Promise<AudioDurationContext>(resolve=>{finish=resolve;});
  mocked.mockImplementation(async url=>url===path?old:Promise.reject(new ApiError('新项目未配置配音',422)));
  const {rerender}=render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);rerender(<AudioDurationResolution projectId="p2" nodeId="audio_mix:s2"/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('新项目未配置配音');await act(async()=>{finish(context());await old;});
  expect(screen.queryByRole('button',{name:'延长镜头'})).toBeNull();
});

it('ignores a late preview after changing projects and never exposes its confirmation',async()=>{
  let finish!:(value:AudioDurationPreview)=>void;const old=new Promise<AudioDurationPreview>(resolve=>{finish=resolve;});
  mocked.mockImplementation(async(url)=>url.endsWith('/preview')?old:url===path?context():Promise.reject(new ApiError('另一项目无音频异常',422)));
  const user=userEvent.setup(),{rerender}=render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1"/>);
  await user.click(await screen.findByRole('button',{name:'延长镜头'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));
  rerender(<AudioDurationResolution projectId="p2" nodeId="audio_mix:s2"/>);await screen.findByRole('alert');
  await act(async()=>{finish(preview());await old;});expect(screen.queryByRole('button',{name:'确认并开始重新处理'})).toBeNull();expect(calls('/apply')).toHaveLength(0);
});

it('does not refresh another project when an old apply finishes after switching',async()=>{
  let finish!:(value:AudioDurationResult)=>void;const old=new Promise<AudioDurationResult>(resolve=>{finish=resolve;}),applied=vi.fn();
  mocked.mockImplementation(async(url,init)=>url.endsWith('/preview')?preview(JSON.parse(String(init?.body)).action):url.endsWith('/apply')?old:url===path?context():Promise.reject(new ApiError('另一项目',422)));
  const user=userEvent.setup(),{rerender}=render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1" onApplied={applied}/>);
  await user.click(await screen.findByRole('button',{name:'延长镜头'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));await user.click(await screen.findByRole('button',{name:'确认并开始重新处理'}));
  rerender(<AudioDurationResolution projectId="p2" nodeId="audio_mix:s2" onApplied={applied}/>);await screen.findByRole('alert');
  await act(async()=>{finish(result());await old;});expect(applied).not.toHaveBeenCalled();expect(screen.queryByText('已提交本镜重新处理')).toBeNull();
});

it('reports a refresh failure separately from an accepted mutation and prevents another apply',async()=>{
  fixture();const user=userEvent.setup();render(<AudioDurationResolution projectId="p1" nodeId="audio_mix:s1" onApplied={async()=>{throw new Error('轮询超时');}}/>);
  await user.click(await screen.findByRole('button',{name:'延长镜头'}));await user.click(screen.getByRole('button',{name:'预览处理影响'}));await user.click(await screen.findByRole('button',{name:'确认并开始重新处理'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('处理已接收，但刷新任务状态失败');expect(screen.getByText('已提交本镜重新处理')).toBeInTheDocument();expect(screen.queryByRole('button',{name:'确认并开始重新处理'})).toBeNull();expect(calls('/apply')).toHaveLength(1);
});
