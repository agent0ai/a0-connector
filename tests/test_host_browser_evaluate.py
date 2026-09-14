import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_zero_cli.config import CLIConfig
from agent_zero_cli.host_browser_cdp import CDPConnection, CDPContext, CDPPage
from agent_zero_cli.host_browser_manager import HostBrowserManager
from agent_zero_cli.host_browser_session import (
    HostBrowserPage, HostBrowserSession, normalize_evaluate_timeout,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _runtime(monkeypatch, cdp=False):
    session = HostBrowserSession("ctx", profile=None)
    protocol = SimpleNamespace(send=AsyncMock(), detach=AsyncMock())
    if cdp:
        page = CDPPage(CDPContext(SimpleNamespace()), "target", "session")
        page.send = protocol.send
        page.close = AsyncMock()
    else:
        page = SimpleNamespace(
            context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=protocol)),
            is_closed=lambda: False, close=AsyncMock(),
        )
    page.evaluate = AsyncMock(return_value=2)
    page.reload = AsyncMock()
    session.pages[1] = HostBrowserPage(1, page)
    monkeypatch.setattr(session, "ensure_started", AsyncMock())
    monkeypatch.setattr(session, "_state", AsyncMock(return_value={"id": 1}))
    return session, page, protocol


def _hang(page):
    started, stopped = asyncio.Event(), asyncio.Event()
    operations = []

    async def evaluate(script, **kwargs):
        if script != "hang":
            return 2
        operations.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.wait_for(stopped.wait(), 1)
        except asyncio.TimeoutError:
            raise RuntimeError("Fake backend watchdog expired") from None
        raise RuntimeError("Execution context destroyed")

    async def recover(**kwargs):
        stopped.set()

    page.evaluate.side_effect = evaluate
    page.reload.side_effect = recover
    page.close.side_effect = recover
    return started, stopped, operations


@pytest.mark.parametrize("cdp", [False, True])
async def test_host_timeout_recovers_and_preserves_multi_results(monkeypatch, cdp):
    session, page, protocol = _runtime(monkeypatch, cdp)
    _, stopped, operations = _hang(page)
    before = asyncio.all_tasks()
    results = await asyncio.wait_for(session.dispatch({
        "action": "multi", "evaluate_timeout_seconds": 0.1, "calls": [
            {"action": "evaluate", "browser_id": 1, "script": "hang"},
            {"action": "evaluate", "browser_id": 1, "script": "1+1"},
        ],
    }), 2)
    assert results == [
        {"ok": False, "error": "evaluate timed out after 0.1 seconds; affected tab reloaded to cancel pending JavaScript"},
        {"ok": True, "result": {"result": 2, "state": {"id": 1}}},
    ]
    protocol.send.assert_awaited_once_with("Runtime.terminateExecution")
    assert stopped.is_set() and all(task.done() for task in operations)
    assert not session.pages[1].evaluate_lock.locked()
    assert not (asyncio.all_tasks() - before)
    assert protocol.detach.await_count == (0 if cdp else 2)
    page.close.assert_not_awaited()


@pytest.mark.parametrize("cdp", [False, True])
@pytest.mark.parametrize("caller_cancel", [False, True])
async def test_host_confirmed_interruption_preserves_page(monkeypatch, cdp, caller_cancel):
    session, page, protocol = _runtime(monkeypatch, cdp)
    started, stopped, operations = _hang(page)
    protocol.send.side_effect = lambda method: stopped.set()
    task = asyncio.create_task(session.evaluate(1, "hang", timeout=0.1))
    await asyncio.wait_for(started.wait(), 1)
    if caller_cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if caller_cancel else TimeoutError) as error:
        await asyncio.wait_for(task, 2)
    if not caller_cancel:
        assert str(error.value) == "evaluate timed out after 0.1 seconds"
    page.reload.assert_not_awaited()
    page.close.assert_not_awaited()
    assert session.pages[1].page is page
    assert all(operation.done() and not operation.cancelled() for operation in operations)
    assert (await session.evaluate(1, "1+1"))["result"] == 2


