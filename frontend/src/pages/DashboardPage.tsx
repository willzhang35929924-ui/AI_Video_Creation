import {
  ArrowRight,
  BookOpenCheck,
  CirclePlus,
  Clock3,
  FileInput,
  Film,
  FolderKanban,
  LoaderCircle,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { formatDate, statusLabel } from "../lib/format";
import type { DashboardSummary } from "../lib/types";
import { useToast } from "../components/ToastProvider";
import { Badge, ErrorState, PageHeader, SkeletonRows } from "../components/ui";
import { LiveSystem } from "../components/LiveSystem";

const emptySummary: DashboardSummary = {
  projectCount: 0,
  pendingShotCount: 0,
  runningJobCount: 0,
  pendingVideoReviewCount: 0,
  recentProjects: [],
};

export function DashboardPage() {
  const [summary, setSummary] = useState<DashboardSummary>(emptySummary);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [name, setName] = useState("我的项目");
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();
  const { notify } = useToast();

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSummary(await api.getDashboard());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取工作台数据");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function createProject(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    notify("已进入任务向导；导入内容后才会持久化项目", "info");
    setDialogOpen(false);
    navigate(`/storyboard?import=1&name=${encodeURIComponent(trimmed)}`);
  }

  const stats = [
    { label: "项目", value: summary.projectCount, icon: FolderKanban, tone: "blue" },
    { label: "待审核分镜", value: summary.pendingShotCount, icon: BookOpenCheck, tone: "amber" },
    { label: "生成任务运行中", value: summary.runningJobCount, icon: Sparkles, tone: "violet" },
    { label: "待成片审核", value: summary.pendingVideoReviewCount, icon: Film, tone: "green" },
  ];

  return (
    <>
      <PageHeader
        eyebrow="制作概览"
        title="让同一个人物，走进下一幕。"
        description="从脚本到候选成片，在一个本地工作流里保持人物与版本清晰可追溯。"
        actions={
          <>
            <Link className="button button-secondary" to="/storyboard?import=1">
              <FileInput size={17} />
              导入脚本
            </Link>
            <button className="button button-primary" type="button" onClick={() => setDialogOpen(true)}>
              <CirclePlus size={17} />
              新建制作任务
            </button>
          </>
        }
      />

      {error ? <ErrorState message={error} onRetry={load} /> : null}
      <LiveSystem />

      <section className="stat-grid" aria-label="项目状态统计">
        {stats.map((stat) => {
          const Icon = stat.icon;
          return (
            <article className="stat-card" key={stat.label}>
              <span className={`stat-icon stat-icon-${stat.tone}`} aria-hidden="true">
                <Icon size={20} />
              </span>
              <div>
                <strong>{loading ? "—" : stat.value.toLocaleString("zh-CN")}</strong>
                <span>{stat.label}</span>
              </div>
            </article>
          );
        })}
      </section>

      <section className="dashboard-grid">
        <article className="card recent-card">
          <div className="section-heading">
            <div>
              <p className="eyebrow">最近更新</p>
              <h2>制作项目</h2>
            </div>
            <Link className="text-link" to="/storyboard">
              查看全部 <ArrowRight size={15} />
            </Link>
          </div>
          {loading ? (
            <SkeletonRows count={4} />
          ) : summary.recentProjects.length ? (
            <div className="project-list">
              {summary.recentProjects.map((project) => (
                <Link className="project-row" key={project.id} to={`/storyboard?project=${encodeURIComponent(project.id)}`}>
                  <span className="project-thumb" aria-hidden="true">
                    <Film size={20} />
                  </span>
                  <span className="project-row-main">
                    <strong>{project.name}</strong>
                    <span>
                      {project.shotCount ?? 0} 个分镜 · {project.stage ? statusLabel(project.stage) : statusLabel(project.status)}
                    </span>
                  </span>
                  <span className="project-row-meta">
                    <Badge status={project.status} />
                    <time dateTime={project.updatedAt}>{formatDate(project.updatedAt)}</time>
                  </span>
                  <ArrowRight size={17} aria-hidden="true" />
                </Link>
              ))}
            </div>
          ) : (
            <div className="dashboard-empty">
              <span className="empty-illustration" aria-hidden="true">
                <Film size={30} />
              </span>
              <h3>从第一支短片开始</h3>
              <p>创建任务并导入脚本，工作流会引导你完成分镜审核、人物绑定和生成。</p>
              <button className="button button-primary" type="button" onClick={() => setDialogOpen(true)}>
                新建制作任务
              </button>
            </div>
          )}
        </article>

        <aside className="card workflow-card">
          <div className="section-heading">
            <div>
              <p className="eyebrow">工作流</p>
              <h2>下一步建议</h2>
            </div>
          </div>
          <ol className="workflow-list">
            {[
              ["01", "导入并解析脚本", "保留原文，生成可编辑分镜"],
              ["02", "审核人物与分镜", "锁定同一角色版本与参考图"],
              ["03", "生成与人工复核", "每个版本都可追溯、可驳回"],
              ["04", "发布清单预演", "只做 dry-run，不触发真实发布"],
            ].map(([step, title, copy], index) => (
              <li key={step}>
                <span className="workflow-index">{step}</span>
                <div>
                  <strong>{title}</strong>
                  <p>{copy}</p>
                </div>
                {index === 0 ? <span className="current-step">开始</span> : null}
              </li>
            ))}
          </ol>
          <div className="local-tip">
            <Clock3 size={17} aria-hidden="true" />
            <p>长任务会在后台运行。刷新页面后，任务状态仍从本地数据库恢复。</p>
          </div>
        </aside>
      </section>

      {dialogOpen ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => !creating && setDialogOpen(false)}>
          <form className="dialog-card task-dialog" onSubmit={createProject} onMouseDown={(event) => event.stopPropagation()}>
            <p className="eyebrow">新建制作任务</p>
            <h2>给项目起个名字</h2>
            <p>创建后会直接进入脚本导入向导，不会留下无法继续的空记录。</p>
            <label className="form-field">
              <span className="form-label">项目名称 *</span>
              <input
                autoFocus
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
                maxLength={120}
                aria-describedby="project-name-hint"
              />
              <span id="project-name-hint" className="form-hint">
                建议使用片名或本次制作目标，稍后仍可修改。
              </span>
            </label>
            <div className="dialog-actions">
              <button className="button button-secondary" type="button" onClick={() => setDialogOpen(false)} disabled={creating}>
                取消
              </button>
              <button className="button button-primary" type="submit" disabled={creating || !name.trim()}>
                {creating ? <LoaderCircle className="spin" size={17} /> : <CirclePlus size={17} />}
                {creating ? "正在创建…" : "创建并导入脚本"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </>
  );
}
