"""Linux candidate archives must satisfy the independent CLI bootstrap parser."""

import hashlib
import io
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace

import pytest

from agent_zero_cli.browser_extension_bootstrap import _extract


BUILDER = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                             "native/browser-bridge/scripts/build-linux-candidate.py"))


def test_linux_payload_is_reproducible_and_cli_reads_exact_executable():
    binary = b"synthetic never-executed Linux payload" * 100
    archive = BUILDER["payload"](binary)
    assert archive == BUILDER["payload"](binary)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as reader:
        member, = reader.getmembers()
        assert member.name == "a0-browser-bridge" and member.isfile()
        assert (member.uid, member.gid, member.mtime, member.mode) == (0, 0, 0, 0o755)
    pin = SimpleNamespace(archive_size=len(archive), companion=SimpleNamespace(
        platform="linux", executable_size=len(binary), executable_sha256=hashlib.sha256(binary).hexdigest()))
    with tempfile.TemporaryFile() as extracted:
        _extract(io.BytesIO(archive), extracted, pin)
        extracted.seek(0)
        assert extracted.read() == binary


def test_linux_candidate_rejects_empty_payload_and_symlinked_source(tmp_path, monkeypatch):
    with pytest.raises(AssertionError):
        BUILDER["payload"](b"")
    (tmp_path / "outside").write_text("must not be fingerprinted")
    (tmp_path / "Cargo.toml").symlink_to(tmp_path / "outside")
    monkeypatch.setitem(BUILDER["source_fingerprint"].__globals__, "ROOT", tmp_path)
    with pytest.raises(AssertionError, match="symlinks"):
        BUILDER["source_fingerprint"]()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux desktop bootstrap")
def test_linux_bootstrap_delegates_to_existing_cli_without_installing_it(tmp_path):
    cli = tmp_path / "a0"
    cli.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    cli.chmod(0o700)
    script = Path(__file__).resolve().parents[1] / "native/browser-bridge/scripts/install-linux.sh"
    result = subprocess.run(["/bin/sh", str(script), "--browser", "chromium", "--json"],
                            env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"}, capture_output=True, text=True)
    if os.getuid() == 0:
        assert result.returncode == 2 and not result.stdout
    else:
        assert result.returncode == 0
        assert result.stdout.splitlines() == ["browser-extension", "install", "--browser", "chromium", "--json"]
