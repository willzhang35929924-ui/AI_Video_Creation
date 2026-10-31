import {
  Check,
  CirclePlus,
  FileVideo2,
  Image as ImageIcon,
  LoaderCircle,
  Search,
  ShieldCheck,
  Upload,
  UserRound,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, request } from "../lib/api";
import { formatDate, statusLabel } from "../lib/format";
import type { Character, CharacterAsset, CharacterVersion } from "../lib/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { useToast } from "../components/ToastProvider";
import { Badge, EmptyState, ErrorState, FormField, LoadingState, PageHeader } from "../components/ui";

const assetLabels: Record<string, string> = {
  front_face: "标准正脸",
  left_45: "左 45°",
  right_45: "右 45°",
  profile: "侧脸",
  half_body: "半身",
  full_body: "全身",
  expression: "表情",
  outfit: "服装",
  reference_video: "参考视频",
  reference_audio: "参考音频",
};

const defaultIdentity = {
  fixedIdentity: "28 岁成年原创女性，黑色齐肩短发、椭圆脸、自然眉形、清晰鼻梁，左眼下方一颗小痣。",
  hairstyle: "黑色齐肩短发",
  facialFeatures: "自然椭圆脸、深棕色眼睛、左眼下方小痣、自然肤质",
  bodyType: "真实自然的成年女性身材比例",
  defaultOutfit: "鼠尾草绿风衣、米白色内搭",
  immutableTraits: "身份、脸型、五官、发型、痣的位置、肤色观感、身材比例、默认服装",
  allowedVariations: "表情、轻微头部转向、手部动作、镜头角度、背景、光照氛围",
  promptTemplateVersion: "v1",
};

