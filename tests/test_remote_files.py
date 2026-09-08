from __future__ import annotations

import json
import ntpath
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_zero_cli import remote_files as remote_files_module
from agent_zero_cli.remote_files import (
    REMOTE_FILE_READ_MAX_LINES,
    REMOTE_FILE_TEXT_MAX_BYTES,
    RemoteFileUtility,
)


def test_remote_file_utility_stat_returns_canonical_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")

    utility = RemoteFileUtility(scan_root=str(tmp_path))
    result = utility.handle_file_op(
        {
            "op_id": "op-stat",
            "op": "stat",
            "path": ".\\sample.txt",
        }
    )

    assert result["ok"] is True
    assert result["result"]["file"] == {
        "realpath": os.path.realpath(str(target)),
        "mtime": os.path.getmtime(target),
        "total_lines": 2,
    }


def test_remote_file_utility_roundtrips_read_write_and_patch(tmp_path: Path) -> None:
    utility = RemoteFileUtility(scan_root=str(tmp_path))
    target = tmp_path / "sample.txt"

    write_result = utility.handle_file_op(
        {
            "op_id": "op-write",
            "op": "write",
            "path": str(target),
            "content": "line-1\nline-2\n",
        }
    )
    read_result = utility.handle_file_op(
        {
            "op_id": "op-read",
            "op": "read",
            "path": str(target),
            "line_from": 1,
            "line_to": 2,
        }
    )
    patch_result = utility.handle_file_op(
        {
            "op_id": "op-patch",
            "op": "patch",
            "path": str(target),
            "edits": [{"from": 2, "to": 2, "content": "line-2-updated\n"}],
        }
    )

    assert write_result["ok"] is True
    assert write_result["result"]["message"] == f"{target} written successfully"
    assert write_result["result"]["file"]["realpath"] == os.path.realpath(str(target))
    assert write_result["result"]["file"]["total_lines"] == 2
    assert read_result["ok"] is True
    assert "1 | line-1" in read_result["result"]["content"]
    assert read_result["result"]["file"]["realpath"] == os.path.realpath(str(target))
    assert read_result["result"]["file"]["total_lines"] == 2
    assert patch_result["ok"] is True
    assert patch_result["result"]["message"] == f"{target} patched successfully"
    assert patch_result["result"]["file"]["realpath"] == os.path.realpath(str(target))
    assert patch_result["result"]["file"]["total_lines"] == 2
    assert target.read_text(encoding="utf-8") == "line-1\nline-2-updated\n"


def test_remote_file_write_failure_preserves_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("original\n", encoding="utf-8")
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    def fail_replace(_source: str, _destination: str) -> None:
        raise OSError("simulated disconnect")

    monkeypatch.setattr(remote_files_module.os, "replace", fail_replace)
    result = utility.handle_file_op(
        {
            "op_id": "op-write-failure",
            "op": "write",
            "path": str(target),
            "content": "replacement\n",
        }
    )

    assert result["ok"] is False
    assert "simulated disconnect" in result["error"]
    assert target.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob(".partial-*")) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"text\x00binary",
        b"\xff" * 8192,
        (b"valid utf-8 prefix\n" * 16) + (b"\xff" * 512) + b"\nvalid suffix",
    ],
)
def test_remote_file_read_rejects_binary_with_http_guidance(
    tmp_path: Path,
    payload: bytes,
) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(payload)

    result = RemoteFileUtility(scan_root=str(tmp_path)).handle_file_op(
        {"op_id": "op-binary", "op": "read", "path": str(target)}
    )

    assert result["ok"] is False
    assert result["code"] == "BINARY_FILE"
    assert result["details"]["alternative"] == "http_bulk_transfer"


