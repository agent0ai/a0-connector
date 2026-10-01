"""Transport-level browser recovery without opening a real user browser."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web, WSMsgType

from agent_zero_cli import host_browser_cdp as cdp
from agent_zero_cli.host_browser_common import BrowserProfile
from agent_zero_cli.host_browser_session import HostBrowserSession

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def peer(handler):
    app = web.Application()
    app.router.add_get('/browser', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        yield f'ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/browser'
    finally:
        await runner.cleanup()


async def test_idle_connection_heartbeats_without_expiring_or_sending_browser_actions(monkeypatch):
    monkeypatch.setattr(cdp, 'CDP_HEARTBEAT_SECONDS', 0.02)
    pings = 0
    alive = asyncio.Event()
    commands = []

    async def handler(request):
        nonlocal pings
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.PING:
                pings += 1
                await ws.pong(message.data)
                if pings >= 3:
                    alive.set()
            elif message.type == WSMsgType.TEXT:
                payload = message.json()
                commands.append(payload['method'])
                await ws.send_json({'id': payload['id'], 'result': {'alive': True}})
        return ws

    async with peer(handler) as endpoint:
        connection = cdp.CDPConnection(endpoint)
        try:
            await connection.connect()
            await asyncio.wait_for(alive.wait(), 2)
            assert connection.is_connected
            assert commands == []
            assert await connection.command('Browser.getVersion') == {'alive': True}
        finally:
            await connection.close()
        assert not connection.is_connected


async def test_unresponsive_peer_is_marked_dead_and_pending_input_is_not_replayed(monkeypatch):
    monkeypatch.setattr(cdp, 'CDP_HEARTBEAT_SECONDS', 0.02)
    commands = []

    async def handler(request):
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                commands.append(message.json()['method'])
        return ws

    async with peer(handler) as endpoint:
        connection = cdp.CDPConnection(endpoint)
        try:
            await connection.connect()
            with pytest.raises(cdp.CDPError, match='closed'):
                await asyncio.wait_for(connection.command('Input.insertText', {'text': 'once'}), 2)
            assert not connection.is_connected
            with pytest.raises(cdp.CDPError, match='not open'):
                await connection.command('Input.insertText', {'text': 'again'})
            assert commands == ['Input.insertText']
            assert not connection._pending
        finally:
            await connection.close()


async def test_reader_exception_resolves_pending_commands():
    class BrokenSocket:
        closed = False
        def __aiter__(self): return self
        async def __anext__(self): raise OSError('connection reset')
        async def close(self): self.closed = True

    connection = cdp.CDPConnection('ws://unused')
    connection._ws = BrokenSocket()
    pending = asyncio.get_running_loop().create_future()
    connection._pending[1] = pending
    connection._reader_task = asyncio.create_task(connection._read_loop())
    await connection._reader_task
    assert not connection.is_connected
    assert 'closed' in (await pending)['error']['message']
    await connection.close()


async def test_reconnect_preserves_exact_tabs_and_never_reuses_a_disappeared_tab_id():
    targets = [('first', 'https://first.example'), ('second', 'about:blank')]
    commands = []
    sockets = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.append(ws)
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            data = message.json()
            method = data['method']
            commands.append(method)
            result = {}
            if method == 'Target.getTargets':
                result = {'targetInfos': [dict(targetId=t, type='page', url=u) for t, u in targets]}
            elif method == 'Target.attachToTarget':
                result = {'sessionId': data['params']['targetId']}
            await ws.send_json({'id': data['id'], 'result': result})
        return ws

    async with peer(handler) as endpoint:
        profile = BrowserProfile('chrome-cdp', 'Chrome', '', Path(), 'Default', '', cdp_endpoint=endpoint)
        session = HostBrowserSession('recovery-test', profile)
        try:
            await session.ensure_started()
            await session._runtime.refresh_pages(session)
            original = {item.page.target_id: item.id for item in session.pages.values()}
            session.last_interacted_browser_id = original['second']
            old = session.browser
            await sockets[-1].close()
            await asyncio.wait_for(old._reader_task, 2)
            targets[:] = [('new', 'https://new.example'), ('second', 'about:blank')]
            await asyncio.gather(session.ensure_started(), session.ensure_started())
            assert len(sockets) == 2
            assert old._session is None
            assert session.browser.is_connected
            assert session.profile.cdp_endpoint == endpoint
            assert session.pages[original['second']].page.target_id == 'second'
            assert session.last_interacted_browser_id == original['second']
            assert original['first'] not in session.pages
            with pytest.raises(KeyError, match='not open'):
                session._resolve_browser_id(original['first'])
        finally:
            await session.close()
        assert not any(method in commands for method in ['Browser.close', 'Target.closeTarget', 'Input.insertText'])


async def test_cancelled_approval_handshake_releases_http_session(monkeypatch):
    entered = asyncio.Event()
    closed = asyncio.Event()
    async def ws_connect(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    async def close(): closed.set()
    monkeypatch.setattr(cdp.aiohttp, 'ClientSession', lambda **kwargs: SimpleNamespace(ws_connect=ws_connect, close=close))
    connection = cdp.CDPConnection('ws://unused')
    task = asyncio.create_task(connection.connect())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert not connection.is_connected


@pytest.mark.parametrize('legacy_limit', [False, True])
async def test_large_capture_response_does_not_disconnect_the_browser(monkeypatch, legacy_limit):
    # The default aiohttp 4 MiB cap disconnected otherwise permitted captures.
    screenshot_data = 'A' * (4 * 1024 * 1024 + 1)
    if legacy_limit:
        monkeypatch.setattr(cdp, 'CDP_MAX_MESSAGE_BYTES', 4 * 1024 * 1024)
    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                data = message.json()
                result = {'data': screenshot_data} if data['method'] == 'Page.captureScreenshot' else {}
                await ws.send_json({'id': data['id'], 'result': result})
        return ws

    async with peer(handler) as endpoint:
        connection = cdp.CDPConnection(endpoint)
        try:
            await connection.connect()
            if legacy_limit:
                with pytest.raises(cdp.CDPError, match='closed'):
                    await connection.command('Page.captureScreenshot')
                assert not connection.is_connected
                return
            result = await connection.command('Page.captureScreenshot')
            assert result['data'] == screenshot_data
            assert connection.is_connected
            assert await connection.command('Browser.getVersion') == {}
        finally:
            await connection.close()
