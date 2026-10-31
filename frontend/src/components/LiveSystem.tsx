import { useEffect, useState } from "react";
import { request } from "../lib/api";

type Status = { gpu: { status: string; raw?: string }; comfyui: { status: string; stats?: { system?: { comfyui_version?: string; pytorch_version?: string } } }; downloads: { path: string; status: string; downloaded_bytes: number; size: number }[] };

export function LiveSystem() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const refresh = () => request<Status>("/api/system/status").then(value => { if (active) { setStatus(value); setError(""); } }).catch(cause => { if (active) setError(String(cause)); });
    void refresh();
    const timer = window.setInterval(refresh, 10000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  return <section className="card live-system" aria-label="实时设备状态">
    <div><p className="eyebrow">本机实测 · 每 10 秒刷新</p><h2>运行环境</h2></div>
    {error ? <p role="alert">{error}</p> : status ? <>
      <dl><div><dt>GPU / 显存总量、已用 MiB / 利用率 / 温度</dt><dd>{status.gpu.raw || status.gpu.status}</dd></div>
        <div><dt>ComfyUI</dt><dd>{status.comfyui.status} · {status.comfyui.stats?.system?.comfyui_version || "—"} · {status.comfyui.stats?.system?.pytorch_version || "—"}</dd></div></dl>
      {status.downloads.some(item => item.status !== "verified") ? <div className="download-lines">{status.downloads.map(item => <p key={item.path}><span>{item.path.split("/").at(-1)}</span><strong>{item.status} · {(item.downloaded_bytes / 1024 ** 3).toFixed(2)} / {(item.size / 1024 ** 3).toFixed(2)} GiB</strong></p>)}</div> : null}
    </> : <p>正在向本地后端读取设备状态…</p>}
  </section>;
}