def test_remote_file_read_honors_range_and_bounds_newline_heavy_output(
    tmp_path: Path,
) -> None:
    target = tmp_path / "many-lines.txt"
    target.write_text("".join(f"line-{line}\n" for line in range(1, 4001)), encoding="utf-8")

    result = RemoteFileUtility(scan_root=str(tmp_path)).handle_file_op(
        {
            "op_id": "op-many-lines",
            "op": "read",
            "path": str(target),
            "line_from": 501,
            "line_to": 3500,
        }
    )["result"]

    assert result["total_lines"] == 4000
    assert result["line_from"] == 501
    assert result["line_to"] == 500 + REMOTE_FILE_READ_MAX_LINES
    assert result["truncated"] is True
    assert result["truncation"] == {
        "reason": "max_lines",
        "next_line": 501 + REMOTE_FILE_READ_MAX_LINES,
        "alternative": "http_bulk_transfer",
    }
    assert " 501 | line-501" in result["content"]
    assert "2501 | line-2501" not in result["content"]
    assert len(result["content"].encode("utf-8")) <= REMOTE_FILE_TEXT_MAX_BYTES


@pytest.mark.parametrize(
    ("op", "payload_key", "target_exists"),
    [("write", "content", False), ("patch", "patch_text", True)],
)
def test_remote_file_modification_rejects_oversized_text_before_writing(
    tmp_path: Path,
    op: str,
    payload_key: str,
    target_exists: bool,
) -> None:
    target = tmp_path / "bounded.txt"
    if target_exists:
        target.write_text("original\n", encoding="utf-8")
    payload = {
        "op_id": f"op-{op}-large",
        "op": op,
        "path": str(target),
        payload_key: "x" * (REMOTE_FILE_TEXT_MAX_BYTES + 1),
    }

    result = RemoteFileUtility(scan_root=str(tmp_path)).handle_file_op(payload)

    assert result["ok"] is False
    assert result["code"] == "PAYLOAD_TOO_LARGE"
    assert result["details"]["actual_bytes"] == REMOTE_FILE_TEXT_MAX_BYTES + 1
    assert result["details"]["limit_bytes"] == REMOTE_FILE_TEXT_MAX_BYTES
    if target_exists:
        assert target.read_text(encoding="utf-8") == "original\n"
    else:
        assert not target.exists()


@pytest.mark.parametrize(
    "size",
    [
        0,
        1,
        REMOTE_FILE_TEXT_MAX_BYTES - 1,
        REMOTE_FILE_TEXT_MAX_BYTES,
        REMOTE_FILE_TEXT_MAX_BYTES + 1,
    ],
)
def test_remote_file_write_boundary_matrix(tmp_path: Path, size: int) -> None:
    target = tmp_path / f"write-{size}.txt"
    content = "x" * size

    result = RemoteFileUtility(scan_root=str(tmp_path)).handle_file_op(
        {
            "op_id": f"write-{size}",
            "op": "write",
            "path": str(target),
            "content": content,
        }
    )

    if size <= REMOTE_FILE_TEXT_MAX_BYTES:
        assert result["ok"] is True
        assert target.read_text(encoding="utf-8") == content
    else:
        assert result["ok"] is False
        assert result["code"] == "PAYLOAD_TOO_LARGE"
        assert not target.exists()


@pytest.mark.parametrize(
    "size",
    [
        0,
        1,
        REMOTE_FILE_TEXT_MAX_BYTES - 1,
        REMOTE_FILE_TEXT_MAX_BYTES,
        REMOTE_FILE_TEXT_MAX_BYTES + 1,
    ],
)
def test_remote_file_read_boundary_matrix(tmp_path: Path, size: int) -> None:
    target = tmp_path / f"read-{size}.txt"
    target.write_bytes(b"x" * size)

    result = RemoteFileUtility(scan_root=str(tmp_path)).handle_file_op(
        {"op_id": f"read-{size}", "op": "read", "path": str(target)}
    )

    assert result["ok"] is True
    body = result["result"]
    assert len(body["content"].encode("utf-8")) <= REMOTE_FILE_TEXT_MAX_BYTES
    assert body["truncated"] is (
        size > 0 and size + len(f"{1:>4} | ") > REMOTE_FILE_TEXT_MAX_BYTES
    )


