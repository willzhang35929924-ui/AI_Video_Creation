export type Id = string;

export type ProviderMode = "demo" | "real" | "unknown";
export type AsyncStatus = "idle" | "loading" | "success" | "error";

export interface HealthStatus {
  ok: boolean;
  status?: string;
  service?: string;
  version?: string;
  database?: string;
  mediaRootWritable?: boolean;
  timestamp?: string;
  [key: string]: unknown;
}

export interface ProviderHealth {
  ok: boolean;
  connected?: boolean;
  configured?: boolean;
  mode?: ProviderMode;
  provider?: string;
  baseUrl?: string;
  version?: string | null;
  workflowName?: string | null;
  referenceToVideoAvailable?: boolean;
  lastCheckedAt?: string;
  reason?: string | null;
  error?: string | null;
  [key: string]: unknown;
}

export interface ProviderCapabilities {
  mode?: ProviderMode;
  provider?: string;
  referenceToVideo?: boolean;
  imageToVideo?: boolean;
  referenceVideo?: boolean;
  supportedAspectRatios?: string[];
  maxReferenceImages?: number | null;
  configured?: boolean;
  [key: string]: unknown;
}

export interface Project {
  id: Id;
  name: string;
  sourceType?: string | null;
  source_type?: string | null;
  sourceUri?: string | null;
  source_uri?: string | null;
  sourceFilename?: string | null;
  source_filename?: string | null;
  sourceText?: string | null;
  source_text?: string | null;
  status: string;
  stage?: string;
  shotCount?: number;
  shot_count?: number;
  pendingReviewCount?: number;
  pending_review_count?: number;
  runningJobCount?: number;
  running_job_count?: number;
  pendingVideoReviewCount?: number;
  pending_video_review_count?: number;
  createdAt?: string;
  created_at?: string;
  updatedAt?: string;
  updated_at?: string;
}

export interface DashboardSummary {
  projectCount: number;
  pendingShotCount: number;
  runningJobCount: number;
  pendingVideoReviewCount: number;
  recentProjects: Project[];
}

export interface ScriptRevision {
  id: Id;
  projectId?: Id;
  project_id?: Id;
  revision: number;
  rawText?: string;
  raw_text?: string;
  parsedJson?: Record<string, unknown>;
  parsed_json?: Record<string, unknown>;
  status: string;
  approvedAt?: string | null;
  approved_at?: string | null;
  approvedBy?: string | null;
  approved_by?: string | null;
  createdAt?: string;
  created_at?: string;
}

export type ShotReviewStatus = "draft" | "approved" | "rejected" | "needs_review" | string;

export interface StoryboardShot {
  participants?: { id: string; character_id: string; name: string; ready: boolean }[];
  generation_ready?: boolean;
  scene_id?: string | null;
  scene_name?: string | null;
  script_revision_id?: string | null;
  id: Id;
  projectId?: Id;
  project_id?: Id;
  order: number;
  sceneTitle: string;
  scene_title?: string;
  narration: string;
  dialogue: string;
  visualPrompt: string;
  visual_prompt?: string;
  negativePrompt: string;
  negative_prompt?: string;
  action: string;
  cameraMove: string;
  camera_move?: string;
  location: string;
  lighting: string;
  audioDirection: string;
  audio_direction?: string;
  durationSeconds: number;
  duration_seconds?: number;
  aspectRatio: "9:16" | "16:9" | "1:1" | string;
  aspect_ratio?: string;
  characterId: Id | null;
  character_id?: Id | null;
  characterAssetVersionId: Id | null;
  character_asset_version_id?: Id | null;
  characterVersionId?: Id | null;
  character_version_id?: Id | null;
  reference_asset_ids_json?: Id[];
  status: ShotReviewStatus;
  revision: number;
  rejectionReason?: string | null;
  rejection_reason?: string | null;
  staleVideoCount?: number;
  createdAt?: string;
  created_at?: string;
  updatedAt?: string;
  updated_at?: string;
}

export type CharacterAssetType =
  | "face_front"
  | "face_left_45"
  | "face_right_45"
  | "profile"
  | "half_body"
  | "full_body"
  | "expression"
  | "outfit"
  | "reference_video"
  | "reference_audio"
  | string;

export interface CharacterAsset {
  id: Id;
  characterVersionId?: Id;
  character_version_id?: Id;
  assetType?: CharacterAssetType;
  asset_type?: CharacterAssetType;
  relativePath?: string;
  relative_path?: string;
  mediaUrl?: string;
  media_url?: string;
  mimeType?: string;
  mime_type?: string;
  width?: number | null;
  height?: number | null;
  duration?: number | null;
  sha256?: string;
  tags?: string[];
  tags_json?: string[];
  status: string;
  isPrimaryReference?: boolean;
  is_primary_reference?: boolean;
  placeholder?: boolean;
  createdAt?: string;
  created_at?: string;
}

