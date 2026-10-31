// UI routing contracts only; no live settings or model requests.
import {cleanup,render,screen,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,beforeEach,expect,it,vi} from 'vitest';
import {ApiError,request} from '../lib/api';
import {ScreenplayServiceSettings} from './ScreenplayServiceSettings';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const services:any[]=[{id:'local',name:'本地剧本',enabled:true,provider:'local_llm',default_model:'qwen',revision:1},{id:'cloud',name:'云端剧本',enabled:true,provider:'openai_compatible',default_model:'vision',revision:1}];
let conflict:boolean;
beforeEach(()=>{conflict=false;mocked.mockImplementation(async(url,init)=>{
  if(url==='/api/screenplay/options')return {services,modes:['text','image','video'].map(input_mode=>({input_mode,candidates:[{service_id:'local',eligible:true}],default_service_id:'local'}))};
  if(url.startsWith('/api/model-services/routes/system/default/screenplay_from_')){
    if(init?.method==='PUT'){
      if(conflict)throw new ApiError('配置修订冲突',409,'route_revision_changed');
      return {scope:'system',target_id:'default',slot:url.split('/').at(-1),revision:3,settings:JSON.parse(String(init.body)).settings};
    }
    return {scope:'system',target_id:'default',slot:url.split('/').at(-1),revision:2,settings:{mode:'local_only',choices:[{service_id:'local',model_id:'qwen'}],parameters:{},allow_fallback:false,allow_paid_fallback:false}};
  }
  throw new Error('Unexpected settings contract '+url);
});});
afterEach(()=>{cleanup();mocked.mockReset();});

it('saves ordered primary and explicit fallback using shared profile IDs without a second key',async()=>{
  const user=userEvent.setup();render(<MemoryRouter><ScreenplayServiceSettings services={services}/></MemoryRouter>);
  const row=await screen.findByRole('group',{name:'文生剧本模型配置'});await within(row).findByRole('option',{name:'本地剧本 · qwen'});
  await user.click(within(row).getByText('失败后的可选回退'));
  await user.click(within(row).getByLabelText('允许回退到另一已登记服务'));
  await user.selectOptions(within(row).getByLabelText('文生剧本回退服务'),'cloud');
  expect(within(row).getByLabelText('允许付费云端回退（生成时还需再次授权）')).not.toBeChecked();
  await user.click(within(row).getByLabelText('允许付费云端回退（生成时还需再次授权）'));
  await user.click(within(row).getByRole('button',{name:'保存文生剧本模型'}));
  await within(row).findByText('默认剧本模型已保存。仅影响后续任务。');
  const put=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
  expect(put[0]).toBe('/api/model-services/routes/system/default/screenplay_from_text');
  expect(JSON.parse(String(put[1]?.body))).toEqual({expected_revision:2,settings:{mode:'local_first',choices:[{service_id:'local',model_id:'qwen'},{service_id:'cloud',model_id:'vision'}],parameters:{},quality_mode:'standard',allow_fallback:true,allow_paid_fallback:true,max_retries:0}});
  expect(String(put[1]?.body)).not.toMatch(/api_key|secret|authorization/);
});

it('keeps a stale draft on 409 and requires an explicit refresh before overwriting',async()=>{
  const user=userEvent.setup();render(<MemoryRouter><ScreenplayServiceSettings services={services}/></MemoryRouter>);const row=await screen.findByRole('group',{name:'图片生剧本模型配置'});await within(row).findByRole('option',{name:'云端剧本 · vision'});
  await user.selectOptions(within(row).getByLabelText('图片生剧本默认模型'),'cloud');conflict=true;
  await user.click(within(row).getByRole('button',{name:'保存图片生剧本模型'}));await within(row).findByRole('alert');
  expect(within(row).getByLabelText('图片生剧本默认模型')).toHaveValue('cloud');expect(within(row).getByRole('button',{name:'保存图片生剧本模型'})).toBeDisabled();
  expect(mocked.mock.calls.filter(([,init])=>init?.method==='PUT')).toHaveLength(1);
});
