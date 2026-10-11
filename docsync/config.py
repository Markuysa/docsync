"""Engine configuration: everything comes from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- where engine data lives (journal, repository clones, mock Confluence, build output)
    data_dir: Path = field(default_factory=lambda: Path(_env("DOCSYNC_DATA_DIR", "./data")).resolve())

    # --- architecture model repository (architecture-model)
    # local : path to a git repository on disk (demo / development)
    # gitlab: a GitLab project; the engine clones it and opens MRs
    git_mode: str = field(default_factory=lambda: _env("DOCSYNC_GIT_MODE", "local"))
    model_repo_path: Path = field(default_factory=lambda: Path(_env("DOCSYNC_MODEL_REPO", "./data/repos/architecture-model")).resolve())
    model_project: str = field(default_factory=lambda: _env("GITLAB_MODEL_PROJECT", "arch/architecture-model"))
    model_default_branch: str = field(default_factory=lambda: _env("DOCSYNC_MODEL_BRANCH", "main"))

    # --- GitLab (gitlab mode)
    gitlab_url: str = field(default_factory=lambda: _env("GITLAB_URL", "https://gitlab.com").rstrip("/"))
    gitlab_token: str = field(default_factory=lambda: _env("GITLAB_TOKEN"))
    gitlab_webhook_secret: str = field(default_factory=lambda: _env("GITLAB_WEBHOOK_SECRET"))
    # local service repositories: "payments/orders-service=/path/to/repo;other=/path" (local mode)
    service_repos: dict[str, Path] = field(default_factory=lambda: {
        k.strip(): Path(v.strip()).resolve()
        for k, _, v in (p.partition("=") for p in _env("DOCSYNC_SERVICE_REPOS").split(";") if "=" in p)
    })

    # --- Confluence: mock (files on disk) or cloud (Atlassian Cloud REST API)
    confluence_mode: str = field(default_factory=lambda: _env("CONFLUENCE_MODE", "mock"))
    confluence_url: str = field(default_factory=lambda: _env("CONFLUENCE_URL").rstrip("/"))
    confluence_email: str = field(default_factory=lambda: _env("CONFLUENCE_EMAIL"))
    confluence_token: str = field(default_factory=lambda: _env("CONFLUENCE_API_TOKEN"))
    confluence_space: str = field(default_factory=lambda: _env("CONFLUENCE_SPACE", "SA"))
    confluence_parent_page: str = field(default_factory=lambda: _env("CONFLUENCE_PARENT_PAGE_ID"))
    dry_run: bool = field(default_factory=lambda: _bool("DOCSYNC_DRY_RUN", False))
    # how to embed diagrams: attachment (SVG attachment) | macro (source inside a PlantUML macro)
    diagram_mode: str = field(default_factory=lambda: _env("DOCSYNC_DIAGRAM_MODE", "attachment"))
    plantuml_macro_name: str = field(default_factory=lambda: _env("CONFLUENCE_PLANTUML_MACRO", "plantuml"))

    # --- C4 diagrams: svg (rendered by Kroki, attached to the page) | structurizr (pushed to a Structurizr server, embedded via iframe)
    c4_mode: str = field(default_factory=lambda: _env("DOCSYNC_C4_MODE", "svg"))
    c4_iframe_height: int = field(default_factory=lambda: int(_env("DOCSYNC_C4_IFRAME_HEIGHT", "650")))
    structurizr_url: str = field(default_factory=lambda: _env("STRUCTURIZR_URL").rstrip("/"))          # public, for iframes
    structurizr_api_url: str = field(default_factory=lambda: _env("STRUCTURIZR_API_URL").rstrip("/"))  # internal, for push (default <url>/api)
    structurizr_workspace_id: str = field(default_factory=lambda: _env("STRUCTURIZR_WORKSPACE_ID"))
    structurizr_api_key: str = field(default_factory=lambda: _env("STRUCTURIZR_API_KEY"))
    structurizr_api_secret: str = field(default_factory=lambda: _env("STRUCTURIZR_API_SECRET"))
    structurizr_admin_key: str = field(default_factory=lambda: _env("STRUCTURIZR_ADMIN_KEY"))
    structurizr_cli: str = field(default_factory=lambda: _env("STRUCTURIZR_CLI", "docker"))

    # --- diagram rendering
    kroki_url: str = field(default_factory=lambda: _env("KROKI_URL", "http://localhost:8000").rstrip("/"))

    # --- LLM: none | anthropic | openai-compatible
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "none"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-opus-5-5"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY") or _env("ANTHROPIC_API_KEY"))

    # --- notifications: Slack incoming webhook and/or Telegram bot with approve buttons
    slack_webhook_url: str = field(default_factory=lambda: _env("SLACK_WEBHOOK_URL"))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    telegram_allowed_users: set[str] = field(default_factory=lambda: {u.strip() for u in _env("TELEGRAM_ALLOWED_USERS").split(",") if u.strip()})
    telegram_polling: bool = field(default_factory=lambda: _bool("TELEGRAM_POLLING", True))
    # approve URL the engine itself calls (from inside Docker n8n is http://n8n:5678, for humans it is localhost)
    approve_internal_url: str = field(default_factory=lambda: _env("DOCSYNC_APPROVE_INTERNAL_URL"))

    # --- engine API security
    api_token: str = field(default_factory=lambda: _env("DOCSYNC_API_TOKEN"))
    public_url: str = field(default_factory=lambda: _env("DOCSYNC_PUBLIC_URL", "http://localhost:8088").rstrip("/"))
    # "approve" link in notifications; {runId} is substituted. Defaults to the n8n approve webhook
    approve_url: str = field(default_factory=lambda: _env("DOCSYNC_APPROVE_URL", "http://localhost:5678/webhook/docsync/approve?run={runId}"))

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "docsync.sqlite3"

    @property
    def build_dir(self) -> Path:
        return self.data_dir / "build"

    @property
    def mock_confluence_dir(self) -> Path:
        return self.data_dir / "confluence-mock"

    @property
    def clones_dir(self) -> Path:
        return self.data_dir / "repos"


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """For tests: re-read the environment."""
    global _settings
    _settings = None
