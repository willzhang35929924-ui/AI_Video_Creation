import {
  ArrowDown,
  ArrowUp,
  Check,
  CheckCheck,
  ChevronRight,
  CirclePlus,
  ClipboardPaste,
  Copy,
  FileText,
  GripVertical,
  Link2,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Save,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, api } from "../lib/api";
import type { Character, ImportResult, Project, ScriptRevision, StoryboardShot } from "../lib/types";
import { formatDate, statusLabel } from "../lib/format";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ProjectPicker } from "../components/ProjectPicker";
import { useToast } from "../components/ToastProvider";
import { Badge, EmptyState, ErrorState, FormField, LoadingState, PageHeader } from "../components/ui";
import { ShotWorkspace } from '../components/ShotWorkspace';
import { WordImportPanel } from '../components/WordImportPanel';
import { DeleteRecordsDialog } from '../components/DeleteRecordsDialog';

type ImportMode = "file" | "text" | "url";

const blankShot = (order: number): Partial<StoryboardShot> => ({
  order,
  sceneTitle: `新分镜 ${order}`,
  narration: "",
  dialogue: "",
  visualPrompt: "",
  negativePrompt: "",
  action: "",
  cameraMove: "",
  location: "",
  lighting: "",
  audioDirection: "",
  durationSeconds: 5,
  aspectRatio: "9:16",
});

