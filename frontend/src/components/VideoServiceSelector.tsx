import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { request } from '../lib/api';
import type { ModelServicesOverview, RouteProfile, TaskCandidate } from '../lib/model-services-contract';
import type { AIService } from '../pages/AIServicesPage';
import { FormField } from './ui';

type VideoBinding = { module: string; service_id: string; model_id?: string };
type SelectionState = { busy: boolean; dirty: boolean; contextEnabled: boolean; hasCloud: boolean };
type VideoConfig = { services: AIService[]; binding?: VideoBinding; profile: RouteProfile; candidates: TaskCandidate[]; container: boolean };
const selectionKey = (serviceId: string, modelId: string) => JSON.stringify([serviceId, modelId]);
const hasExplicitRoute = (profile: RouteProfile) => profile.revision > 0 || Boolean(profile.inherited_from && profile.inherited_from !== 'legacy_module_default');

function compatibleVideoModel(service: AIService | undefined, model: string) {
  if (!service?.enabled || !model) return false;
  if (service.provider === 'video_api') return ['video-jobs-v1','wan26-i2v','lk888-h3-v2'].includes(String(service.settings?.video_contract || ''));
  if (service.provider !== 'comfyui' || service.has_api_key) return false;
  try {
    const url = new URL(service.base_url);
    return ['http:', 'https:'].includes(url.protocol) && (service.trusted_local_endpoint === true || (url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)))
      && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash;
  } catch {
    return false;
  }
}

function usesContext(service: AIService | undefined, model: string, container: boolean) {
  if (!compatibleVideoModel(service, model) || service?.provider !== 'comfyui' || !model.toLowerCase().replace(/-/g, '_').startsWith('minimax_h3')) return false;
  if (service.settings?.h3_context_ir === true || service.settings?.workflow_profile === 'context_ir') return true;
  if (service.settings?.workflow_profile || service.settings?.h3_context_ir === false || container) return false;
  try { return ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(service.base_url).hostname); } catch { return false; }
}

