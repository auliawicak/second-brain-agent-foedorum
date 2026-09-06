"""Central configuration for the Second Brain Agent."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env file from project root
load_dotenv(Path(__file__).parent / ".env")


class Config:
    """Application configuration loaded from environment variables."""

    # --- Required ---
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_USER_ID: int = int(os.environ.get("TELEGRAM_USER_ID", "0"))
    OPENCODE_ZEN_API_KEY: str = os.environ.get("OPENCODE_ZEN_API_KEY", "")
    NEWS_API_KEY: str = os.environ.get("NEWS_API_KEY", "")

    # --- Models ---
    # OpenCode Zen (https://opencode.ai/zen/v1) serves Muse Spark 1.3
    # Contributor Free at no cost via the Responses API.
    # Since Phase 1 these are NOT read directly by the model layer: they act
    # as priority-0 overrides injected into the model registry (agent/registry.py).
    FAST_MODEL: str = os.environ.get("FAST_MODEL", "muse-spark-1.3-contributor-free")
    DEEP_MODEL: str = os.environ.get("DEEP_MODEL", "muse-spark-1.3-contributor-free")
    MODEL_API_URL: str = os.environ.get(
        "MODEL_API_URL", "https://opencode.ai/zen/v1"
    )

    # --- Model registry (Phase 1) ---
    # Optional path to a YAML/JSON file that fully overrides the seeded registry.
    MODELS_FILE: str | None = os.environ.get("MODELS_FILE") or None
    # Timeout (seconds) for any single provider request.
    PROVIDER_TIMEOUT: float = float(os.environ.get("PROVIDER_TIMEOUT", "60"))

    # --- Timezone ---
    TIMEZONE_STR: str = os.environ.get("TIMEZONE", "Asia/Jakarta")
    TIMEZONE: ZoneInfo = ZoneInfo(TIMEZONE_STR)

    # --- Schedule ---
    NEWS_DELIVERY_HOUR: int = int(os.environ.get("NEWS_DELIVERY_HOUR", "6"))
    NEWS_DELIVERY_MINUTE: int = int(os.environ.get("NEWS_DELIVERY_MINUTE", "0"))
    AGENDA_DELIVERY_HOUR: int = int(os.environ.get("AGENDA_DELIVERY_HOUR", "6"))
    AGENDA_DELIVERY_MINUTE: int = int(
        os.environ.get("AGENDA_DELIVERY_MINUTE", "30")
    )

    # --- Storage ---
    PROJECT_DIR: Path = Path(__file__).parent
    DATABASE_PATH: Path = Path(
        os.environ.get("DATABASE_PATH", str(PROJECT_DIR / "data" / "second_brain.db"))
    )
    AGENT_SAVE_DIR: Path = PROJECT_DIR / "data" / "agent_state"
    EXPORT_DIR: Path = PROJECT_DIR / "data" / "export"

    # --- Data hygiene (Phase 3) ---
    # Conversation rows older than this are exported and pruned nightly.
    RETENTION_DAYS: int = int(os.environ.get("RETENTION_DAYS", "60"))
    # GCS bucket for nightly backups. Same-region (us-central1) egress is
    # free. Empty → the backup job is skipped cleanly.
    BACKUP_BUCKET: str = os.environ.get("BACKUP_BUCKET", "")
    # Hard wall-clock budget (seconds) for each backup sub-step, so a wedged
    # copy or upload fails with an alert instead of hanging the job forever.
    BACKUP_TIMEOUT_SECONDS: int = int(os.environ.get("BACKUP_TIMEOUT_SECONDS", "300"))

    # --- Logging ---
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    # --- News ---
    NEWS_CATEGORIES: list[str] = ["general", "technology", "business", "science"]
    NEWS_COUNTRY: str = "us"  # NewsAPI country code
    MAX_NEWS_ARTICLES: int = 10

    # --- Context & limits ---
    MAX_CONTEXT_MESSAGES: int = int(os.environ.get("MAX_CONTEXT_MESSAGES", "12"))
    MAX_PREFS_INJECTED: int = int(os.environ.get("MAX_PREFS_INJECTED", "8"))
    MAX_PROMPT_CHARS: int = int(os.environ.get("MAX_PROMPT_CHARS", "24000"))

    @classmethod
    def validate(cls) -> list[str]:
        """Validate that all required configuration is present.

        Returns a list of error messages (empty if valid).
        """
        errors: list[str] = []
        if not cls.TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if cls.TELEGRAM_USER_ID == 0:
            errors.append("TELEGRAM_USER_ID is required")
        if not cls.OPENCODE_ZEN_API_KEY:
            errors.append("OPENCODE_ZEN_API_KEY is required (get one at opencode.ai/auth)")
        if not cls.NEWS_API_KEY:
            errors.append("NEWS_API_KEY is required (get one at newsapi.org)")
        return errors
