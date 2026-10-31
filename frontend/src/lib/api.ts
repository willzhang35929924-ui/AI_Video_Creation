import type {
  Character,
  CharacterAsset,
  CharacterVersion,
  DashboardSummary,
  GenerationJob,
  HealthStatus,
  ImportResult,
  Project,
  ProviderCapabilities,
  ProviderHealth,
  PublishBatch,
  PublishingAccount,
  PublishingPreset,
  ScriptRevision,
  StoryboardShot,
  VideoRevision,
} from "./types";

const configuredBase = (import.meta.env.VITE_API_BASE_URL ?? "").trim().replace(/\/$/, "");
export const API_BASE_URL = configuredBase;

type JsonRecord = Record<string, unknown>;

export class ApiError extends Error {
  status: number;
  code?: string;
  details?: unknown;
  requestId?: string;

  constructor(message: string, status: number, code?: string, details?: unknown, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

function apiUrl(path: string) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

function errorMessage(payload: unknown, fallback: string) {
  if (!payload || typeof payload !== "object") return fallback;
  const value = payload as JsonRecord;
  if (value.error && typeof value.error === "object") {
    const nested = value.error as JsonRecord;
    if (typeof nested.message === "string") return nested.message;
  }
  const detail = value.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) return String((item as JsonRecord).msg);
        return String(item);
      })
      .join("；");
  }
  return String(value.message ?? value.error ?? fallback);
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const isForm = init.body instanceof FormData;
  if (init.body && !isForm && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("Accept", "application/json");

  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers,
      credentials: "same-origin",
    });
  } catch (cause) {
    throw new ApiError(
      cause instanceof Error ? `无法连接本地服务：${cause.message}` : "无法连接本地服务",
      0,
      "NETWORK_ERROR",
      cause,
    );
  }

  const contentType = response.headers.get("content-type") ?? "";
  let payload: unknown = null;
  if (response.status !== 204) {
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => null);
    } else {
      payload = await response.text().catch(() => null);
    }
  }

  if (!response.ok) {
    const record = payload && typeof payload === "object" ? (payload as JsonRecord) : undefined;
    const nestedError = record?.error && typeof record.error === "object" ? (record.error as JsonRecord) : undefined;
    throw new ApiError(
      `${errorMessage(payload, `请求失败（HTTP ${response.status}）`)}${typeof nestedError?.request_id === "string" ? `（请求编号：${nestedError.request_id}）` : ""}`,
      response.status,
      typeof nestedError?.code === "string"
        ? nestedError.code
        : typeof record?.code === "string"
          ? record.code
          : undefined,
      nestedError?.details ?? record?.details ?? record?.detail,
      typeof nestedError?.request_id === "string" ? nestedError.request_id : undefined,
    );
  }

  return payload as T;
}

function unwrap<T>(payload: unknown): T {
  if (payload && typeof payload === "object") {
    const value = payload as JsonRecord;
    if ("data" in value && value.data !== undefined) return value.data as T;
    if ("result" in value && value.result !== undefined) return value.result as T;
  }
  return payload as T;
}

function unwrapList<T>(payload: unknown, keys: string[] = []): T[] {
  const value = unwrap<unknown>(payload);
  if (Array.isArray(value)) return value as T[];
  if (value && typeof value === "object") {
    const record = value as JsonRecord;
    for (const key of ["items", ...keys]) {
      if (Array.isArray(record[key])) return record[key] as T[];
    }
  }
  return [];
}

function normalizeProject(raw: Project): Project {
  return {
    ...raw,
    name: raw.name ?? "未命名项目",
    status: raw.status ?? "draft",
    sourceType: raw.sourceType ?? raw.source_type,
    sourceUri: raw.sourceUri ?? raw.source_uri,
    sourceFilename: raw.sourceFilename ?? raw.source_filename,
    sourceText: raw.sourceText ?? raw.source_text,
    shotCount: raw.shotCount ?? raw.shot_count,
    pendingReviewCount: raw.pendingReviewCount ?? raw.pending_review_count,
    runningJobCount: raw.runningJobCount ?? raw.running_job_count,
    pendingVideoReviewCount: raw.pendingVideoReviewCount ?? raw.pending_video_review_count,
    createdAt: raw.createdAt ?? raw.created_at,
    updatedAt: raw.updatedAt ?? raw.updated_at,
  };
}

