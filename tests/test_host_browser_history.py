"""A history entry may belong to an iframe while the top document stays ready."""
import asyncio
import os

import pytest
from aiohttp import web

from agent_zero_cli import host_browser_session
from agent_zero_cli.host_browser import BrowserProfile, HostBrowserSession
from agent_zero_cli.host_browser_cdp import CDPError


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("A0_TEST_CHROMIUM"), reason="set A0_TEST_CHROMIUM to run Chromium"),
]


@pytest.mark.parametrize("frame", ["top", "cached", "same-site", "cross-site", "site-switch"])
async def test_native_chromium_history_waits_for_traversed_document(tmp_path, monkeypatch, frame):
    requested, release = asyncio.Event(), asyncio.Event()
    release.set()
    headers = {"Cache-Control": "public, max-age=600" if frame == "cached" else "no-store"}

    async def serve(request):
        if request.path == "/parser.js":
            requested.set()
            await release.wait()
            return web.Response(text="", content_type="text/javascript", headers=headers)
        if request.path == "/parent":
            first_origin = origin if frame == "site-switch" else child_origin
            body = f"<iframe src='{first_origin}/one'></iframe>"
            if frame == "cross-site":
                body += f"<iframe id='transient' src='{child_origin}/unrelated'></iframe>"
        elif request.path == "/unrelated":
            body = "<title>Transient sibling</title>"
        else:
            body = "<script src='/parser.js'></script><script>top.postMessage(location.pathname, '*')</script>"
        events = "<script>onmessage=e=>window.last=e.data; onpageshow=e=>window.restored=e.persisted</script>"
        return web.Response(text=events + body, content_type="text/html", headers=headers)

    app = web.Application()
    app.router.add_get("/{path}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    port = runner.addresses[0][1]
    origin = f"http://127.0.0.1:{port}"
    child_origin = f"http://localhost:{port}" if frame in {"cross-site", "site-switch"} else origin
    original_flags = host_browser_session.chromium_launch_args
    monkeypatch.setattr(host_browser_session, "chromium_launch_args", lambda directory: [
        *original_flags(directory), "--headless=new", "--no-sandbox", "--site-per-process",
        *([] if frame == "cached" else ["--disable-features=BackForwardCache"]),
    ])
    profile = BrowserProfile("chrome-a0", "Chrome", os.environ["A0_TEST_CHROMIUM"], tmp_path, "Default", "Test")
    session = HostBrowserSession("history", profile)
    try:
        top = frame in {"top", "cached"}
        tab = (await session.open(origin + ("/one" if top else "/parent")))["id"]
        page = session.pages[tab].page
        if top:
            await session.navigate(tab, origin + "/two")
        else:
            await page.evaluate(f"frames[0].location.href = '{child_origin}/two'")
        async with asyncio.timeout(3):
            while await page.evaluate("window.last") != "/two":
                await asyncio.sleep(.02)
        if frame in {"cross-site", "site-switch"}:
            targets = await page.connection.command("Target.getTargets", {
                "filter": [{"type": "iframe"}, {"exclude": True}],
            })
            assert any(info["type"] == "iframe" and info["url"] == child_origin + "/two"
                       for info in targets["targetInfos"])

        if frame == "cached":
            for traverse, expected in ((session.back, "/one"), (session.forward, "/two")):
                assert (await traverse(tab))["currentUrl"] == origin + expected
                assert await page.evaluate("window.last") == expected
                navigation = "performance.getEntriesByType('navigation')[0]"
                assert await page.evaluate(f"window.restored || {navigation}.type === 'back_forward'")
            return

        if frame == "cross-site":
            # Remove a different child exactly while its temporary observer is being installed.
            command = page.connection.command
            async def remove_sibling(method, params=None, *, session_id=None, **kwargs):
                if method == "Page.enable" and session_id != page.session_id:
                    info = await command("Target.getTargetInfo", session_id=session_id)
                    if info["targetInfo"]["url"].endswith("/unrelated"):
                        await page.evaluate("document.querySelector('#transient').remove()")
                        async with asyncio.timeout(2):
                            while session_id in page.connection._history_events:
                                await asyncio.sleep(.01)
                return await command(method, params, session_id=session_id, **kwargs)
            monkeypatch.setattr(page.connection, "command", remove_sibling)

        for traverse, expected in ((session.back, "/one"), (session.forward, "/two")):
            release.clear()
            requested.clear()
            task = asyncio.create_task(traverse(tab))
            try:
                await asyncio.wait_for(requested.wait(), 3)
                finished, _ = await asyncio.wait([task], timeout=.8)
                assert not finished, "History returned while the traversed document's parser was blocked"
            finally:
                release.set()
                await task
            assert await page.evaluate("window.last") == expected
        if frame == "cross-site":
            assert await page.evaluate("document.querySelector('#transient') === null")

        await page.evaluate("location.hash = 'part'")
        await session.back(tab)
        assert await page.evaluate("location.hash") == ""
        await session.forward(tab)
        assert await page.evaluate("location.hash") == "#part"

        # Interrupt a second cross-document traversal and reuse the same session.
        await session.back(tab)
        release.clear()
        requested.clear()
        traversal = asyncio.create_task(page.go_back(timeout=1500 if frame == "top" else 3000))
        try:
            await asyncio.wait_for(requested.wait(), 2)
            if frame == "top":
                with pytest.raises(TimeoutError):
                    await traversal
            else:
                traversal.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await traversal
        finally:
            release.set()
            await asyncio.gather(traversal, return_exceptions=True)
        assert not page.connection._history_events
        async with asyncio.timeout(3):
            while await page.evaluate("window.last") != "/one":
                await asyncio.sleep(.02)
        await session.forward(tab)
        assert await page.evaluate("window.last") == "/two"

        if frame in {"same-site", "cross-site"}:
            requested.clear()
            release.clear()
            traversal = asyncio.create_task(session.back(tab))
            try:
                await asyncio.wait_for(requested.wait(), 3)
                await page.evaluate("document.querySelector('iframe').remove()")
                with pytest.raises(CDPError, match="frame was removed"):
                    await asyncio.wait_for(traversal, 2)
                assert not page.connection._history_events
            finally:
                release.set()
                await asyncio.gather(traversal, return_exceptions=True)
    finally:
        release.set()
        await session.close()
        await runner.cleanup()
