import { QualityDurationWarning } from '../components/QualitySettings';
import {
  Check,
  CircleAlert,
  Film,
  ImageOff,
  LoaderCircle,
  PlayCircle,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { api, type VideoRevisionInput, type VideoRevisionPreview } from "../lib/api";
import { formatBytes, formatDate, formatDuration, statusLabel } from "../lib/format";
import type { Project, QualityCheck, VideoRevision } from "../lib/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ProjectPicker } from "../components/ProjectPicker";
import { useToast } from "../components/ToastProvider";
import { Badge, EmptyState, ErrorState, FormField, LoadingState, PageHeader } from "../components/ui";

const defaultInstruction = "保持当前绑定人物的脸型、发型、固定特征和服装不变；镜头运动更缓慢，保持动作连续，避免手部穿模。";

export function ReviewPage() {
  const location = useLocation();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [videos, setVideos] = useState<VideoRevision[]>([]);
  const [revisionSelection, setRevisionSelection] = useState({ selectedId: "", instruction: defaultInstruction, useProjectVideoService: false });
  const { selectedId, instruction, useProjectVideoService } = revisionSelection;
  // Select the version and initialize its draft atomically. A passive effect
  // must never restore defaults after the newly visible editor has been edited.
  const setSelectedId = useCallback((value: string | ((current: string) => string)) => {
    setRevisionSelection(current => {
      const next = typeof value === "function" ? value(current.selectedId) : value;
      return next === current.selectedId ? current : { selectedId: next, instruction: defaultInstruction, useProjectVideoService: false };
    });
  }, []);
  const setInstruction = (value: string) => setRevisionSelection(current => current.selectedId === selectedId ? { ...current, instruction: value } : current);
  const setUseProjectVideoService = (value: boolean) => setRevisionSelection(current => current.selectedId === selectedId ? { ...current, useProjectVideoService: value } : current);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [approveOpen, setApproveOpen] = useState(false);
  const [approvalOperator, setApprovalOperator] = useState("local-user");
  const [approvalReason, setApprovalReason] = useState("");
  const [mediaFailed, setMediaFailed] = useState(false);
  const [revisionPreview, setRevisionPreview] = useState<{ videoId: string; input: VideoRevisionInput; result: VideoRevisionPreview } | null>(null);
  const previewRequest = useRef<AbortController | null>(null);
  const [revisionJobId, setRevisionJobId] = useState("");
  const { notify } = useToast();

  const selected = videos.find((video) => video.id === selectedId) ?? null;
  const reviewedRevision = revisionPreview?.videoId === selectedId ? revisionPreview : null;
  const clearRevisionPreview = useCallback(() => {
    previewRequest.current?.abort();
    setRevisionPreview(null);
  }, []);
  useEffect(() => () => previewRequest.current?.abort(), []);

  const loadVideos = useCallback(async (id: string) => {
    if (!id) {
      setVideos([]);
      return;
    }
    setLoading(true);
    try {
      const result = await api.listVideos(id);
      const ordered = result.sort((a, b) => new Date(b.createdAt ?? 0).getTime() - new Date(a.createdAt ?? 0).getTime());
      setVideos(ordered);
      const requestedJob = new URLSearchParams(location.search).get("job");
      const requestedVideo = ordered.find(video => video.generationJobId === requestedJob);
      setSelectedId((current) => requestedVideo?.id ?? (ordered.some((video) => video.id === current) ? current : ordered[0]?.id ?? ""));
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "成片列表加载失败");
    } finally {
      setLoading(false);
    }
  }, [location.search, setSelectedId]);

  const initialLoad = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const nextProjects = await api.listProjects();
      setProjects(nextProjects);
      const requested = new URLSearchParams(location.search).get("project");
      const target = nextProjects.find((project) => project.id === requested)?.id ?? nextProjects[0]?.id ?? "";
      setProjectId(target);
      if (target) await loadVideos(target);
      else setLoading(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法加载成片审核");
      setLoading(false);
    }
  }, [loadVideos, location.search]);

  useEffect(() => {
    void initialLoad();
  }, [initialLoad]);

  useEffect(() => {
    clearRevisionPreview();
    setRejectOpen(false);
    setRejectReason("");
    setMediaFailed(false);
  }, [clearRevisionPreview, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    void api
      .getVideo(selectedId)
      .then((detail) => {
        if (active) setVideos((current) => current.map((video) => (video.id === detail.id ? detail : video)));
      })
      .catch((cause) => {
        if (active) notify(cause instanceof Error ? cause.message : "视频详情加载失败", "error");
      });
    return () => {
      active = false;
    };
  }, [notify, selectedId]);

  const shotVersions = useMemo(
    () =>
      selected
        ? videos.filter((video) => video.shotId === selected.shotId).sort((a, b) => b.revision - a.revision)
        : [],
    [selected, videos],
  );

  async function runQualityCheck() {
    if (!selected) return;
    setBusy("quality");
    try {
      await api.runQualityCheck(selected.id);
      const refreshed = await api.getVideo(selected.id);
      setVideos((current) => current.map((video) => (video.id === refreshed.id ? refreshed : video)));
      notify("文件级质检已完成；未配置的检测项会如实显示不可用", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "质检失败", "error");
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    if (!revisionJobId) return;
    const timer = window.setInterval(async () => { const job = await api.getJob(revisionJobId); if (!["queued", "running"].includes(job.status)) { setRevisionJobId(""); await loadVideos(projectId); if (job.status === "failed") notify(job.errorMessage || "真实修订生成失败", "error"); } }, 2500);
    return () => window.clearInterval(timer);
  }, [revisionJobId, projectId, loadVideos, notify]);

  async function revise(confirmed = false) {
    if (!selected || !instruction.trim() || (confirmed && !reviewedRevision)) return;
    const input = confirmed && reviewedRevision ? reviewedRevision.input : {
      instruction: instruction.trim(), use_project_video_service: useProjectVideoService,
    };
    const controller = confirmed ? undefined : new AbortController();
    if (controller) previewRequest.current = controller;
    setBusy("revise");
    try {
      if (controller) {
        const result = await api.previewVideoRevision(selected.id, input, controller.signal);
        if (!controller.signal.aborted) setRevisionPreview({ videoId: selected.id, input, result });
        return;
      }
      const result = await api.reviseVideo(selected.id, input);
      clearRevisionPreview();
      if (result.job) setRevisionJobId(result.job.id);
      await loadVideos(projectId);
      notify("修订生成已提交；旧视频仍保留在版本历史中", "success");
    } catch (cause) {
      if (!controller?.signal.aborted) notify(cause instanceof Error ? cause.message : "提交修订失败", "error");
    } finally {
      if (controller && previewRequest.current === controller) previewRequest.current = null;
      setBusy("");
    }
  }

  async function approve() {
    if (!selected) return;
    setBusy("approve");
    try {
      const updated = await api.approveVideo(selected.id, { operator: approvalOperator.trim() || "local-user", reason: approvalReason.trim() || undefined });
      setVideos((current) => current.map((video) => (video.id === updated.id ? updated : video)));
      setApproveOpen(false);
      notify("人工审核已通过，此版本已锁为候选成片", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "审核通过失败", "error");
    } finally {
      setBusy("");
    }
  }

  async function reject() {
    if (!selected || !rejectReason.trim()) return;
    setBusy("reject");
    try {
      const updated = await api.rejectVideo(selected.id, rejectReason.trim());
      setVideos((current) => current.map((video) => (video.id === updated.id ? updated : video)));
      setRejectOpen(false);
      notify("已记录驳回原因，可继续填写修改指令并重做", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "驳回失败", "error");
    } finally {
      setBusy("");
    }
  }

  const metadata = selected?.metadata ?? selected?.metadata_json ?? {};
  const provider = String(metadata.provider || "").toLowerCase();
  const service = metadata.service_snapshot as { provider?: string; is_cloud?: boolean } | undefined;
  const cloudSource = metadata.mode === "cloud" || provider.startsWith("cloud_") || service?.provider === "video_api" || service?.is_cloud === true;
  const showLocalRecovery = cloudSource || !(metadata.mode === "local" || metadata.mode === "demo" || provider === "comfyui_h3" || provider === "demo");
  const productionParams = new URLSearchParams({ project: selected?.projectId || projectId });
  if (selected?.shotId) productionParams.set("shot", selected.shotId);
  const qualityChecks = selected?.qualityChecks ?? selected?.quality_checks ?? [];
  const mediaSrc = selected ? api.mediaUrl(selected.mediaUrl ?? selected.media_url ?? selected.relativePath ?? selected.relative_path, selected.id) : "";

  return (
    <>
      <PageHeader
        eyebrow="创作流程 · 04"
        title="成片审核"
        description="自动检查只提供证据；只有人工通过的版本才会成为候选成片。"
        actions={
          <ProjectPicker
            projects={projects}
            value={projectId}
            onChange={(id) => {
              setProjectId(id);
              void loadVideos(id);
            }}
          />
        }
      />

      {error ? <ErrorState message={error} onRetry={initialLoad} /> : null}
      {loading ? <LoadingState label="正在读取成片版本…" /> : null}

      {!loading && !videos.length ? (
        <EmptyState title="还没有待审核视频" description="生成任务成功后，每个视频版本都会在这里保留并独立审核。" />
      ) : null}

      {!loading && videos.length ? (
        <section className="review-layout">
          <aside className="card review-list" aria-label="待审核视频列表">
            <div className="section-heading compact">
              <div><p className="eyebrow">项目成片</p><h2>{videos.length} 个版本</h2></div>
              <button className="icon-button" type="button" onClick={() => loadVideos(projectId)} aria-label="刷新成片列表"><RefreshCw size={17} /></button>
            </div>
            <div className="video-version-list">
              {videos.map((video) => {
                const thumb = (video.thumbnailPaths ?? video.thumbnail_paths_json ?? [])[0];
                return (
                  <button className={`video-version-button${video.id === selectedId ? " active" : ""}`} type="button" key={video.id} onClick={() => setSelectedId(video.id)}>
                    <span className="video-thumb">
                      {thumb ? <img src={api.mediaUrl(thumb)} alt="视频缩略图" /> : <Film size={22} />}
                      <span>v{video.revision}</span>
                    </span>
                    <span className="video-version-copy">
                      <strong>镜头 {video.shotId?.slice(0, 8) ?? "—"}</strong>
                      <small>{formatDate(video.createdAt)}</small>
                    </span>
                    <Badge status={video.reviewStatus} />
                  </button>
                );
              })}
            </div>
          </aside>

          {selected ? (
            <div className="review-main">
              <article className="card player-card">
                <div className="player-toolbar">
                  <div>
                    <div className="title-with-badge">
                      <h2>镜头版本 v{selected.revision}</h2>
                      <Badge status={selected.reviewStatus} />
                      {String(metadata.provider ?? "").toLowerCase().includes("demo") ? <Badge tone="demo">DEMO</Badge> : null}
                    </div>
                    <p>生成于 {formatDate(selected.createdAt)}</p>
                  </div>
                  <div className="metadata-line">
                    <span>{formatDuration(Number(metadata.duration_seconds ?? metadata.duration))}</span>
                    <span>{metadata.width && metadata.height ? `${metadata.width}×${metadata.height}` : "分辨率待检查"}</span>
                    <span>{formatBytes(Number(metadata.file_size ?? metadata.size))}</span>
                  </div>
                </div>
                <div className="video-player-wrap">
                  {mediaSrc && !mediaFailed && metadata.playable_video !== false ? (
                    <video
                      key={mediaSrc}
                      controls
                      preload="metadata"
                      src={mediaSrc}
                      onError={() => setMediaFailed(true)}
                      aria-label={`镜头版本 ${selected.revision} 视频播放器`}
                    >
                      当前浏览器无法播放此视频。
                    </video>
                  ) : (
                    <div className="media-unavailable" role="alert">
                      <ImageOff size={30} />
                      <strong>视频暂时无法播放</strong>
                      <p>{metadata.playable_video === false ? "当前 Demo Provider 只生成显式状态回放，不会伪装成可播放视频。" : mediaSrc ? "媒体加载失败，请检查受控媒体路由和文件是否存在。" : "该版本没有可访问的媒体地址。"}</p>
                    </div>
                  )}
                </div>
                <QualityDurationWarning metadata={metadata} videoId={selected.id}/>
                <div className="reference-summary">
                  <div>
                    <span>生成提示词</span>
                    <p>{selected.prompt ?? String(metadata.prompt ?? "未记录")}</p>
                    {metadata.revision_instruction ? <><span>本次修订要求</span><p>{String(metadata.revision_instruction)}</p><details><summary>原始提示词</summary><p>{String(metadata.original_prompt || "未记录")}</p></details></> : null}
                  </div>
                  <div>
                    <span>人物参考</span>
                    <p>角色：{String(metadata.character_id || "未记录")} · 版本：{String(metadata.character_version_id || "未记录")} · ComfyUI ID：{String(metadata.comfyui_prompt_id || "未记录")}</p>
                    <div className="reference-preview">{(selected.referenceAssets || []).map((asset, index) => <figure key={String(asset.asset_id)}><img src={api.mediaUrl(String(asset.relative_path))} alt={`实际参考 Picture ${index + 1}`} /><figcaption>{`<Picture ${index + 1}>`} · {String(asset.asset_id)}</figcaption></figure>)}</div>
                  </div>
                </div>
              </article>

              <div className="review-columns">
                <article className="card quality-card">
                  <div className="section-heading compact">
                    <div><p className="eyebrow">客观证据</p><h2>自动检查结果</h2></div>
                    <button className="button button-secondary button-small" type="button" onClick={runQualityCheck} disabled={busy === "quality"}>
                      {busy === "quality" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}
                      运行质检
                    </button>
                  </div>
                  {qualityChecks.length ? (
                    <div className="quality-list">
                      {qualityChecks.map((check) => <QualityRow check={check} key={check.id} />)}
                    </div>
                  ) : (
                    <div className="compact-empty"><CircleAlert size={20} /><p>尚未运行自动质检。</p></div>
                  )}
                  <div className="inline-notice">
                    人脸一致性模型未配置时会显示“不可用”，绝不会填入虚构相似度。
                  </div>
                </article>

                <article className="card human-review-card">
                  <p className="eyebrow">人工决策</p>
                  <h2>人工审核状态</h2>
                  <div className="manual-state">
                    <span className={`manual-icon ${selected.reviewStatus}`}>
                      {selected.reviewStatus === "approved" ? <Check size={22} /> : selected.reviewStatus === "rejected" ? <X size={22} /> : <PlayCircle size={22} />}
                    </span>
                    <div><strong>{statusLabel(selected.reviewStatus)}</strong><p>{selected.reviewReason ?? "尚未留下人工审核理由。"}</p></div>
                  </div>
                  {rejectOpen ? (
                    <div className="reject-box">
                      <FormField label="驳回原因" required>
                        <textarea rows={3} value={rejectReason} onChange={(event) => setRejectReason(event.target.value)} placeholder="说明需要重做的具体问题…" />
                      </FormField>
                      <div className="form-actions">
                        <button className="button button-secondary button-small" type="button" onClick={() => setRejectOpen(false)}>取消</button>
                        <button className="button button-danger button-small" type="button" onClick={reject} disabled={!rejectReason.trim() || busy === "reject"}>
                          {busy === "reject" ? <LoaderCircle className="spin" size={14} /> : <X size={14} />} 驳回并记录
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="human-actions">
                      <button className="button button-secondary" type="button" onClick={() => setRejectOpen(true)} disabled={selected.reviewStatus === "approved"}>驳回并重做</button>
                      <button className="button button-primary" type="button" onClick={() => setApproveOpen(true)} disabled={selected.reviewStatus === "approved"}>
                        <Check size={16} /> 通过为候选成片
                      </button>
                    </div>
                  )}
                </article>
              </div>

              <article className="card revision-card">
                <div className="section-heading">
                  <div><p className="eyebrow">参考约束的再生成</p><h2>修改指令</h2><p>这不是逐帧精准修补，也不保证未指定像素完全不变。</p></div>
                  <Badge tone="neutral">旧版本保留</Badge>
                </div>
                <FormField label="本次修改要求" required hint="身份固定块与同一人物参考会再次带入；仅在工作流真实支持时才使用旧视频作为参考。">
                  <textarea rows={4} value={instruction} onChange={(event) => { setInstruction(event.target.value); clearRevisionPreview(); }} />
                </FormField>
                {showLocalRecovery ? <div>
                  <label className="studio-check"><input type="checkbox" checked={useProjectVideoService}
                    disabled={Boolean(revisionJobId)} onChange={event => { setUseProjectVideoService(event.target.checked); clearRevisionPreview(); }} />使用本项目的本地 H3 服务</label>
                  <p className="form-hint">{cloudSource ? "此版本来自云端。勾选后，按本项目的本地 H3 配置重新生成。" : "勾选后，按本项目的本地 H3 配置重新生成。"} <Link to={`/video-production?${productionParams}`}>查看项目视频服务与画质</Link></p>
                </div> : null}
                <div className="form-actions">
                  <button className="button button-primary" type="button" onClick={() => revise()} disabled={!instruction.trim() || busy === "revise" || !!revisionJobId}>
                    {busy === "revise" ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}
                    基于当前版本重新生成
                  </button>
                </div>
                {shotVersions.length > 1 ? (
                  <div className="version-comparison" aria-label="旧版与新版对比">{shotVersions.slice(0, 2).map(video => <figure key={video.id}><video controls preload="metadata" src={api.mediaUrl(video.mediaUrl || video.relativePath)} /><figcaption>v{video.revision} · {video.instruction || "原始版本"}</figcaption></figure>)}</div>
                ) : null}
                {shotVersions.length > 1 ? (
                  <div className="history-strip" aria-label="版本历史">
                    {shotVersions.map((video) => (
                      <button type="button" key={video.id} className={video.id === selected.id ? "active" : ""} onClick={() => setSelectedId(video.id)}>
                        <span>v{video.revision}</span><small>{formatDate(video.createdAt)}</small><Badge status={video.reviewStatus} />
                      </button>
                    ))}
                  </div>
                ) : null}
              </article>
            </div>
          ) : null}
        </section>
      ) : null}

      <ConfirmDialog
        open={approveOpen}
        title="通过为候选成片？"
        description="自动检查与人工审核是两套独立结果。确认后，此版本会锁为候选成片并可加入发布 dry-run 清单。"
        confirmLabel="确认通过"
        busy={busy === "approve"}
        onConfirm={approve}
        onCancel={() => setApproveOpen(false)}
      >
        <FormField label="审核人"><input value={approvalOperator} onChange={event => setApprovalOperator(event.target.value)} /></FormField>
        <FormField label="通过意见"><textarea rows={3} value={approvalReason} onChange={event => setApprovalReason(event.target.value)} placeholder="记录实际观看结果及保留意见…" /></FormField>
      </ConfirmDialog>
      {reviewedRevision ? <div className="dialog-backdrop"><section className="dialog-card wide-dialog prompt-preview" role="dialog" aria-label="修订完整提示词"><h2>修订将新建任务和视频文件</h2>{reviewedRevision.input.use_project_video_service ? <p>生成服务 · 本项目的本地 H3</p> : null}<p>{reviewedRevision.result.character_id ? `人物 ${reviewedRevision.result.character_id} · 版本 ${reviewedRevision.result.character_version_id ?? "未记录"}` : "此镜没有绑定人物版本"}</p><pre>{reviewedRevision.result.prompt}</pre><p>系统会按参考素材重新生成，并保留旧版本。</p><div className="dialog-actions"><button className="button button-secondary" onClick={clearRevisionPreview}>返回修改</button><button className="button button-primary" disabled={busy === "revise"} onClick={() => revise(true)}>提交真实修订</button></div></section></div> : null}
    </>
  );
}

function QualityRow({ check }: { check: QualityCheck }) {
  const details = check.details ?? check.details_json ?? {};
  const label = (check.checkType ?? check.check_type ?? "检查项").replaceAll("_", " ");
  return (
    <div className="quality-row">
      <span className={`quality-icon ${check.status}`} aria-hidden="true">
        {check.status === "passed" || check.status === "approved" ? <Check size={15} /> : check.status === "not_available" ? <RotateCcw size={15} /> : <CircleAlert size={15} />}
      </span>
      <div><strong>{label}</strong><small>{String(details.message ?? details.reason ?? (check.score != null ? `评分 ${check.score}` : statusLabel(check.status)))}</small></div>
      <Badge status={check.status} />
    </div>
  );
}