function normalizeHealth(raw: HealthStatus): HealthStatus {
  const source = raw as HealthStatus & { app?: string; media_root_writable?: boolean };
  return {
    ...raw,
    ok: raw.ok ?? raw.status === "ok",
    service: raw.service ?? source.app,
    mediaRootWritable: raw.mediaRootWritable ?? source.media_root_writable,
  };
}

function normalizeProviderHealth(raw: ProviderHealth): ProviderHealth {
  const source = raw as ProviderHealth & {
    ready?: boolean;
    base_url?: string;
    workflow_name?: string;
    reference_to_video_available?: boolean;
    checked_at?: string;
  };
  return {
    ...raw,
    ok: raw.ok ?? Boolean(source.ready ?? raw.connected),
    configured: raw.configured ?? Boolean(source.workflow_name ?? raw.workflowName),
    baseUrl: raw.baseUrl ?? source.base_url,
    workflowName: raw.workflowName ?? source.workflow_name,
    referenceToVideoAvailable:
      raw.referenceToVideoAvailable ?? source.reference_to_video_available ?? false,
    lastCheckedAt: raw.lastCheckedAt ?? source.checked_at,
  };
}

function normalizeCapabilities(raw: ProviderCapabilities): ProviderCapabilities {
  const source = raw as ProviderCapabilities & {
    reference_to_video?: boolean;
    image_to_video?: boolean;
    reference_video?: boolean;
    supported_aspect_ratios?: string[];
    max_reference_images?: number;
    ready?: boolean;
  };
  return {
    ...raw,
    referenceToVideo: raw.referenceToVideo ?? source.reference_to_video ?? false,
    imageToVideo: raw.imageToVideo ?? source.image_to_video ?? false,
    referenceVideo: raw.referenceVideo ?? source.reference_video ?? false,
    supportedAspectRatios: raw.supportedAspectRatios ?? source.supported_aspect_ratios ?? [],
    maxReferenceImages: raw.maxReferenceImages ?? source.max_reference_images,
    configured: raw.configured ?? source.ready,
  };
}

function normalizeShot(raw: StoryboardShot): StoryboardShot {
  return {
    ...raw,
    sceneTitle: raw.sceneTitle ?? raw.scene_title ?? "未命名分镜",
    narration: raw.narration ?? "",
    dialogue: raw.dialogue ?? "",
    visualPrompt: raw.visualPrompt ?? raw.visual_prompt ?? "",
    negativePrompt: raw.negativePrompt ?? raw.negative_prompt ?? "",
    action: raw.action ?? "",
    cameraMove: raw.cameraMove ?? raw.camera_move ?? "",
    location: raw.location ?? "",
    lighting: raw.lighting ?? "",
    audioDirection: raw.audioDirection ?? raw.audio_direction ?? "",
    durationSeconds: Number(raw.durationSeconds ?? raw.duration_seconds ?? 5),
    aspectRatio: raw.aspectRatio ?? raw.aspect_ratio ?? "9:16",
    characterId: raw.characterId ?? raw.character_id ?? null,
    characterAssetVersionId:
      raw.characterAssetVersionId ??
      raw.character_asset_version_id ??
      raw.characterVersionId ??
      raw.character_version_id ??
      null,
    status: raw.status ?? "draft",
    revision: raw.revision ?? 1,
    rejectionReason: raw.rejectionReason ?? raw.rejection_reason,
    createdAt: raw.createdAt ?? raw.created_at,
    updatedAt: raw.updatedAt ?? raw.updated_at,
  };
}

