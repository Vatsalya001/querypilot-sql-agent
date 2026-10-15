"""The health endpoint reports the state of the startup prompt-cache warm-up."""

import asyncio
import contextlib
from types import SimpleNamespace

from app.api.routes import _warmup_status


def _request(**state: object) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


async def test_warmup_status_tracks_the_task_lifecycle() -> None:
    assert _warmup_status(_request()) == "skipped"

    gate = asyncio.Event()

    async def warm() -> bool:
        await gate.wait()
        return True

    pending = asyncio.create_task(warm())
    await asyncio.sleep(0)
    assert _warmup_status(_request(warmup_task=pending)) == "warming"
    gate.set()
    await pending
    assert _warmup_status(_request(warmup_task=pending)) == "ready"


async def test_warmup_status_skipped_and_failed() -> None:
    async def not_applicable() -> bool:
        return False

    async def boom() -> bool:
        raise RuntimeError("model unavailable")

    skipped = asyncio.create_task(not_applicable())
    await skipped
    assert _warmup_status(_request(warmup_task=skipped)) == "skipped"

    failed = asyncio.create_task(boom())
    with contextlib.suppress(RuntimeError):
        await failed
    assert _warmup_status(_request(warmup_task=failed)) == "failed"
