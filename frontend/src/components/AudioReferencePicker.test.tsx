// UI ownership guards only. Actual audio decoding is verified by backend tests.
import {render,screen} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {AudioReferencePicker} from './AudioReferencePicker';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
afterEach(()=>mocked.mockReset());
it('does not bind an uploaded reference returned for a different character version',async()=>{
  const user=userEvent.setup(),change=vi.fn();
  mocked.mockImplementation(async (_url,init)=>init?.method==='POST'?{id:'audio-wrong',project_id:'p',character_version_id:'other',relative_path:'x.wav',sha256:'0'.repeat(64),metadata:{human_approved:false},media_url:'/media/x.wav'}:[]);
  render(<AudioReferencePicker projectId="p" versionId="v" name="甲" value="" onChange={change}/>);
  await user.click(screen.getByText('甲 配音参考音频'));
  await user.upload(screen.getByLabelText('上传本人物参考音频'),new File(['RIFF'],'voice.wav',{type:'audio/wav'}));
  await user.type(screen.getByLabelText('来源与授权说明'),'本项目原创合成人声，仅技术测试');
  await user.click(screen.getByRole('button',{name:'上传并选入本人物草稿'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('返回音频不属于当前人物版本');
  expect(change).not.toHaveBeenCalled();
});
