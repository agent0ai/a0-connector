from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Iterable


_DEFAULT_IGNORE_PATTERNS = (
    ".git/",
    ".a0proj/",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
)


@dataclass(frozen=True)
class RemoteTreeSnapshot:
    root_path: str
    tree: str
    tree_hash: str
    generated_at: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "root_path": self.root_path,
            "tree": self.tree,
            "tree_hash": self.tree_hash,
            "generated_at": self.generated_at,
            "source": "a0",
        }


@dataclass
class _TreeState:
    lines_used: int = 0
    limit_hit: bool = False


class RemoteFileUtility:
    def __init__(
        self,
        *,
        scan_root: str,
        allow_writes: bool = True,
        max_depth: int = 5,
        max_files: int = 20,
        max_folders: int = 20,
        max_lines: int = 250,
    ) -> None:
        self.scan_root = os.path.abspath(scan_root or os.getcwd())
        self.allow_writes = allow_writes
        self.max_depth = max_depth
        self.max_files = max_files
        self.max_folders = max_folders
        self.max_lines = max_lines

    def set_write_enabled(self, enabled: bool) -> None:
        self.allow_writes = enabled

    def handle_file_op(self, data: dict[str, Any]) -> dict[str, Any]:
        op_id = data.get("op_id", "")
        op = str(data.get("op", "")).strip().lower()
        path = str(data.get("path", "")).strip()

        try:
            if op == "read":
                return self._file_op_read(op_id, path, data)
            if op in {"write", "patch"} and not self.allow_writes:
                return {
                    "op_id": op_id,
                    "ok": False,
                    "error": (
                        "Frontend file writes are disabled in this CLI session. "
                        "Press F3 to switch to Read&Write."
                    ),
                }
            if op == "write":
                return self._file_op_write(op_id, path, data)
            if op == "patch":
                return self._file_op_patch(op_id, path, data)
            return {"op_id": op_id, "ok": False, "error": f"Unknown op: {op}"}
        except Exception as exc:
            return {"op_id": op_id, "ok": False, "error": str(exc)}

    def _file_op_read(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        line_from = data.get("line_from")
        line_to = data.get("line_to")

        if not os.path.isfile(path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()

        total = len(lines)
        start = (line_from - 1) if line_from and line_from > 0 else 0
        end = line_to if line_to and line_to <= total else total
        selected = lines[start:end]

        content = "".join(f"{index:>4} | {line}" for index, line in enumerate(selected, start=start + 1))

        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "content": content,
                "total_lines": total,
                "line_from": start + 1,
                "line_to": end,
            },
        }

    def _file_op_write(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        content = str(data.get("content", ""))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "path": path,
                "message": f"{path} written successfully",
            },
        }

    def _file_op_patch(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        edits = data.get("edits", [])
        if not isinstance(edits, list) or not edits:
            return {"op_id": op_id, "ok": False, "error": "edits must be a non-empty list"}

        if not os.path.isfile(path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()

        sorted_edits = sorted(edits, key=lambda item: int(item.get("from", 0) or 0), reverse=True)
        for edit in sorted_edits:
            fr = int(edit.get("from", 1) or 1)
            to = edit.get("to")
            to_idx = int(to or fr)
            content = edit.get("content")
            idx = fr - 1

            if fr < 1:
                raise ValueError("patch 'from' must be >= 1")
            if to is not None and to_idx < fr:
                raise ValueError("patch 'to' must be >= 'from'")

            if to is None and content is not None:
                lines[idx:idx] = str(content).splitlines(True)
            elif content is None:
                del lines[idx:to_idx]
            else:
                lines[idx:to_idx] = str(content).splitlines(True)

        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)

        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "path": path,
                "message": f"{path} patched successfully",
            },
        }

    def build_tree_snapshot(self) -> RemoteTreeSnapshot:
        tree = self._render_tree()
        generated_at = datetime.now(timezone.utc).isoformat()
        digest = hashlib.sha256(tree.encode("utf-8")).hexdigest()
        return RemoteTreeSnapshot(
            root_path=self.scan_root,
            tree=tree,
            tree_hash=digest,
            generated_at=generated_at,
        )

    def _render_tree(self) -> str:
        root = self.scan_root.rstrip(os.sep) + "/"
        if not os.path.exists(self.scan_root):
            return f"{root}\n└── # path not found"
        if not os.path.isdir(self.scan_root):
            return f"{root}\n└── # not a directory"

        ignore_patterns = self._load_ignore_patterns()
        lines: list[str] = [root]
        state = _TreeState()

        self._walk_directory(
            abs_dir=self.scan_root,
            rel_dir="",
            depth=1,
            branch_flags=[],
            lines=lines,
            state=state,
            ignore_patterns=ignore_patterns,
        )

        if len(lines) == 1:
            lines.append("└── # Empty")

        if state.limit_hit:
            marker = "# limit reached"
            if not any(marker in line for line in lines):
                lines.append(f"└── {marker}")

        return "\n".join(lines)

    def _walk_directory(
        self,
        *,
        abs_dir: str,
        rel_dir: str,
        depth: int,
        branch_flags: list[bool],
        lines: list[str],
        state: _TreeState,
        ignore_patterns: list[str],
    ) -> None:
        if self.max_depth > 0 and depth > self.max_depth:
            return
        if state.limit_hit:
            return

        dirs, files = self._list_entries(abs_dir, rel_dir, ignore_patterns)
        nodes: list[tuple[str, Any]] = []

        shown_dirs = dirs
        hidden_dirs = 0
        if self.max_folders > 0 and len(dirs) > self.max_folders:
            shown_dirs = dirs[: self.max_folders]
            hidden_dirs = len(dirs) - len(shown_dirs)

        shown_files = files
        hidden_files = 0
        if self.max_files > 0 and len(files) > self.max_files:
            shown_files = files[: self.max_files]
            hidden_files = len(files) - len(shown_files)

        nodes.extend(("dir", entry) for entry in shown_dirs)
        if hidden_dirs:
            label = "folder" if hidden_dirs == 1 else "folders"
            nodes.append(("comment", f"# {hidden_dirs} more {label}"))

        nodes.extend(("file", entry) for entry in shown_files)
        if hidden_files:
            label = "file" if hidden_files == 1 else "files"
            nodes.append(("comment", f"# {hidden_files} more {label}"))

        for index, (kind, payload) in enumerate(nodes):
            is_last = index == len(nodes) - 1
            prefix = "".join("    " if is_done else "│   " for is_done in branch_flags)
            connector = "└── " if is_last else "├── "

            if kind == "dir":
                label = f"{payload.name}/"
            elif kind == "file":
                label = payload.name
            else:
                label = str(payload)

            if self.max_lines > 0 and state.lines_used >= self.max_lines:
                state.limit_hit = True
                return

            lines.append(f"{prefix}{connector}{label}")
            state.lines_used += 1

            if kind == "dir":
                child_abs = payload.path
                child_rel = f"{rel_dir}/{payload.name}".strip("/")
                self._walk_directory(
                    abs_dir=child_abs,
                    rel_dir=child_rel,
                    depth=depth + 1,
                    branch_flags=[*branch_flags, is_last],
                    lines=lines,
                    state=state,
                    ignore_patterns=ignore_patterns,
                )
                if state.limit_hit:
                    return

    def _list_entries(
        self,
        abs_dir: str,
        rel_dir: str,
        ignore_patterns: list[str],
    ) -> tuple[list[os.DirEntry[str]], list[os.DirEntry[str]]]:
        dirs: list[os.DirEntry[str]] = []
        files: list[os.DirEntry[str]] = []
        try:
            with os.scandir(abs_dir) as iterator:
                for entry in iterator:
                    rel_path = f"{rel_dir}/{entry.name}".strip("/").replace("\\", "/")
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if self._is_ignored(rel_path, is_dir, ignore_patterns):
                        continue
                    if is_dir:
                        dirs.append(entry)
                    else:
                        files.append(entry)
        except FileNotFoundError:
            return [], []

        dirs.sort(key=lambda item: item.name.casefold())
        files.sort(key=lambda item: item.name.casefold())
        return dirs, files

    def _load_ignore_patterns(self) -> list[str]:
        patterns = list(_DEFAULT_IGNORE_PATTERNS)
        gitignore = os.path.join(self.scan_root, ".gitignore")
        if not os.path.isfile(gitignore):
            return patterns

        try:
            with open(gitignore, "r", encoding="utf-8", errors="replace") as handle:
                for raw in handle.readlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    patterns.append(line)
        except Exception:
            pass
        return patterns

    def _is_ignored(self, rel_path: str, is_dir: bool, patterns: Iterable[str]) -> bool:
        rel_path = rel_path.strip("/")
        basename = os.path.basename(rel_path)
        ignored = False

        for raw in patterns:
            rule = raw.strip()
            if not rule:
                continue

            negate = rule.startswith("!")
            if negate:
                rule = rule[1:].strip()
                if not rule:
                    continue

            matched = self._match_ignore_rule(rule, rel_path, basename, is_dir)
            if matched:
                ignored = not negate

        return ignored

    def _match_ignore_rule(self, rule: str, rel_path: str, basename: str, is_dir: bool) -> bool:
        rel_as_dir = f"{rel_path}/" if is_dir else rel_path

        if rule.endswith("/"):
            prefix = rule.rstrip("/")
            return rel_path == prefix or rel_path.startswith(prefix + "/")

        if "/" in rule:
            return fnmatch(rel_path, rule) or fnmatch(rel_as_dir, rule)

        return fnmatch(basename, rule) or fnmatch(rel_path, rule)
