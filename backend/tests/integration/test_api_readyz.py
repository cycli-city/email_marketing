from __future__ import annotations

import httpx
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis

from app.core.config import Settings
from app.main import create_app


async def test_readyz_is_ready_with_real_postgres_and_redis(migrated_database: str) -> None:
    settings = Settings(
        _env_file=None,
        encryption_key=Fernet.generate_key().decode(),
        jwt_secret="j" * 40,
        database_url=migrated_database,
    )
    app = create_app(settings, redis=FakeAsyncRedis())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}
