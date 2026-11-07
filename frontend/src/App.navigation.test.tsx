import { render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import App from './App';

vi.mock('./components/AppShell', () => ({ AppShell: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('./pages/StudioPage', () => ({ StudioPage: () => <h1>项目工作台内容</h1> }));
vi.mock('./pages/StoryboardPage', () => ({ StoryboardPage: () => <h1>脚本与分镜内容</h1> }));
vi.mock('./pages/ReviewPage', () => ({ ReviewPage: () => <h1>成片审核内容</h1> }));
vi.mock('./pages/AssemblyPage', () => ({ AssemblyPage: () => <h1>合成成片内容</h1> }));

function LocationProbe() { const location = useLocation(); return <output aria-label="当前地址">{location.pathname}{location.search}{location.hash}</output>; }
function mount(url: string) { render(<MemoryRouter initialEntries={[url]}><App/><LocationProbe/></MemoryRouter>); }

describe('independent production navigation compatibility', () => {
  it.each([
    ['/studio-review', '/review', '成片审核内容'],
    ['/video-production', '/storyboard', '脚本与分镜内容'],
    ['/production', '/storyboard', '脚本与分镜内容'],
    ['/generation', '/storyboard', '脚本与分镜内容'],
  ])('redirects retired %s to %s while retaining project and media selections', async (oldPath, target, heading) => {
    mount(`${oldPath}?project=p1&shot=s2&job=j3&video=v4&quality_render=q5&slot=video_generation&workspace=review#details`);
    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument();
    const url = new URL(screen.getByLabelText('当前地址').textContent!, 'http://localhost');
    expect(url.pathname).toBe(target);
    expect(Object.fromEntries(url.searchParams)).toEqual({ project:'p1', shot:'s2', job:'j3', video:'v4', quality_render:'q5', slot:'video_generation' });
    expect(url.hash).toBe('#details');
    expect(screen.queryByRole('navigation', { name:'项目工作台区域' })).not.toBeInTheDocument();
  });
  it.each([
    ['/', '项目工作台内容'], ['/storyboard', '脚本与分镜内容'], ['/review', '成片审核内容'], ['/final', '合成成片内容'],
  ])('opens %s directly as an independent page', (path, heading) => {
    mount(`${path}?project=p1`);
    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name:'项目工作台区域' })).not.toBeInTheDocument();
  });
});
