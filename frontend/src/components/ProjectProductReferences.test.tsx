// UI/API selection contract. The real product workflow is verified separately.
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ProjectProductReferences} from './ProjectProductReferences';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
afterEach(()=>{cleanup();vi.mocked(request).mockReset();});
it('requires an explicit three-image keyframe selection while retaining five video images and the binding revision',async()=>{
  const images=Array.from({length:5},(_,index)=>({id:`image${index+1}`,url:'/image.png'})),ids=images.map(image=>image.id);let data={project_id:'p1',shot_id:'s1',products:[{product_id:'product1',name:'黑色水杯',revision:2,images,video_image_ids:ids,keyframe_image_ids:ids}],limits:{keyframe_total_max:4,video_total_max:9},note:'人物与场景也占用预算'};
  const mocked=vi.mocked(request),states:unknown[]=[];mocked.mockImplementation(async(_url,init)=>{if(init?.method==='PUT'){const body=JSON.parse(String(init.body));data={...data,products:[{...data.products[0],revision:3,...body.products[0]}]};}return structuredClone(data);});
  const user=userEvent.setup();render(<ProjectProductReferences projectId="p1" shots={[{id:'s1',order:1,title:'展示杯身'}]} disabled={false} onStateChange={state=>states.push(state)}/>);expect(mocked).not.toHaveBeenCalled();await user.click(screen.getByText('产品图的制作引用（逐镜明确选择）'));expect(await screen.findByRole('alert')).toHaveTextContent('超过模型预算');expect(states.at(-1)).toEqual({busy:false,dirty:false,overBudget:true});await user.click(screen.getByLabelText('黑色水杯图 4 用于关键画面'));await user.click(screen.getByLabelText('黑色水杯图 5 用于关键画面'));await user.click(screen.getByRole('button',{name:'保存此镜头的产品参考选择'}));
  await waitFor(()=>expect(screen.getByRole('status')).toHaveTextContent('产品参考已保存'));const sent=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;expect(sent[0]).toBe('/api/product-library/projects/p1/shots/s1/references');expect(JSON.parse(String(sent[1]?.body))).toEqual({products:[{product_id:'product1',expected_revision:2,keyframe_image_ids:['image1','image2','image3'],video_image_ids:ids}]});expect(states.at(-1)).toEqual({busy:false,dirty:false,overBudget:false});
});
