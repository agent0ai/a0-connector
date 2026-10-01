import asyncio
import base64
from types import SimpleNamespace

import pytest
from agent_zero_cli.host_control import HostControl, ControlError
from agent_zero_cli.setup_verification import verify_connection
from agent_zero_cli.host_browser_manager import RELAUNCH_CONTEXT_ID


def test_browser_check_outlives_native_approval_handshake(monkeypatch):
    from agent_zero_cli.host_browser_common import REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS
    from agent_zero_cli import setup_verification
    timeouts = []
    async def bounded(coroutine, *, timeout):
        coroutine.close()
        timeouts.append(timeout)
        return {}
    monkeypatch.setattr(setup_verification.asyncio, 'wait_for', bounded)
    asyncio.run(verify_connection(None, 'browser'))
    asyncio.run(verify_connection(None, 'computer_use'))
    assert REMOTE_DEBUGGING_CONNECT_TIMEOUT_SECONDS < timeouts[0] < 100
    assert timeouts[1] == 40


class Browser:
    def __init__(self, fail=False):
        self._sessions = {}
        self.calls = []
        self.fail = fail
        self.page = SimpleNamespace(evaluate=self.evaluate, screenshot=self.screenshot)

    async def evaluate(self, script):
        return "Agent Zero connected" if "?.value" in script else None

    async def screenshot(self, **kwargs):
        if self.fail:
            raise ValueError("capture failed")
        return b"capture"

    async def handle_op(self, payload):
        self.calls.append(payload)
        if payload["action"] == "open":
            async def current_url(page):
                return "about:blank"
            self._sessions[payload["context_id"]] = SimpleNamespace(
                _page=lambda ident: self.page, _runtime=SimpleNamespace(current_url=current_url))
        return {"ok": True, "result": {"id": 7}}


def session(tmp_path, browser=None):
    return SimpleNamespace(_scope_available=lambda key: True,
        host_control=HostControl(tmp_path / "control.json"), host_browser=browser or Browser())


@pytest.mark.parametrize("fail", [False, True])
def test_browser_closes_only_owned_page_and_preserves_prepared_profile(tmp_path, fail):
    async def scenario():
        browser = Browser(fail)
        s = session(tmp_path, browser)
        before = s.host_control.snapshot().copy()
        if fail:
            with pytest.raises(ValueError):
                await verify_connection(s, "browser")
        else:
            result = await verify_connection(s, "browser")
            assert result["evidence"] == ["test_page_input", "fresh_capture"]
            assert "capture" not in result
        assert [x["action"] for x in browser.calls] == ["open", "keyboard", "close"]
        assert browser.calls[-1]["browser_id"] == 7
        assert list(browser._sessions) == [RELAUNCH_CONTEXT_ID]
        assert s.host_control.snapshot() == before
        assert not s.host_control.setup_active
    asyncio.run(scenario())


def test_hold_and_revoked_scope_prevent_any_browser_work(tmp_path):
    async def scenario():
        s = session(tmp_path)
        s.host_control.state["phase"] = "held"
        with pytest.raises(ControlError): await verify_connection(s, "browser")
        s.host_control.state["phase"] = "watching"
        s._scope_available = lambda key: False
        with pytest.raises(ValueError): await verify_connection(s, "browser")
        assert not s.host_browser.calls
    asyncio.run(scenario())


def test_verification_blocks_agent_work_until_cleanup(tmp_path):
    async def scenario():
        gate = session(tmp_path).host_control
        async with gate.setup_operation():
            with pytest.raises(ControlError):
                async with gate.operation({}): pytest.fail("agent ran")
        async with gate.operation({}): pass
    asyncio.run(scenario())


def test_computer_test_never_sends_input_or_returns_pixels(tmp_path):
    async def scenario():
        s = session(tmp_path)
        calls = []
        async def operation(payload):
            calls.append(payload["action"])
            return {"ok":True,"result":{"artifact":{"data":base64.b64encode(b"fresh pixels").decode()}}}
        s.computer_use = SimpleNamespace(handle_op=operation,_sessions={})
        result = await verify_connection(s,"computer_use")
        assert calls == ["start_session","capture","stop_session"]
        assert result["evidence"] == ["fresh_capture"]
        assert "pixels" not in str(result)
    asyncio.run(scenario())
