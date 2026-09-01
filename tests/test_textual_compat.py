from __future__ import annotations

from codecs import getincrementaldecoder
import sys

import pytest

from agent_zero_cli import textual_compat


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Textual driver")


def test_textual_linux_input_guard_replaces_invalid_utf8_bytes(monkeypatch) -> None:
    from textual.drivers import linux_driver

    monkeypatch.setattr(textual_compat, "_TEXTUAL_INPUT_DECODER_GUARD_INSTALLED", False)
    monkeypatch.setattr(linux_driver, "getincrementaldecoder", getincrementaldecoder)

    with pytest.raises(UnicodeDecodeError):
        linux_driver.getincrementaldecoder("utf-8")().decode(b"\x1b[M \x82 ", final=False)

    textual_compat.install_textual_linux_input_decoder_guard()

    decoder = linux_driver.getincrementaldecoder("utf-8")()
    decoded = decoder.decode(b"\x1b[M \x82 ", final=False)

    assert decoded == "\x1b[M \ufffd "


def test_textual_linux_input_guard_preserves_other_decoders(monkeypatch) -> None:
    from textual.drivers import linux_driver

    monkeypatch.setattr(textual_compat, "_TEXTUAL_INPUT_DECODER_GUARD_INSTALLED", False)
    monkeypatch.setattr(linux_driver, "getincrementaldecoder", getincrementaldecoder)

    textual_compat.install_textual_linux_input_decoder_guard()

    decoder = linux_driver.getincrementaldecoder("latin-1")()

    assert decoder.decode(b"\x82", final=False) == "\x82"
