"""Local, in-memory Redis-compatible server for development on machines without Redis (e.g. this one).

    python scripts/dev_redis.py [--port 6379]

DEVELOPMENT ONLY: data lives in memory and is lost on exit. Production uses Upstash / Render Key-Value,
and CI uses a real ``redis:7`` service container.
"""

from __future__ import annotations

import argparse

from fakeredis import TcpFakeServer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    args = parser.parse_args()
    server = TcpFakeServer((args.host, args.port), server_type="redis")
    print(f"fake redis listening on {args.host}:{args.port} (dev only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
