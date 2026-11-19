"""Run a command against a throwaway local PostgreSQL (pip-installed ``pgserver``; no Docker needed).

    python scripts/with_pg.py -- alembic upgrade head
    python scripts/with_pg.py --keep-data -- pytest -m integration

Exports ``DATABASE_URL`` / ``DATABASE_URL_TEST`` (asyncpg form) for the child process. The data dir is
``backend/.pgdata/<name>`` (git-ignored). Without ``--keep-data`` the cluster is wiped first.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pgserver

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="dev")
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = [c for c in args.cmd if c != "--"]
    if not cmd:
        parser.error("no command given")

    data_dir = ROOT / ".pgdata" / args.name
    if data_dir.exists() and not args.keep_data:
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    server = pgserver.get_server(data_dir, cleanup_mode="stop")
    try:
        uri = server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
        env = dict(os.environ)
        env["DATABASE_URL"] = uri
        env["DATABASE_URL_TEST"] = uri
        env["APP_ENV"] = env.get("APP_ENV", "test")
        scripts_dir = str(Path(sys.executable).parent)
        env["PATH"] = scripts_dir + os.pathsep + env.get("PATH", "")
        # Resolve console scripts (alembic, pytest, ruff) from the venv.
        exe = Path(scripts_dir) / f"{cmd[0]}.exe"
        if exe.exists():
            cmd[0] = str(exe)
        return subprocess.call(cmd, cwd=ROOT, env=env)
    finally:
        server.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