function normalizeCharacterVersion(raw: CharacterVersion): CharacterVersion {
  const identity = raw.identityJson ?? raw.identity_json ?? {};
  return {
    ...raw,
    characterId: raw.characterId ?? raw.character_id,
    identityJson: { ...identity, fixedIdentity: identity.fixedIdentity ?? identity.fixed_identity, hairstyle: identity.hairstyle ?? identity.hair, facialFeatures: identity.facialFeatures ?? (Array.isArray(identity.distinguishing_features) ? identity.distinguishing_features.join("、") : undefined), defaultOutfit: identity.defaultOutfit ?? identity.default_clothing, allowedVariations: identity.allowedVariations ?? (Array.isArray(identity.allowed_changes) ? identity.allowed_changes.join("、") : undefined) },
    approvalState: raw.approvalState ?? raw.approval_state ?? "draft",
    approvedAt: raw.approvedAt ?? raw.approved_at,
    createdAt: raw.createdAt ?? raw.created_at,
    assets: (raw.assets ?? []).map(normalizeAsset),
  };
}

function normalizeAsset(raw: CharacterAsset): CharacterAsset {
  return {
    ...raw,
    characterVersionId: raw.characterVersionId ?? raw.character_version_id,
    assetType: raw.assetType ?? raw.asset_type ?? "reference",
    relativePath: raw.relativePath ?? raw.relative_path,
    mediaUrl: raw.mediaUrl ?? raw.media_url,
    mimeType: raw.mimeType ?? raw.mime_type,
    tags: raw.tags ?? raw.tags_json ?? [],
    isPrimaryReference: raw.isPrimaryReference ?? raw.is_primary_reference ?? false,
    createdAt: raw.createdAt ?? raw.created_at,
  };
}

function normalizeCharacter(raw: Character): Character {
  const versions = (raw.versions ?? []).map(normalizeCharacterVersion);
  const currentRaw = raw.currentVersion ?? raw.current_version;
  return {
    ...raw,
    displayName: raw.displayName ?? raw.display_name ?? raw.slug,
    description: raw.description ?? "",
    status: raw.status ?? "active",
    tags: raw.tags ?? raw.tags_json ?? [],
    currentApprovedVersionId: raw.currentApprovedVersionId ?? raw.current_approved_version_id,
    currentVersion: currentRaw ? normalizeCharacterVersion(currentRaw) : null,
    versions,
    avatarUrl: raw.avatarUrl ?? raw.avatar_url,
    createdAt: raw.createdAt ?? raw.created_at,
    updatedAt: raw.updatedAt ?? raw.updated_at,
  };
}

function normalizeJob(raw: GenerationJob): GenerationJob {
  return {
    ...raw,
    projectId: raw.projectId ?? raw.project_id,
    shotId: raw.shotId ?? raw.shot_id,
    providerJobId: raw.providerJobId ?? raw.provider_job_id,
    parameters: raw.parameters ?? raw.parameters_json ?? {},
    referenceAssets: raw.referenceAssets ?? raw.reference_assets_json ?? [],
    workflowVersion: raw.workflowVersion ?? raw.workflow_version,
    errorCode: raw.errorCode ?? raw.error_code,
    errorMessage: raw.errorMessage ?? raw.error_message,
    retryCount: raw.retryCount ?? raw.retry_count ?? 0,
    startedAt: raw.startedAt ?? raw.started_at,
    finishedAt: raw.finishedAt ?? raw.finished_at,
    createdAt: raw.createdAt ?? raw.created_at,
  };
}

function extractJob(payload: unknown): GenerationJob {
  const value = unwrap<unknown>(payload);
  if (value && typeof value === "object" && "job" in value) {
    return (value as { job: GenerationJob }).job;
  }
  return value as GenerationJob;
}

