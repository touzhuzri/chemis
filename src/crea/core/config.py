"""Runtime configuration.

Phase 1 has no secrets to manage; the settings object exists so that Phase 2
providers can be added without touching call sites.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREA_", env_file=".env", extra="ignore")

    # Phase 2 placeholders. Absent credentials must degrade a provider to
    # `missing_key`, never raise.
    openalex_email: str | None = None
    # Optional. OpenAlex serves keyless requests; a free key only raises the
    # daily budget. Absent is a normal state, not an error.
    openalex_api_key: str | None = None
    crossref_email: str | None = None
    unpaywall_email: str | None = None
    pubchem_api_key: str | None = None

    # Chemistry
    normalization_profile: str = Field(default="parent")
    mcs_profile: str = Field(default="relaxed")
    mcs_timeout_seconds: int = Field(default=10)
    max_heavy_atoms: int = Field(default=150)

    data_dir: str = Field(default="data")

    # Resource protection for local documents, not a PDF standard limit.
    # Unset means: use the default in core.policy.document.
    max_local_document_bytes: int | None = None


settings = Settings()