export function CharactersPage() {
  const [assetFilter, setAssetFilter] = useState("all");
  const [characters, setCharacters] = useState<Character[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [selected, setSelected] = useState<Character | null>(null);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [versionOpen, setVersionOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [approveOpen, setApproveOpen] = useState(false);
  const [newCharacter, setNewCharacter] = useState({ slug: "hero_001", displayName: "", description: "虚构成年女性人物，写实电影摄影风格", tags: "主角, 原创人物" });
  const [identity, setIdentity] = useState({ ...defaultIdentity });
  const [assetFile, setAssetFile] = useState<File | null>(null);
  const [assetType, setAssetType] = useState("front_face");
  const [assetTags, setAssetTags] = useState("主身份参考");
  const [assetPrimary, setAssetPrimary] = useState(true);
  const [assetApprove, setAssetApprove] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const { notify } = useToast();

  const loadCharacters = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await api.listCharacters();
      setCharacters(result);
      setSelectedId((current) => current || result[0]?.id || "");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "人物库加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (id: string) => {
    if (!id) {
      setSelected(null);
      return;
    }
    setDetailLoading(true);
    try {
      const result = await api.getCharacter(id);
      setSelected(result);
      const currentId = result.currentApprovedVersionId ?? result.current_approved_version_id;
      setSelectedVersionId((value) =>
        result.versions?.some((version) => version.id === value)
          ? value
          : currentId ?? result.versions?.[0]?.id ?? result.currentVersion?.id ?? "",
      );
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "人物详情加载失败", "error");
    } finally {
      setDetailLoading(false);
    }
  }, [notify]);

  useEffect(() => {
    void loadCharacters();
  }, [loadCharacters]);

  useEffect(() => {
    void loadDetail(selectedId);
  }, [loadDetail, selectedId]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return characters.filter((character) => {
      const matchesQuery =
        !needle ||
        character.displayName.toLowerCase().includes(needle) ||
        character.slug.toLowerCase().includes(needle) ||
        (character.tags ?? []).some((tag) => tag.toLowerCase().includes(needle));
      return matchesQuery && (statusFilter === "all" || character.status === statusFilter);
    });
  }, [characters, query, statusFilter]);

  const versions = selected?.versions?.length
    ? selected.versions
    : selected?.currentVersion
      ? [selected.currentVersion]
      : [];
  const selectedVersion = versions.find((version) => version.id === selectedVersionId) ?? versions[0] ?? null;
  const assets = selectedVersion?.assets ?? [];

  async function createCharacter(event: React.FormEvent) {
    event.preventDefault();
    setBusy("create");
    try {
      const created = await api.createCharacter({
        slug: newCharacter.slug.trim(),
        displayName: newCharacter.displayName.trim(),
        description: newCharacter.description.trim(),
        tags: newCharacter.tags.split(",").map((tag) => tag.trim()).filter(Boolean),
      });
      setCharacters((current) => [...current, created]);
      setSelectedId(created.id);
      setCreateOpen(false);
      notify("人物档案已创建；请建立身份版本并上传参考素材", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "创建人物失败", "error");
    } finally {
      setBusy("");
    }
  }

  function openNewVersion() {
    const currentIdentity = selectedVersion?.identityJson ?? selectedVersion?.identity_json;
    setIdentity({
      ...defaultIdentity,
      ...(currentIdentity && typeof currentIdentity === "object" ? currentIdentity : {}),
    });
    setVersionOpen(true);
  }

  async function createVersion(event: React.FormEvent) {
    event.preventDefault();
    if (!selected) return;
    setBusy("version");
    try {
      const version = await api.createCharacterVersion(selected.id, identity);
      setSelected((current) => (current ? { ...current, versions: [...(current.versions ?? []), version] } : current));
      setSelectedVersionId(version.id);
      setVersionOpen(false);
      notify("已创建独立人物版本；历史批准版本未被覆盖", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "创建人物版本失败", "error");
    } finally {
      setBusy("");
    }
  }

  async function uploadAsset(event: React.FormEvent) {
    event.preventDefault();
    if (!selected || !selectedVersion || !assetFile) return;
    setBusy("upload");
    try {
      await api.uploadCharacterAsset(selected.id, {
        versionId: selectedVersion.id,
        file: assetFile,
        assetType,
        isPrimary: assetPrimary,
        tags: assetTags.split(",").map((tag) => tag.trim()).filter(Boolean),
        approve: assetApprove,
      });
      await loadDetail(selected.id);
      setUploadOpen(false);
      setAssetFile(null);
      notify("参考素材已上传并记录文件哈希", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "素材上传失败", "error");
    } finally {
      setBusy("");
    }
  }

  async function approveVersion() {
    if (!selected || !selectedVersion) return;
    setBusy("approve");
    try {
      await api.approveCharacterVersion(selected.id, selectedVersion.id);
      await Promise.all([loadDetail(selected.id), loadCharacters()]);
      setApproveOpen(false);
      notify("人物版本已批准，可供分镜绑定", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "批准人物版本失败", "error");
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="创作流程 · 02"
        title="人物素材库"
        description="人物身份按不可变版本管理；项目可继续使用旧版本，不会被后续修改悄悄替换。"
        actions={
          <button className="button button-primary" type="button" onClick={() => setCreateOpen(true)}>
            <CirclePlus size={17} />
            新建人物
          </button>
        }
      />

      {error ? <ErrorState message={error} onRetry={loadCharacters} /> : null}
      {loading ? <LoadingState label="正在读取人物素材…" /> : null}

      {!loading ? (
        <section className="characters-layout">
          <aside className="card character-browser">
            <div className="filter-stack">
              <label className="search-input">
                <Search size={16} aria-hidden="true" />
                <span className="sr-only">搜索人物</span>
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索角色名、ID 或标签" />
              </label>
              <label>
                <span className="sr-only">筛选状态</span>
                <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                  <option value="all">全部状态</option>
                  <option value="active">启用</option>
                  <option value="inactive">停用</option>
                </select>
              </label>
            </div>
            {filtered.length ? (
              <div className="character-card-list">
                {filtered.map((character) => (
                  <button
                    className={`character-card-button${character.id === selectedId ? " active" : ""}`}
                    type="button"
                    key={character.id}
                    onClick={() => setSelectedId(character.id)}
                  >
                    <CharacterAvatar character={character} />
                    <span className="character-card-copy">
                      <strong>{character.displayName}</strong>
                      <small>{character.slug}</small>
                      <span className="tag-row">
                        {(character.tags ?? []).slice(0, 2).map((tag) => (
                          <span className="mini-tag" key={tag}>{tag}</span>
                        ))}
                      </span>
                    </span>
                    <Badge status={character.status} />
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState
                title={characters.length ? "没有匹配人物" : "人物库还是空的"}
                description={characters.length ? "调整搜索或筛选条件。" : "创建人物档案，再添加不可变身份版本与参考素材。"}
                action={!characters.length ? <button className="button button-secondary button-small" type="button" onClick={() => setCreateOpen(true)}>新建人物</button> : undefined}
              />
            )}
          </aside>

          <article className="card character-detail">
            {detailLoading ? (
              <LoadingState label="正在加载人物详情…" />
            ) : selected ? (
              <>
                <div className="character-hero">
                  <CharacterAvatar character={selected} large />
                  <div className="character-hero-copy">
                    <div className="title-with-badge">
                      <h2>{selected.displayName}</h2>
                      <Badge status={selected.status} />
                    </div>
                    <p>{selected.description || "暂无人物说明"}</p>
                    <div className="metadata-line">
                      <span>ID：{selected.slug}</span>
                      <span>创建于 {formatDate(selected.createdAt ?? selected.created_at)}</span>
                    </div>
                  </div>
                  <button className="button button-secondary" type="button" onClick={openNewVersion}>
                    <CirclePlus size={16} /> 新建版本
                  </button>
                </div>

                <div className="version-strip">
                  <div>
                    <span>身份版本</span>
                    <div className="version-tabs" role="tablist" aria-label="人物身份版本">
                      {versions.map((version) => (
                        <button
                          role="tab"
                          aria-selected={version.id === selectedVersionId}
                          className={version.id === selectedVersionId ? "active" : ""}
                          type="button"
                          key={version.id}
                          onClick={() => setSelectedVersionId(version.id)}
                        >
                          v{version.version}
                          {(version.approvalState ?? version.approval_state) === "approved" ? <Check size={13} /> : null}
                        </button>
                      ))}
                    </div>
                  </div>
                  {selectedVersion ? (
                    <div className="version-actions">
                      <Badge status={selectedVersion.approvalState ?? selectedVersion.approval_state ?? "draft"} />
                      <button className="button button-secondary button-small" type="button" onClick={() => setUploadOpen(true)}>
                        <Upload size={15} /> 上传素材
                      </button>
                      <button
                        className="button button-primary button-small"
                        type="button"
                        onClick={() => setApproveOpen(true)}
                        disabled={(selectedVersion.approvalState ?? selectedVersion.approval_state) === "approved"}
                      >
                        <ShieldCheck size={15} /> 批准版本
                      </button>
                    </div>
                  ) : null}
                </div>

                {selectedVersion ? (
                  <>
                    <div className="identity-grid">
                      <IdentityItem label="固定身份描述" value={String((selectedVersion.identityJson ?? selectedVersion.identity_json)?.fixedIdentity ?? "未填写")} wide />
                      <IdentityItem label="发型" value={String((selectedVersion.identityJson ?? selectedVersion.identity_json)?.hairstyle ?? "未填写")} />
                      <IdentityItem label="五官与特征" value={String((selectedVersion.identityJson ?? selectedVersion.identity_json)?.facialFeatures ?? "未填写")} />
                      <IdentityItem label="默认服装" value={String((selectedVersion.identityJson ?? selectedVersion.identity_json)?.defaultOutfit ?? "未填写")} />
                      <IdentityItem label="允许变化" value={String((selectedVersion.identityJson ?? selectedVersion.identity_json)?.allowedVariations ?? "未填写")} />
                    </div>
                    <div className="asset-heading">
                      <div>
                        <p className="eyebrow">参考集合</p>
                        <h3>{assets.length} 项素材</h3>
                        <select aria-label="素材分类" value={assetFilter} onChange={event => setAssetFilter(event.target.value)}><option value="all">全部分类</option>{Object.entries(assetLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
                      </div>
                      <p>参考优先级：标准正脸 → 当前景别参考 → 可选参考视频</p>
                    </div>
                    {assets.length ? (
                      <div className="asset-grid">
                        {assets.filter(asset => assetFilter === "all" || (asset.assetType ?? asset.asset_type) === assetFilter).map((asset) => (
                          <AssetCard asset={asset} key={asset.id} locked={(selectedVersion.approvalState ?? selectedVersion.approval_state) === "approved"} onReview={async (isPrimary, status) => {
                            try { await request(`/api/assets/${asset.id}/review`, { method: "POST", body: JSON.stringify({ status, is_primary_reference: isPrimary, reason: isPrimary ? "用户设定标准脸并确认身份参考" : "用户在素材库逐张审核" }) }); await loadDetail(selected.id); notify("素材审核已保存", "success"); } catch (cause) { notify(String(cause), "error"); }
                          }} />
                        ))}
                      </div>
                    ) : (
                      <EmptyState
                        title="这个版本还没有参考素材"
                        description="真实 H3 生成前至少需要一张已审核、非占位的标准正脸图。"
                        action={<button className="button button-primary button-small" type="button" onClick={() => setUploadOpen(true)}>上传标准正脸</button>}
                      />
                    )}
                  </>
                ) : (
                  <EmptyState
                    title="尚无人物版本"
                    description="创建首个身份版本，固定人物描述、外形、服装和允许变化范围。"
                    action={<button className="button button-primary" type="button" onClick={openNewVersion}>创建 v1</button>}
                  />
                )}
              </>
            ) : (
              <EmptyState title="选择一个人物" description="人物详情、版本与素材会显示在这里。" />
            )}
          </article>
        </section>
      ) : null}

      {createOpen ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => !busy && setCreateOpen(false)}>
          <form className="dialog-card form-dialog" onSubmit={createCharacter} onMouseDown={(event) => event.stopPropagation()}>
            <button className="icon-button dialog-close" type="button" onClick={() => setCreateOpen(false)} aria-label="关闭"><X size={18} /></button>
            <p className="eyebrow">人物档案</p>
            <h2>新建原创人物</h2>
            <FormField label="稳定人物 ID" required hint="创建后不应更改，例如 hero_001。">
              <input value={newCharacter.slug} onChange={(event) => setNewCharacter((value) => ({ ...value, slug: event.target.value }))} pattern="[a-z0-9_-]+" maxLength={64} required />
            </FormField>
            <FormField label="角色名" required>
              <input value={newCharacter.displayName} onChange={(event) => setNewCharacter((value) => ({ ...value, displayName: event.target.value }))} required />
            </FormField>
            <FormField label="说明">
              <textarea rows={3} value={newCharacter.description} onChange={(event) => setNewCharacter((value) => ({ ...value, description: event.target.value }))} />
            </FormField>
            <FormField label="标签" hint="用英文逗号分隔。">
              <input value={newCharacter.tags} onChange={(event) => setNewCharacter((value) => ({ ...value, tags: event.target.value }))} />
            </FormField>
            <div className="dialog-actions">
              <button className="button button-secondary" type="button" onClick={() => setCreateOpen(false)} disabled={!!busy}>取消</button>
              <button className="button button-primary" type="submit" disabled={busy === "create"}>
                {busy === "create" ? <LoaderCircle className="spin" size={16} /> : <CirclePlus size={16} />}
                创建人物
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {versionOpen ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => !busy && setVersionOpen(false)}>
          <form className="dialog-card form-dialog wide-dialog" onSubmit={createVersion} onMouseDown={(event) => event.stopPropagation()}>
            <button className="icon-button dialog-close" type="button" onClick={() => setVersionOpen(false)} aria-label="关闭"><X size={18} /></button>
            <p className="eyebrow">不可变版本</p>
            <h2>创建新身份版本</h2>
            <p>新版本不会覆盖历史批准版本；已存在的项目仍可继续绑定旧版本。</p>
            <div className="editor-form-grid">
              <div className="form-span-2">
                <FormField label="固定身份描述" required>
                  <textarea rows={4} value={identity.fixedIdentity} onChange={(event) => setIdentity((value) => ({ ...value, fixedIdentity: event.target.value }))} required />
                </FormField>
              </div>
              <FormField label="发型"><input value={identity.hairstyle} onChange={(event) => setIdentity((value) => ({ ...value, hairstyle: event.target.value }))} /></FormField>
              <FormField label="五官与可辨认特征"><input value={identity.facialFeatures} onChange={(event) => setIdentity((value) => ({ ...value, facialFeatures: event.target.value }))} /></FormField>
              <FormField label="体型"><input value={identity.bodyType} onChange={(event) => setIdentity((value) => ({ ...value, bodyType: event.target.value }))} /></FormField>
              <FormField label="默认服装"><input value={identity.defaultOutfit} onChange={(event) => setIdentity((value) => ({ ...value, defaultOutfit: event.target.value }))} /></FormField>
              <FormField label="禁止变化"><textarea rows={3} value={identity.immutableTraits} onChange={(event) => setIdentity((value) => ({ ...value, immutableTraits: event.target.value }))} /></FormField>
              <FormField label="允许变化"><textarea rows={3} value={identity.allowedVariations} onChange={(event) => setIdentity((value) => ({ ...value, allowedVariations: event.target.value }))} /></FormField>
            </div>
            <div className="dialog-actions">
              <button className="button button-secondary" type="button" onClick={() => setVersionOpen(false)} disabled={!!busy}>取消</button>
              <button className="button button-primary" type="submit" disabled={busy === "version"}>
                {busy === "version" ? <LoaderCircle className="spin" size={16} /> : <CirclePlus size={16} />}
                创建独立版本
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {uploadOpen && selectedVersion ? (
        <div className="dialog-backdrop" role="presentation" onMouseDown={() => !busy && setUploadOpen(false)}>
          <form className="dialog-card form-dialog" onSubmit={uploadAsset} onMouseDown={(event) => event.stopPropagation()}>
            <button className="icon-button dialog-close" type="button" onClick={() => setUploadOpen(false)} aria-label="关闭"><X size={18} /></button>
            <p className="eyebrow">v{selectedVersion.version} 参考集合</p>
            <h2>上传人物素材</h2>
            <FormField label="素材类型" required>
              <select value={assetType} onChange={(event) => setAssetType(event.target.value)}>
                {Object.entries(assetLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
              </select>
            </FormField>
            <div className="drop-zone compact" role="button" tabIndex={0} onClick={() => fileInput.current?.click()} onKeyDown={(event) => (event.key === "Enter" || event.key === " ") && fileInput.current?.click()}>
              <Upload size={22} />
              <strong>{assetFile?.name ?? "选择图片、视频或音频"}</strong>
              <span>服务端会验证 MIME、文件签名、大小并计算 sha256。</span>
              <input ref={fileInput} className="sr-only" type="file" accept="image/*,video/*,audio/*" onChange={(event) => setAssetFile(event.target.files?.[0] ?? null)} />
            </div>
            <FormField label="标签"><input value={assetTags} onChange={(event) => setAssetTags(event.target.value)} /></FormField>
            <label className="check-row">
              <input type="checkbox" checked={assetPrimary} onChange={(event) => setAssetPrimary(event.target.checked)} />
              <span><strong>设为主要身份参考</strong><small>每个版本应只有一项主要身份参考。</small></span>
            </label>
            <label className="check-row">
              <input type="checkbox" checked={assetApprove} onChange={(event) => setAssetApprove(event.target.checked)} />
              <span><strong>上传后标记为已人工审核</strong><small>仅在你已确认素材归属、质量与使用权时勾选；真实生成要求主参考已批准。</small></span>
            </label>
            <div className="dialog-actions">
              <button className="button button-secondary" type="button" onClick={() => setUploadOpen(false)} disabled={!!busy}>取消</button>
              <button className="button button-primary" type="submit" disabled={!assetFile || busy === "upload"}>
                {busy === "upload" ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
                上传并校验
              </button>
            </div>
          </form>
        </div>
      ) : null}

      <ConfirmDialog
        open={approveOpen}
        title="批准这个人物版本？"
        description={assets.length ? "批准后该版本可以绑定分镜。后续修改身份资料时必须创建新版本，不能覆盖本版本。" : "此版本还没有参考素材。服务端可能拒绝批准；真实 H3 生成一定会要求已审核且非占位的主参考图。"}
        confirmLabel="批准版本"
        busy={busy === "approve"}
        onConfirm={approveVersion}
        onCancel={() => setApproveOpen(false)}
      />
    </>
  );
}

function CharacterAvatar({ character, large = false }: { character: Character; large?: boolean }) {
  const url = api.mediaUrl(character.avatarUrl ?? character.avatar_url);
  return (
    <span className={`character-avatar${large ? " large" : ""}`}>
      {url ? <img src={url} alt={`${character.displayName}头像`} /> : <UserRound size={large ? 32 : 21} aria-hidden="true" />}
    </span>
  );
}

function IdentityItem({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={`identity-item${wide ? " wide" : ""}`}>
      <span>{label}</span>
      <p>{value}</p>
    </div>
  );
}

function AssetCard({ asset, locked, onReview }: { asset: CharacterAsset; locked: boolean; onReview: (primary: boolean, status: string) => void }) {
  const url = api.mediaUrl(asset.mediaUrl ?? asset.media_url ?? asset.relativePath ?? asset.relative_path, asset.id);
  const mime = asset.mimeType ?? asset.mime_type ?? "";
  const type = asset.assetType ?? asset.asset_type ?? "reference";
  return (
    <article className="asset-card">
      <div className="asset-preview">
        {mime.startsWith("image/") && url ? <img src={url} alt={`${assetLabels[type] ?? type}参考`} /> : mime.startsWith("video/") && url ? <video src={url} muted preload="metadata" aria-label={`${assetLabels[type] ?? type}参考视频`} /> : mime.startsWith("video/") ? <FileVideo2 size={28} /> : <ImageIcon size={28} />}
        {asset.placeholder ? <span className="asset-watermark">PLACEHOLDER</span> : null}
      </div>
      <div className="asset-card-body">
        {url ? <a className="text-link" href={url} target="_blank" rel="noreferrer">查看原始大图 / 媒体</a> : null}
        <div className="title-with-badge">
          <strong>{assetLabels[type] ?? type}</strong>
          {asset.isPrimaryReference ?? asset.is_primary_reference ? <Badge tone="primary">主要参考</Badge> : null}
        </div>
        <span>{asset.width && asset.height ? `${asset.width} × ${asset.height}` : mime || "未知格式"}</span>
        <span>SHA256 {asset.sha256?.slice(0, 10) ?? "等待计算"}…</span>
        <div className="tag-row">
          {(asset.tags ?? asset.tags_json ?? []).map((tag) => <span className="mini-tag" key={tag}>{tag}</span>)}
        </div>
        <div className="asset-footer">
          <Badge status={asset.status} />
          <time dateTime={asset.createdAt ?? asset.created_at}>{formatDate(asset.createdAt ?? asset.created_at)}</time>
        </div>
        {!locked ? <div className="asset-review-actions"><button className="button button-secondary button-small" onClick={() => onReview(false, "approved")}>审核通过</button><button className="button button-secondary button-small" onClick={() => onReview(true, "approved")}>设为标准脸</button><button className="button button-ghost button-small" onClick={() => onReview(false, "needs_review")}>需复核</button></div> : <p className="form-hint">已锁定；替换素材需新建角色版本。</p>}
      </div>
    </article>
  );
}
