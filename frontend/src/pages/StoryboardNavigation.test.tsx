import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { api, ApiError } from '../lib/api';
import type { StoryboardShot } from '../lib/types';
import { ToastProvider } from '../components/ToastProvider';
import { StoryboardPage } from './StoryboardPage';

vi.mock('../components/ShotWorkspace', () => ({ ShotWorkspace: ({shotId}:{shotId:string}) => <p>当前结构化镜头 {shotId}</p> }));

it.each([['s2','第二镜'],['another-project-shot','第一镜']])('continues legacy shot %s using the loaded project list',async(requested,title)=>{
  vi.spyOn(api,'listProjects').mockResolvedValue([{id:'p1',name:'项目'}] as any);
  vi.spyOn(api,'getScript').mockRejectedValue(new ApiError('尚无剧本记录',404));
  const shots=['第一镜','第二镜'].map((sceneTitle,index)=>({id:`s${index+1}`,projectId:'p1',order:index+1,sceneTitle,status:'draft',revision:1,
    aspectRatio:'16:9',durationSeconds:5,visualPrompt:'保留镜头内容',narration:'',dialogue:'',negativePrompt:'',action:'',cameraMove:'',location:'',lighting:'',audioDirection:''} as StoryboardShot));
  const list=vi.spyOn(api,'listShots').mockResolvedValue(shots);
  vi.spyOn(api,'listCharacters').mockResolvedValue([]);
  render(<MemoryRouter initialEntries={[`/storyboard?project=p1&shot=${requested}`]}><ToastProvider><StoryboardPage/></ToastProvider></MemoryRouter>);
  expect(await screen.findByLabelText('场景标题')).toHaveValue(title);
  expect(list).toHaveBeenCalledWith('p1',undefined,false);
  expect(screen.getByText(`当前结构化镜头 ${title==='第二镜'?'s2':'s1'}`)).toBeInTheDocument();
});
