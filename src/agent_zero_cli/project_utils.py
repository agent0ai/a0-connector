from __future__ import annotations

import re
from typing import Mapping


def _string(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


_HEX_COLOR_RE = re.compile(r"^#(?P<rgb>[0-9a-fA-F]{6})(?P<alpha>[0-9a-fA-F]{2})?$")


def _normalize_color(value: object) -> str:
    color = _string(value)
    if not color:
        return ""

    match = _HEX_COLOR_RE.fullmatch(color)
    if match is not None:
        return f"#{match.group('rgb').lower()}"

    return color


def normalize_project_summary(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None

    name = _string(value.get("name"))
    if not name:
        return None

    return {
        "name": name,
        "title": _string(value.get("title")),
        "description": _string(value.get("description")),
        "color": _normalize_color(value.get("color")),
    }


def normalize_project_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    projects: list[dict[str, str]] = []
    for item in value:
        normalized = normalize_project_summary(item)
        if normalized is not None:
            projects.append(normalized)
    return projects


def project_name(project: Mapping[str, object] | None) -> str:
    if not isinstance(project, Mapping):
        return ""
    return _string(project.get("name"))


def project_title(project: Mapping[str, object] | None) -> str:
    if not isinstance(project, Mapping):
        return ""
    return _string(project.get("title"))


def display_project_title(project: Mapping[str, object] | None, *, default: str = "No project") -> str:
    name = project_name(project)
    title = project_title(project)
    return title or name or default


def project_color(project: Mapping[str, object] | None) -> str:
    if not isinstance(project, Mapping):
        return ""
    return _string(project.get("color"))