export function VideoServiceSelector({ projectId, disabled = false, onStateChange, onSaved }: {
  projectId: string; disabled?: boolean; onStateChange?: (state: SelectionState) => void; onSaved?: () => void | Promise<void>;
}) {
  const [config, setConfig] = useState<VideoConfig | null>(null);
  const [draft, setDraft] = useState(''), [loading, setLoading] = useState(true), [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState(''), [error, setError] = useState(''), [reload, setReload] = useState(0);
  const callbacks = useRef({ onStateChange, onSaved });
  callbacks.current = { onStateChange, onSaved };
  const project = encodeURIComponent(projectId);
  const routeUrl = `/api/model-services/routes/project/${project}/video_generation`;
  const bindingsUrl = `/api/projects/${project}/ai-services`;
  const busy = loading || saving;

  useEffect(() => () => callbacks.current.onStateChange?.({ busy: false, dirty: false, contextEnabled: false, hasCloud: false }), []);
  useEffect(() => {
    const abort = new AbortController();
    const init = { signal: abort.signal };
    setLoading(true);
    setError('');
    void Promise.all([
      request<AIService[]>('/api/settings/ai-services?module=video_generation', init),
      request<VideoBinding[]>(bindingsUrl, init),
      request<RouteProfile>(routeUrl, init),
      request<{container:boolean}>('/api/settings/ai-service-options', init).catch(() => ({container:true})),
    ]).then(async ([services, bindings, profile, deployment]) => {
      if (profile.scope !== 'project' || profile.target_id !== projectId || profile.slot !== 'video_generation') throw new Error('视频路由响应不属于当前项目');
      const overview = hasExplicitRoute(profile) ? await request<ModelServicesOverview>('/api/model-services', init) : null;
      if (!abort.signal.aborted) setConfig({ services, binding: bindings.find(item => item.module === 'video_generation'), profile,
        candidates: overview?.slots.find(item => item.slot === 'video_generation')?.candidates || [], container: deployment.container });
    }).catch(cause => { if (!abort.signal.aborted) setError(String(cause)); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [bindingsUrl, projectId, reload, routeUrl]);

  const profile = config?.profile;
  const explicit = profile ? hasExplicitRoute(profile) : false;
  const choices = profile?.settings.choices || [];
  const services = config?.services || [];
  const legacy = config?.binding;
  const currentChoice = explicit ? choices[0] : legacy;
  const currentService = currentChoice ? services.find(service => service.id === currentChoice.service_id)
    : !explicit ? services.find(service => service.is_default && service.enabled) : undefined;
  const currentModel = currentChoice?.model_id || currentService?.default_model || '';
  const savedKey = currentService ? selectionKey(currentService.id, currentModel) : '';
  const activeChoices = explicit ? choices : legacy ? [legacy] : currentService ? [{ service_id: currentService.id, model_id: currentModel }] : [];
  const routeHasCloud = profile?.settings.mode === 'cloud_only' || activeChoices.some(choice => {
    const service = services.find(item => item.id === choice.service_id);
    return service?.provider === 'video_api' || service?.provider === 'cloud_draft';
  });
  const includesUnsupported = activeChoices.some(choice => {
    const service = services.find(item => item.id === choice.service_id);
    return !compatibleVideoModel(service, choice.model_id || service?.default_model || '');
  });
  const localOptions = services.flatMap(service => {
    const boundModel = service.id === currentService?.id ? currentModel : '';
    const models = Array.from(new Set([service.default_model, ...(service.models || []).map(item => item.id), boundModel]));
    return models.filter(model => compatibleVideoModel(service, model)).map(model => ({ service, model, key: selectionKey(service.id, model),
      eligible: !explicit || Boolean(config?.candidates.find(candidate => candidate.service_id === service.id && candidate.model_id === model)?.eligible) }));
  });
  const selected = localOptions.find(option => option.key === draft && option.eligible);
  const value = draft || (localOptions.some(option => option.key === savedKey) && (!explicit || choices.length === 1) ? savedKey : '');
  const canRestore = explicit ? Boolean(profile?.revision) : Boolean(legacy);
  const displayedService = draft ? selected?.service : currentService;
  const displayedModel = draft ? selected?.model || '' : currentModel;
  const contextEnabled = draft ? usesContext(displayedService, displayedModel, config?.container ?? true)
    : activeChoices.length > 0 && activeChoices.every(choice => {
      const service = services.find(item => item.id === choice.service_id);
      return usesContext(service, choice.model_id || service?.default_model || '', config?.container ?? true);
    });
  const hasCloud = draft ? displayedService?.provider === 'video_api' : routeHasCloud;
  useEffect(() => { callbacks.current.onStateChange?.({ busy, dirty: Boolean(draft), contextEnabled, hasCloud }); }, [busy, draft, contextEnabled, hasCloud]);

  async function save(restore = false) {
    if (!profile || busy || disabled || (restore ? !canRestore : !selected)) return;
    setSaving(true);
    setNotice('');
    setError('');
    try {
      const url = explicit ? routeUrl : `${bindingsUrl}/video_generation`;
      if (restore) await request(url, { method: 'DELETE' });
      else if (selected) {
        const choice = { service_id: selected.service.id, model_id: selected.model };
        const body = explicit ? { expected_revision: profile.revision, settings: { ...profile.settings,
          mode: selected.service.provider === 'video_api' ? 'cloud_only' : 'local_only', choices: [choice], allow_fallback: false, allow_paid_fallback: false } } : choice;
        await request(url, { method: 'PUT', body: JSON.stringify(body) });
      }
      setDraft('');
      setLoading(true);
      setReload(previous => previous + 1);
      setNotice(restore ? '已恢复继承，正在读取实际配置。' : '已保存本项目的视频服务与模型。');
      await callbacks.current.onSaved?.();
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSaving(false);
    }
  }

  return <section className="card assembly-output" id="production-video-service">
    <div className="section-heading"><h2>视频服务与模型</h2>
      <button type="button" className="button button-ghost" disabled={busy || disabled || !canRestore} onClick={() => void save(true)}>恢复继承设置</button>
      <button type="button" className="button button-secondary" disabled={busy || disabled || !selected} onClick={() => void save()}>保存为项目覆盖</button>
    </div>
    <p className="form-hint">可选择已配置的本地或云端视频服务。只有 Context-IR 服务启用 4／20／30 步工作流；已派发任务沿用各自的服务快照。</p>
    {loading ? <p role="status">读取当前项目视频服务…</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    {config && !loading && includesUnsupported ? <p role="alert">当前项目的视频路由包含不兼容或尚未接入的服务。请配置支持的视频接口或本地工作流，再保存项目覆盖。</p> : null}
    {hasCloud ? <p className="form-hint">当前使用云端视频服务，接口契约：{String(displayedService?.settings?.video_contract || '按已保存路由')}；采样与工作流由供应商决定，实际参数和返回视频保存在任务快照中。启动前必须确认供应商费用。</p> : null}
    {config && !loading && !localOptions.some(option => option.eligible) ? <p role="status">{explicit && localOptions.length ? '当前是显式路由，保存前需要候选具备真实任务验证。' : '尚未配置兼容的视频服务。'} <Link to="/settings">管理服务档案</Link></p> : null}
    <FormField label="项目视频服务与模型"><select disabled={busy || disabled || !localOptions.some(option => option.eligible)} value={value} onChange={event => setDraft(event.target.value)}>
      <option value="" disabled>选择视频服务与模型</option>
      {localOptions.map(option => <option key={option.key} value={option.key} disabled={!option.eligible}>
        {option.service.name} · {option.model}{option.eligible ? '' : ' · 尚无真实任务验证'}
      </option>)}
    </select></FormField>
    {draft ? <button type="button" className="button button-ghost" disabled={busy} onClick={() => setDraft('')}>放弃此模型选择</button> : null}
    {currentService ? <p className="form-hint">当前来源 {explicit ? profile?.inherited_from || '项目视频路由' : legacy ? '项目服务绑定' : '视频模块默认'} · {currentService.name}{choices.length > 1 ? ` 等 ${choices.length} 项` : ''}</p> : null}
  </section>;
}
