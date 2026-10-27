import asyncio
from types import SimpleNamespace

from agentdeck.auth_watch import AuthWatch


async def test_auth_watch_owns_refresh_task_until_it_finishes(monkeypatch):
    watch = AuthWatch()
    account = SimpleNamespace(
        provider_id="claude_code",
        key="claude_code:test",
        root="/tmp/agentdeck-auth-test",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh_async(_account):
        started.set()
        await release.wait()

    monkeypatch.setattr(watch, "refresh_async", refresh_async)

    watch.request_refresh((account,))
    await started.wait()

    assert account.key in watch._refreshing
    assert len(watch._refresh_tasks) == 1
    task = next(iter(watch._refresh_tasks))
    assert not task.done()

    # A render while the first status read is still running must not start a
    # second subprocess or replace the task the watch is responsible for.
    watch.request_refresh((account,))
    assert watch._refresh_tasks == {task}

    release.set()
    await task
    await asyncio.sleep(0)

    assert account.key not in watch._refreshing
    assert watch._refresh_tasks == set()
