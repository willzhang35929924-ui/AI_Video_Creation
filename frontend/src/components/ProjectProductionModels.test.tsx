import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {MemoryRouter} from 'react-router-dom';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ProjectProductionModels} from './ProjectProductionModels';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
afterEach(()=>{cleanup();vi.mocked(request).mockReset();});
it('reads project routes lazily and leaves verified cloud image options available without saving them for video',async()=>{
  const slots=['character_image','keyframe_image','video_generation'],candidate={service_id:'cloud-image',model_id:'verified-v1',name:'云端已验证图像模型',eligible:true,is_cloud:true,reasons:[]};const mocked=vi.mocked(request),saved=vi.fn();
  mocked.mockImplementation(async(url,init)=>{if(url==='/api/model-services')return {slots:slots.map(slot=>({slot,candidates:[candidate,{...candidate,service_id:'untested',name:'未验证模型',eligible:false}]}))};if(url.includes('/ai-services'))return [];const slot=url.split('/').at(-1);return {scope:'project',target_id:'p1',slot,revision:init?.method==='PUT'?4:3,inherited_from:init?.method==='PUT'?null:'system',settings:{mode:'local_only',choices:[],parameters:{retain:'existing'},max_retries:2}};});
  const user=userEvent.setup();render(<MemoryRouter><ProjectProductionModels projectId="p1" disabled={false} onSaved={saved}/></MemoryRouter>);expect(mocked).not.toHaveBeenCalled();await user.click(screen.getByText('本项目图像与视频模型（继承或项目级覆盖）'));const select=await screen.findByLabelText('分镜关键画面');expect(screen.getAllByRole('option',{name:/未验证模型/}).every(option=>(option as HTMLOptionElement).disabled)).toBe(true);await user.selectOptions(select,JSON.stringify(['cloud-image','verified-v1']));await user.click(screen.getByRole('button',{name:'保存分镜关键画面模型'}));await waitFor(()=>expect(saved).toHaveBeenCalledOnce());
  const sent=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;expect(sent[0]).toBe('/api/model-services/routes/project/p1/keyframe_image');expect(JSON.parse(String(sent[1]?.body))).toMatchObject({expected_revision:3,settings:{mode:'cloud_only',choices:[{service_id:'cloud-image',model_id:'verified-v1'}],parameters:{retain:'existing'},max_retries:2,allow_fallback:false,allow_paid_fallback:false}});expect(mocked.mock.calls.some(([url,init])=>url.includes('/system/')&&init?.method)).toBe(false);
});
