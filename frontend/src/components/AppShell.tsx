import {
  BookOpenText,
  Boxes,
  Clapperboard,
  Gauge,
  Menu,
  PanelLeftClose,
  PlaySquare,
  Send,
  Settings,
  Users,
  X,
  ChevronDown,
  Images,
  Mic,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { api } from "../lib/api";
import type { ProviderHealth, ProviderMode } from "../lib/types";
import { Badge } from "./ui";

const navigation = [
  { to: "/", label: "项目工作台", icon: Gauge, end: true },
  { to: "/screenplay-generation", label: "剧本生成", icon: BookOpenText },
  { to: "/studio-review", label: "审片与成片", icon: PlaySquare },
  { to: "/settings", label: "系统设置", icon: Settings },
];
const advancedNavigation = [
  { to: "/dashboard", label: "运行概览", icon: Gauge },
  { to: "/storyboard", label: "脚本与分镜", icon: BookOpenText },
  { to: "/video-production", label: "视频制作", icon: Clapperboard },
  { to: "/characters", label: "人物素材库", icon: Users },
  { to: "/assets/products", label: "产品图库", icon: Images },
  { to: "/assets/videos", label: "参考视频库", icon: Clapperboard },
  { to: "/assets/voices", label: "配音库", icon: Mic },
  { to: "/review", label: "成片审核", icon: PlaySquare },
  { to: "/final", label: "合成成片", icon: Clapperboard },
  { to: "/publishing", label: "批量发布", icon: Send },
  { to: "/content", label: "内容管理", icon: Boxes },
];

function resolveMode(health?: ProviderHealth | null): ProviderMode {
  if (!health) return "unknown";
  if (health.connected === false) return "unknown";
  if (health.mode === "demo" || health.provider?.toLowerCase().includes("demo")) return "demo";
  if (health.mode === "real") return "real";
  if (health.connected && health.configured !== false) return "real";
  return "unknown";
}

export function AppShell({ children }: { children: ReactNode }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [health, setHealth] = useState<ProviderHealth | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const location = useLocation();

  const refreshHealth = useCallback(async () => {
    try {
      const result = await api.getProviderHealth();
      setHealth(result);
      setHealthError(false);
    } catch {
      setHealth(null);
      setHealthError(true);
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
    const timer = window.setInterval(refreshHealth, 30_000);
    return () => window.clearInterval(timer);
  }, [refreshHealth]);

  useEffect(() => setMenuOpen(false), [location.pathname]);
  useEffect(() => {
    if (advancedNavigation.some(item => location.pathname.startsWith(item.to))) setAdvancedOpen(true);
  }, [location.pathname]);

  const mode = useMemo(() => resolveMode(health), [health]);
  const modeLabel = !health && !healthError ? "正在检测 ComfyUI" : mode === "demo" ? "DEMO 模式" : mode === "real" ? "ComfyUI 已连接" : "H3 disconnected";

  return (
    <div className={`app-shell${collapsed ? " sidebar-collapsed" : ""}`}>
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <button className="mobile-menu-button" type="button" onClick={() => setMenuOpen(true)} aria-label="打开导航">
        <Menu size={20} />
      </button>
      <aside className={`sidebar${menuOpen ? " sidebar-open" : ""}`}>
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">
            <Clapperboard size={21} />
          </div>
          <div className="brand-copy">
            <strong>AI视频生产</strong>
            <span>AI Video Studio</span>
          </div>
          <button className="icon-button mobile-close" type="button" onClick={() => setMenuOpen(false)} aria-label="关闭导航">
            <X size={19} />
          </button>
        </div>

        <nav className="sidebar-nav" aria-label="主导航">
          {navigation.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
                title={collapsed ? item.label : undefined}
              >
                <Icon size={19} aria-hidden="true" />
                <span>{item.label}</span>
              </NavLink>
            );
          })}
        </nav>

        <details className="sidebar-advanced" open={advancedOpen} onToggle={e => setAdvancedOpen(e.currentTarget.open)}>
          <summary title={collapsed ? '辅助与高级工具' : undefined}><ChevronDown size={17} aria-hidden="true"/><span>辅助与高级工具</span></summary>
          <nav className="sidebar-nav" aria-label="辅助与高级工具">
            {advancedNavigation.map(item => {
              const Icon = item.icon;
              return <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`} title={collapsed ? item.label : undefined}><Icon size={17} aria-hidden="true"/><span>{item.label}</span></NavLink>;
            })}
          </nav>
        </details>

        <div className="sidebar-spacer" />
        <button
          className="sidebar-collapse"
          type="button"
          onClick={() => setCollapsed((value) => !value)}
          aria-label={collapsed ? "展开侧边栏" : "收起侧边栏"}
        >
          <PanelLeftClose size={18} />
          <span>{collapsed ? "展开" : "收起导航"}</span>
        </button>
        <div className="local-only-note">
          <Boxes size={16} aria-hidden="true" />
          <div>
            <strong>本地优先</strong>
            <span>数据保存在本机工作区</span>
          </div>
        </div>
      </aside>
      {menuOpen ? <button className="sidebar-scrim" type="button" onClick={() => setMenuOpen(false)} aria-label="关闭导航" /> : null}

      <div className="app-content">
        <header className="topbar">
          <div className="topbar-spacer" />
          <button className="provider-control" type="button" onClick={refreshHealth} aria-label="刷新本地模型连接状态" title="本地 ComfyUI 状态；云端服务连接在系统设置中检测">
            <span className={`live-dot ${mode}${healthError ? " error" : ""}`} aria-hidden="true" />
            <Badge tone={mode === "real" ? "success" : mode === "demo" ? "demo" : "neutral"}>{modeLabel}</Badge>
          </button>
          <div className="local-avatar" aria-label="当前操作人 local-user">
            LU
          </div>
        </header>
        {mode === "demo" ? (
          <div className="mode-banner mode-banner-demo" role="status">
            <strong>DEMO 模拟流程</strong>
            <span>生成产物会带 DEMO 标记；当前结果不代表真实 MiniMax H3 输出。</span>
          </div>
        ) : null}
        {mode === "unknown" && healthError ? (
          <div className="mode-banner mode-banner-offline" role="alert">
            <strong>本地后端不可达</strong>
            <span>页面不会伪造数据。启动后端后点击右上角状态重新检测。</span>
          </div>
        ) : null}
        <main id="main-content" className="main-content" tabIndex={-1}>
          {children}
        </main>
      </div>
    </div>
  );
}
