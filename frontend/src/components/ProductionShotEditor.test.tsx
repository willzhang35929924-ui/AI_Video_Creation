// Contract fixture only; no real shot or reference policy is changed.
import {render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ProductionShotEditor} from './ProductionShotEditor';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
afterEach(()=>mocked.mockReset());
it('persists an explicitly selected independent-face policy only when saving this shot and preserves other requested controls',async()=>{
  const user=userEvent.setup();mocked.mockResolvedValue({});
  const item={shot:{id:'s1',order:1,scene_title:'人物交接',project_id:'p1',duration_seconds:5},scene_id:'scene1',preflight:{ready:true,counts:{blocker:0,review:0},findings:[],generation_inputs_hash:'frozen-hash'},production:{start_state:'站稳',requested_controls:{camera_note:'保留原构图'},wardrobe_states:{},prop_states:[],beats:[]},cast:[]};
  render(<ProductionShotEditor item={item} designs={[]} reload={async()=>{}} onDirty={vi.fn()}/>);
  await user.click(screen.getByText('1 · 人物交接 · 输入已就绪'));
  await user.click(screen.getByRole('button',{name:'编辑连续性与动作节拍'}));
  expect(screen.getByLabelText('本镜人物身份参考策略')).toHaveValue('full_reference');
  await user.selectOptions(screen.getByLabelText('本镜人物身份参考策略'),'independent_faces_scene');
  expect(mocked.mock.calls).toHaveLength(0);
  expect(screen.getByText(/不能区域锁脸/)).toBeInTheDocument();
  await user.click(screen.getByRole('button',{name:'保存本镜制作状态'}));
  await waitFor(()=>expect(mocked).toHaveBeenCalledTimes(1));
  expect(mocked.mock.calls[0][0]).toBe('/api/shots/s1/production');
  expect(JSON.parse(String(mocked.mock.calls[0][1]?.body))).toEqual(expect.objectContaining({expected_input_hash:'frozen-hash',requested_controls:{camera_note:'保留原构图',identity_reference_policy:'independent_faces_scene'}}));
});

it('requires explicit permission and saves the chosen motion video and exact trim in the shot payload',async()=>{
  const user=userEvent.setup();
  mocked.mockResolvedValueOnce({items:[{id:'v1',name:'动作样片',state:'active',revision:3,sha256:'a'.repeat(64),metadata:{duration_seconds:6,fps:24}}]});
  mocked.mockResolvedValueOnce({});
  const item={shot:{id:'s1',order:1,scene_title:'动作镜头',project_id:'p1',duration_seconds:5},scene_id:'scene1',preflight:{ready:true,counts:{blocker:0,review:0},findings:[],generation_inputs_hash:'hash'},production:{wardrobe_states:{},prop_states:[],beats:[],requested_controls:{}},cast:[]};
  render(<ProductionShotEditor item={item} designs={[]} reload={async()=>{}} onDirty={vi.fn()}/>);
  await user.click(screen.getByText('1 · 动作镜头 · 输入已就绪'));
  await user.click(screen.getByRole('button',{name:'编辑连续性与动作节拍'}));
  await user.click(screen.getByRole('button',{name:'浏览参考视频'}));
  await user.selectOptions(await screen.findByLabelText('选择动作参考视频'),'v1');
  await user.click(screen.getByRole('button',{name:'添加本镜参考'}));
  expect(screen.getByText(/不使用原音轨/)).toBeInTheDocument();
  expect(screen.getByText(/预估实际参考 107 帧/)).toBeInTheDocument();
  expect(screen.getByText(/短于所选范围/)).toBeInTheDocument();
  await user.click(screen.getByRole('button',{name:'保存本镜制作状态'}));
  expect(screen.getByRole('alert')).toHaveTextContent('请逐条确认');
  expect(mocked).toHaveBeenCalledTimes(1);
  await user.click(screen.getByLabelText('我确认有权将此视频用于本镜的动作参考'));
  await user.clear(screen.getByLabelText('参考 1 结束秒'));
  await user.type(screen.getByLabelText('参考 1 结束秒'),'1');
  await user.click(screen.getByRole('button',{name:'保存本镜制作状态'}));
  expect(screen.getByRole('alert')).toHaveTextContent('2–15 秒');
  expect(mocked).toHaveBeenCalledTimes(1);
  await user.clear(screen.getByLabelText('参考 1 结束秒'));
  await user.type(screen.getByLabelText('参考 1 结束秒'),'5');
  await user.click(screen.getByRole('button',{name:'保存本镜制作状态'}));
  await waitFor(()=>expect(mocked).toHaveBeenCalledTimes(2));
  expect(JSON.parse(String(mocked.mock.calls[1][1]?.body)).motion_references).toEqual([{
    video_id:'v1',expected_revision:3,source_sha256:'a'.repeat(64),trim_start_seconds:0,trim_end_seconds:5,authorized_for_motion:true,
  }]);
});

it('rejects a stale selected video rather than saving a changed reference',async()=>{
  const user=userEvent.setup();
  mocked.mockResolvedValueOnce({items:[{id:'v1',name:'更新后',state:'active',revision:4,sha256:'b'.repeat(64),metadata:{duration_seconds:6}}]});
  const item={shot:{id:'s1',order:1,scene_title:'已保存镜头',project_id:'p1',duration_seconds:5},scene_id:'scene1',preflight:{ready:true,counts:{blocker:0,review:0},findings:[],generation_inputs_hash:'hash'},production:{wardrobe_states:{},prop_states:[],beats:[],requested_controls:{},motion_references:[{video_id:'v1',expected_revision:3,source_sha256:'a'.repeat(64),trim_start_seconds:0,trim_end_seconds:5,authorized_for_motion:true}]},cast:[]};
  render(<ProductionShotEditor item={item} designs={[]} reload={async()=>{}} onDirty={vi.fn()}/>);
  await user.click(screen.getByText('1 · 已保存镜头 · 输入已就绪'));
  await user.click(screen.getByRole('button',{name:'编辑连续性与动作节拍'}));
  await user.click(screen.getByRole('button',{name:'浏览参考视频'}));
  expect(await screen.findByText(/已变化或不可用/)).toBeInTheDocument();
  await user.click(screen.getByRole('button',{name:'保存本镜制作状态'}));
  expect(screen.getByRole('alert')).toHaveTextContent('参考视频已变化');
  expect(mocked).toHaveBeenCalledTimes(1);
});