def test_remote_file_read_64_mib_single_line_keeps_rss_delta_below_10_mib(
    tmp_path: Path,
) -> None:
    target = tmp_path / "single-line.txt"
    block = b"x" * (64 * 1024)
    with target.open("wb") as handle:
        for _ in range(1024):
            handle.write(block)

    project_root = Path(__file__).resolve().parents[1]
    script = """
import json
import resource
import sys
from agent_zero_cli.remote_files import RemoteFileUtility

before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
result = RemoteFileUtility(scan_root=sys.argv[2]).handle_file_op(
    {"op_id": "rss", "op": "read", "path": sys.argv[1]}
)["result"]
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({
    "delta_kib": after - before,
    "content_bytes": len(result["content"].encode("utf-8")),
    "total_lines": result["total_lines"],
    "truncated": result["truncated"],
    "reason": result["truncation"]["reason"],
}))
"""
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(project_root / "src"), existing_pythonpath) if part
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(target), str(tmp_path)],
        cwd=project_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    measurement = json.loads(completed.stdout)

    assert measurement == {
        "delta_kib": measurement["delta_kib"],
        "content_bytes": REMOTE_FILE_TEXT_MAX_BYTES,
        "total_lines": 1,
        "truncated": True,
        "reason": "max_bytes",
    }
    assert measurement["delta_kib"] < 10 * 1024


def test_remote_file_utility_blocks_absolute_read_outside_scan_root_in_read_only_mode(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("folder escape\n", encoding="utf-8")

    utility = RemoteFileUtility(scan_root=str(root), allow_writes=False)
    result = utility.handle_file_op(
        {
            "op_id": "op-read-escape",
            "op": "read",
            "path": str(outside),
        }
    )

    assert result["ok"] is False
    assert "outside the allowed local workspace" in result["error"]


def test_remote_file_utility_blocks_windows_drive_escape_in_read_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_files_module.os, "path", ntpath)

    utility = RemoteFileUtility(
        scan_root=r"C:\Users\alex\Desktop\dummy",
        allow_writes=False,
    )
    result = utility.handle_file_op(
        {
            "op_id": "op-read-drive-escape",
            "op": "read",
            "path": r"E:\dummy.txt",
        }
    )

    assert result["ok"] is False
    assert "outside the allowed local workspace" in result["error"]


def test_remote_file_utility_blocks_parent_traversal_outside_scan_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("folder escape\n", encoding="utf-8")

    utility = RemoteFileUtility(scan_root=str(root))
    result = utility.handle_file_op(
        {
            "op_id": "op-read-parent-escape",
            "op": "read",
            "path": "../outside.txt",
        }
    )

    assert result["ok"] is False
    assert "outside the allowed local workspace" in result["error"]


def test_remote_file_utility_blocks_symlink_escape_outside_scan_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("folder escape\n", encoding="utf-8")
    link = root / "linked-outside"

    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    utility = RemoteFileUtility(scan_root=str(root))
    result = utility.handle_file_op(
        {
            "op_id": "op-read-symlink-escape",
            "op": "read",
            "path": "linked-outside/secret.txt",
        }
    )

    assert result["ok"] is False
    assert "outside the allowed local workspace" in result["error"]


def test_remote_file_utility_context_patch_chains_after_line_shift(tmp_path: Path) -> None:
    utility = RemoteFileUtility(scan_root=str(tmp_path))
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    first_patch = utility.handle_file_op(
        {
            "op_id": "op-context-patch-1",
            "op": "patch",
            "path": str(target),
            "patch_text": (
                "*** Begin Patch\n"
                "*** Update File: sample.txt\n"
                "@@ alpha\n"
                "+inserted\n"
                "*** End Patch"
            ),
        }
    )
    second_patch = utility.handle_file_op(
        {
            "op_id": "op-context-patch-2",
            "op": "patch",
            "path": str(target),
            "patch_text": (
                "*** Begin Patch\n"
                "*** Update File: sample.txt\n"
                " beta\n"
                "-gamma\n"
                "+gamma-updated\n"
                "*** End Patch"
            ),
        }
    )

    assert first_patch["ok"] is True
    assert first_patch["result"]["file"]["total_lines"] == 4
    assert second_patch["ok"] is True
    assert second_patch["result"]["file"]["total_lines"] == 4
    assert target.read_text(encoding="utf-8") == "alpha\ninserted\nbeta\ngamma-updated\n"


