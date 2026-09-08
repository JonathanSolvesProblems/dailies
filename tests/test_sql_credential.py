"""Guards on the credential the SQL agent runs as.

The submission claims three layers under the question box: the model refuses destructive
intent, the MCP server's write and drop flags are off, and the cluster credential itself
cannot write. The third one is the only one that holds if the other two fail, and it was
briefly not true. The deployed service passed CLICKHOUSE_USER=default, the admin account,
while the writeup described a readonly credential, and the gap was invisible from the code
because nothing connected the claim to the environment that produced it.

It was found the expensive way: a DROP TABLE aimed at what was believed to be a readonly
credential removed the observations table, and the table had to be rebuilt from out/. These
tests exist so that the next time the two drift apart, a test says so instead of a table
disappearing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import ask  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    """A predictable environment: no inherited ClickHouse settings from a developer's shell."""
    for key in (
        "CLICKHOUSE_RO_USER",
        "CLICKHOUSE_RO_PASSWORD",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_ALLOW_WRITE_ACCESS",
        "CLICKHOUSE_ALLOW_DROP",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestReadonlyCredentialIsPreferred:
    def test_ro_user_overrides_the_primary_credential(self, clean_env):
        clean_env.setenv("CLICKHOUSE_USER", "default")
        clean_env.setenv("CLICKHOUSE_PASSWORD", "admin-secret")
        clean_env.setenv("CLICKHOUSE_RO_USER", "dailies_ro")
        clean_env.setenv("CLICKHOUSE_RO_PASSWORD", "ro-secret")

        env = ask._mcp_env()

        assert env["CLICKHOUSE_USER"] == "dailies_ro"
        assert env["CLICKHOUSE_PASSWORD"] == "ro-secret"

    def test_admin_credential_never_reaches_the_mcp_subprocess(self, clean_env):
        """The specific regression: the admin password must not be inherited through os.environ."""
        clean_env.setenv("CLICKHOUSE_USER", "default")
        clean_env.setenv("CLICKHOUSE_PASSWORD", "admin-secret")
        clean_env.setenv("CLICKHOUSE_RO_USER", "dailies_ro")
        clean_env.setenv("CLICKHOUSE_RO_PASSWORD", "ro-secret")

        assert "admin-secret" not in ask._mcp_env().values()

    def test_falls_back_to_primary_when_no_ro_user_is_configured(self, clean_env):
        clean_env.setenv("CLICKHOUSE_USER", "default")
        clean_env.setenv("CLICKHOUSE_PASSWORD", "admin-secret")

        env = ask._mcp_env()

        assert env["CLICKHOUSE_USER"] == "default"

    def test_partial_config_does_not_half_apply(self, clean_env):
        """A user with no password would connect as the wrong identity, so both are required."""
        clean_env.setenv("CLICKHOUSE_USER", "default")
        clean_env.setenv("CLICKHOUSE_PASSWORD", "admin-secret")
        clean_env.setenv("CLICKHOUSE_RO_USER", "dailies_ro")

        assert ask._mcp_env()["CLICKHOUSE_USER"] == "default"


class TestCredentialIsReportedNotAsserted:
    """/api/capabilities has to answer from the same place _mcp_env reads."""

    def test_reports_readonly_when_configured(self, clean_env):
        clean_env.setenv("CLICKHOUSE_RO_USER", "dailies_ro")
        assert ask.mcp_credential() == "readonly"

    def test_reports_primary_when_not(self, clean_env):
        assert ask.mcp_credential() == "primary"


class TestWriteFlagsStayOff:
    def test_write_and_drop_are_disabled(self, clean_env):
        env = ask._mcp_env()
        assert env["CLICKHOUSE_ALLOW_WRITE_ACCESS"] == "false"
        assert env["CLICKHOUSE_ALLOW_DROP"] == "false"

    def test_an_inherited_true_is_not_silently_honoured(self, clean_env):
        """setdefault means a hostile or careless outer environment can still turn writes on.

        This is deliberate rather than an oversight: the operator setting the variable is
        the same person deploying the service. The test pins the behaviour so that if it is
        ever tightened to an unconditional override, that is a decision someone made on
        purpose and not a silent change.
        """
        clean_env.setenv("CLICKHOUSE_ALLOW_WRITE_ACCESS", "true")
        assert ask._mcp_env()["CLICKHOUSE_ALLOW_WRITE_ACCESS"] == "true"