@pytest.mark.parametrize("cdp", [False, True])
async def test_host_cancellation_ack_waits_for_browser_recovery(monkeypatch, cdp):
    session, page, protocol = _runtime(monkeypatch, cdp)
    started, stopped, operations = _hang(page)
    manager = HostBrowserManager(CLIConfig(host_browser_enabled=True))

    async def dispatch(payload):
        return {"ok": True, "result": await session.dispatch(payload)}

    monkeypatch.setattr(manager, "_handle_op", dispatch)
    call = asyncio.create_task(manager.handle_op({
        "op_id": "run", "context_id": "ctx", "action": "evaluate",
        "browser_id": 1, "script": "hang", "evaluate_timeout_seconds": 0.5,
    }))
    await asyncio.wait_for(started.wait(), 1)
    wrong_context = await manager.handle_op({
        "op_id": "wrong", "context_id": "another-chat", "action": "cancel_evaluate", "target_op_id": "run",
    })
    assert wrong_context["ok"] is False and not stopped.is_set()
    result = await asyncio.wait_for(manager.handle_op({
        "op_id": "cancel", "context_id": "ctx", "action": "cancel_evaluate", "target_op_id": "run",
    }), 2)
    assert result["ok"] is True and result["result"] == {"cancelled": True}
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not manager._evaluate_ops
    assert stopped.is_set() and all(task.done() for task in operations)
    assert (await session.evaluate(1, "1+1"))["result"] == 2


async def test_host_disconnect_recovers_before_closing_session(monkeypatch):
    session, page, _ = _runtime(monkeypatch)
    started, stopped, _ = _hang(page)
    manager = HostBrowserManager(CLIConfig(host_browser_enabled=True))
    manager._sessions["ctx"] = session

    async def dispatch(payload):
        return {"ok": True, "result": await session.dispatch(payload)}

    async def close():
        assert stopped.is_set()

    monkeypatch.setattr(session, "close", close)
    monkeypatch.setattr(manager, "_handle_op", dispatch)
    call = asyncio.create_task(manager.handle_op({
        "op_id": "run", "context_id": "ctx", "action": "evaluate", "browser_id": 1, "script": "hang",
    }))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(manager.disconnect(), 2)
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not manager._evaluate_ops and not manager._sessions


@pytest.mark.parametrize("close_fails", [False, True])
async def test_host_failed_reload_closes_only_affected_tab(monkeypatch, close_fails):
    session, page, protocol = _runtime(monkeypatch)
    _, _, operations = _hang(page)
    other = HostBrowserPage(2, SimpleNamespace())
    session.pages[2] = other
    page.reload.side_effect = RuntimeError("reload failed")
    if close_fails:
        page.close.side_effect = RuntimeError("close failed")
    with pytest.raises(TimeoutError, match="0.1 seconds") as error:
        await asyncio.wait_for(session.evaluate(1, "hang", timeout=0.1), 2)
    assert ("recovery failed" in str(error.value)) is close_fails
    assert (1 in session.pages) is close_fails
    assert session.pages[2] is other
    assert all(task.done() for task in operations)
    protocol.detach.assert_awaited_once()


async def test_safari_evaluate_is_rejected_before_start(monkeypatch):
    session, page, protocol = _runtime(monkeypatch)
    session.profile = SimpleNamespace(is_safari=True)
    with pytest.raises(RuntimeError, match="cannot forcibly interrupt"):
        await session.evaluate(1, "1+1")
    session.ensure_started.assert_not_awaited()
    page.evaluate.assert_not_awaited()


@pytest.mark.parametrize("cdp", [False, True])
async def test_host_provider_wait_timeout_does_not_confirm_javascript_cancellation(monkeypatch, cdp):
    session, page, protocol = _runtime(monkeypatch, cdp)
    page.evaluate.side_effect = asyncio.TimeoutError
    with pytest.raises(TimeoutError, match="reloaded to cancel pending JavaScript"):
        await session.evaluate(1, "hang", timeout=0.1)
    protocol.send.assert_awaited_once_with("Runtime.terminateExecution")
    page.reload.assert_awaited_once()


@pytest.mark.parametrize("value", [None, True, "", "bad", 0, -1, 0.09, 60.1, float("inf"), float("nan")])
async def test_host_rejects_invalid_timeout_before_start(monkeypatch, value):
    session, _, _ = _runtime(monkeypatch)
    with pytest.raises(ValueError, match="between 0.1 and 60 seconds"):
        await session.evaluate(1, "1+1", timeout=value)
    session.ensure_started.assert_not_awaited()
    assert normalize_evaluate_timeout("0.1") == 0.1


@pytest.mark.parametrize("during_send", [False, True])
async def test_cdp_cancellation_clears_protocol_future_and_lock(during_send):
    connection = CDPConnection("ws://unused")
    started = asyncio.Event()

    async def send_json(payload):
        started.set()
        if during_send:
            await asyncio.sleep(1)

    connection._ws = SimpleNamespace(send_json=send_json)
    operation = asyncio.create_task(connection.command("Runtime.evaluate", {"expression": "1+1"}))
    await asyncio.wait_for(started.wait(), 1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, 1)
    assert not connection._pending
    assert not connection._send_lock.locked()
