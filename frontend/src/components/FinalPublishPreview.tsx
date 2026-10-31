import { useEffect, useState } from "react";
import { request } from "../lib/api";
import { Badge } from "./ui";

type Composition = { id: string; status: string; media_url: string; metadata_json: { cover_relative_path?: string } };
type Result = { platform: string; status: string; notice: string; failures: string[]; account: { display_name: string | null; id: string | null; authorization_status: string } };
const platformNames: Record<string, string> = { douyin: "抖音", kuaishou: "快手", xiaohongshu: "小红书", wechat_channels: "微信视频号" };
export function FinalPublishPreview({ projectId }: { projectId: string }) {
  const [items, setItems] = useState<Composition[]>([]);
  const [selected, setSelected] = useState("");
  const [title, setTitle] = useState("我的作品");
  const [description, setDescription] = useState("AI 角色的人物一致性内部测试。");
  const [topics, setTopics] = useState("AI创作,视频创作");
  const [results, setResults] = useState<Result[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (projectId) void request<Composition[]>(`/api/projects/${projectId}/compositions`).then(next => { const generated = next.filter(item => item.status === "generated"); setItems(generated); setSelected(generated[0]?.id || ""); setResults([]); }).catch(cause => setError(String(cause))); }, [projectId]);
  const current = items.find(item => item.id === selected);
  async function run() { setBusy(true); setError(""); try { const result = await request<{ results: Result[] }>(`/api/compositions/${selected}/dry-run`, { method: "POST", body: JSON.stringify({ title, description, hashtags: topics.split(/[,，]/).map(topic => topic.trim()).filter(Boolean) }) }); setResults(result.results); } catch (cause) { setError(String(cause)); } finally { setBusy(false); } }
  return <section className="card final-section" aria-label="真实成片发布预览"><p className="eyebrow">真实成片 · 本地 Dry-run</p><h2>四平台发布预览</h2><p>抖音、快手、小红书、微信视频号 · 均使用模拟账号。无登录、上传或真实发布操作。</p>
    <div className="editor-form-grid"><label className="form-field">选择真实合成文件<select aria-label="选择真实合成文件" value={selected} onChange={event => setSelected(event.target.value)}><option value="">等待真实合成成片</option>{items.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}</select></label><label className="form-field">成片标题<input aria-label="发布成片标题" value={title} onChange={event => setTitle(event.target.value)} /></label><label className="form-field">成片描述<textarea aria-label="发布成片描述" value={description} onChange={event => setDescription(event.target.value)} /></label><label className="form-field">话题<input aria-label="发布成片话题" value={topics} onChange={event => setTopics(event.target.value)} /></label></div>
    {current ? <div className="final-clips"><video controls preload="metadata" src={current.media_url} />{current.metadata_json.cover_relative_path ? <img style={{ maxHeight: 220 }} src={`/api/media/${current.metadata_json.cover_relative_path}`} alt="从真实成片提取的封面" /> : null}</div> : <p className="form-hint">完成实际片段合成后，文件和真实封面才会出现在这里。</p>}
    {error ? <p role="alert">{error}</p> : null}<button className="button button-primary" disabled={!selected || !title.trim() || busy} onClick={run}>{busy ? "正在解码与检查…" : "检查真实成片并预览四平台"}</button>
    {results.length ? <div className="dry-run-results" aria-label="成片四平台检查结果">{results.map(result => <article key={result.platform}><strong>{platformNames[result.platform] || result.platform}</strong><Badge tone={result.status === "PASS" ? "success" : "danger"}>{result.status}</Badge><p>{result.notice}</p><small>{result.account.display_name || "未配置模拟账号"}</small>{result.failures.map(reason => <p key={reason}>{reason}</p>)}</article>)}</div> : null}
  </section>;
}
