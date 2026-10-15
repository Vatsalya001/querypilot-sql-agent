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


async def test_an_unreachable_local_model_is_reported_as_failed_not_skipped() -> None:
    # docs/API.md: "skipped" means not applicable (hosted provider, mock, LLM_WARMUP=false). The Ollama warm-up
    # returns False when the server is unreachable, which used to be reported as "skipped" too.
    async def unreachable() -> bool:
        return False

    task = asyncio.create_task(unreachable())
    await task
    ollama = SimpleNamespace(llm_provider="ollama", llm_warmup=True)
    assert _warmup_status(_request(warmup_task=task, settings=ollama)) == "failed"
    for settings in (SimpleNamespace(llm_provider="groq", llm_warmup=True),
                     SimpleNamespace(llm_provider="ollama", llm_warmup=False)):
        assert _warmup_status(_request(warmup_task=task, settings=settings)) == "skipped"
