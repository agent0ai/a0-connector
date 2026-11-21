"""Keyboard and text-insertion contracts for native Chromium."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_zero_cli.host_browser import BrowserProfile, CDPKeyboard, HostBrowserSession


pytestmark = pytest.mark.anyio


async def test_chromium_key_state_survives_failed_dispatch_and_distinguishes_shift_keys():
    send = AsyncMock(side_effect=RuntimeError("disconnected"))
    keyboard = CDPKeyboard(SimpleNamespace(send=send))
    with pytest.raises(RuntimeError):
        await keyboard.down("Shift")
    assert keyboard.modifiers == 0

    send.side_effect = None
    await keyboard.down("ShiftRight")
    await keyboard.down("ShiftLeft")
    await keyboard.up("ShiftLeft")
    await keyboard.press("1")
    down, up = [call.args[1] for call in send.await_args_list[-2:]]
    assert (down["type"], down["key"], down["code"], down["modifiers"]) == ("keyDown", "!", "Digit1", 8)
    assert (up["type"], up["key"]) == ("keyUp", "!")
    await keyboard.up("ShiftRight")
    assert keyboard.modifiers == 0
    await keyboard.press("Ctrl++")
    assert send.await_args_list[-3].args[1]["code"] == "Equal"
    assert keyboard.modifiers == 0


@pytest.mark.skipif(not os.environ.get("A0_TEST_CHROMIUM"), reason="set A0_TEST_CHROMIUM to run a headed Chromium")
async def test_native_chromium_keyboard_events_and_paste(tmp_path):
    profile = BrowserProfile("chrome-a0", "Chrome", os.environ["A0_TEST_CHROMIUM"], tmp_path, "Default", "Test")
    session = HostBrowserSession("keyboard", profile)
    try:
        tab = (await session.open())["id"]
        await session.evaluate(tab, """() => {
            document.body.innerHTML = '<textarea id="field"></textarea>';
            field.focus();
            globalThis.keys = [];
            field.onkeydown = event => {
                keys.push([event.key, event.code, event.shiftKey, event.isTrusted]);
                if (event.key === 'b') event.preventDefault();
            };
        }""")
        await session.keyboard(tab, text="ab+雪")
        await session.key_chord(tab, ["Shift", "a"])
        await session.keyboard(tab, key="Shift+1")
        before = (await session.evaluate(tab, "({text: field.value, keys})"))["result"]
        assert before["text"] == "a+雪A!"
        assert before["keys"] == [
            ["a", "KeyA", False, True], ["b", "KeyB", False, True], ["+", "Equal", False, True],
            ["Shift", "ShiftLeft", True, True], ["A", "KeyA", True, True],
            ["Shift", "ShiftLeft", True, True], ["!", "Digit1", True, True],
        ]
        await session.clipboard(tab, action="paste", text="b+ pasted")
        after = (await session.evaluate(tab, "({text: field.value, keys})"))["result"]
        assert after == {"text": "a+雪A!b+ pasted", "keys": before["keys"]}
    finally:
        await session.close()
