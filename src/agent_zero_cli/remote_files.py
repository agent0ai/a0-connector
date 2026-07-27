from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Iterable

from agent_zero_cli.context_patch import apply_context_patch


_DEFAULT_IGNORE_PATTERNS = (
    ".git/",
    ".a0proj/",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
)

REMOTE_FILE_TEXT_MAX_BYTES = 256 * 1024
REMOTE_FILE_READ_MAX_LINES = 2000
_BINARY_SNIFF_BYTES = 8192
_FILE_READ_BLOCK_BYTES = 64 * 1024
_MAX_DECODE_ERROR_RATIO = 0.01


def _atomic_write_text(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".partial-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _bounded_utf8(value: str, available_bytes: int) -> tuple[str, int, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= available_bytes:
        return value, len(encoded), False
    clipped = encoded[:available_bytes].decode("utf-8", errors="ignore")
    return clipped, len(clipped.encode("utf-8")), True


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
        self._scan_root_real = os.path.normcase(os.path.realpath(self.scan_root))
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
            if op == "stat":
                return self._file_op_stat(op_id, path)
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

    def _expand_file_path(self, path: str) -> str:
        normalized = str(path or "").replace("\\", os.sep)
        expanded = os.path.expanduser(normalized)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.scan_root, expanded)
        target_path = os.path.abspath(expanded)
        self._ensure_path_contained(target_path, original_path=path)
        return target_path

    def _ensure_path_contained(self, target_path: str, *, original_path: str) -> None:
        target_real = os.path.normcase(os.path.realpath(target_path))
        try:
            contained = os.path.commonpath([self._scan_root_real, target_real]) == self._scan_root_real
        except ValueError:
            contained = False

        if not contained:
            raise PermissionError(
                f"Path is outside the allowed local workspace: {original_path}"
            )

    def _count_content_lines(self, content: str) -> int:
        return content.count("\n") + (
            1 if content and not content.endswith("\n") else 0
        )

    def _file_metadata(
        self,
        path: str,
        *,
        total_lines: int | None = None,
    ) -> dict[str, Any]:
        target_path = self._expand_file_path(path)
        if not os.path.isfile(target_path):
            raise FileNotFoundError(f"File not found: {path}")

        if total_lines is None:
            with open(target_path, "r", encoding="utf-8", errors="replace") as handle:
                total_lines = sum(1 for _ in handle)

        mtime: float | None = None
        try:
            mtime = os.path.getmtime(target_path)
        except OSError:
            pass

        return {
            "realpath": os.path.realpath(target_path),
            "mtime": mtime,
            "total_lines": total_lines,
        }

    def _file_op_stat(self, op_id: str, path: str) -> dict[str, Any]:
        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "file": self._file_metadata(path),
            },
        }

    def _file_op_read(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        target_path = self._expand_file_path(path)

        if not os.path.isfile(target_path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        total_newlines = 0
        total_bytes = 0
        last_byte = b""
        with open(target_path, "rb") as handle:
            chunk = handle.read(_FILE_READ_BLOCK_BYTES)
            sample = chunk[:_BINARY_SNIFF_BYTES]
            decoded_sample = sample.decode("utf-8", errors="replace")
            decode_error_ratio = decoded_sample.count("\ufffd") / max(len(sample), 1)
            if b"\x00" in sample or decode_error_ratio > _MAX_DECODE_ERROR_RATIO:
                return {
                    "op_id": op_id,
                    "ok": False,
                    "code": "BINARY_FILE",
                    "error": (
                        "Remote text read rejected a binary-looking file. "
                        "Use the authenticated HTTP bulk transfer path instead."
                    ),
                    "details": {
                        "type": "binary_file",
                        "alternative": "http_bulk_transfer",
                        "sample_bytes": len(sample),
                        "decode_error_ratio": decode_error_ratio,
                    },
                }
            while chunk:
                total_newlines += chunk.count(b"\n")
                total_bytes += len(chunk)
                last_byte = chunk[-1:]
                chunk = handle.read(_FILE_READ_BLOCK_BYTES)

        total = total_newlines + (1 if total_bytes and last_byte != b"\n" else 0)
        line_from = int(data.get("line_from") or 1)
        line_to_value = data.get("line_to")
        line_to = int(line_to_value) if line_to_value else total
        if line_from < 1:
            raise ValueError("line_from must be >= 1")
        if line_to < line_from and total:
            raise ValueError("line_to must be >= line_from")
        end = min(line_to, total)

        content_parts: list[str] = []
        content_bytes = 0
        current_line = 1
        selected_lines = 0
        last_selected_line = 0
        line_started = True
        truncation_reason = ""
        next_line: int | None = None

        with open(
            target_path,
            "r",
            encoding="utf-8",
            errors="replace",
            newline="",
        ) as handle:
            while current_line <= end:
                piece = handle.readline(_FILE_READ_BLOCK_BYTES)
                if not piece:
                    break
                line_ended = piece.endswith("\n")

                if current_line >= line_from:
                    prefix = ""
                    if line_started:
                        if selected_lines >= REMOTE_FILE_READ_MAX_LINES:
                            truncation_reason = "max_lines"
                            next_line = current_line
                            break
                        prefix = f"{current_line:>4} | "
                        selected_lines += 1
                        last_selected_line = current_line
                        line_started = False

                    bounded, used, clipped = _bounded_utf8(
                        prefix + piece,
                        REMOTE_FILE_TEXT_MAX_BYTES - content_bytes,
                    )
                    content_parts.append(bounded)
                    content_bytes += used
                    if clipped:
                        truncation_reason = "max_bytes"
                        next_line = current_line + 1
                        break

                if line_ended:
                    current_line += 1
                    line_started = True

                if content_bytes >= REMOTE_FILE_TEXT_MAX_BYTES:
                    if not line_ended or current_line <= end:
                        truncation_reason = "max_bytes"
                        next_line = current_line + (0 if line_ended else 1)
                    break

        file_meta = self._file_metadata(target_path, total_lines=total)
        result: dict[str, Any] = {
            "op_id": op_id,
            "ok": True,
            "result": {
                "content": "".join(content_parts),
                "total_lines": total,
                "line_from": line_from,
                "line_to": last_selected_line or end,
                "truncated": bool(truncation_reason),
                "limits": {
                    "max_lines": REMOTE_FILE_READ_MAX_LINES,
                    "max_bytes": REMOTE_FILE_TEXT_MAX_BYTES,
                },
                "file": file_meta,
            },
        }
        if truncation_reason:
            result["result"]["truncation"] = {
                "reason": truncation_reason,
                "next_line": next_line,
                "alternative": "http_bulk_transfer",
            }
        return result

    def _file_op_write(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        content = str(data.get("content", ""))
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > REMOTE_FILE_TEXT_MAX_BYTES:
            return self._file_op_too_large(op_id, content_bytes)
        target_path = self._expand_file_path(path)
        os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
        _atomic_write_text(target_path, content)
        file_meta = self._file_metadata(
            target_path,
            total_lines=self._count_content_lines(content),
        )
        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "path": path,
                "message": f"{path} written successfully",
                "file": file_meta,
            },
        }

    def _file_op_patch(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        patch_bytes = self._patch_payload_bytes(data)
        if patch_bytes > REMOTE_FILE_TEXT_MAX_BYTES:
            return self._file_op_too_large(op_id, patch_bytes)
        if data.get("patch_text") is not None:
            return self._file_op_context_patch(op_id, path, data)

        edits = data.get("edits", [])
        if not isinstance(edits, list) or not edits:
            return {"op_id": op_id, "ok": False, "error": "edits must be a non-empty list"}

        target_path = self._expand_file_path(path)
        if not os.path.isfile(target_path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(target_path, "r", encoding="utf-8") as handle:
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

        with open(target_path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        file_meta = self._file_metadata(target_path, total_lines=len(lines))

        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "path": path,
                "message": f"{path} patched successfully",
                "file": file_meta,
            },
        }

    def _patch_payload_bytes(self, data: dict[str, Any]) -> int:
        if data.get("patch_text") is not None:
            return len(str(data["patch_text"]).encode("utf-8"))
        edits = data.get("edits")
        if not isinstance(edits, list):
            return 0
        return sum(
            len(str(edit.get("content")).encode("utf-8"))
            for edit in edits
            if isinstance(edit, dict) and edit.get("content") is not None
        )

    def _file_op_too_large(self, op_id: str, actual_bytes: int) -> dict[str, Any]:
        return {
            "op_id": op_id,
            "ok": False,
            "code": "PAYLOAD_TOO_LARGE",
            "error": (
                f"Remote text writes are limited to {REMOTE_FILE_TEXT_MAX_BYTES} bytes; "
                f"received {actual_bytes}. Use the authenticated HTTP bulk transfer path."
            ),
            "details": {
                "type": "payload_too_large",
                "actual_bytes": actual_bytes,
                "limit_bytes": REMOTE_FILE_TEXT_MAX_BYTES,
                "alternative": "http_bulk_transfer",
            },
        }

    def _file_op_context_patch(self, op_id: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("edits") is not None:
            return {
                "op_id": op_id,
                "ok": False,
                "error": "provide either edits or patch_text, not both",
            }

        patch_text = str(data.get("patch_text", ""))
        target_path = self._expand_file_path(path)
        if not os.path.isfile(target_path):
            return {"op_id": op_id, "ok": False, "error": f"File not found: {path}"}

        with open(target_path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()

        new_content = apply_context_patch(content, patch_text)
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write(new_content)

        file_meta = self._file_metadata(
            target_path,
            total_lines=self._count_content_lines(new_content),
        )
        return {
            "op_id": op_id,
            "ok": True,
            "result": {
                "path": path,
                "message": f"{path} patched successfully",
                "file": file_meta,
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
                    try:
                        rel_path = f"{rel_dir}/{entry.name}".strip("/").replace("\\", "/")
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if self._is_ignored(rel_path, is_dir, ignore_patterns):
                        continue
                    if is_dir:
                        dirs.append(entry)
                    else:
                        files.append(entry)
        except OSError:
            pass

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