export function StoryboardPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(searchParams.get("project") ?? "");
  const [script, setScript] = useState<ScriptRevision | null>(null);
  const [shots, setShots] = useState<StoryboardShot[]>([]);
  const [characters, setCharacters] = useState<Character[]>([]);
  const [selectedShotId, setSelectedShotId] = useState("");
  const [draft, setDraft] = useState<StoryboardShot | null>(null);
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [importOpen, setImportOpen] = useState(searchParams.get("import") === "1");
  const [wordOpen, setWordOpen] = useState(false);
  const [includeHistory,setIncludeHistory]=useState(false);
  const [sceneFilter,setSceneFilter]=useState('');
  const [structuredDirty,setStructuredDirty]=useState(false);
  const [deleteIds, setDeleteIds] = useState<string[] | null>(null);
  const [importMode, setImportMode] = useState<ImportMode>("file");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importText, setImportText] = useState("");
  const [importUrl, setImportUrl] = useState("");
  const [newProjectName, setNewProjectName] = useState(searchParams.get("name") ?? "我的项目");
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [confirmAction, setConfirmAction] = useState<"reparse" | "delete" | null>(null);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { notify } = useToast();

  const selectedProject = projects.find((project) => project.id === projectId);
  const selectedShot = shots.find((shot) => shot.id === selectedShotId) ?? null;
  const visibleShots=shots.filter(shot=>!sceneFilter||shot.scene_id===sceneFilter);
  const sceneOptions=Array.from(new Map(shots.filter(s=>s.scene_id).map(s=>[s.scene_id,{id:s.scene_id!,name:s.scene_name||s.location||'未命名场景',count:shots.filter(x=>x.scene_id===s.scene_id).length}])).values());
  const boundVersion = characters.find(character => character.id === draft?.characterId)?.versions?.find(version => version.id === draft?.characterAssetVersionId);
  const boundAssets = (boundVersion?.assets || []).filter(asset => draft?.reference_asset_ids_json?.includes(asset.id));

  const characterOptions = useMemo(
    () =>
      characters.flatMap((character) => {
        const versions = character.versions?.length
          ? character.versions
          : character.currentVersion
            ? [character.currentVersion]
            : [];
        return versions
          .filter((version) => (version.approvalState ?? version.approval_state) === "approved")
          .map((version) => ({
            value: `${character.id}::${version.id}`,
            characterId: character.id,
            versionId: version.id,
            label: `${character.displayName} · v${version.version} · ${character.id}`,
          }));
      }),
    [characters],
  );

  const loadProjects = useCallback(async () => {
    const nextProjects = await api.listProjects();
    setProjects(nextProjects);
    setProjectId((current) => current || nextProjects[0]?.id || "");
    return nextProjects;
  }, []);

  const loadProjectDetail = useCallback(async (id: string,history=false) => {
    if (!id) {
      setScript(null);
      setShots([]);
      setSelectedShotId("");
      setDetailLoading(false);
      return;
    }
    setDetailLoading(true);
    setError("");
    const [scriptResult, shotsResult, charactersResult] = await Promise.allSettled([
      api.getScript(id),
      api.listShots(id,undefined,history),
      api.listCharacters().then((rows) => Promise.all(rows.map((character) => api.getCharacter(character.id)))),
    ]);
    if (scriptResult.status === "fulfilled") {
      setScript(scriptResult.value);
    } else if (scriptResult.reason instanceof ApiError && scriptResult.reason.status === 404) {
      setScript(null);
    } else {
      setError(scriptResult.reason instanceof Error ? scriptResult.reason.message : "脚本加载失败");
    }
    if (shotsResult.status === "fulfilled") {
      setShots(shotsResult.value);
      setSelectedShotId((current) =>
        shotsResult.value.some((shot) => shot.id === current) ? current : shotsResult.value[0]?.id ?? "",
      );
    } else {
      setShots([]);
      setError(shotsResult.reason instanceof Error ? shotsResult.reason.message : "分镜加载失败");
    }
    if (charactersResult.status === "fulfilled") setCharacters(charactersResult.value);
    setDetailLoading(false);
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = await loadProjects();
      const requested = searchParams.get("project");
      const target = requested && next.some((project) => project.id === requested) ? requested : next[0]?.id ?? "";
      setProjectId(target);
      if (target) await loadProjectDetail(target);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法加载项目");
    } finally {
      setLoading(false);
    }
  }, [loadProjectDetail, loadProjects, searchParams]);

  useEffect(() => {
    void loadAll();
    // Initial load only; subsequent query updates are controlled locally.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (selectedShot) {
      setDraft({ ...selectedShot });
      setDirty(false);
      setRejectOpen(false);
      setRejectReason("");
    } else {
      setDraft(null);
    }
  }, [selectedShot]);

  function selectProject(nextId: string) {
    if ((dirty||structuredDirty) && !window.confirm("当前分镜有未保存修改，切换项目会丢失这些修改。仍要继续吗？")) return;
    setIncludeHistory(false);setSceneFilter('');
    setProjectId(nextId);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("project", nextId);
    nextParams.delete("import");
    setSearchParams(nextParams, { replace: true });
    void loadProjectDetail(nextId);
  }

  function updateDraft<Key extends keyof StoryboardShot>(key: Key, value: StoryboardShot[Key]) {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
    setDirty(true);
  }

  async function saveShot() {
    if (!draft) return;
    if (!draft.sceneTitle.trim() || !draft.visualPrompt.trim()) {
      notify("场景标题和画面提示词不能为空", "error");
      return;
    }
    setBusyAction("save");
    try {
      const saved = await api.updateShot(draft.id, draft);
      setShots((current) => current.map((shot) => (shot.id === saved.id ? saved : shot)));
      setDraft(saved);
      setDirty(false);
      notify(saved.status === "needs_review" ? "已保存；已批准内容变更后需要重新审核" : "分镜已保存", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "保存失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function addShot(copyFrom?: StoryboardShot) {
    if (!projectId) return;
    setBusyAction("create");
    try {
      const input = copyFrom
        ? { ...copyFrom, id: undefined, order: shots.length + 1, sceneTitle: `${copyFrom.sceneTitle}（副本）`, status: "draft" }
        : blankShot(shots.length + 1);
      const created = copyFrom ? await api.duplicateShot(copyFrom.id) : await api.createShot(projectId, input);
      setShots((current) => [...current, created]);
      setSelectedShotId(created.id);
      notify(copyFrom ? "分镜副本已创建" : "新分镜已创建", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "创建分镜失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function deleteShot() {
    if (!selectedShot) return;
    setBusyAction("delete");
    try {
      await api.deleteShot(selectedShot.id);
      const next = shots.filter((shot) => shot.id !== selectedShot.id);
      setShots(next);
      setSelectedShotId(next[0]?.id ?? "");
      setConfirmAction(null);
      notify("分镜已删除", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "删除失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function reviewShot(decision: "approved" | "rejected") {
    if (!selectedShot) return;
    if (dirty||structuredDirty) {
      notify("请先保存修改，再执行审核", "error");
      return;
    }
    if (decision === "rejected" && !rejectReason.trim()) {
      setRejectOpen(true);
      return;
    }
    setBusyAction(decision);
    try {
      const reviewed = await api.approveShot(selectedShot.id, decision, rejectReason.trim() || undefined);
      setShots((current) => current.map((shot) => (shot.id === reviewed.id ? reviewed : shot)));
      setRejectOpen(false);
      setRejectReason("");
      notify(decision === "approved" ? "分镜已通过审核" : "分镜已驳回", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "审核操作失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function approveScript() {
    if (!projectId) return;
    setBusyAction("approve-script");
    try {
      const result = await api.approveScript(projectId);
      setScript(result);
      notify("脚本已通过审核", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "脚本审核失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function approveAll() {
    if (!projectId || dirty) {
      if (dirty) notify("请先保存当前分镜修改", "error");
      return;
    }
    setBusyAction("approve-all");
    try {
      await api.approveAllShots(projectId);
      await loadProjectDetail(projectId);
      notify("全部分镜已审核通过", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "批量审核失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function assignCharacter(value: string) {
    if (!selectedShot || !value) return;
    const option = characterOptions.find((item) => item.value === value);
    if (!option) return;
    setBusyAction("assign");
    try {
      const updated = await api.assignCharacter(selectedShot.id, option.characterId, option.versionId);
      setShots((current) => current.map((shot) => (shot.id === updated.id ? updated : shot)));
      setDraft(updated);
      notify("人物版本已绑定并持久化", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "人物绑定失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function runParse() {
    if (!projectId) return;
    setBusyAction("parse");
    try {
      await api.parseProject(projectId, true);
      await loadProjectDetail(projectId);
      setConfirmAction(null);
      notify("脚本已解析为结构化分镜", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "解析失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function submitImport(event: React.FormEvent) {
    event.preventDefault();
    if (importMode === "file" && !importFile) return notify("请选择要导入的文件", "error");
    if (importMode === "text" && !importText.trim()) return notify("请粘贴脚本内容", "error");
    if (importMode === "url" && !importUrl.trim()) return notify("请输入在线文档链接", "error");
    if (!projectId && !newProjectName.trim()) return notify("请输入项目名称", "error");

    setBusyAction("import");
    setImportResult(null);
    try {
      const result =
        importMode === "file"
          ? await api.importFile(importFile!, newProjectName.trim())
          : importMode === "text"
            ? await api.importText({ text: importText, projectName: newProjectName.trim() })
            : await api.importUrl({ url: importUrl, projectName: newProjectName.trim() });
      setImportResult(result);
      const resultProject = result.project;
      const nextId = resultProject?.id ?? result.projectId ?? projectId;
      const nextProjects = await loadProjects();
      if (nextId) {
        setProjectId(nextId);
        const nextParams = new URLSearchParams(searchParams);
        nextParams.set("project", nextId);
        nextParams.set("import", "1");
        setSearchParams(nextParams, { replace: true });
        if (!nextProjects.some((project) => project.id === nextId) && resultProject) {
          setProjects((current) => [...current, resultProject]);
        }
        await loadProjectDetail(nextId);
      }
      notify("脚本导入成功，请检查预览后执行解析", "success");
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "导入失败", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function reorderShots(sourceId: string, targetId: string) {
    if (sourceId === targetId) return;
    const sourceIndex = shots.findIndex((shot) => shot.id === sourceId);
    const targetIndex = shots.findIndex((shot) => shot.id === targetId);
    if (sourceIndex < 0 || targetIndex < 0) return;
    const reordered = [...shots];
    const [moved] = reordered.splice(sourceIndex, 1);
    reordered.splice(targetIndex, 0, moved);
    const withOrder = reordered.map((shot, index) => ({ ...shot, order: index + 1 }));
    setShots(withOrder);
    setBusyAction("reorder");
    try {
      const changed = withOrder.filter((shot, index) => shots[index]?.id !== shot.id || shots[index]?.order !== shot.order);
      const persisted = await Promise.all(changed.map((shot) => api.updateShot(shot.id, { order: shot.order })));
      setShots((current) =>
        current.map((shot) => persisted.find((item) => item.id === shot.id) ?? shot).sort((a, b) => a.order - b.order),
      );
      notify("分镜顺序已保存", "success");
    } catch (cause) {
      await loadProjectDetail(projectId);
      notify(cause instanceof Error ? cause.message : "排序保存失败，已恢复服务器顺序", "error");
    } finally {
      setBusyAction("");
    }
  }

  async function moveShot(id: string, delta: number) {
    const index = shots.findIndex((shot) => shot.id === id);
    const target = shots[index + delta];
    if (!target) return;
    await reorderShots(id, target.id);
  }

  if (loading) return <LoadingState label="正在加载脚本与分镜…" />;

  return (
    <>
      <PageHeader
        eyebrow="创作流程 · 01"
        title="脚本与分镜"
        description="原文始终保留；结构化分镜经人工审核后才允许进入生成。"
        actions={
          <>
            <ProjectPicker projects={projects} value={projectId} onChange={selectProject} />
            <button className="button button-primary" type="button" onClick={() => setWordOpen(true)}>导入 Word 剧本</button>
            <a className="button button-ghost" href="/content?resource=shots">批量管理</a>
            <button className="button button-secondary" type="button" onClick={() => setImportOpen(true)}>
              <Upload size={17} />
              导入脚本
            </button>
          </>
        }
      />

      {error ? <ErrorState message={error} onRetry={loadAll} /> : null}
      {wordOpen ? <WordImportPanel onClose={() => setWordOpen(false)} /> : null}
      {deleteIds ? <DeleteRecordsDialog resource="shots" ids={deleteIds} onClose={() => setDeleteIds(null)} onComplete={() => { void loadProjectDetail(projectId); }} /> : null}

      {!projects.length && !importOpen ? (
        <EmptyState
          title="还没有制作项目"
          description="导入文档或粘贴脚本，系统会创建项目并保留完整原文。"
          action={
            <button className="button button-primary" type="button" onClick={() => setImportOpen(true)}>
              导入第一份脚本
            </button>
          }
        />
      ) : null}

      {importOpen ? (
        <section className="card import-panel" aria-labelledby="import-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">内容来源</p>
              <h2 id="import-heading">导入脚本</h2>
              <p>支持 DOCX、Markdown、纯文本与受控在线链接。</p>
            </div>
            <button className="icon-button" type="button" onClick={() => setImportOpen(false)} aria-label="关闭导入面板">
              <X size={18} />
            </button>
          </div>
          <div className="segmented" role="tablist" aria-label="导入方式">
            {([
              ["file", FileText, "本地文档"],
              ["text", ClipboardPaste, "粘贴文本"],
              ["url", Link2, "在线链接"],
            ] as const).map(([mode, Icon, label]) => (
              <button
                key={mode}
                type="button"
                role="tab"
                aria-selected={importMode === mode}
                className={importMode === mode ? "active" : ""}
                onClick={() => setImportMode(mode)}
              >
                <Icon size={16} /> {label}
              </button>
            ))}
          </div>
          <form className="import-form" onSubmit={submitImport}>
            <FormField label="新项目名称" required hint={projectId ? `导入会新建项目，不覆盖当前项目“${selectedProject?.name}”。` : "导入成功后项目才会持久化，避免留下空记录。"}>
              <input value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} maxLength={120} required />
            </FormField>
            {importMode === "file" ? (
              <div
                className="drop-zone"
                role="button"
                tabIndex={0}
                onClick={() => fileInputRef.current?.click()}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") fileInputRef.current?.click();
                }}
                onDragOver={(event) => event.preventDefault()}
                onDrop={(event) => {
                  event.preventDefault();
                  setImportFile(event.dataTransfer.files[0] ?? null);
                }}
              >
                <Upload size={24} aria-hidden="true" />
                <strong>{importFile ? importFile.name : "选择或拖入脚本文档"}</strong>
                <span>{importFile ? `${(importFile.size / 1024).toFixed(1)} KB` : ".docx、.md、.txt，最大大小由本地服务配置"}</span>
                <input
                  ref={fileInputRef}
                  className="sr-only"
                  type="file"
                  accept=".docx,.md,.txt,text/plain,text/markdown,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  onChange={(event) => setImportFile(event.target.files?.[0] ?? null)}
                />
              </div>
            ) : importMode === "text" ? (
              <FormField label="原始脚本" required hint="原文会完整保存，解析不会覆盖这里的内容。">
                <textarea rows={8} value={importText} onChange={(event) => setImportText(event.target.value)} placeholder="粘贴项目标题、分镜、剧情与画面提示词…" required />
              </FormField>
            ) : (
              <FormField
                label="在线文档 URL"
                required
                hint="仅允许公开 http/https 文档；私有文档需配置官方 API/OAuth，禁止绕过登录。"
              >
                <input type="url" value={importUrl} onChange={(event) => setImportUrl(event.target.value)} placeholder="https://…" required />
              </FormField>
            )}
            <div className="form-actions">
              <button className="button button-primary" type="submit" disabled={busyAction === "import"}>
                {busyAction === "import" ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}
                {busyAction === "import" ? "正在导入…" : "导入并保留原文"}
              </button>
            </div>
          </form>
          {importResult ? (
            <div className="import-result" role="status">
              <div>
                <Check size={18} />
                <strong>导入完成</strong>
                <span>{importResult.sourceFilename ?? importResult.source_filename ?? importMode}</span>
              </div>
              <p>{importResult.textPreview ?? importResult.text_preview ?? "内容已保存在项目原文中。"}</p>
              {(importResult.warnings ?? []).map((warning) => (
                <p className="warning-copy" key={warning}>
                  {warning}
                </p>
              ))}
              <button className="button button-primary" type="button" onClick={runParse} disabled={busyAction === "parse"}>
                {busyAction === "parse" ? <LoaderCircle className="spin" size={17} /> : <ChevronRight size={17} />}
                解析为分镜
              </button>
            </div>
          ) : null}
        </section>
      ) : null}

      {projectId ? (
        <section className="workflow-toolbar card">
          <div className="script-status-block">
            <span>脚本修订</span>
            <strong>v{script?.revision ?? 0}</strong>
            <Badge status={script?.status ?? "draft"} />
            <span className="muted">{script?.createdAt ?? script?.created_at ? formatDate(script?.createdAt ?? script?.created_at) : "尚未解析"}</span>
          </div>
          <div className="toolbar-actions">
            <button className="button button-ghost" type="button" onClick={() => setConfirmAction("reparse")} disabled={!script || !!busyAction}>
              <RotateCcw size={16} />
              重新解析
            </button>
            <button className="button button-secondary" type="button" onClick={approveScript} disabled={!script || script.status === "approved" || !!busyAction}>
              {busyAction === "approve-script" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
              审核脚本
            </button>
            <button className="button button-primary" type="button" onClick={approveAll} disabled={!shots.length || script?.status !== "approved" || !!busyAction}>
              {busyAction === "approve-all" ? <LoaderCircle className="spin" size={16} /> : <CheckCheck size={16} />}
              全量审核分镜
            </button>
          </div>
        </section>
      ) : null}

      {projectId && detailLoading ? <LoadingState label="正在读取项目内容…" /> : null}

      {projectId && !detailLoading ? (
        <section className="storyboard-layout">
          <aside className="card shot-sidebar" aria-label="分镜列表">
            <div className="section-heading compact">
              <div>
                <p className="eyebrow">镜头列表</p>
                <h2>{shots.length} 个分镜</h2>
              </div>
              <button className="icon-button" type="button" onClick={() => addShot()} disabled={!!busyAction} aria-label="添加分镜">
                <CirclePlus size={19} />
              </button>
            </div>
            <label className="form-field"><span>场景与分镜</span><select aria-label="按场景筛选分镜" value={sceneFilter} onChange={e=>{if((dirty||structuredDirty)&&!window.confirm('当前修改尚未保存，切换场景会丢失修改。仍要继续吗？'))return;setSceneFilter(e.target.value);const first=shots.find(s=>!e.target.value||s.scene_id===e.target.value);if(first)setSelectedShotId(first.id);}}><option value="">全部场景 · {shots.length} 镜</option>{sceneOptions.map(s=><option key={s.id} value={s.id}>{s.name} · {s.count} 镜</option>)}</select></label>
            <label><input type="checkbox" checked={includeHistory} onChange={e=>{if((dirty||structuredDirty)&&!window.confirm('当前修改尚未保存，加载历史修订会丢失修改。仍要继续吗？'))return;setIncludeHistory(e.target.checked);setSceneFilter('');void loadProjectDetail(projectId,e.target.checked);}}/>包含历史剧本修订的分镜</label>
            {shots.length ? (
              <ol className="shot-list">
                {visibleShots.map((shot) => (
                  <li
                    key={shot.id}
                    draggable={!busyAction}
                    onDragStart={() => setDraggedId(shot.id)}
                    onDragOver={(event) => event.preventDefault()}
                    onDrop={() => {
                      if (draggedId) void reorderShots(draggedId, shot.id);
                      setDraggedId(null);
                    }}
                    className={draggedId === shot.id ? "dragging" : ""}
                  >
                    <button
                      className={`shot-item${shot.id === selectedShotId ? " active" : ""}`}
                      type="button"
                      onClick={() => {
                        if ((dirty||structuredDirty) && !window.confirm("当前修改尚未保存，切换分镜会丢失修改。仍要继续吗？")) return;
                        setSelectedShotId(shot.id);
                      }}
                    >
                      <GripVertical className="drag-handle" size={16} aria-hidden="true" />
                      <span className="shot-number">{String(shot.order).padStart(2, "0")}</span>
                      <span className="shot-copy">
                        <strong>{shot.sceneTitle}</strong>
                        <small>{shot.scene_name||shot.location} · {shot.durationSeconds} 秒 · {shot.participants?.map(p=>p.name).join('、')||'人物待添加'}</small>
                      </span>
                      <Badge status={shot.status} />
                    </button>
                    <div className="shot-reorder-actions" aria-label={`调整${shot.sceneTitle}顺序`}>
                      <button type="button" onClick={() => moveShot(shot.id, -1)} disabled={shot.order === 1 || !!busyAction} aria-label="上移">
                        <ArrowUp size={13} />
                      </button>
                      <button type="button" onClick={() => moveShot(shot.id, 1)} disabled={shot.order === shots.length || !!busyAction} aria-label="下移">
                        <ArrowDown size={13} />
                      </button>
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState
                title="尚无分镜"
                description="导入并解析脚本，或手动添加第一条分镜。"
                action={
                  <button className="button button-secondary button-small" type="button" onClick={() => addShot()}>
                    添加分镜
                  </button>
                }
              />
            )}
          </aside>

          <article className="card shot-editor">
            {draft ? (
              <>
                <div className="editor-heading">
                  <div>
                    <div className="editor-title-row">
                      <span className="shot-number large">{String(draft.order).padStart(2, "0")}</span>
                      <div>
                        <h2>{draft.sceneTitle || "未命名分镜"}</h2>
                        <p>版本 v{draft.revision} · {statusLabel(draft.status)}</p>
                      </div>
                    </div>
                  </div>
                  <div className="editor-actions">
                    <button className="button button-ghost button-small" type="button" onClick={() => addShot(draft)} disabled={!!busyAction}>
                      <Copy size={15} /> 复制
                    </button>
                    <button className="button button-ghost-danger button-small" type="button" onClick={() => setDeleteIds([draft.id])} disabled={!!busyAction}>
                      <Trash2 size={15} /> 删除
                    </button>
                    <button className="button button-primary button-small" type="button" onClick={saveShot} disabled={!dirty || !!busyAction}>
                      {busyAction === "save" ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
                      {busyAction === "save" ? "保存中…" : dirty ? "保存修改" : "已保存"}
                    </button>
                  </div>
                </div>

                {draft.status === "approved" && dirty ? (
                  <div className="inline-notice warning" role="alert">
                    修改已批准分镜会创建新修订，并把关联视频标记为需要重新生成。
                  </div>
                ) : null}

                <div className="editor-form-grid">
                  <FormField label="场景标题" required>
                    <input value={draft.sceneTitle} onChange={(event) => updateDraft("sceneTitle", event.target.value)} />
                  </FormField>
                  <FormField label="画幅">
                    <select value={draft.aspectRatio} onChange={(event) => updateDraft("aspectRatio", event.target.value)}>
                      <option value="9:16">9:16 竖屏</option>
                      <option value="16:9">16:9 横屏</option>
                      <option value="1:1">1:1 方形</option>
                    </select>
                  </FormField>
                  <FormField label="时长（秒）">
                    <input
                      type="number"
                      min={1}
                      max={30}
                      step={0.5}
                      value={draft.durationSeconds}
                      onChange={(event) => updateDraft("durationSeconds", Number(event.target.value))}
                    />
                  </FormField>
                  <FormField label="快速绑定主人物 兼容旧流程" hint="多人镜头请使用下方人物参与区域，分别绑定身份版本和素材。">
                    <select
                      value={
                        draft.characterId && draft.characterAssetVersionId
                          ? `${draft.characterId}::${draft.characterAssetVersionId}`
                          : ""
                      }
                      onChange={(event) => void assignCharacter(event.target.value)}
                      disabled={busyAction === "assign"}
                    >
                      <option value="">未绑定</option>
                      {characterOptions.map((option) => (
                        <option value={option.value} key={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </FormField>
                  <FormField label="剧情 / 旁白">
                    <textarea rows={3} value={draft.narration} onChange={(event) => updateDraft("narration", event.target.value)} />
                  </FormField>
                  {boundAssets.length ? <div className="form-span-2 reference-preview" aria-label="锁定人物参考">{boundAssets.map((asset, index) => <figure key={asset.id}><img src={api.mediaUrl(asset.mediaUrl || asset.relativePath)} alt={`锁定参考 Picture ${index + 1}`} /><figcaption>{draft.characterId} · v{boundVersion?.version} · {asset.id}</figcaption></figure>)}</div> : null}
                  <FormField label="台词">
                    <textarea rows={3} value={draft.dialogue} onChange={(event) => updateDraft("dialogue", event.target.value)} />
                  </FormField>
                  <div className="form-span-2">
                    <FormField label="画面提示词" required>
                      <textarea rows={5} value={draft.visualPrompt} onChange={(event) => updateDraft("visualPrompt", event.target.value)} />
                    </FormField>
                  </div>
                  <div className="form-span-2">
                    <FormField label="负面提示词">
                      <textarea rows={3} value={draft.negativePrompt} onChange={(event) => updateDraft("negativePrompt", event.target.value)} />
                    </FormField>
                  </div>
                  <FormField label="人物动作">
                    <input value={draft.action} onChange={(event) => updateDraft("action", event.target.value)} />
                  </FormField>
                  <FormField label="镜头运动">
                    <input value={draft.cameraMove} onChange={(event) => updateDraft("cameraMove", event.target.value)} />
                  </FormField>
                  <FormField label="场景位置">
                    <input value={draft.location} onChange={(event) => updateDraft("location", event.target.value)} />
                  </FormField>
                  <FormField label="光线与氛围">
                    <input value={draft.lighting} onChange={(event) => updateDraft("lighting", event.target.value)} />
                  </FormField>
                  <div className="form-span-2">
                    <FormField label="声音方向">
                      <input value={draft.audioDirection} onChange={(event) => updateDraft("audioDirection", event.target.value)} />
                    </FormField>
                  </div>
                </div>

                <ShotWorkspace shotId={draft.id} projectId={projectId} disabled={dirty} onDirtyChange={setStructuredDirty} onSaved={async () => { setShots(await api.listShots(projectId,undefined,includeHistory)); }} />
                <div className="review-bar">
                  <div>
                    <span className={`save-dot${dirty ? " dirty" : ""}`} aria-hidden="true" />
                    {dirty ? "有未保存修改" : `已保存 · v${draft.revision}`}
                  </div>
                  <div className="review-actions">
                    {rejectOpen ? (
                      <div className="reject-inline">
                        <label>
                          <span className="sr-only">驳回原因</span>
                          <input
                            autoFocus
                            value={rejectReason}
                            onChange={(event) => setRejectReason(event.target.value)}
                            placeholder="填写驳回原因（必填）"
                          />
                        </label>
                        <button className="button button-danger button-small" type="button" onClick={() => reviewShot("rejected")} disabled={!rejectReason.trim() || !!busyAction}>
                          确认驳回
                        </button>
                        <button className="icon-button" type="button" onClick={() => setRejectOpen(false)} aria-label="取消驳回">
                          <X size={16} />
                        </button>
                      </div>
                    ) : (
                      <button className="button button-secondary" type="button" onClick={() => setRejectOpen(true)} disabled={dirty || !!busyAction}>
                        驳回
                      </button>
                    )}
                    <button className="button button-primary" type="button" onClick={() => reviewShot("approved")} disabled={dirty || draft.status === "approved" || !!busyAction || script?.status !== "approved"}>
                      {busyAction === "approved" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                      通过此分镜
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <EmptyState title="选择一条分镜" description="从左侧选择分镜查看和编辑完整生成参数。" />
            )}
          </article>
        </section>
      ) : null}

      <ConfirmDialog
        open={confirmAction === "reparse"}
        title="重新解析脚本？"
        description="重新解析会创建新的剧本修订与分镜，保留原有分镜、人物关系和生成历史。请先保存当前修改。"
        confirmLabel="继续解析"
        busy={busyAction === "parse"}
        onConfirm={runParse}
        onCancel={() => setConfirmAction(null)}
      />
      <ConfirmDialog
        open={confirmAction === "delete"}
        title="删除这个分镜？"
        description="删除会影响关联生成任务与视频。服务端将按策略保留审计记录；此操作需要明确确认。"
        confirmLabel="删除分镜"
        tone="danger"
        busy={busyAction === "delete"}
        onConfirm={deleteShot}
        onCancel={() => setConfirmAction(null)}
      />
    </>
  );
}
