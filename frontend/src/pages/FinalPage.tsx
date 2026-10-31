import { useCallback, useEffect, useState } from "react";
import { api, request } from "../lib/api";
import type { Project, VideoRevision } from "../lib/types";
import { ProjectPicker } from "../components/ProjectPicker";
import { Badge, EmptyState, ErrorState, PageHeader } from "../components/ui";

type Composition = { id: string; status: string; video_ids_json: string[]; media_url: string | null; error_message?: string; metadata_json: { has_audio?: boolean; audio_note?: string } };

export function FinalPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [videos, setVideos] = useState<VideoRevision[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [items, setItems] = useState<Composition[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("我的作品");
  const [preview, setPreview] = useState<{ results: { platform: string; status: string; notice: string; failures: string[] }[]; external_requests: number } | null>(null);
  const load = useCallback(async (id: string) => {
    try { const [clips, compositions] = await Promise.all([api.listVideos(id), request<Composition[]>(`/api/projects/${id}/compositions`)]); setVideos(clips); setItems(compositions); setError(""); } catch (cause) { setError(String(cause)); }
  }, []);
  useEffect(() => { void api.listProjects().then(next => { setProjects(next); setProjectId(next[0]?.id || ""); }).catch(cause => setError(String(cause))); }, []);
  useEffect(() => { if (projectId) { void load(projectId); setSelected([]); setPreview(null); } }, [projectId, load]);
  const active = items.some(item => ["queued", "running"].includes(item.status));
  useEffect(() => { if (!active || !projectId) return; const timer = window.setInterval(() => void load(projectId), 2500); return () => window.clearInterval(timer); }, [active, projectId, load]);
  async function compose() {
    setBusy(true); setError("");
    try { await request(`/api/projects/${projectId}/compositions`, { method: "POST", body: JSON.stringify({ video_ids: selected }) }); await load(projectId); } catch (cause) { setError(String(cause)); } finally { setBusy(false); }
  }
  async function dryRun(item: Composition) {
    setBusy(true);
    try { setPreview(await request(`/api/compositions/${item.id}/dry-run`, { method: "POST", body: JSON.stringify({ title, description: "原创视频作品。", hashtags: ["AI创作", "视频创作"] }) })); } catch (cause) { setError(String(cause)); } finally { setBusy(false); }
  }
  return <>
    <PageHeader eyebrow="创作流程 · 06" title="合成与成片" description="按选择顺序，实际调用 FFmpeg 合成审核通过的片段。原版本继续保留。" actions={<ProjectPicker projects={projects} value={projectId} onChange={setProjectId} />} />
    {error ? <ErrorState message={error} onRetry={() => load(projectId)} /> : null}
    <section className="card final-section"><div className="section-heading"><h2>选择片段与顺序</h2><button className="button button-primary" disabled={busy || !selected.length || active} onClick={compose}>提交真实合成</button></div>
      <p>当前顺序：{selected.map(id => `v${videos.find(video => video.id === id)?.revision} · ${id.slice(-8)}`).join(" → ") || "尚未选择"}</p>
      <div className="final-clips">{videos.filter(video => video.reviewStatus === "approved").map(video => <label key={video.id} data-video-id={video.id}><video src={api.mediaUrl(video.mediaUrl || video.relativePath)} controls preload="metadata" /><span><input type="checkbox" checked={selected.includes(video.id)} onChange={event => setSelected(current => event.target.checked ? [...current, video.id] : current.filter(id => id !== video.id))} />{video.shotId} · v{video.revision}</span></label>)}</div>
      {!videos.some(video => video.reviewStatus === "approved") ? <EmptyState title="等待审核通过的真实片段" description="生成视频并完成人工审核后，片段会出现在这里。" /> : null}
    </section>
    {items.map(item => <section className="card final-section" key={item.id}><div className="section-heading"><h2>成片 {item.id.slice(-8)}</h2><Badge status={item.status} /></div>{item.media_url ? <video className="final-player" src={item.media_url} controls preload="metadata" /> : <p>{item.error_message || "等待后端完成真实合成…"}</p>}<p>{item.metadata_json.audio_note}</p>
      {item.status === "generated" ? <div className="dry-run-form"><label>发布预览标题<input aria-label="成片预览标题" value={title} onChange={event => setTitle(event.target.value)} /></label><button className="button button-secondary" disabled={busy} onClick={() => dryRun(item)}>四平台 Dry-run 预览</button></div> : null}
    </section>)}
    {preview ? <section className="card final-section"><h2>预览完成</h2><p>真实平台请求：{preview.external_requests} · 账号均为模拟数据</p><div className="dry-run-results">{preview.results.map(result => <article key={result.platform}><strong>{result.platform}</strong><Badge tone={result.status === "PASS" ? "success" : "danger"}>{result.status}</Badge><p>{result.notice}</p>{result.failures.map(reason => <p key={reason}>{reason}</p>)}</article>)}</div></section> : null}
  </>;
}
