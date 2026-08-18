from __future__ import annotations

import ntpath
import os
from pathlib import Path

import pytest

from agent_zero_cli import remote_files as remote_files_module
from agent_zero_cli.remote_files import RemoteFileUtility


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
