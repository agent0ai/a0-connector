"""Ownership races use explicit barriers; no real host, config, or sleeps."""
import asyncio
import pytest
from agent_zero_cli.host_control import HostControl, ControlError


def gate(tmp_path):
    value = HostControl(tmp_path / "control.json")
    value.enabled = True
    return value


def test_takeover_fences_queued_work_before_drain_and_return(tmp_path):
    async def scenario():
        g = gate(tmp_path)
        old = g.state["epoch"]
        started, finish = asyncio.Event(), asyncio.Event()
        async def operation():
            async with g.operation({"host_epoch": old}):
                started.set()
                await finish.wait()
        task = asyncio.create_task(operation())
        await started.wait()
        acquire = asyncio.create_task(g.acquire("a" * 32, "chat"))
        # Scheduling barrier after the acquire task has installed its fence.
        await asyncio.sleep(0)
        assert g.state["phase"] == "held" and not acquire.done()
        with pytest.raises(ControlError):
            async with g.operation({"host_epoch": old}):
                pytest.fail("queued agent command executed")
        finish.set()
        await task
        state = await acquire
        assert state["phase"] == "human"
        request = {key: state[key] for key in ("lease", "epoch", "context")}
        await g.prepare_return(request, lambda: asyncio.sleep(0, result={"fresh": True}))
        await g.release(request)
        with pytest.raises(ControlError):
            async with g.operation({"host_epoch": old}):
                pytest.fail("pre-takeover command executed after handback")
        async with g.operation({"host_epoch": g.state["epoch"]}):
            pass
    asyncio.run(scenario())


def test_expiry_restart_and_second_viewer_never_resume_automation(tmp_path):
    async def scenario():
        g = gate(tmp_path)
        await g.acquire("a" * 32, "chat")
        g.deadline = -1
        assert g.snapshot()["phase"] == "held"
        restored = gate(tmp_path)
        assert restored.snapshot()["phase"] == "held"
        with pytest.raises(ControlError):
            await restored.acquire("b" * 32, "other")
        with pytest.raises(ControlError):
            async with restored.operation({"host_epoch": restored.state["epoch"]}):
                pytest.fail("expired lease resumed automation")
    asyncio.run(scenario())


def test_background_execution_prevents_human_acknowledgement(tmp_path):
    async def scenario():
        g = gate(tmp_path)
        with pytest.raises(ControlError, match="execution session"):
            await g.acquire("a" * 32, "chat", blocked=lambda: True)
        assert g.snapshot()["phase"] == "held"
    asyncio.run(scenario())


def test_input_consumed_before_effect_and_never_replayed(tmp_path):
    async def scenario():
        g = gate(tmp_path)
        state = await g.acquire("a" * 32, "chat")
        payload = {**state, "source": "browser", "frame": "frame", "sequence": 1}
        g.frames["browser"] = {"id": "frame", "time": g.clock()}
        calls = []
        async def effect(_):
            calls.append(1)
            raise RuntimeError("acknowledgement lost")
        with pytest.raises(RuntimeError):
            await g.input(payload, effect)
        assert calls == [1] and g.state["sequence"] == 1 and g.state["phase"] == "held"
        with pytest.raises(ControlError):
            await g.input(payload, effect)
        assert calls == [1]
    asyncio.run(scenario())


def test_stale_frame_rejected_and_return_waits_for_input(tmp_path):
    async def scenario():
        g = gate(tmp_path)
        state = await g.acquire("a" * 32, "chat")
        payload = {**state, "source": "browser", "frame": "old", "sequence": 1}
        g.frames["browser"] = {"id": "fresh", "time": g.clock()}
        with pytest.raises(ControlError, match="stale"):
            await g.input(payload, lambda _: pytest.fail("stale input ran"))
        entered, finish = asyncio.Event(), asyncio.Event()
        async def effect(_):
            entered.set()
            await finish.wait()
        payload["frame"] = "fresh"
        running = asyncio.create_task(g.input(payload, effect))
        await entered.wait()
        returning = asyncio.create_task(g.prepare_return(state, lambda: asyncio.sleep(0, result="fresh")))
        await asyncio.sleep(0)
        assert not returning.done()
        finish.set()
        await running
        assert await returning == "fresh"
    asyncio.run(scenario())


def test_corrupt_journal_fails_closed(tmp_path):
    (tmp_path / "control.json").write_text("incomplete")
    assert gate(tmp_path).state["phase"] == "held"


def test_session_routes_all_four_families_through_the_same_gate(tmp_path, monkeypatch):
    from agent_zero_cli import config
    from agent_zero_cli.session import ConnectorSession
    monkeypatch.setattr(config, "_ENV_DIR", tmp_path)
    async def scenario():
        session = ConnectorSession(config.CLIConfig(), workspace=tmp_path)
        session.host_control.enabled = True
        await session.host_control.acquire("a" * 32, "chat")
        for handler in (session._handle_browser_op, session._handle_computer_use_op,
                        session._handle_file_op, session._handle_exec_op):
            result = await handler({"op_id":"operation", "context_id":"competing-chat"})
            assert result["ok"] is False and result["code"] == "HOST_HELD"
        session.host_control.enabled = False  # a downgrade cannot release a hold
        result = await session._handle_exec_op({"op_id":"operation"})
        assert result["code"] == "HOST_HELD"
    asyncio.run(scenario())


def test_displayed_frame_survives_two_new_captures_but_expires(tmp_path):
    async def scenario():
        g = gate(tmp_path)
        state = await g.acquire("a" * 32, "chat")
        for identity in ("displayed", "pending", "newest"):
            g.remember_frame("browser", {"id": identity, "time": g.clock()})
        received = []
        async def effect(frame):
            received.append(frame["id"])
        request = {**state, "source": "browser", "frame": "displayed", "sequence": 1}
        await g.input(request, effect)
        assert received == ["displayed"]
        g.remember_frame("browser", {"id": "next", "time": g.clock()})
        with pytest.raises(ControlError, match="stale"):
            await g.input({**request, "sequence": 2}, effect)
        assert received == ["displayed"]
    asyncio.run(scenario())
