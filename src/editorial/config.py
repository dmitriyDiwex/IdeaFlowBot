from __future__ import annotations

from dataclasses import dataclass
import os


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    return int(raw)


DEFAULT_MCP_EXCLUDED_BOTS = (
    "misisforever_bot,lovespolispb_bot,ranhigs_slushaet_bot,urfu_overhear_bot,"
    "spbgmtuu_bot,predloshka_moscow_poli_bot,guap_never_sleep_bot,rudn_slushaetbot,"
    "gorni_anon_bot,kfu_ovehear_bot,predloshka_mpei_bot,mephi_podsl_bot,lobach_unn_bot,"
    "kosygin_bot,spbgti_predloga_bot,igmuni_bot,shtiglewiggle_bot,kubguniver_bot,mgtusluhi_bot"
)


@dataclass(slots=True)
class EditorialSettings:
    postgres_dsn: str = os.getenv(
        "EDITORIAL_POSTGRES_DSN",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/ideaflow_editorial",
    )
    editorial_api_host: str = os.getenv("EDITORIAL_API_HOST", "0.0.0.0")
    editorial_api_port: int = _get_int("EDITORIAL_API_PORT", 8080)
    redis_dsn: str = os.getenv("EDITORIAL_REDIS_DSN", "redis://redis:6379/0")
    editorial_log_level: str = os.getenv("EDITORIAL_LOG_LEVEL", "INFO")
    review_api_key: str | None = os.getenv("EDITORIAL_REVIEW_API_KEY")
    mcp_token: str | None = os.getenv("EDITORIAL_MCP_TOKEN")
    mcp_host: str = os.getenv("EDITORIAL_MCP_HOST", "0.0.0.0")
    mcp_port: int = _get_int("EDITORIAL_MCP_PORT", 8082)
    mcp_write_enabled: bool = _get_bool("EDITORIAL_MCP_WRITE_ENABLED", False)
    mcp_max_batch_size: int = max(1, min(100, _get_int("EDITORIAL_MCP_MAX_BATCH", 20)))
    mcp_max_list_size: int = max(1, min(500, _get_int("EDITORIAL_MCP_MAX_LIST", 100)))
    mcp_actor_id: int = _get_int("EDITORIAL_MCP_ACTOR_ID", 0)
    mcp_excluded_bots: tuple[str, ...] = tuple(
        name.strip().lstrip("@").lower()
        for name in os.getenv("EDITORIAL_MCP_EXCLUDED_BOTS", DEFAULT_MCP_EXCLUDED_BOTS).split(",")
        if name.strip()
    )
    mcp_snapshot_ttl_hours: int = max(1, _get_int("EDITORIAL_MCP_SNAPSHOT_TTL_HOURS", 24))
    mcp_snapshot_max_size: int = max(1, min(100, _get_int("EDITORIAL_MCP_SNAPSHOT_MAX_SIZE", 100)))
    legacy_import_batch_size: int = _get_int("EDITORIAL_IMPORT_BATCH_SIZE", 200)
    scheduler_window_minutes: int = _get_int("EDITORIAL_SCHEDULER_WINDOW_MINUTES", 15)
    scheduler_commit_batch_size: int = max(1, _get_int("EDITORIAL_SCHEDULER_COMMIT_BATCH_SIZE", 25))
    publisher_batch_size: int = _get_int("EDITORIAL_PUBLISHER_BATCH_SIZE", 20)
    telegram_request_timeout_seconds: int = max(
        1,
        _get_int("TELEGRAM_REQUEST_TIMEOUT_SECONDS", 15),
    )
    publisher_retry_base_seconds: int = max(
        1,
        _get_int("EDITORIAL_PUBLISHER_RETRY_BASE_SECONDS", 60),
    )
    publisher_retry_max_seconds: int = max(
        publisher_retry_base_seconds,
        _get_int("EDITORIAL_PUBLISHER_RETRY_MAX_SECONDS", 900),
    )
    generation_default_provider: str = os.getenv("EDITORIAL_GENERATION_PROVIDER", "stub")
    generation_openrouter_base_url: str = os.getenv(
        "OPENROUTER_BASE_URL",
        "https://openrouter.ai/api/v1",
    )
    generation_openrouter_api_key: str | None = os.getenv("OPENROUTER_API_KEY")
    generation_model_name: str = os.getenv(
        "EDITORIAL_GENERATION_MODEL",
        "z-ai/glm-4.5-air:free",
    )
    generation_fallback_models: str = os.getenv(
        "EDITORIAL_GENERATION_FALLBACK_MODELS",
        "minimax/minimax-m2.5:free,google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free",
    )
    generation_enabled: bool = _get_bool("EDITORIAL_GENERATION_ENABLED", True)
    similarity_threshold: float = float(os.getenv("EDITORIAL_SIMILARITY_THRESHOLD", "0.82"))
    minimum_submission_length: int = _get_int("EDITORIAL_MIN_SUBMISSION_LENGTH", 20)
    default_timezone: str = os.getenv("EDITORIAL_DEFAULT_TIMEZONE", "Europe/Moscow")
    scheduler_enabled: bool = _get_bool("EDITORIAL_SCHEDULER_ENABLED", True)
    publisher_enabled: bool = _get_bool("EDITORIAL_PUBLISHER_ENABLED", True)


settings = EditorialSettings()

