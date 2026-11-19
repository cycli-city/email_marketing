"""Tests for the repository secret scanner. Fake credentials are assembled at runtime so this file
itself never contains anything that looks like a real token."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "scan_secrets.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scan_secrets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_secrets"] = module  # dataclasses resolves annotations through sys.modules
    spec.loader.exec_module(module)
    return module


ss = _load()


def fake_jwt(role: str) -> str:
    def b64(obj: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'HS256'})}.{b64({'role': role, 'iss': 'supabase'})}.{'s' * 24}"


def rules(text: str, **kw: object) -> set[str]:
    return {f.rule for f in ss.scan_text("f.txt", text, known=kw.pop("known", set()), **kw)}


class TestTokenFormats:
    @pytest.mark.parametrize(
        ("rule", "value"),
        [
            ("github-fine-grained-token", "github" + "_pat_" + "A1b2" * 15),
            ("github-token", "gh" + "p_" + "A" * 36),
            ("groq-api-key", "gs" + "k_" + "a1B2" * 12),
            ("anthropic-api-key", "sk" + "-ant-" + "a" * 30),
            ("openai-style-api-key", "sk" + "-" + "a1B2" * 10),
            ("google-api-key", "AI" + "za" + "A" * 35),
            ("google-oauth-client-secret", "GOC" + "SPX-" + "a" * 28),
            ("aws-access-key-id", "AK" + "IA" + "B" * 16),
            ("slack-token", "xo" + "xb-" + "1234567890-abcdef"),
            ("telegram-bot-token", "123456789" + ":" + "A" * 35),
            ("supabase-secret-key", "sb" + "_secret_" + "a" * 30),
            ("private-key-block", "-----BEGIN " + "RSA PRIVATE KEY-----"),
            ("fernet-key-assignment", "ENCRYPTION" + "_KEY=" + "A" * 43 + "="),
        ],
    )
    def test_detects(self, rule: str, value: str) -> None:
        assert rule in rules(f"x = {value}")

    def test_credentials_in_connection_url(self) -> None:
        url = "postgresql://" + "postgres" + ":" + "s3cretpass" + "@" + "db.example.com:5432/x"
        assert "hardcoded-credential-in-url" in rules(url)

    def test_localhost_urls_and_placeholders_are_not_flagged(self) -> None:
        assert not rules("postgresql://postgres:postgres@localhost:5432/aoi")
        assert not rules("postgresql://user:${DB_PASSWORD}@host/db")
        assert not rules("DATABASE_URL=")
        assert not rules("ENCRYPTION_KEY=")

    def test_allow_marker_suppresses_a_line(self) -> None:
        token = "gh" + "p_" + "A" * 36
        assert not rules(f"{token}  # secretscan:allow (documented fake)")

    def test_finding_never_contains_the_full_secret(self) -> None:
        token = "gh" + "p_" + "Z" * 36
        (finding,) = ss.scan_text("f", token, known=set())
        assert token not in str(finding)


class TestJwts:
    def test_service_role_jwt_is_always_fatal_even_when_anon_is_allowed(self) -> None:
        assert "supabase-service-role-jwt" in rules(fake_jwt("service_role"), allow_anon_jwt=True)

    def test_anon_jwt_flagged_in_source_but_allowed_in_bundle_mode(self) -> None:
        token = fake_jwt("anon")
        assert any(r.startswith("jwt(") for r in rules(token))
        assert not rules(token, allow_anon_jwt=True)

    def test_unknown_jwt_is_flagged_even_in_bundle_mode(self) -> None:
        assert any(
            r.startswith("jwt(") for r in rules(fake_jwt("authenticated"), allow_anon_jwt=True)
        )


class TestKnownLocalSecrets:
    def test_extracts_real_values_but_ignores_placeholders_and_urls(self, tmp_path: Path) -> None:
        f = tmp_path / "SECRETS.local.md"
        f.write_text(
            "- GITHUB_TOKEN=tok_abcdefgh12345\n"
            "  - password: Hunter2-Strong!\n"
            "- GROQ_API_KEY: NOT YET PROVIDED\n"
            "- SUPABASE_URL=https://x.supabase.co\n"
            "- email: someone@example.com\n"
        )
        assert ss.load_known_secret_values(f) == {"tok_abcdefgh12345", "Hunter2-Strong!"}

    def test_known_values_are_detected_anywhere(self) -> None:
        found = rules("config = 'Hunter2-Strong!'", known={"Hunter2-Strong!"})
        assert "known-local-secret-value" in found

    def test_known_values_are_never_echoed_not_even_a_prefix(self) -> None:
        (finding,) = ss.scan_text("f", "pw = 'Hunter2-Strong!'", known={"Hunter2-Strong!"})
        rendered = str(finding)
        assert "Hunt" not in rendered
        assert "Hunter2-Strong!" not in rendered

    def test_missing_file_yields_empty_set(self, tmp_path: Path) -> None:
        assert ss.load_known_secret_values(tmp_path / "nope.md") == set()


class TestFilenames:
    @pytest.mark.parametrize(
        "name",
        [
            ".env",
            "backend/.env.production",
            "keys/server.pem",
            ".local/SECRETS.local.md",
            "client_secret_123.json",
            "NOTES.local.md",
        ],
    )
    def test_forbidden(self, name: str) -> None:
        assert ss.check_filename(name) is not None

    @pytest.mark.parametrize(
        "name", [".env.example", "frontend/.env.example", "README.md", "src/env.ts"]
    )
    def test_allowed(self, name: str) -> None:
        assert ss.check_filename(name) is None


class TestDiffParsing:
    def test_reports_added_lines_with_file_and_line_number(self) -> None:
        token = "gh" + "p_" + "A" * 36
        diff = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ -1,0 +10,2 @@\n+ok = 1\n+" + f"t = '{token}'\n"
        )
        (finding,) = ss.scan_diff(diff, known=set(), label_commit=False)
        assert (finding.where, finding.line, finding.rule) == ("x.py", 11, "github-token")

    def test_removed_lines_are_ignored(self) -> None:
        token = "gh" + "p_" + "A" * 36
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +0,0 @@\n-" + f"t = '{token}'\n"
        assert ss.scan_diff(diff, known=set(), label_commit=False) == []


class TestBundleScanning:
    def test_flags_server_only_names_leaking_into_client_code(self, tmp_path: Path) -> None:
        (tmp_path / "chunk.js").write_text("var a=process.env.SUPABASE_SERVICE_ROLE_KEY;export{a}")
        findings = ss.scan_paths([tmp_path], known=set(), forbid_names=True, allow_anon_jwt=True)
        assert [f.rule for f in findings] == ["server-only-name-in-client-code"]

    def test_clean_bundle_passes(self, tmp_path: Path) -> None:
        (tmp_path / "chunk.js").write_text(
            "console.log('hello'); const url='https://api.example.com'"
        )
        assert ss.scan_paths([tmp_path], known=set(), forbid_names=True, allow_anon_jwt=True) == []

    def test_binary_and_oversized_files_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "a.png").write_bytes(b"\x89PNG\x00\x00")
        assert ss.scan_paths([tmp_path], known=set(), forbid_names=True, allow_anon_jwt=False) == []
