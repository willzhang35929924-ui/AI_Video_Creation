import {
  CalendarClock,
  Check,
  CircleAlert,
  CirclePlus,
  Eye,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  Save,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { formatDate, isoLocalInput, statusLabel } from "../lib/format";
import type { Project, PublishBatch, PublishingAccount, PublishingPreset, VideoRevision } from "../lib/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ProjectPicker } from "../components/ProjectPicker";
import { useToast } from "../components/ToastProvider";
import { Badge, EmptyState, ErrorState, FormField, LoadingState, PageHeader } from "../components/ui";
import { FinalPublishPreview } from "../components/FinalPublishPreview";

const platformInfo = {
  douyin: { name: "抖音", mark: "抖", color: "black" },
  kuaishou: { name: "快手", mark: "快", color: "orange" },
  xiaohongshu: { name: "小红书", mark: "红", color: "red" },
  wechat_channels: { name: "微信视频号", mark: "视", color: "green" },
} as const;

export function PublishingPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [videos, setVideos] = useState<VideoRevision[]>([]);
  const [accounts, setAccounts] = useState<PublishingAccount[]>([]);
  const [presets, setPresets] = useState<PublishingPreset[]>([]);
  const [selectedVideos, setSelectedVideos] = useState<string[]>([]);
  const [selectedAccounts, setSelectedAccounts] = useState<string[]>([]);
  const [selectedPreset, setSelectedPreset] = useState("");
  const [schedule, setSchedule] = useState("");
  const [batch, setBatch] = useState<PublishBatch | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [accountOpen, setAccountOpen] = useState(false);
  const [presetOpen, setPresetOpen] = useState(false);
  const [deleteAccount, setDeleteAccount] = useState<PublishingAccount | null>(null);
  const [newAccount, setNewAccount] = useState({ platform: "douyin", displayName: "抖音测试账号" });
  const [presetForm, setPresetForm] = useState({
    accountId: "",
    name: "竖屏短片默认预设",
    titleTemplate: "我的视频作品",
    captionTemplate: "原创视频作品。",
    hashtags: "AI短片, 原创人物, 电影感",
    visibility: "private",
    defaultSchedule: "",
  });
  const { notify } = useToast();

  const loadProjectVideos = useCallback(async (id: string) => {
    if (!id) {
      setVideos([]);
      return;
    }
    const result = await api.listVideos(id);
    const approved = result.filter((video) => video.reviewStatus === "approved");
    setVideos(approved);
    setSelectedVideos((current) => current.filter((value) => approved.some((video) => video.id === value)));
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextProjects, nextAccounts, nextPresets] = await Promise.all([
        api.listProjects(),
        api.listPublishingAccounts(),
        api.listPublishingPresets(),
      ]);
      setProjects(nextProjects);
      setAccounts(nextAccounts);
      setPresets(nextPresets);
      const target = projectId || nextProjects[0]?.id || "";
      setProjectId(target);
      if (target) await loadProjectVideos(target);
      setSelectedAccounts((current) => current.filter((id) => nextAccounts.some((account) => account.id === id)));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "发布工作台加载失败");
    } finally {
      setLoading(false);
    }
  }, [loadProjectVideos, projectId]);

  useEffect(() => {
    void loadAll();
    // Initial fetch only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectedPresetObject = presets.find((preset) => preset.id === selectedPreset);
  const dryRunItems = useMemo(
    () =>
      selectedVideos.flatMap((videoId) =>
        selectedAccounts.map((accountId) => ({
          video: videos.find((video) => video.id === videoId),
          account: accounts.find((account) => account.id === accountId),
        })),
      ),
    [accounts, selectedAccounts, selectedVideos, videos],
  );

  function toggle(list: string[], value: string, setter: (next: string[]) => void) {
    setter(list.includes(value) ? list.filter((item) => item !== value) : [...list, value]);
  }

  async function createAccount(event: React.FormEvent) {
    event.preventDefault();
    setBusy("account");
    try {
      const account = await api.createPublishingAccount(newAccount);
      setAccounts((current) => [...current, account]);
      setSelectedAccounts((current) => [...current, account.id]);
      setAccountOpen(false);
      notify("测试账号卡片已保存；未写入任何明文 token", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "账号保存失败", "error");
    } finally {
      setBusy("");
    }
  }

  async function removeAccount() {
    if (!deleteAccount) return;
    setBusy("delete-account");
    try {
      await api.deletePublishingAccount(deleteAccount.id);
      setAccounts((current) => current.filter((account) => account.id !== deleteAccount.id));
      setSelectedAccounts((current) => current.filter((id) => id !== deleteAccount.id));
      setDeleteAccount(null);
      notify("账号配置已移除；外部平台账号未受影响", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "移除账号失败", "error");
    } finally {
      setBusy("");
    }
  }

  function openPreset() {
    setPresetForm((current) => ({ ...current, accountId: selectedAccounts[0] ?? accounts[0]?.id ?? "" }));
    setPresetOpen(true);
  }

  async function savePreset(event: React.FormEvent) {
    event.preventDefault();
    setBusy("preset");
    try {
      const preset = await api.createPublishingPreset({
        accountId: presetForm.accountId,
        name: presetForm.name.trim(),
        titleTemplate: presetForm.titleTemplate.trim(),
        captionTemplate: presetForm.captionTemplate.trim(),
        hashtags: presetForm.hashtags.split(",").map((tag) => tag.trim()).filter(Boolean),
        visibility: presetForm.visibility,
        defaultSchedule: presetForm.defaultSchedule ? new Date(presetForm.defaultSchedule).toISOString() : null,
      });
      const persisted = await api.listPublishingPresets();
      setPresets(persisted);
      setSelectedPreset(preset.id);
      setPresetOpen(false);
      notify("发布预设已保存并从数据库重新加载", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "保存预设失败", "error");
    } finally {
      setBusy("");
    }
  }

  async function runDryRun() {
    if (!selectedVideos.length || !selectedAccounts.length) return;
    setBusy("dry-run");
    setBatch(null);
    try {
      const result = await api.dryRunPublish({
        accountIds: selectedAccounts,
        videoIds: selectedVideos,
        presetId: selectedPreset || undefined,
        schedule: schedule ? new Date(schedule).toISOString() : null,
      });
      setBatch(result);
      notify("Dry-run 验证完成；未调用任何真实发布接口", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "Dry-run 验证失败", "error");
    } finally {
      setBusy("");
    }
  }

  if (loading) return <LoadingState label="正在读取发布配置…" />;

  return (
    <>
      <PageHeader
        eyebrow="创作流程 · 05"
        title="批量发布"
        description="本次仅验证清单与持久化预设；真实发布入口保持禁用，外部调用次数为 0。"
        actions={
          <ProjectPicker
            projects={projects}
            value={projectId}
            onChange={(id) => {
              setProjectId(id);
              void loadProjectVideos(id);
            }}
          />
        }
      />

      <div className="dry-run-banner" role="status">
        <LockKeyhole size={20} />
        <div><strong>DRY-RUN 安全模式</strong><span>不会上传视频、创建平台草稿或发布内容。</span></div>
        <Badge tone="success">真实发布调用：0</Badge>
      </div>

      {error ? <ErrorState message={error} onRetry={loadAll} /> : null}
      <FinalPublishPreview projectId={projectId} />

      <section className="card accounts-section">
        <div className="section-heading">
          <div><p className="eyebrow">目标渠道</p><h2>平台与账号</h2><p>同一平台可配置多个账号；授权能力按平台分别描述。</p></div>
          <button className="button button-secondary" type="button" onClick={() => setAccountOpen(true)}><CirclePlus size={16} /> 添加测试账号</button>
        </div>
        {accounts.length ? (
          <div className="account-grid">
            {accounts.map((account) => {
              const info = platformInfo[account.platform as keyof typeof platformInfo] ?? { name: account.platform, mark: account.platform.slice(0, 1), color: "blue" };
              const checked = selectedAccounts.includes(account.id);
              return (
                <article className={`account-card${checked ? " selected" : ""}`} key={account.id}>
                  <label className="account-select">
                    <input type="checkbox" checked={checked} onChange={() => toggle(selectedAccounts, account.id, setSelectedAccounts)} />
                    <span className={`platform-mark ${info.color}`}>{info.mark}</span>
                    <span><strong>{info.name}</strong><small>{account.displayName}</small></span>
                  </label>
                  <dl>
                    <div><dt>授权</dt><dd><Badge status={account.authStatus} /></dd></div>
                    <div><dt>能力</dt><dd>{statusLabel(account.capabilityStatus)}</dd></div>
                    <div><dt>最近检查</dt><dd>{formatDate(account.lastCheckedAt)}</dd></div>
                    <div><dt>凭据到期</dt><dd>{account.tokenExpiry ? formatDate(account.tokenExpiry) : "未保存凭据"}</dd></div>
                  </dl>
                  <div className="account-actions">
                    <button className="button button-ghost button-small" type="button" disabled title="本期未配置平台 OAuth；不会伪造授权成功。">授权 / 重新授权</button>
                    <button className="icon-button danger" type="button" onClick={() => setDeleteAccount(account)} aria-label={`移除${account.displayName}`}><Trash2 size={15} /></button>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <EmptyState title="尚无平台账号" description="添加占位测试账号即可验证发布预设和 dry-run；不会发起 OAuth 或发布请求。" action={<button className="button button-primary button-small" type="button" onClick={() => setAccountOpen(true)}>添加测试账号</button>} />
        )}
      </section>

      <section className="publish-workspace">
        <article className="card publish-config">
          <div className="section-heading compact"><div><p className="eyebrow">步骤 1</p><h2>选择候选成片</h2></div><Badge tone="neutral">{selectedVideos.length} 已选</Badge></div>
          {videos.length ? (
            <div className="publish-video-list">
              {videos.map((video) => {
                const checked = selectedVideos.includes(video.id);
                const thumbnail = (video.thumbnailPaths ?? video.thumbnail_paths_json ?? [])[0];
                return (
                  <label className={`publish-video${checked ? " selected" : ""}`} key={video.id}>
                    <input type="checkbox" checked={checked} onChange={() => toggle(selectedVideos, video.id, setSelectedVideos)} />
                    <span className="publish-video-thumb">{thumbnail ? <img src={api.mediaUrl(thumbnail)} alt="候选成片缩略图" /> : <Eye size={20} />}</span>
                    <span><strong>镜头 {video.shotId?.slice(0, 8)} · v{video.revision}</strong><small>人工审核通过 · {formatDate(video.createdAt)}</small></span>
                    <Badge status={video.reviewStatus} />
                  </label>
                );
              })}
            </div>
          ) : <EmptyState title="没有可发布候选成片" description="只有通过人工审核的视频版本才能进入发布清单。" />}

          <div className="publish-step-divider" />
          <div className="section-heading compact"><div><p className="eyebrow">步骤 2</p><h2>应用预设</h2></div><button className="button button-ghost button-small" type="button" onClick={openPreset} disabled={!accounts.length}><CirclePlus size={14} /> 新建预设</button></div>
          <FormField label="已保存预设">
            <select value={selectedPreset} onChange={(event) => setSelectedPreset(event.target.value)}>
              <option value="">不使用预设</option>
              {presets.map((preset) => <option value={preset.id} key={preset.id}>{preset.name}</option>)}
            </select>
          </FormField>
          {selectedPresetObject ? (
            <div className="preset-preview">
              <strong>{selectedPresetObject.titleTemplate}</strong>
              <p>{selectedPresetObject.captionTemplate}</p>
              <div className="tag-row">{(selectedPresetObject.hashtags ?? []).map((tag) => <span className="mini-tag" key={tag}>#{tag}</span>)}</div>
            </div>
          ) : null}
          <FormField label="计划时间（仅用于清单验证）">
            <input type="datetime-local" value={schedule} min={isoLocalInput(new Date().toISOString())} onChange={(event) => setSchedule(event.target.value)} />
          </FormField>
        </article>

        <article className="card publish-preview">
          <div className="section-heading compact"><div><p className="eyebrow">步骤 3</p><h2>预览发布清单</h2></div><Badge tone="demo">DRY-RUN</Badge></div>
          {dryRunItems.length ? (
            <div className="manifest-list">
              {dryRunItems.map((item, index) => (
                <div className="manifest-row" key={`${item.video?.id}-${item.account?.id}`}>
                  <span className="manifest-index">{String(index + 1).padStart(2, "0")}</span>
                  <div><strong>{item.video ? `镜头 ${item.video.shotId?.slice(0, 8)} · v${item.video.revision}` : "未知视频"}</strong><small>{item.account ? `${platformInfo[item.account.platform as keyof typeof platformInfo]?.name ?? item.account.platform} / ${item.account.displayName}` : "未知账号"}</small></div>
                  <Badge tone="neutral">待验证</Badge>
                </div>
              ))}
            </div>
          ) : (
            <div className="manifest-placeholder"><Send size={28} /><strong>选择成片和账号</strong><p>组合后的发布清单会显示在这里。</p></div>
          )}
          <div className="publish-summary">
            <div><span>候选成片</span><strong>{selectedVideos.length}</strong></div>
            <div><span>目标账号</span><strong>{selectedAccounts.length}</strong></div>
            <div><span>清单项目</span><strong>{dryRunItems.length}</strong></div>
          </div>
          <button className="button button-primary button-full" type="button" onClick={runDryRun} disabled={!dryRunItems.length || busy === "dry-run"}>
            {busy === "dry-run" ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}
            验证清单并执行 Dry-run
          </button>
          <button className="button button-disabled button-full" type="button" disabled title="本次验收明确禁止真实发布">
            <LockKeyhole size={16} /> 真实发布（本次禁用）
          </button>
        </article>
      </section>

      {batch ? (
        <section className="card dry-run-result" aria-live="polite">
          <span className="result-icon"><Check size={22} /></span>
          <div><p className="eyebrow">Dry-run 结果</p><h2>清单验证完成</h2><p>批次 {batch.id} · {statusLabel(batch.status)} · 创建于 {formatDate(batch.createdAt ?? batch.created_at)}</p></div>
          <div className="result-proof"><strong>{(batch.items ?? batch.items_json ?? []).reduce((sum, item) => sum + Number(item.external_publish_calls ?? 0), 0)}</strong><span>真实平台调用</span></div>
          {(batch.validationErrors ?? batch.validation_errors ?? []).length ? <div className="validation-errors">{(batch.validationErrors ?? batch.validation_errors ?? []).map((item) => <p key={item}><CircleAlert size={14} />{item}</p>)}</div> : null}
        </section>
      ) : null}

      {accountOpen ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => !busy && setAccountOpen(false)}>
          <form className="dialog-card form-dialog" onSubmit={createAccount} onMouseDown={(event) => event.stopPropagation()}>
            <button className="icon-button dialog-close" type="button" onClick={() => setAccountOpen(false)} aria-label="关闭"><X size={18} /></button>
            <p className="eyebrow">Dry-run 配置</p><h2>添加测试账号</h2><p>只保存账号备注和能力状态；不创建 OAuth 会话，也不保存明文 token。</p>
            <FormField label="平台" required><select value={newAccount.platform} onChange={(event) => setNewAccount((value) => ({ ...value, platform: event.target.value }))}>{Object.entries(platformInfo).map(([value, info]) => <option value={value} key={value}>{info.name}</option>)}</select></FormField>
            <FormField label="账号备注" required><input value={newAccount.displayName} onChange={(event) => setNewAccount((value) => ({ ...value, displayName: event.target.value }))} required /></FormField>
            <div className="dialog-actions"><button className="button button-secondary" type="button" onClick={() => setAccountOpen(false)}>取消</button><button className="button button-primary" type="submit" disabled={busy === "account"}>{busy === "account" ? <LoaderCircle className="spin" size={16} /> : <CirclePlus size={16} />} 保存测试账号</button></div>
          </form>
        </div>
      ) : null}

      {presetOpen ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => !busy && setPresetOpen(false)}>
          <form className="dialog-card form-dialog wide-dialog" onSubmit={savePreset} onMouseDown={(event) => event.stopPropagation()}>
            <button className="icon-button dialog-close" type="button" onClick={() => setPresetOpen(false)} aria-label="关闭"><X size={18} /></button>
            <p className="eyebrow">账号级复用配置</p><h2>新建发布预设</h2>
            <div className="editor-form-grid">
              <FormField label="所属账号" required><select value={presetForm.accountId} onChange={(event) => setPresetForm((value) => ({ ...value, accountId: event.target.value }))} required><option value="">选择账号</option>{accounts.map((account) => <option value={account.id} key={account.id}>{account.displayName}</option>)}</select></FormField>
              <FormField label="预设名称" required><input value={presetForm.name} onChange={(event) => setPresetForm((value) => ({ ...value, name: event.target.value }))} required /></FormField>
              <div className="form-span-2"><FormField label="标题模板" required><input value={presetForm.titleTemplate} onChange={(event) => setPresetForm((value) => ({ ...value, titleTemplate: event.target.value }))} required /></FormField></div>
              <div className="form-span-2"><FormField label="文案模板"><textarea rows={4} value={presetForm.captionTemplate} onChange={(event) => setPresetForm((value) => ({ ...value, captionTemplate: event.target.value }))} /></FormField></div>
              <FormField label="话题" hint="英文逗号分隔"><input value={presetForm.hashtags} onChange={(event) => setPresetForm((value) => ({ ...value, hashtags: event.target.value }))} /></FormField>
              <FormField label="可见性"><select value={presetForm.visibility} onChange={(event) => setPresetForm((value) => ({ ...value, visibility: event.target.value }))}><option value="private">仅自己可见</option><option value="draft">保存草稿</option><option value="public">公开（仅预览）</option></select></FormField>
              <FormField label="默认计划时间"><input type="datetime-local" value={presetForm.defaultSchedule} onChange={(event) => setPresetForm((value) => ({ ...value, defaultSchedule: event.target.value }))} /></FormField>
            </div>
            <div className="dialog-actions"><button className="button button-secondary" type="button" onClick={() => setPresetOpen(false)}>取消</button><button className="button button-primary" type="submit" disabled={!presetForm.accountId || busy === "preset"}>{busy === "preset" ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />} 保存并重新加载</button></div>
          </form>
        </div>
      ) : null}

      <ConfirmDialog open={Boolean(deleteAccount)} title="移除账号配置？" description="只会移除本地账号卡片及其引用，不会删除或修改外部平台账号。" confirmLabel="移除配置" tone="danger" busy={busy === "delete-account"} onConfirm={removeAccount} onCancel={() => setDeleteAccount(null)} />
    </>
  );
}
