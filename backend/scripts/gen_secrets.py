"""Print fresh values for ENCRYPTION_KEY and JWT_SECRET (nothing is written to disk).

python scripts/gen_secrets.py
"""

from __future__ import annotations

from app.core.security import Encryptor, generate_secret


def main() -> None:
    print("# Paste these into backend/.env (and your host's secret store). Do NOT commit them.")
    print(f"ENCRYPTION_KEY={Encryptor.generate_key()}")
    print(f"JWT_SECRET={generate_secret(48)}")


if __name__ == "__main__":
    main()