def test_remote_file_utility_context_patch_can_replace_anchor_line(tmp_path: Path) -> None:
    utility = RemoteFileUtility(scan_root=str(tmp_path))
    target = tmp_path / "sample.py"
    target.write_text(
        (
            "def main():\n"
            "    print(greet(\"Agent Zero\"))\n"
            "\n"
            "\n"
            "if __name__ == \"__main__\":\n"
            "    main()\n"
        ),
        encoding="utf-8",
    )

    patch = utility.handle_file_op(
        {
            "op_id": "op-context-patch-anchor-line",
            "op": "patch",
            "path": str(target),
            "patch_text": (
                "*** Begin Patch\n"
                "*** Update File: sample.py\n"
                "@@     print(greet(\"Agent Zero\"))\n"
                "-    print(greet(\"Agent Zero\"))\n"
                "+    print(greet(\"Agent Zero\").upper())\n"
                "*** End Patch"
            ),
        }
    )

    assert patch["ok"] is True
    assert patch["result"]["file"]["total_lines"] == 6
    assert target.read_text(encoding="utf-8") == (
        "def main():\n"
        "    print(greet(\"Agent Zero\").upper())\n"
        "\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    main()\n"
    )


def test_remote_file_utility_blocks_writes_and_bounds_tree_snapshots(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("b\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    utility = RemoteFileUtility(
        scan_root=str(tmp_path),
        allow_writes=False,
        max_depth=3,
        max_files=1,
        max_folders=5,
        max_lines=20,
    )

    blocked = utility.handle_file_op(
        {
            "op_id": "op-write-disabled",
            "op": "write",
            "path": str(tmp_path / "blocked.txt"),
            "content": "hello\n",
        }
    )
    snapshot = utility.build_tree_snapshot()

    assert blocked["ok"] is False
    assert "Press F3" in blocked["error"]
    assert snapshot.root_path == str(tmp_path)
    assert snapshot.tree_hash
    assert "# 1 more file" in snapshot.tree
    assert "src/" in snapshot.tree


def test_remote_file_utility_lists_reference_entries_inside_workspace(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    utility = RemoteFileUtility(scan_root=str(tmp_path))

    assert utility.list_reference_entries() == [
        {"name": "src", "path": "src", "is_dir": True},
    ]
    assert utility.list_reference_entries("src") == [
        {"name": "main.py", "path": "src/main.py", "is_dir": False},
    ]

    with pytest.raises(PermissionError):
        utility.list_reference_entries("../outside")


def test_remote_tree_snapshot_skips_directories_that_cannot_be_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = tmp_path / "public"
    locked = tmp_path / "locked"
    public.mkdir()
    locked.mkdir()
    (public / "visible.txt").write_text("hello\n", encoding="utf-8")
    (locked / "secret.txt").write_text("hidden\n", encoding="utf-8")

    real_scandir = remote_files_module.os.scandir

    def guarded_scandir(path: str | os.PathLike[str]) -> os.ScandirIterator[str]:
        if os.fspath(path) == str(locked):
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(remote_files_module.os, "scandir", guarded_scandir)

    utility = RemoteFileUtility(scan_root=str(tmp_path), max_depth=3)
    snapshot = utility.build_tree_snapshot()

    assert "public/" in snapshot.tree
    assert "visible.txt" in snapshot.tree
    assert "locked/" in snapshot.tree
    assert "secret.txt" not in snapshot.tree
