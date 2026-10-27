import pytest

from app.integrations.retry import call_with_backoff


class Transient(Exception):
    pass


class Fatal(Exception):
    pass


def _hint(exc: BaseException) -> float | None:
    if isinstance(exc, Transient):
        return 0.0
    return None


async def test_returns_result_after_transient_failures(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.integrations.retry.asyncio.sleep", fake_sleep)
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Transient("try again")
        return "ok"

    assert await call_with_backoff(flaky, retry_hint=_hint, base_delay=1.0) == "ok"
    assert attempts["n"] == 3
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # exponential growth, even with jitter


async def test_non_retryable_error_is_raised_immediately():
    attempts = {"n": 0}

    async def broken() -> None:
        attempts["n"] += 1
        raise Fatal("no")

    with pytest.raises(Fatal):
        await call_with_backoff(broken, retry_hint=_hint)
    assert attempts["n"] == 1


async def test_gives_up_after_max_attempts(monkeypatch):
    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.integrations.retry.asyncio.sleep", fake_sleep)

    async def always_transient() -> None:
        raise Transient("down")

    with pytest.raises(Transient):
        await call_with_backoff(always_transient, retry_hint=_hint, max_attempts=3)


async def test_retry_after_hint_is_honoured(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.integrations.retry.asyncio.sleep", fake_sleep)
    attempts = {"n": 0}

    async def rate_limited() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise Transient("429")
        return "ok"

    await call_with_backoff(rate_limited, retry_hint=lambda e: 60.0 if isinstance(e, Transient) else None)
    assert sleeps[0] >= 60.0
