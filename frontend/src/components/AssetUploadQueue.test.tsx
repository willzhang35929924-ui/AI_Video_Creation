// UI contracts only; these fixtures are not media generation evidence.
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {AssetUploadQueue} from './AssetUploadQueue';
afterEach(cleanup);

it('keeps per-file product ownership and only marks server-confirmed uploads saved, then retries the failed file',async()=>{
  const user=userEvent.setup(),completed=vi.fn();let failed=false;
  const upload=vi.fn(async(file:File,_target:string,progress:(value:number)=>void)=>{progress(100);if(file.name==='second.png'&&!failed){failed=true;throw new Error('实际解码失败');}return {id:file.name};});
  render(<AssetUploadQueue label="批量产品图片" accept="image/png" targets={[{id:'p1',name:'产品一'},{id:'p2',name:'产品二'}]} targetId="p1" upload={upload} onCompleted={completed}/>);
  const files=[new File(['first'],'first.png',{type:'image/png'}),new File(['second'],'second.png',{type:'image/png'})];
  await user.upload(screen.getByLabelText('批量产品图片'),files);await user.selectOptions(screen.getByLabelText('second.png归属'),'p2');await user.click(screen.getByRole('button',{name:'开始上传队列'}));
  expect(await screen.findByText('Error: 实际解码失败')).toBeInTheDocument();
  expect(screen.getAllByText('已保存')).toHaveLength(1);expect(upload.mock.calls.map(call=>[call[0].name,call[1]])).toEqual([['first.png','p1'],['second.png','p2']]);
  await user.click(screen.getByRole('button',{name:'重试失败文件'}));await waitFor(()=>expect(screen.getAllByText('已保存')).toHaveLength(2));
  expect(upload.mock.calls.map(call=>call[0].name)).toEqual(['first.png','second.png','second.png']);expect(completed).toHaveBeenCalledTimes(2);
});

it('requires an explicit destination before uploading files without a chosen product',async()=>{
  const user=userEvent.setup(),upload=vi.fn();render(<AssetUploadQueue label="选择产品图片" accept="image/png" targets={[{id:'p1',name:'产品一'}]} upload={upload}/>);
  await user.upload(screen.getByLabelText('选择产品图片'),new File(['a'],'a.png',{type:'image/png'}));await user.click(screen.getByRole('button',{name:'开始上传队列'}));
  expect(screen.getByRole('status')).toHaveTextContent('请先为队列中的每个文件选择归属');expect(upload).not.toHaveBeenCalled();
});
