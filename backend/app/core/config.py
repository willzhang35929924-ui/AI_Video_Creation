from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def load_local_environment():
    """Read dotenv as data, equally on Windows and macOS. Never execute it."""
    path = PROJECT_ROOT / '.env'
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        name, value = name.strip(), value.strip()
        if not name or not name.replace('_', 'a').isalnum() or name[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'':
            value = value[1:-1]
        os.environ.setdefault(name, value)


load_local_environment()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path(value: str, default: Path) -> Path:
    candidate = Path(value) if value.strip() else default
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _resolve_database_url(value: str) -> str:
    prefix = "sqlite:///"
    if not value.startswith(prefix) or value in {"sqlite:///:memory:", "sqlite+pysqlite:///:memory:"}:
        return value
    raw_path = value[len(prefix) :]
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return f"{prefix}{candidate.resolve().as_posix()}"


@dataclass(slots=True)
class Settings:
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8765
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'video_factory.db').as_posix()}"
    media_root: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "media")
    import_max_mb: int = 50
    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_client_timeout_seconds: float | None = None
    h3_workflow_api_json: Path | None = None
    h3_demo_mode: bool = False
    allow_external_doc_import: bool = False
    doc_import_allowed_hosts: tuple[str, ...] = ()
    seed_demo_data: bool = False
    seed_local_model_services: bool = True
    container_mode: bool = False
    ffprobe_path: str = "ffprobe"
    ffmpeg_path: str = "ffmpeg"

    @property
    def import_max_bytes(self) -> int:
        return self.import_max_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        workflow = os.getenv("H3_WORKFLOW_API_JSON", "").strip()
        database_url = _resolve_database_url(
            os.getenv("DATABASE_URL", "sqlite:///./data/video_factory.db")
        )
        media_root = _resolve_path(
            os.getenv("MEDIA_ROOT", "./data/media"), PROJECT_ROOT / "data" / "media"
        )
        hosts = tuple(
            host.strip().lower()
            for host in os.getenv("DOC_IMPORT_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        )
        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            app_host=os.getenv("APP_HOST", "127.0.0.1"),
            app_port=int(os.getenv("APP_PORT", "8765")),
            database_url=database_url,
            media_root=media_root,
            import_max_mb=int(os.getenv("IMPORT_MAX_MB", "50")),
            comfyui_base_url=os.getenv("COMFYUI_BASE_URL", "http://127.0.0.1:8188").rstrip("/"),
            comfyui_client_timeout_seconds=(
                float(os.getenv("COMFYUI_CLIENT_TIMEOUT_SECONDS", "none"))
                if os.getenv("COMFYUI_CLIENT_TIMEOUT_SECONDS", "none").strip().lower() not in {"", "none", "null"}
                else None
            ),
            h3_workflow_api_json=_resolve_path(workflow, PROJECT_ROOT / "workflows" / "h3.json") if workflow else None,
            h3_demo_mode=_as_bool(os.getenv("H3_DEMO_MODE")),
            allow_external_doc_import=_as_bool(os.getenv("ALLOW_EXTERNAL_DOC_IMPORT")),
            doc_import_allowed_hosts=hosts,
            seed_demo_data=_as_bool(os.getenv("SEED_DEMO_DATA"), False),
            seed_local_model_services=_as_bool(os.getenv("SEED_LOCAL_MODEL_SERVICES"), True),
            container_mode=_as_bool(os.getenv('VIDEO_FACTORY_CONTAINER')),
            ffprobe_path=os.getenv("FFPROBE_PATH", "ffprobe"),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()

