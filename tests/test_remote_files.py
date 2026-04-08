from __future__ import annotations

from pathlib import Path

from agent_zero_cli.remote_files import RemoteFileUtility


def test_remote_file_utility_read_write_patch_roundtrip(tmp_path: Path) -> None:
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
    assert write_result["ok"] is True

    read_result = utility.handle_file_op(
        {
            "op_id": "op-read",
            "op": "read",
            "path": str(target),
            "line_from": 1,
            "line_to": 2,
        }
    )
    assert read_result["ok"] is True
    assert "1 | line-1" in read_result["result"]["content"]

    patch_result = utility.handle_file_op(
        {
            "op_id": "op-patch",
            "op": "patch",
            "path": str(target),
            "edits": [{"from": 2, "to": 2, "content": "line-2-updated\n"}],
        }
    )
    assert patch_result["ok"] is True
    assert target.read_text(encoding="utf-8") == "line-1\nline-2-updated\n"


def test_remote_file_tree_snapshot_is_bounded_and_hashed(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("b\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    utility = RemoteFileUtility(
        scan_root=str(tmp_path),
        max_depth=3,
        max_files=1,
        max_folders=5,
        max_lines=20,
    )

    snapshot = utility.build_tree_snapshot()

    assert snapshot.root_path == str(tmp_path)
    assert snapshot.tree_hash
    assert "# 1 more file" in snapshot.tree
    assert "src/" in snapshot.tree
