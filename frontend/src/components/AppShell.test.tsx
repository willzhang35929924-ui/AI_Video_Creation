import { render, screen, within } from "@testing-library/react";
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { AppShell } from "./AppShell";

describe("AppShell provider truth labels", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows a prominent Demo disclaimer when the backend is in Demo mode", async () => {
    vi.spyOn(api, "getProviderHealth").mockResolvedValue({
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
    vi.spyOn(api, "getProviderHealth").mockResolvedValue({
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
    vi.spyOn(api, 'getProviderHealth').mockResolvedValue({ok:true,connected:true,configured:true,provider:'comfyui_h3',mode:'real'});
    const user=userEvent.setup();
    render(<MemoryRouter><AppShell><p>测试内容</p></AppShell></MemoryRouter>);
    const primary=within(screen.getByRole('navigation',{name:'主导航'}));
    expect(primary.getAllByRole('link').map(link=>link.textContent)).toEqual(['项目工作台','剧本生成','审片与成片','系统设置']);
    await user.click(screen.getByText('辅助与高级工具'));
    expect(within(screen.getByRole('navigation',{name:'辅助与高级工具'})).getByRole('link',{name:'人物素材库'})).toHaveAttribute('href','/characters');
  });
});
