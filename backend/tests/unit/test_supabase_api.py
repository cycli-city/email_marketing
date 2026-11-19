"""Tests for the Supabase helper's pure functions (no network, no real credentials)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "supabase_api.py"
SCAN = Path(__file__).resolve().parents[3] / "scripts" / "scan_secrets.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sb = _load("supabase_api", SCRIPT)
scan = _load("scan_secrets_for_sb", SCAN)


def test_mask_hides_sensitive_fields_at_any_depth() -> None:
    data = {
        "name": "p",
        "api_key": "x",
        "nested": [{"db_password": "y", "ok": 1}],
        "connection_string": "z",
    }
    masked = sb.mask(data)
    assert masked["name"] == "p"
    assert masked["api_key"] == "***masked***"
    assert masked["nested"][0] == {"db_password": "***masked***", "ok": 1}
    assert masked["connection_string"] == "***masked***"


def test_set_secret_replaces_value_and_keeps_trailing_comment(tmp_path: Path) -> None:
    f = tmp_path / "secrets.md"
    f.write_text("- DATABASE_URL=            # how to find it\n- OTHER=keep\n", encoding="utf-8")
    sb.set_secret("DATABASE_URL", "postgresql://u:p@h/db", f)
    text = f.read_text(encoding="utf-8")
    assert (
        "- DATABASE_URL=postgresql://u:p@h/db  # how to find it" in text
        or "# how to find it" in text
    )
    assert "OTHER=keep" in text


def test_set_secret_appends_when_missing(tmp_path: Path) -> None:
    f = tmp_path / "secrets.md"
    f.write_text("- A=1\n", encoding="utf-8")
    sb.set_secret("NEW_ONE", "abc", f)
    assert f.read_text(encoding="utf-8").splitlines()[-1] == "- NEW_ONE=abc"


def test_scanner_treats_the_anon_key_as_public_but_service_role_as_secret(tmp_path: Path) -> None:
    f = tmp_path / "s.md"
    f.write_text(
        "- SUPABASE_ANON_KEY=anonvalue12345\n- SUPABASE_SERVICE_ROLE_KEY=servicevalue12345\n",
        encoding="utf-8",
    )
    known = scan.load_known_secret_values(f)
    assert known == {"servicevalue12345"}
