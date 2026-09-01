from pathlib import Path
from pydantic_settings import BaseSettings
from functools import lru_cache

_ENV_FILE = Path(__file__).resolve().parent / ".env"

# The literal default below — kept as a named constant so the production
# startup guard (validate_production_secrets) and the Settings field default
# always agree on what counts as "the known insecure value" without
# duplicating the string.
_INSECURE_SESSION_JWT_SECRET_DEFAULT = "dev-only-insecure-session-secret-change-me"


class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_max_tokens: int = 1024
    openai_temperature: float = 0.4
    cors_origins: list[str] = ["http://localhost:3000"]
    log_level: str = "INFO"

    # "development" keeps the no-session-cookie demo-user fallback alive
    # (needed until apps/web has a real Google-authenticated session).
    # Set to "production" to require a valid ORQIS session cookie on every
    # /api/* call, and to mark cookies Secure (HTTPS-only).
    environment: str = "development"

    # Google OAuth (Authorization Code + PKCE) — the ORQIS sign-in flow.
    # google_client_id/secret are issued by a Google Cloud OAuth client;
    # google_redirect_uri must exactly match one of that client's
    # "Authorized redirect URIs" (e.g. http://localhost:8000/api/auth/google/callback
    # for local dev). frontend_base_url is where the backend sends the
    # browser after the OAuth callback finishes (post-login/access-denied).
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    frontend_base_url: str = "http://localhost:3000"

    # Secret used to sign ORQIS's own HttpOnly session-cookie JWTs
    # (services/auth_service.py). Generate with `openssl rand -hex 32` — a
    # blank/default value must never be used in production since it lets
    # anyone forge a session.
    session_jwt_secret: str = _INSECURE_SESSION_JWT_SECRET_DEFAULT

    # Backend-enforced email-domain allowlist for onboarding (new tenant
    # creation). Comma-separated; case-insensitive. This is the actual
    # security boundary — apps/web's client-side domain gate
    # (config/allowed-domains.ts) is UX only (fast redirect before a real
    # API round-trip) and does not replace this check; a request straight to
    # the API must be rejected here regardless of what the frontend did.
    # Defaults to the same navedas.com-only policy in every environment,
    # dev included — there's no environment where "allow any domain to
    # create a tenant" is a safe default.
    allowed_email_domains: str = "navedas.com"

    # Database
    database_url: str = "postgresql://orqis:orqis@localhost:5432/orqis"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # LLM call retry/backoff (services/llm_retry.py) — the single retry path
    # shared by every llm.chat() call site in the one Agent Runtime
    # (services/agent_runtime_executor.py's single-step execution and
    # services/pipeline_executor.py's per-node execution both go through
    # llm_retry.call_with_retry with these settings). Only genuinely
    # transient failures are retried — see llm_retry.is_retryable.
    llm_retry_max_attempts: int = 3
    llm_retry_initial_delay_seconds: float = 1.0
    llm_retry_max_delay_seconds: float = 20.0

    # Agent Scheduler / Heartbeat background loops (services/
    # agent_scheduler_service.py, services/agent_heartbeat_service.py) —
    # both poll agent_definitions for due work and dispatch through the same
    # governed Agent Runtime as any other execution path. Poll interval
    # controls how often each background loop checks for due
    # AgentDefinitions; disabled by default outside of main.py's real
    # startup so importing these services (e.g. from tests) never starts a
    # background asyncio task on its own.
    scheduler_enabled: bool = True
    scheduler_poll_interval_seconds: int = 30
    heartbeat_enabled: bool = True
    heartbeat_poll_interval_seconds: int = 30
    # How long a schedule/heartbeat occurrence may stay marked "running"
    # before a claim is allowed to reclaim it as stale (e.g. after a backend
    # crash mid-execution left the flag stuck true) — see the claim SQL in
    # both services above.
    scheduler_stale_running_seconds: int = 900

    # Knowledge Graph backend (currently implemented on Graphiti / Neo4j
    # Aura — see services/knowledge_graph_service.py) — optional. Graph
    # ingestion and graph-backed context retrieval are additive features
    # that no-op (not error) when these are unset, so the app runs fine
    # without a graph backend.
    knowledge_graph_uri: str = ""
    knowledge_graph_username: str = ""
    knowledge_graph_password: str = ""
    knowledge_graph_database: str = "neo4j"

    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_production_secrets(settings: "Settings | None" = None) -> None:
    """Fail-fast startup guard (called from main.py at import time, before the
    ASGI app is built): a production process must never run with an unset or
    known-default SESSION_JWT_SECRET, since that value is either blank or
    checked into this file and would let anyone forge an ORQIS session
    cookie (services/auth_service.py issue_session_jwt/verify_session_jwt).
    Raises RuntimeError with a message that never includes the actual
    configured secret. `settings` is accepted as an argument (rather than
    always calling get_settings()) so tests can exercise this against a
    throwaway Settings instance without fighting get_settings()'s lru_cache.
    """
    settings = settings or get_settings()
    if settings.environment.strip().lower() not in ("production", "prod"):
        return
    secret = settings.session_jwt_secret
    if not secret or secret == _INSECURE_SESSION_JWT_SECRET_DEFAULT:
        raise RuntimeError(
            "Refusing to start: ENVIRONMENT=production but SESSION_JWT_SECRET is unset or still "
            "the insecure development default. Set a unique secret (e.g. `openssl rand -hex 32`) "
            "via the SESSION_JWT_SECRET environment variable before starting in production."
        )


def is_email_domain_allowed(email: str) -> bool:
    """Backend-side source of truth for the onboarding domain allowlist.

    An empty ALLOWED_EMAIL_DOMAINS is treated as "no restriction configured"
    (allows everything) rather than "block everything" — a misconfigured
    empty allowlist should fail open to a support/config issue, not silently
    make onboarding impossible for legitimate users. To require an
    allowlist, set ALLOWED_EMAIL_DOMAINS explicitly.
    """
    domains = {d.strip().lower() for d in get_settings().allowed_email_domains.split(",") if d.strip()}
    if not domains:
        return True
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return domain in domains
