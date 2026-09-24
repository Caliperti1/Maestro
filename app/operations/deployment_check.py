"""Fail-fast checks for production deployment configuration.

This command validates settings that the cloud API and worker consume before migrations run.
"""

import json
import sys
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class DeploymentFinding:
    level: str
    key: str
    message: str


def deployment_findings(settings: Settings) -> list[DeploymentFinding]:
    findings: list[DeploymentFinding] = []
    database = urlparse(settings.database_url.replace("postgresql+psycopg", "postgresql", 1))
    if database.scheme != "postgresql":
        findings.append(
            DeploymentFinding("error", "DATABASE_URL", "Production requires PostgreSQL."),
        )
    if (database.hostname or "").lower() in {"", "localhost", "127.0.0.1"}:
        findings.append(
            DeploymentFinding(
                "error",
                "DATABASE_URL",
                "Production DATABASE_URL must not point to the local machine.",
            ),
        )

    origins = settings.cors_origins
    if not origins:
        findings.append(DeploymentFinding("error", "CORS_ALLOW_ORIGINS", "At least one origin is required."))
    for origin in origins:
        parsed = urlparse(origin)
        if origin == "*" or parsed.scheme != "https" or not parsed.netloc:
            findings.append(
                DeploymentFinding(
                    "error",
                    "CORS_ALLOW_ORIGINS",
                    f"Production origin must be an exact HTTPS origin: {origin}",
                ),
            )

    frontend = urlparse(settings.frontend_origin)
    if frontend.scheme != "https" or not frontend.netloc:
        findings.append(
            DeploymentFinding(
                "error",
                "FRONTEND_ORIGIN",
                "Production frontend origin must be an HTTPS URL.",
            ),
        )
    elif settings.frontend_origin not in origins:
        findings.append(
            DeploymentFinding(
                "error",
                "CORS_ALLOW_ORIGINS",
                "CORS_ALLOW_ORIGINS must include FRONTEND_ORIGIN.",
            ),
        )

    if settings.llm_provider == "openrouter" and not settings.openrouter_api_key:
        findings.append(DeploymentFinding("error", "OPENROUTER_API_KEY", "OpenRouter API key is missing."))
    if settings.llm_provider == "openai" and not settings.openai_api_key:
        findings.append(DeploymentFinding("error", "OPENAI_API_KEY", "OpenAI API key is missing."))

    if settings.embedding_provider == "ollama" and "localhost" in settings.embedding_base_url:
        findings.append(
            DeploymentFinding(
                "warning",
                "EMBEDDING_PROVIDER",
                "Local Ollama is unavailable in Render; configure hosted embeddings before cutover.",
            ),
        )
    if settings.owner_auth_mode != "oidc":
        findings.append(
            DeploymentFinding(
                "gate",
                "OWNER_AUTH_MODE",
                "Production requires OWNER_AUTH_MODE=oidc.",
            )
        )
    elif not settings.owner_oidc_configured:
        findings.append(
            DeploymentFinding(
                "gate",
                "OWNER_OIDC",
                "OIDC issuer, client, subject, redirect URI, and session secret are required.",
            )
        )
    else:
        for key, value in (
            ("OWNER_OIDC_ISSUER", settings.owner_oidc_issuer),
            ("OWNER_OIDC_REDIRECT_URI", settings.owner_oidc_redirect_uri),
        ):
            parsed = urlparse(value or "")
            if parsed.scheme != "https" or not parsed.netloc:
                findings.append(
                    DeploymentFinding(
                        "error",
                        key,
                        f"{key} must be an HTTPS URL.",
                    )
                )
    if settings.owner_session_secret and len(settings.owner_session_secret) < 32:
        findings.append(
            DeploymentFinding(
                "error",
                "OWNER_SESSION_SECRET",
                "OWNER_SESSION_SECRET must contain at least 32 characters.",
            )
        )
    if not settings.owner_cookie_secure:
        findings.append(
            DeploymentFinding(
                "error",
                "OWNER_COOKIE_SECURE",
                "Production owner cookies must be Secure.",
            )
        )
    if settings.owner_auth_mode == "oidc" and settings.owner_cookie_samesite != "none":
        findings.append(
            DeploymentFinding(
                "error",
                "OWNER_COOKIE_SAMESITE",
                "The separate Vercel and Render origins require SameSite=None.",
            )
        )
    if (
        settings.artifact_store_backend != "s3"
        or not settings.artifact_store_s3_bucket
        or not settings.artifact_store_s3_region
    ):
        findings.append(
            DeploymentFinding(
                "gate",
                "ARTIFACT_STORE",
                "Production requires a private S3 artifact bucket.",
            )
        )
    return findings


def main() -> None:
    findings = deployment_findings(get_settings())
    print(json.dumps([asdict(finding) for finding in findings], indent=2, sort_keys=True))
    # A release gate is intentionally fatal in the Render pre-deploy command. This keeps the
    # Blueprint useful for validation without allowing an unauthenticated API to be promoted by
    # mistake.
    if any(finding.level in {"error", "gate"} for finding in findings):
        sys.exit(1)


if __name__ == "__main__":
    main()
