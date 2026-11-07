import { render, screen, within } from "@testing-library/react";
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { AppShell } from "./AppShell";

function LocationProbe() { const location = useLocation(); return <output aria-label="当前地址">{location.pathname}{location.search}{location.hash}</output>; }

describe("AppShell provider truth labels", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("shows a prominent Demo disclaimer when the backend is in Demo mode", async () => {
    vi.spyOn(api, "getContextProviderHealth").mockResolvedValue({
      ok: true,
      connected: true,
      configured: true,
      provider: "demo",
      mode: "demo_status_replay" as "demo",
      workflowName: "demo-status-replay-v1",
      referenceToVideoAvailable: false,
    });

    render(
      <MemoryRouter>
        <AppShell>
          <p>测试内容</p>
        </AppShell>
      </MemoryRouter>,
    );

    expect(await screen.findByText("DEMO 模式")).toBeInTheDocument();
    expect(screen.getByText("生成产物会带 DEMO 标记；当前结果不代表真实 MiniMax H3 输出。")).toBeInTheDocument();
  });

  it("labels a verified connected provider as real without showing the Demo banner", async () => {
    vi.spyOn(api, "getContextProviderHealth").mockResolvedValue({
      ok: true,
      connected: true,
      configured: true,
      provider: "comfyui_h3",
      mode: "real",
      referenceToVideoAvailable: true,
    });

    render(
      <MemoryRouter>
        <AppShell>
          <p>测试内容</p>
        </AppShell>
      </MemoryRouter>,
    );

    expect(await screen.findByText("ComfyUI 已连接")).toBeInTheDocument();
    expect(screen.queryByText("DEMO 模拟流程")).not.toBeInTheDocument();
  });

  it('adds screenplay generation to the compact primary navigation while preserving advanced tools', async () => {
    vi.spyOn(api, 'getContextProviderHealth').mockResolvedValue({ok:true,connected:true,configured:true,provider:'comfyui_h3',mode:'real'});
    const user=userEvent.setup();
    render(<MemoryRouter><AppShell><p>测试内容</p></AppShell></MemoryRouter>);
    const primary=within(screen.getByRole('navigation',{name:'主导航'}));
    expect(primary.getAllByRole('link').map(link=>link.textContent)).toEqual(['项目工作台','剧本生成','脚本与分镜','成片审核','合成成片','系统设置']);
    expect(primary.getByRole('link',{name:'脚本与分镜'})).toHaveAttribute('href','/storyboard');
    expect(primary.getByRole('link',{name:'成片审核'})).toHaveAttribute('href','/review');
    expect(primary.getByRole('link',{name:'合成成片'})).toHaveAttribute('href','/final');
    await user.click(screen.getByText('辅助与高级工具'));
    expect(within(screen.getByRole('navigation',{name:'辅助与高级工具'})).getByRole('link',{name:'人物素材库'})).toHaveAttribute('href','/characters');
    expect(within(screen.getByRole('navigation',{name:'辅助与高级工具'})).queryByRole('link',{name:'脚本与分镜'})).not.toBeInTheDocument();
    expect(within(screen.getByRole('navigation',{name:'辅助与高级工具'})).queryByRole('link',{name:'成片审核'})).not.toBeInTheDocument();
    expect(within(screen.getByRole('navigation',{name:'辅助与高级工具'})).queryByRole('link',{name:'合成成片'})).not.toBeInTheDocument();
    expect(screen.queryByRole('link',{name:'审片与成片'})).not.toBeInTheDocument();
    expect(screen.queryByRole('link',{name:'视频制作'})).not.toBeInTheDocument();
  });

  it('shows a live queue when compiler health is temporarily withheld during work',async()=>{
    vi.spyOn(api,'getContextProviderHealth').mockResolvedValue({ok:false,connected:true,ready:false,busy:true,mode:'real'});
    render(<MemoryRouter><AppShell><p>测试内容</p></AppShell></MemoryRouter>);
    expect(await screen.findByText('本地 H3 队列处理中')).toBeInTheDocument();
    expect(screen.queryByText('本地后端不可达')).not.toBeInTheDocument();
  });

  it.each([['项目工作台','/'],['脚本与分镜','/storyboard'],['成片审核','/review'],['合成成片','/final']])('keeps the older current project when opening %s without carrying another module’s selection',async(label,path)=>{
    vi.spyOn(api,'getContextProviderHealth').mockResolvedValue({ok:true,connected:true,configured:true,provider:'comfyui_h3',mode:'real'});
    const user=userEvent.setup();
    render(<MemoryRouter initialEntries={['/storyboard?project=prj_older&shot=old-shot&video=old-video&job=old-job&slot=video_generation&workspace=review#old-details']}><AppShell><LocationProbe/></AppShell></MemoryRouter>);
    const primary=within(screen.getByRole('navigation',{name:'主导航'}));
    expect(primary.getByRole('link',{name:label})).toHaveAttribute('href',`${path}?project=prj_older`);
    expect(primary.getByRole('link',{name:'剧本生成'})).toHaveAttribute('href','/screenplay-generation');
    expect(primary.getByRole('link',{name:'系统设置'})).toHaveAttribute('href','/settings');
    expect(within(screen.getByRole('navigation',{name:'辅助与高级工具'})).getByRole('link',{name:'人物素材库'})).toHaveAttribute('href','/characters');
    await user.click(primary.getByRole('link',{name:label}));
    expect(screen.getByLabelText('当前地址')).toHaveTextContent(`${path}?project=prj_older`);
    expect(screen.getByLabelText('当前地址').textContent).toBe(`${path}?project=prj_older`);
  });

  it('does not create a project scope from an empty project query',async()=>{
    vi.spyOn(api,'getContextProviderHealth').mockResolvedValue({ok:true,connected:true,configured:true,provider:'comfyui_h3',mode:'real'});
    render(<MemoryRouter initialEntries={['/?project=%20%20&shot=old-shot']}><AppShell><p>内容</p></AppShell></MemoryRouter>);
    const primary=within(screen.getByRole('navigation',{name:'主导航'}));
    expect(primary.getByRole('link',{name:'项目工作台'})).toHaveAttribute('href','/');
    expect(primary.getByRole('link',{name:'成片审核'})).toHaveAttribute('href','/review');
    expect(await screen.findByText('ComfyUI 已连接')).toBeInTheDocument();
  });

  it('distinguishes an offline model from an offline backend',async()=>{
    vi.spyOn(api,'getContextProviderHealth').mockResolvedValue({ok:false,connected:false,ready:false,busy:false,mode:'real'});
    const health=vi.spyOn(api,'getHealth');
    render(<MemoryRouter><AppShell><p>测试内容</p></AppShell></MemoryRouter>);
    expect(await screen.findByText('本地 H3 未连接')).toBeInTheDocument();
    expect(screen.queryByText('本地后端不可达')).not.toBeInTheDocument();
    expect(health).not.toHaveBeenCalled();
  });

  it.each([true,false])('checks API health independently if the model status request fails (%s)',async(backendReachable)=>{
    vi.spyOn(api,'getContextProviderHealth').mockRejectedValue(new Error('模型状态查询失败'));
    const health=vi.spyOn(api,'getHealth');
    if(backendReachable)health.mockResolvedValue({ok:true});
    else health.mockRejectedValue(new Error('后端连接失败'));
    render(<MemoryRouter><AppShell><p>测试内容</p></AppShell></MemoryRouter>);
    if(backendReachable){
      expect(await screen.findByText('本地模型状态待确认')).toBeInTheDocument();
      expect(screen.queryByText('本地后端不可达')).not.toBeInTheDocument();
    }else{
      expect(await screen.findByText('本地后端不可达')).toBeInTheDocument();
    }
    expect(health).toHaveBeenCalledOnce();
  });
});