export interface CharacterVersion {
  id: Id;
  characterId?: Id;
  character_id?: Id;
  version: number;
  identityJson?: Record<string, unknown>;
  identity_json?: Record<string, unknown>;
  approvalState?: string;
  approval_state?: string;
  approvedAt?: string | null;
  approved_at?: string | null;
  assets?: CharacterAsset[];
  createdAt?: string;
  created_at?: string;
}

export interface Character {
  id: Id;
  slug: string;
  displayName: string;
  display_name?: string;
  description?: string;
  status: string;
  tags?: string[];
  tags_json?: string[];
  currentApprovedVersionId?: Id | null;
  current_approved_version_id?: Id | null;
  currentVersion?: CharacterVersion | null;
  current_version?: CharacterVersion | null;
  versions?: CharacterVersion[];
  avatarUrl?: string | null;
  avatar_url?: string | null;
  createdAt?: string;
  created_at?: string;
  updatedAt?: string;
  updated_at?: string;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled" | string;

export interface GenerationJob {
  id: Id;
  projectId?: Id;
  project_id?: Id;
  shotId?: Id;
  shot_id?: Id;
  provider: string;
  providerJobId?: string | null;
  provider_job_id?: string | null;
  status: JobStatus;
  prompt?: string;
  parameters?: Record<string, unknown>;
  parameters_json?: Record<string, unknown>;
  referenceAssets?: Array<Record<string, unknown>>;
  reference_assets_json?: Array<Record<string, unknown>>;
  workflowVersion?: string | null;
  workflow_version?: string | null;
  seed?: number | null;
  progress?: number | null;
  errorCode?: string | null;
  error_code?: string | null;
  errorMessage?: string | null;
  error_message?: string | null;
  retryCount?: number;
  retry_count?: number;
  startedAt?: string | null;
  started_at?: string | null;
  finishedAt?: string | null;
  finished_at?: string | null;
  createdAt?: string;
  created_at?: string;
}

export interface QualityCheck {
  id: Id;
  checkType?: string;
  check_type?: string;
  status: string;
  score?: number | null;
  score_nullable?: number | null;
  details?: Record<string, unknown>;
  details_json?: Record<string, unknown>;
  createdAt?: string;
  created_at?: string;
}

export interface VideoRevision {
  id: Id;
  projectId?: Id;
  project_id?: Id;
  shotId?: Id;
  shot_id?: Id;
  generationJobId?: Id;
  generation_job_id?: Id;
  relativePath?: string;
  relative_path?: string;
  mediaUrl?: string;
  media_url?: string;
  thumbnailPaths?: string[];
  thumbnail_paths_json?: string[];
  metadata?: Record<string, unknown>;
  metadata_json?: Record<string, unknown>;
  revision: number;
  instruction?: string | null;
  reviewStatus?: string;
  review_status?: string;
  reviewReason?: string | null;
  review_reason?: string | null;
  prompt?: string;
  referenceAssets?: Array<Record<string, unknown>>;
  qualityChecks?: QualityCheck[];
  quality_checks?: QualityCheck[];
  createdAt?: string;
  created_at?: string;
}

export interface PublishingAccount {
  id: Id;
  platform: "douyin" | "kuaishou" | "xiaohongshu" | "wechat_channels" | string;
  displayName: string;
  display_name?: string;
  authStatus?: string;
  auth_status?: string;
  capabilityStatus?: string;
  capability_status?: string;
  accountMetadata?: Record<string, unknown>;
  account_metadata_json?: Record<string, unknown>;
  tokenExpiry?: string | null;
  token_expiry_nullable?: string | null;
  lastCheckedAt?: string | null;
  last_checked_at?: string | null;
}

export interface PublishingPreset {
  id: Id;
  accountId?: Id;
  account_id?: Id;
  name: string;
  titleTemplate?: string;
  title_template?: string;
  captionTemplate?: string;
  caption_template?: string;
  hashtags?: string[];
  hashtags_json?: string[];
  visibility?: string;
  defaultSchedule?: string | null;
  default_schedule?: string | null;
  extra?: Record<string, unknown>;
  extra_json?: Record<string, unknown>;
}

export interface PublishBatch {
  id: Id;
  status: string;
  dryRun?: boolean;
  dry_run?: boolean;
  items?: Array<Record<string, unknown>>;
  items_json?: Array<Record<string, unknown>>;
  validationErrors?: string[];
  validation_errors?: string[];
  createdAt?: string;
  created_at?: string;
}

export interface AuditEvent {
  id: Id;
  objectType?: string;
  object_type?: string;
  objectId?: Id;
  object_id?: Id;
  action: string;
  details?: Record<string, unknown>;
  details_json?: Record<string, unknown>;
  createdAt?: string;
  created_at?: string;
}

export interface ImportResult {
  project?: Project;
  projectId?: Id;
  sourceType?: string | null;
  source_type?: string | null;
  sourceFilename?: string | null;
  source_filename?: string | null;
  textPreview?: string | null;
  text_preview?: string | null;
  paragraphCount?: number;
  paragraph_count?: number;
  warnings?: string[];
  confidence?: string | number;
  [key: string]: unknown;
}

export interface PageResult<T> {
  items: T[];
  total?: number;
}
