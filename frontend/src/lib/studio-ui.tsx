import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { request } from './api';
import type { StudioOverview, StudioProject } from './studio-contract';
import { Badge } from '../components/ui';

const statusLabels: Record<string, string> = {
  draft: '尚未开始', pending: '待处理', queued: '等待执行', running: '正在执行',
  submitting: '正在提交', submitted: '已提交服务', waiting_dependencies: '等待前置素材',
  waiting_assets: '等待参考素材', waiting: '等待前置条件', retrying: '正在重试',
  completed: '执行完成', succeeded: '执行成功', generated: '已生成', approved: '已审核',
  needs_review: '待统一审片', awaiting_review: '待统一审片', waiting_review: '待统一审片',
  ready_for_review: '待统一审片', failed: '执行失败', quality_failed: '技术检查未通过',
  blocked: '等待处理异常', waiting_exception: '等待处理异常', waiting_user: '等待处理异常',
  paused: '已暂停', cancelled: '已取消', unknown: '结果待核对', skipped: '已跳过',
  reused: '复用已有结果', ready: '已就绪', rendering: '正在合成',
  exception: '等待处理异常', dispatching: '正在提交', completed_with_errors: '部分完成，存在异常', studio_paused: '已暂停',
  keep: '保留', exclude: '排除',
};

export const studioStatus = (value?: string | null) => value ? statusLabels[value] || value : '尚无任务';
export function StudioStatus({ status }: { status?: string | null }) {
  const tone = ['completed', 'succeeded', 'generated', 'approved', 'keep', 'reused'].includes(status || '') ? 'success'
    : ['failed', 'quality_failed', 'blocked', 'unknown', 'waiting_exception', 'waiting_user', 'exception', 'completed_with_errors'].includes(status || '') ? 'warning' : 'neutral';
  return <Badge tone={tone}>{studioStatus(status)}</Badge>;
}

export function useStudio() {
  const [params, setParams] = useSearchParams();
  const projectId = params.get('project') || '';
  const [projects, setProjects] = useState<StudioProject[]>([]);
  const [data, setData] = useState<StudioOverview | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [listLoading, setListLoading] = useState(true);
  const projectRef = useRef(projectId);
  projectRef.current = projectId;
  const listRequest = useRef<Promise<void> | null>(null);
  const detailRequests = useRef(new Map<string,Promise<void>>());
  const refreshList = useCallback(() => {
    if(listRequest.current) return listRequest.current;
    const work=(async()=>{
      try { setProjects(await request<StudioProject[]>('/api/studio/projects')); }
      catch(e) { setError(String(e)); }
      finally { setListLoading(false);listRequest.current=null; }
    })();
    listRequest.current=work;return work;
  }, []);
  const refresh = useCallback(() => {
    if (!projectId) return Promise.resolve();
    const active=detailRequests.current.get(projectId);if(active)return active;
    const work=(async()=>{
      try {
        const next = await request<StudioOverview>('/api/studio/projects/' + encodeURIComponent(projectId));
        if (projectRef.current === projectId) { setData(next);setError(''); }
      } catch (e) { if (projectRef.current === projectId) setError(String(e)); }
      finally {
        if (projectRef.current === projectId) setLoading(false);
        detailRequests.current.delete(projectId);
      }
    })();
    detailRequests.current.set(projectId,work);return work;
  }, [projectId]);
  const refreshAfterChange = useCallback(async()=>{
    const active=detailRequests.current.get(projectId);
    if(active)await active;
    await refresh();
  },[projectId,refresh]);
  useEffect(() => { void refreshList(); }, [refreshList]);
  useEffect(() => {
    setData(null); setError(''); setLoading(Boolean(projectId));
    void refresh();
    const timer = window.setInterval(() => { void refresh(); void refreshList(); }, 4000);
    return () => window.clearInterval(timer);
  }, [refresh, refreshList, projectId]);
  const selectProject = useCallback((id: string) => {
    setParams(current => {
      const next = new URLSearchParams(current);
      next.delete('shot');next.delete('video');next.delete('quality_render');
      if (id) next.set('project', id); else next.delete('project');
      return next;
    });
  }, [setParams]);
  return { projectId, projects, data, error, loading, listLoading, selectProject, refresh, refreshAfterChange, refreshList };
}

export function StudioProjectPicker({ projects, projectId, selectedName, onChange, disabled=false }: {
  projects: StudioProject[]; projectId: string; selectedName?: string; onChange: (id: string) => void; disabled?:boolean;
}) {
  return <label className="studio-project-picker"><span>当前项目</span><select disabled={disabled} value={projectId} onChange={e => onChange(e.target.value)}>
    <option value="">选择项目</option>{projectId&&!projects.some(project=>project.id===projectId)?<option value={projectId}>{selectedName || '正在读取项目名称…'}</option>:null}{projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
  </select></label>;
}

export const recordText = (value: unknown): string => typeof value === 'string' || typeof value === 'number' ? String(value) : '';
export const shotTime = (value: number | undefined | null) => Number.isFinite(value) ? `${Number(value).toFixed(2).replace(/\.00$/, '')} 秒` : '—';
export const studioVideoDuration = (video: Record<string,any>|null|undefined) => {
  const duration=Number(video?.duration_seconds ?? video?.metadata_json?.duration_seconds);
  return Number.isFinite(duration)&&duration>0?duration:undefined;
};
export const studioAssetPath = (item: Record<string, unknown>) => {
  if (typeof item.relative_path === 'string') return item.relative_path;
  if (typeof item.reference_path === 'string') return item.reference_path;
  const assets = Array.isArray(item.assets) ? item.assets : Array.isArray(item.visuals) ? item.visuals : [];
  const asset = assets.find((value: Record<string, unknown>) => value.relative_path && !['failed','rejected','cancelled'].includes(String(value.status)));
  return asset ? recordText(asset.relative_path) : '';
};
