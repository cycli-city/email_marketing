"""Scriptable provider for tests and local development. Production refuses mock providers (config validator)."""

from __future__ import annotations

import inspect
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from app.providers.registry import Provider
from app.providers.spec import ProviderResult, ProviderSpec

Step = ProviderResult[str] | BaseException | Callable[[], Any] | str


class MockProvider(Provider):
    """Each call consumes the next scripted step: an exception to raise, a value / ``ProviderResult`` to return, or a
    (sync or async) callable whose result is used the same way. When the script is empty it succeeds with
    ``"ok:<key>"``. ``calls`` counts every attempt, including in-call retries."""

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        configured: bool = True,
        script: list[Step] | None = None,
    ) -> None:
        self.spec = spec
        self._configured = configured
        self._script: deque[Step] = deque(script or [])
        self.calls = 0

    @property
    def configured(self) -> bool:
        return self._configured

    def set_configured(self, value: bool) -> None:
        self._configured = value

    def queue(self, *steps: Step) -> None:
        self._script.extend(steps)

    async def respond(self) -> ProviderResult[str]:
        self.calls += 1
        step: Any = self._script.popleft() if self._script else f"ok:{self.spec.key}"
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step = step()
            if inspect.isawaitable(step):
                step = await step
            if isinstance(step, BaseException):
                raise step
        if isinstance(step, ProviderResult):
            return step
        return ProviderResult(str(step))


def respond(provider: Any) -> Awaitable[ProviderResult[str]]:
    """Operation for ``ProviderManager.call`` that exercises a ``MockProvider``."""
    result: Awaitable[ProviderResult[str]] = provider.respond()
    return result
