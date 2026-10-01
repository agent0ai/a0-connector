"""Remote input binds to captured targets, with no actual desktop effects."""
import asyncio
from types import SimpleNamespace
import pytest
from agent_zero_cli.host_control import ControlError
from agent_zero_cli.host_viewer import HostViewer


def test_browser_rejects_changed_viewport_before_mouse_effect():
    async def run():
        class Page:
            url = "https://example.test"
            async def evaluate(self, _): return {"width": 800, "height": 600}
        page = Page()
        viewer = HostViewer(SimpleNamespace(_scope_available=lambda _: True), None)
        viewer.browser = lambda _: (None, 1, page)
        frame = {"context":"chat", "page":page, "browser_id":1, "url":page.url, "geometry":{"width":400,"height":600}}
        with pytest.raises(ControlError, match="viewport"):
            await viewer.input({"context":"chat","source":"browser","input":{"kind":"click","x":.5,"y":.5}},frame)
    asyncio.run(run())


def test_computer_rechecks_geometry_and_translates_keyboard():
    async def run():
        calls = []
        current = SimpleNamespace(session_id="session")
        class Manager:
            _sessions = {"chat":current}
            async def handle_op(self, payload):
                calls.append(payload)
                return {"ok":True,"result":{"width":1440,"height":900}}
        viewer = HostViewer(SimpleNamespace(computer_use=Manager(),_scope_available=lambda _:True),None)
        frame = {"context":"chat","session_id":"session","geometry":(1440,900)}
        request = {"context":"chat","source":"computer_use","sequence":1,"input":{"kind":"key","keys":["Meta","a"]}}
        await viewer.input(request,frame)
        assert [p["action"] for p in calls] == ["capture","key"]
        assert calls[-1]["keys"] == ["super","a"]
        calls.clear(); frame["geometry"] = (1280,800)
        with pytest.raises(ControlError,match="geometry"):
            await viewer.input(request,frame)
        assert [p["action"] for p in calls] == ["capture"]
    asyncio.run(run())


def test_browser_drag_releases_mouse_after_failure():
    async def run():
        calls = []
        class Mouse:
            async def move(self,*_,**kw):
                calls.append("move")
                if kw: raise RuntimeError("lost drag")
            async def down(self): calls.append("down")
            async def up(self): calls.append("up")
        class Page:
            url = "https://example.test"
            mouse = Mouse()
            async def evaluate(self,_): return {"width":800,"height":600}
        page = Page()
        viewer = HostViewer(SimpleNamespace(_scope_available=lambda _:True),None)
        viewer.browser = lambda _: (None,1,page)
        frame = {"context":"chat","page":page,"browser_id":1,"url":page.url,"geometry":{"width":800,"height":600}}
        with pytest.raises(RuntimeError):
            await viewer.input({"context":"chat","source":"browser","input":{"kind":"drag","x":.1,"y":.1,"end_x":.8,"end_y":.8}},frame)
        assert calls == ["move","down","move","up"]
    asyncio.run(run())


def test_return_waits_for_inflight_capture_and_keeps_command_routing(tmp_path):
    from agent_zero_cli.host_control import HostControl
    async def run():
        gate = HostControl(tmp_path / "gate.json"); gate.enabled = True
        state = await gate.acquire("a"*32,"chat")
        current = SimpleNamespace(pages=[1])
        session = SimpleNamespace(host_browser=SimpleNamespace(_sessions={"chat":current}),_scope_available=lambda _:True)
        viewer = HostViewer(session,gate)
        async def capture(_): return {"fresh":True}
        viewer.frame = capture
        await viewer.frame_lock.acquire()
        task = asyncio.create_task(viewer.handle({**state,"source":"browser","command":"prepare_return"}))
        await asyncio.sleep(0)
        assert not task.done()
        assert gate.state["phase"] == "returning"
        viewer.frame_lock.release()
        assert await task == {"fresh":True}
        assert (await viewer.handle({**state,"command":"release"}))["phase"] == "watching"
        assert await viewer.handle({"command":"frame","source":"browser","context":"chat"}) == {"fresh":True}
    asyncio.run(run())
