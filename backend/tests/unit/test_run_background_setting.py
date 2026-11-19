"""When does a process run the job worker?"""

from __future__ import annotations

import pytest

from tests.helpers import make_settings


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"app_env": "development"}, True),  # the normal case
        ({"app_env": "test"}, False),  # tests start background work only when they ask for it
        ({"app_env": "test", "background_enabled": True}, True),
        ({"app_env": "development", "background_enabled": False}, False),
        ({"app_role": "api"}, False),  # an API-only process never runs the worker
        ({"app_role": "api", "background_enabled": True}, False),  # ... even if asked to
        ({"app_role": "worker", "app_env": "development"}, True),
    ],
)
def test_run_background(overrides: dict[str, object], expected: bool) -> None:
    assert make_settings(**overrides).run_background is expected