function normalizeVideo(raw: VideoRevision): VideoRevision {
  return {
    ...raw,
    projectId: raw.projectId ?? raw.project_id,
    shotId: raw.shotId ?? raw.shot_id,
    generationJobId: raw.generationJobId ?? raw.generation_job_id,
    relativePath: raw.relativePath ?? raw.relative_path,
    mediaUrl: raw.mediaUrl ?? raw.media_url,
    thumbnailPaths: raw.thumbnailPaths ?? raw.thumbnail_paths_json ?? [],
    metadata: raw.metadata ?? raw.metadata_json ?? {},
    reviewStatus: raw.reviewStatus ?? raw.review_status ?? "pending",
    reviewReason: raw.reviewReason ?? raw.review_reason,
    qualityChecks: raw.qualityChecks ?? raw.quality_checks ?? [],
    createdAt: raw.createdAt ?? raw.created_at,
  };
}

function normalizeAccount(raw: PublishingAccount): PublishingAccount {
  return {
    ...raw,
    displayName: raw.displayName ?? raw.display_name ?? raw.platform,
    authStatus: raw.authStatus ?? raw.auth_status ?? "not_configured",
    capabilityStatus: raw.capabilityStatus ?? raw.capability_status ?? "dry_run_only",
    accountMetadata: raw.accountMetadata ?? raw.account_metadata_json ?? {},
    tokenExpiry: raw.tokenExpiry ?? raw.token_expiry_nullable,
    lastCheckedAt: raw.lastCheckedAt ?? raw.last_checked_at,
  };
}

function normalizePreset(raw: PublishingPreset): PublishingPreset {
  return {
    ...raw,
    accountId: raw.accountId ?? raw.account_id,
    titleTemplate: raw.titleTemplate ?? raw.title_template ?? "",
    captionTemplate: raw.captionTemplate ?? raw.caption_template ?? "",
    hashtags: raw.hashtags ?? raw.hashtags_json ?? [],
    defaultSchedule: raw.defaultSchedule ?? raw.default_schedule,
    extra: raw.extra ?? raw.extra_json ?? {},
  };
}

const json = (value: unknown) => JSON.stringify(value);

