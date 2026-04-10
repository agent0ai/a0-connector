from __future__ import annotations

from pathlib import Path

from agent_zero_cli.remote_files import RemoteFileUtility


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
    assert read_result["ok"] is True
    assert "1 | line-1" in read_result["result"]["content"]
    assert patch_result["ok"] is True
    assert target.read_text(encoding="utf-8") == "line-1\nline-2-updated\n"


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
