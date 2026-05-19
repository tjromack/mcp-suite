"""Unit tests for pydantic-settings config: defaults, env override, singleton.

`_env_file=None` disables reading the real project `.env` so these are
hermetic; relevant OS env vars are cleared first.
"""

from __future__ import annotations

import pytest

from clinical_trial_mcp.config import Settings
from clinical_trial_mcp.config import settings as singleton

_ENV_KEYS = [
    "DATABASE_URL",
    "ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "EMBEDDING_MODEL",
    "SUMMARY_MODEL",
    "SEARCH_TOP_K",
    "DEFAULT_INGEST_QUERY",
    "DEFAULT_INGEST_MAX",
    "CTGOV_IMPERSONATE",
    "CTGOV_SSL_VERIFY",
    "CTGOV_CA_BUNDLE",
]


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    s = Settings(_env_file=None)
    assert s.embedding_model == "voyage-3"
    assert s.summary_model == "claude-haiku-4-5-20251001"
    assert s.search_top_k == 10
    assert s.default_ingest_query == "cancer"
    assert s.default_ingest_max == 500
    assert s.ctgov_impersonate == "chrome"
    assert s.ctgov_ssl_verify is True
    assert s.ctgov_ca_bundle is None
    assert s.anthropic_api_key == ""
    assert s.voyage_api_key == ""


def test_env_override_and_type_coercion(clean_env):
    clean_env.setenv("EMBEDDING_MODEL", "voyage-3-large")
    clean_env.setenv("SEARCH_TOP_K", "25")  # str → int
    clean_env.setenv("CTGOV_SSL_VERIFY", "false")  # str → bool

    s = Settings(_env_file=None)

    assert s.embedding_model == "voyage-3-large"
    assert s.search_top_k == 25
    assert isinstance(s.search_top_k, int)
    assert s.ctgov_ssl_verify is False


def test_case_insensitive_env(clean_env):
    clean_env.setenv("default_ingest_query", "diabetes")  # lowercase
    s = Settings(_env_file=None)
    assert s.default_ingest_query == "diabetes"


def test_singleton_is_settings_instance():
    assert isinstance(singleton, Settings)
    # Re-importing returns the same cached module-level object.
    from clinical_trial_mcp.config import settings as again

    assert again is singleton