export const api = {
  getHealth: async (signal?: AbortSignal) =>
    normalizeHealth(unwrap<HealthStatus>(await request("/api/health", { signal }))),

  getProviderHealth: async (signal?: AbortSignal) =>
    normalizeProviderHealth(unwrap<ProviderHealth>(await request("/api/providers/h3/health", { signal }))),

  getProviderCapabilities: async (signal?: AbortSignal) =>
    normalizeCapabilities(unwrap<ProviderCapabilities>(await request("/api/providers/h3/capabilities", { signal }))),

  listProjects: async (signal?: AbortSignal) =>
    unwrapList<Project>(await request("/api/projects", { signal }), ["projects"]).map(normalizeProject),

  getDashboard: async (signal?: AbortSignal): Promise<DashboardSummary> => {
    try {
      const result = unwrap<DashboardSummary>(await request("/api/dashboard", { signal }));
      if (result && typeof result.projectCount === "number") return result;
      const raw = result as unknown as JsonRecord;
      const recentFromResponse = unwrapList<Project>(raw.recent_projects ?? []).map(normalizeProject);
      const projectRows = recentFromResponse.length ? recentFromResponse : await api.listProjects(signal);
      return {
        projectCount: Number(raw.project_count ?? raw.projects ?? 0),
        pendingShotCount: Number(raw.pending_shot_count ?? raw.pending_shots ?? 0),
        runningJobCount: Number(raw.running_job_count ?? raw.running_jobs ?? 0),
        pendingVideoReviewCount: Number(raw.pending_video_review_count ?? raw.pending_video_reviews ?? 0),
        recentProjects: [...projectRows]
          .sort((a, b) => new Date(b.updatedAt ?? 0).getTime() - new Date(a.updatedAt ?? 0).getTime())
          .slice(0, 6),
      };
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
      const projects = await api.listProjects(signal);
      return {
        projectCount: projects.length,
        pendingShotCount: projects.reduce((sum, project) => sum + (project.pendingReviewCount ?? 0), 0),
        runningJobCount: projects.reduce((sum, project) => sum + (project.runningJobCount ?? 0), 0),
        pendingVideoReviewCount: projects.reduce((sum, project) => sum + (project.pendingVideoReviewCount ?? 0), 0),
        recentProjects: [...projects]
          .sort((a, b) => new Date(b.updatedAt ?? 0).getTime() - new Date(a.updatedAt ?? 0).getTime())
          .slice(0, 6),
      };
    }
  },

  createProject: async (input: { name: string }) =>
    normalizeProject(unwrap<Project>(await request("/api/projects", { method: "POST", body: json(input) }))),

  getProject: async (id: string, signal?: AbortSignal) =>
    normalizeProject(unwrap<Project>(await request(`/api/projects/${encodeURIComponent(id)}`, { signal }))),

  updateProject: async (id: string, input: Partial<Project>) =>
    normalizeProject(
      unwrap<Project>(
        await request(`/api/projects/${encodeURIComponent(id)}`, { method: "PATCH", body: json(input) }),
      ),
    ),

  importFile: async (file: File, projectName?: string, projectId?: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("name", projectName || file.name.replace(/\.[^.]+$/, "") || "未命名项目");
    const project = normalizeProject(
      unwrap<Project>(await request("/api/imports/file", { method: "POST", body: form })),
    );
    return {
      project,
      projectId: project.id,
      sourceType: project.sourceType,
      sourceFilename: project.sourceFilename,
      textPreview: (project.sourceText ?? "").slice(0, 260),
    } satisfies ImportResult;
  },

  importText: async (input: { text: string; projectName?: string; projectId?: string }) => {
    const project = normalizeProject(
      unwrap<Project>(await request("/api/imports/text", {
        method: "POST",
        body: json({ name: input.projectName || "未命名项目", text: input.text, source_type: "paste" }),
      })),
    );
    return {
      project,
      projectId: project.id,
      sourceType: project.sourceType,
      textPreview: (project.sourceText ?? "").slice(0, 260),
    } satisfies ImportResult;
  },

  importUrl: async (input: { url: string; projectName?: string; projectId?: string }) => {
    const project = normalizeProject(
      unwrap<Project>(await request("/api/imports/url", {
        method: "POST",
        body: json({ name: input.projectName || "未命名项目", url: input.url }),
      })),
    );
    return {
      project,
      projectId: project.id,
      sourceType: project.sourceType,
      textPreview: (project.sourceText ?? "").slice(0, 260),
    } satisfies ImportResult;
  },

  parseProject: async (projectId: string, _preserveManualEdits = true) =>
    unwrap<unknown>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/parse`, { method: "POST" }),
    ),

  getScript: async (projectId: string, signal?: AbortSignal) =>
    unwrap<ScriptRevision>(await request(`/api/projects/${encodeURIComponent(projectId)}/script`, { signal })),

  updateScript: async (projectId: string, rawText: string) =>
    unwrap<ScriptRevision>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/script`, {
        method: "PATCH",
        body: json({ raw_text: rawText }),
      }),
    ),

  approveScript: async (projectId: string) =>
    unwrap<ScriptRevision>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/script/approve`, {
        method: "POST",
        body: json({ operator: "local-user" }),
      }),
    ),

  listShots: async (projectId: string, signal?: AbortSignal, includeHistory=false) =>
    unwrapList<StoryboardShot>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/shots${includeHistory?'?include_history=true':''}`, { signal }),
      ["shots"],
    )
      .map(normalizeShot)
      .sort((a, b) => a.order - b.order),

  duplicateShot: async (shotId:string) => normalizeShot(await request<StoryboardShot>(`/api/shots/${encodeURIComponent(shotId)}/duplicate`,{method:'POST'})),
  createShot: async (projectId: string, shot: Partial<StoryboardShot>) =>
    normalizeShot(
      unwrap<StoryboardShot>(
        await request(`/api/projects/${encodeURIComponent(projectId)}/shots`, {
          method: "POST",
          body: json(toShotPayload(shot)),
        }),
      ),
    ),

  updateShot: async (shotId: string, shot: Partial<StoryboardShot>) =>
    normalizeShot(
      unwrap<StoryboardShot>(
        await request(`/api/shots/${encodeURIComponent(shotId)}`, {
          method: "PATCH",
          body: json(toShotPayload(shot)),
        }),
      ),
    ),

  deleteShot: async (shotId: string) =>
    unwrap<unknown>(await request(`/api/shots/${encodeURIComponent(shotId)}`, { method: "DELETE" })),

  approveShot: async (shotId: string, decision: "approved" | "rejected", reason?: string) =>
    normalizeShot(
      unwrap<StoryboardShot>(
        await request(`/api/shots/${encodeURIComponent(shotId)}/${decision === "rejected" ? "reject" : "approve"}`, {
          method: "POST",
          body: json({ reason, operator: "local-user" }),
        }),
      ),
    ),

  approveAllShots: async (projectId: string) =>
    unwrap<unknown>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/shots/approve-all`, {
        method: "POST",
        body: json({ operator: "local-user" }),
      }),
    ),

  assignCharacter: async (shotId: string, characterId: string, characterVersionId: string) =>
    normalizeShot(
      unwrap<StoryboardShot>(
        await request(`/api/shots/${encodeURIComponent(shotId)}/assign-character`, {
          method: "POST",
          body: json({ character_id: characterId, character_version_id: characterVersionId }),
        }),
      ),
    ),

  listCharacters: async (signal?: AbortSignal) =>
    unwrapList<Character>(await request("/api/characters", { signal }), ["characters"]).map(normalizeCharacter),

  getCharacter: async (id: string, signal?: AbortSignal) =>
    normalizeCharacter(unwrap<Character>(await request(`/api/characters/${encodeURIComponent(id)}`, { signal }))),

  createCharacter: async (input: { slug: string; displayName: string; description: string; tags: string[] }) =>
    normalizeCharacter(
      unwrap<Character>(
        await request("/api/characters", {
          method: "POST",
          body: json({
            slug: input.slug,
            id: input.slug,
            display_name: input.displayName,
            description: input.description,
          }),
        }),
      ),
    ),

  createCharacterVersion: async (characterId: string, identity: Record<string, unknown>) =>
    normalizeCharacterVersion(
      unwrap<CharacterVersion>(
        await request(`/api/characters/${encodeURIComponent(characterId)}/versions`, {
          method: "POST",
          body: json({ identity: normalizeIdentityPayload(identity) }),
        }),
      ),
    ),

  uploadCharacterAsset: async (
    characterId: string,
    input: { versionId: string; file: File; assetType: string; isPrimary: boolean; tags: string[]; approve?: boolean },
  ) => {
    const form = new FormData();
    form.append("file", input.file);
    form.append("version_id", input.versionId);
    form.append("asset_type", input.assetType);
    form.append("is_primary_reference", String(input.isPrimary));
    form.append("tags", input.tags.join(","));
    form.append("approve", String(input.approve ?? false));
    return normalizeAsset(
      unwrap<CharacterAsset>(
        await request(`/api/characters/${encodeURIComponent(characterId)}/assets`, {
          method: "POST",
          body: form,
        }),
      ),
    );
  },

  approveCharacterVersion: async (characterId: string, versionId: string) =>
    normalizeCharacterVersion(
      unwrap<CharacterVersion>(
        await request(
          `/api/characters/${encodeURIComponent(characterId)}/versions/${encodeURIComponent(versionId)}/approve`,
          { method: "POST", body: json({ operator: "local-user" }) },
        ),
      ),
    ),

  listJobs: async (projectId?: string, signal?: AbortSignal) => {
    if (!projectId) return [];
    return unwrapList<GenerationJob>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/jobs`, { signal }),
      ["jobs"],
    ).map(normalizeJob);
  },

  getJob: async (id: string, signal?: AbortSignal) =>
    normalizeJob(unwrap<GenerationJob>(await request(`/api/jobs/${encodeURIComponent(id)}`, { signal }))),

  generateShot: async (shotId: string, providerMode: "demo" | "real", options?: { seed: number; width: number; height: number; steps: number; service_id?: string; model_id?: string }) =>
    normalizeJob(
      extractJob(
        await request(`/api/shots/${encodeURIComponent(shotId)}/generate`, {
          method: "POST",
          body: json({ seed: options?.seed, service_id:options?.service_id,model_id:options?.model_id, parameters: { requested_mode: providerMode, ...(options ? { width: options.width, height: options.height, steps: options.steps } : {}) } }),
        }),
      ),
    ),

  generateApprovedShots: async (projectId: string, providerMode: "demo" | "real", service_id?:string,model_id?:string) =>
    unwrap<unknown>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/generate-approved-shots`, {
        method: "POST",
        body: json({ service_id,model_id,parameters: { requested_mode: providerMode } }),
      }),
    ),

  retryJob: async (id: string) =>
    normalizeJob(
      extractJob(
        await request(`/api/jobs/${encodeURIComponent(id)}/retry`, { method: "POST", body: json({}) }),
      ),
    ),

  cancelJob: async (id: string) =>
    normalizeJob(
      unwrap<GenerationJob>(
        await request(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST", body: json({}) }),
      ),
    ),

  listVideos: async (projectId?: string, signal?: AbortSignal) => {
    if (!projectId) return [];
    return unwrapList<VideoRevision>(
      await request(`/api/projects/${encodeURIComponent(projectId)}/videos`, { signal }),
      ["videos"],
    ).map(normalizeVideo);
  },

  getVideo: async (id: string, signal?: AbortSignal) =>
    normalizeVideo(unwrap<VideoRevision>(await request(`/api/videos/${encodeURIComponent(id)}`, { signal }))),

  reviseVideo: async (id: string, instruction: string) =>
    unwrap<GenerationJob | VideoRevision>(
      await request(`/api/videos/${encodeURIComponent(id)}/revise`, {
        method: "POST",
        body: json({ instruction }),
      }),
    ),

  approveVideo: async (id: string, review: { operator: string; reason?: string } = { operator: "local-user" }) =>
    normalizeVideo(
      unwrap<VideoRevision>(
        await request(`/api/videos/${encodeURIComponent(id)}/approve`, {
          method: "POST",
          body: json(review),
        }),
      ),
    ),

  rejectVideo: async (id: string, reason: string) =>
    normalizeVideo(
      unwrap<VideoRevision>(
        await request(`/api/videos/${encodeURIComponent(id)}/reject`, {
          method: "POST",
          body: json({ reason, operator: "local-user" }),
        }),
      ),
    ),

  runQualityCheck: async (id: string) =>
    unwrap<unknown>(
      await request(`/api/videos/${encodeURIComponent(id)}/quality-check`, {
        method: "POST",
        body: json({}),
      }),
    ),

  listPublishingAccounts: async (signal?: AbortSignal) =>
    unwrapList<PublishingAccount>(await request("/api/publishing/accounts", { signal }), ["accounts"]).map(
      normalizeAccount,
    ),

  createPublishingAccount: async (input: { platform: string; displayName: string }) =>
    normalizeAccount(
      unwrap<PublishingAccount>(
        await request("/api/publishing/accounts", {
          method: "POST",
          body: json({ platform: input.platform, display_name: input.displayName, auth_status: "placeholder", account_metadata: { simulated: true, dry_run_enabled: true, purpose: "local_internal_dry_run_only" } }),
        }),
      ),
    ),

  updatePublishingAccount: async (id: string, input: Partial<PublishingAccount>) =>
    normalizeAccount(
      unwrap<PublishingAccount>(
        await request(`/api/publishing/accounts/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: json(input),
        }),
      ),
    ),

  deletePublishingAccount: async (id: string) =>
    unwrap<unknown>(
      await request(`/api/publishing/accounts/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
    ),

  listPublishingPresets: async (accountId?: string, signal?: AbortSignal) => {
    const query = accountId ? `?account_id=${encodeURIComponent(accountId)}` : "";
    return unwrapList<PublishingPreset>(
      await request(`/api/publishing/presets${query}`, { signal }),
      ["presets"],
    ).map(normalizePreset);
  },

  createPublishingPreset: async (input: {
    accountId: string;
    name: string;
    titleTemplate: string;
    captionTemplate: string;
    hashtags: string[];
    visibility: string;
    defaultSchedule?: string | null;
  }) =>
    normalizePreset(
      unwrap<PublishingPreset>(
        await request("/api/publishing/presets", {
          method: "POST",
          body: json({
            account_id: input.accountId,
            name: input.name,
            title_template: input.titleTemplate,
            caption_template: input.captionTemplate,
            hashtags: input.hashtags,
            visibility: input.visibility,
            default_schedule: input.defaultSchedule,
            extra: {},
          }),
        }),
      ),
    ),

  dryRunPublish: async (input: {
    accountIds: string[];
    videoIds: string[];
    presetId?: string;
    schedule?: string | null;
  }) =>
    unwrap<PublishBatch>(
      await request("/api/publishing/batches/dry-run", {
        method: "POST",
        body: json({
          items: input.videoIds.flatMap((videoId) =>
            input.accountIds.map((accountId) => ({
              video_revision_id: videoId,
              account_id: accountId,
              preset_id: input.presetId,
              scheduled_at: input.schedule,
            })),
          ),
        }),
      }),
    ),

  getPublishBatch: async (id: string, signal?: AbortSignal) =>
    unwrap<PublishBatch>(
      await request(`/api/publishing/batches/${encodeURIComponent(id)}`, { signal }),
    ),

  mediaUrl: (value?: string | null, mediaId?: string) => {
    if (value && /^(https?:|blob:|data:)/i.test(value)) return value;
    if (value?.startsWith("/")) return apiUrl(value);
    if (value) return apiUrl(`/api/media/${value.split("/").map(encodeURIComponent).join("/")}`);
    return mediaId ? apiUrl(`/api/media/${encodeURIComponent(mediaId)}`) : "";
  },
};

function toShotPayload(shot: Partial<StoryboardShot>) {
  return {
    order: shot.order,
    scene_title: shot.sceneTitle ?? shot.scene_title,
    narration: shot.narration,
    dialogue: shot.dialogue,
    visual_prompt: shot.visualPrompt ?? shot.visual_prompt,
    negative_prompt: shot.negativePrompt ?? shot.negative_prompt,
    action: shot.action,
    camera_move: shot.cameraMove ?? shot.camera_move,
    location: shot.location,
    lighting: shot.lighting,
    audio_direction: shot.audioDirection ?? shot.audio_direction,
    duration_seconds: shot.durationSeconds ?? shot.duration_seconds,
    aspect_ratio: shot.aspectRatio ?? shot.aspect_ratio,
  };
}

function normalizeIdentityPayload(identity: Record<string, unknown>) {
  return {
    ...identity,
    fixed_identity: identity.fixed_identity ?? identity.fixedIdentity,
    hairstyle: identity.hairstyle,
    facial_features: identity.facial_features ?? identity.facialFeatures,
    body_type: identity.body_type ?? identity.bodyType,
    default_clothing: identity.default_clothing ?? identity.defaultOutfit,
    negative_prompt: identity.negative_prompt ?? identity.immutableTraits,
    allowed_variations: identity.allowed_variations ?? identity.allowedVariations,
  };
}
