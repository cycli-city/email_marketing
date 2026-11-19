"""Export the OpenAPI schema for the frontend's generated, contract-first TypeScript types.

python scripts/export_openapi.py            # write frontend/openapi/openapi.json
python scripts/export_openapi.py --check    # exit 1 if the committed file is out of date (CI)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["AOI_DISABLE_DOTENV"] = "1"

from app.core.config import Settings
from app.main import create_app

TARGET = Path(__file__).resolve().parents[2] / "frontend" / "openapi" / "openapi.json"


def render() -> str:
    # Explicit blanks so the export never depends on the caller's environment.
    settings = Settings(_env_file=None, app_env="development", encryption_key="", jwt_secret="")
    schema = create_app(settings).openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    rendered = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(
                "OpenAPI schema drift: run `python scripts/export_openapi.py` and "
                "`npm run generate:api` in frontend/, then commit the result.",
                file=sys.stderr,
            )
            return 1
        print("OpenAPI schema is up to date.")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
