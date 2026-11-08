const statusLabels: Record<string, string> = {
  draft: "草稿",
  approved: "已通过",
  rejected: "已驳回",
  needs_review: "需重新审核",
  pending: "待处理",
  pending_review: "待审核",
  queued: "排队中",
  running: "生成中",
  postprocessing: "修订后处理",
  succeeded: "已完成",
  generated: "已生成 · 待审核",
  reviewing: "审核中",
  revised: "已创建修订",
  failed: "失败",
  cancelled: "已取消",
  active: "启用",
  inactive: "停用",
  configured: "已配置",
  not_configured: "未配置",
  authorized: "已授权",
  unauthorized: "未授权",
  expired: "已过期",
  validated: "已验证",
  dry_run_completed: "预演完成",
  not_available: "不可用",
};

export function statusLabel(status?: string | null) {
  if (!status) return "未知";
  return statusLabels[status] ?? status.replaceAll("_", " ");
}

export function statusTone(status?: string | null) {
  if (!status) return "neutral";
  if (["approved", "succeeded", "active", "authorized", "validated", "dry_run_completed", "healthy"].includes(status)) return "success";
  if (["failed", "rejected", "expired", "error"].includes(status)) return "danger";
  if (["queued", "running", "pending", "pending_review", "needs_review"].includes(status)) return "warning";
  return "neutral";
}

export function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDuration(seconds?: number | null) {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds % 1 ? 1 : 0)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}:${remainder.toString().padStart(2, "0")}`;
}

export function formatBytes(bytes?: number | null) {
  if (bytes == null || Number.isNaN(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function projectSource(project: { sourceType?: string | null; source_type?: string | null }) {
  return project.sourceType ?? project.source_type ?? "未导入";
}

export function isoLocalInput(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}
