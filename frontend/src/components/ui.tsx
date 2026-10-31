import { AlertCircle, Inbox, LoaderCircle, RefreshCw } from "lucide-react";
import { Children, cloneElement, isValidElement, type ReactNode } from "react";
import { statusLabel, statusTone } from "../lib/format";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  const resource = ({'/storyboard':'shots','/characters':'characters','/generation':'video_jobs','/video-production':'video_jobs','/review':'videos','/final':'assemblies','/publishing':'accounts','/settings':'services'} as Record<string,string>)[window.location.pathname];
  return (
    <header className="page-header">
      <div>
        {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
        <h1>{title}</h1>
        {description ? <p className="page-description">{description}</p> : null}
      </div>
      {actions || resource ? <div className="page-actions">{actions}{resource ? <a className="button button-ghost button-small" href={'/content?resource='+resource}>编辑与批量删除</a> : null}</div> : null}
    </header>
  );
}

export function Badge({ status, children, tone }: { status?: string | null; children?: ReactNode; tone?: string }) {
  const resolvedTone = tone ?? statusTone(status);
  return <span className={`badge badge-${resolvedTone}`}>{children ?? statusLabel(status)}</span>;
}

export function LoadingState({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="state-panel" role="status">
      <LoaderCircle className="spin" size={24} aria-hidden="true" />
      <p>{label}</p>
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-panel state-error" role="alert">
      <AlertCircle size={24} aria-hidden="true" />
      <div>
        <strong>暂时无法加载</strong>
        <p>{message}</p>
      </div>
      {onRetry ? (
        <button className="button button-secondary button-small" type="button" onClick={onRetry}>
          <RefreshCw size={15} />
          重试
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon" aria-hidden="true">
        <Inbox size={22} />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function SkeletonRows({ count = 3 }: { count?: number }) {
  return (
    <div className="skeleton-stack" role="status" aria-label="正在加载">
      {Array.from({ length: count }, (_, index) => (
        <div className="skeleton-row" key={index} />
      ))}
    </div>
  );
}

export function FormField({
  label,
  hint,
  error,
  required,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <label className={`form-field${error ? " has-error" : ""}`}>
      <span className="form-label">
        {label}
        {required ? <span aria-hidden="true"> *</span> : null}
      </span>
      {Children.map(children, child => isValidElement<{ 'aria-label'?: string }>(child) && typeof child.type === 'string' && ['input','select','textarea'].includes(child.type) ? cloneElement(child, { 'aria-label': child.props['aria-label'] || label }) : child)}
      {error ? <span className="form-error">{error}</span> : hint ? <span className="form-hint">{hint}</span> : null}
    </label>
  );
}

export function ProgressBar({ value = 0, label }: { value?: number | null; label?: string }) {
  const safe = Math.max(0, Math.min(100, value ?? 0));
  return (
    <div className="progress-wrap">
      <div className="progress-label">
        <span>{label ?? "进度"}</span>
        <span>{Math.round(safe)}%</span>
      </div>
      <div className="progress-track" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={safe}>
        <span style={{ width: `${safe}%` }} />
      </div>
    </div>
  );
}

export function VisuallyHidden({ children }: { children: ReactNode }) {
  return <span className="sr-only">{children}</span>;
}
