from __future__ import annotations

from agent_zero_cli.media_refs import extract_image_references


def test_extracts_browser_screenshot_from_webui_metadata() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_output",
        "data": {
            "meta": {
                "tool_name": "browser",
                "Screenshot": "img:///a0/tmp/browser/history.jpg&t=123.4",
                "browser_snapshot": {"mime": "image/jpeg", "browser_id": 1},
            }
        },
    }

    refs = extract_image_references(event, base_url="http://agent.test")

    assert [(ref.owner, ref.value, ref.caption) for ref in refs] == [
        ("browser", "/a0/tmp/browser/history.jpg", "Browser screenshot")
    ]


def test_extracts_browser_screenshot_from_persisted_core_tool_event() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_start",
        "data": {
            "meta": {
                "_tool_name": "browser",
                "action": "screenshot",
                "browser_id": 1,
                "Screenshot": "img:///a0/tmp/browser/history.jpg&t=123.4",
                "browser_snapshot": {
                    "a0_path": "/a0/tmp/browser/history.jpg",
                    "browser_id": 1,
                },
            }
        },
    }

    refs = extract_image_references(event, base_url="http://agent.test")

    assert [(ref.owner, ref.value, ref.caption) for ref in refs] == [
        ("browser", "/a0/tmp/browser/history.jpg", "Browser screenshot")
    ]


def test_extracts_browser_snapshot_uri_a0_path_and_path_in_precedence_order() -> None:
    snapshots = [
        (
            {
                "uri": "img:///a0/tmp/uri.png&t=2",
                "a0_path": "img:///a0/tmp/a0-path.png&t=2",
                "path": "img:///a0/tmp/path.png&t=2",
            },
            "/a0/tmp/uri.png",
        ),
        (
            {
                "a0_path": "img:///a0/tmp/a0-path.png&t=2",
                "path": "img:///a0/tmp/path.png&t=2",
            },
            "/a0/tmp/a0-path.png",
        ),
        ({"path": "img:///a0/tmp/path.png&t=2"}, "/a0/tmp/path.png"),
    ]

    for snapshot, expected in snapshots:
        event = {
            "context_id": "ctx-1",
            "sequence": 8,
            "event": "tool_output",
            "data": {"meta": {"tool_name": "browser", "browser_snapshot": snapshot}},
        }

        refs = extract_image_references(event, base_url="http://agent.test")

        assert [ref.value for ref in refs] == [expected]


def test_extracts_user_attachment_basename() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 2,
        "event": "user_message",
        "data": {"text": "See this", "meta": {"attachments": ["scan.png"]}},
    }

    refs = extract_image_references(event, base_url="http://agent.test")

    assert refs[0].value == "/a0/usr/uploads/scan.png"
    assert refs[0].copy_text == "[image: User attachment — scan.png]"


def test_extracts_attachment_paths_from_dictionaries() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 2,
        "event": "user_message",
        "data": {"meta": {"attachments": [{"path": "nested/scan.png"}]}},
    }

    refs = extract_image_references(event, base_url="http://agent.test")

    assert [ref.value for ref in refs] == ["/a0/usr/uploads/scan.png"]


def test_sanitizes_attachment_filenames_to_final_basename() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 2,
        "event": "user_message",
        "data": {
            "meta": {
                "attachments": [
                    "nested/scan.png",
                    "nested\\scan.jpg",
                    "query.png?size=large",
                    "fragment.webp#preview",
                    ".",
                    "..",
                ]
            }
        },
    }

    refs = extract_image_references(event, base_url="http://agent.test")

    assert [ref.value for ref in refs] == [
        "/a0/usr/uploads/scan.png",
        "/a0/usr/uploads/scan.jpg",
        "/a0/usr/uploads/query.png",
        "/a0/usr/uploads/fragment.webp",
    ]


def test_extracts_assistant_markdown_and_bounded_data_image() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 3,
        "event": "assistant_message",
        "data": {
            "text": (
                "![chart](img:///a0/usr/charts/result.png) "
                "![pixel](data:image/png;base64,cG5nLWJ5dGVz)"
            )
        },
    }

    refs = extract_image_references(event, base_url="http://agent.test")

    assert [(ref.owner, ref.caption, ref.source, ref.value) for ref in refs] == [
        ("assistant", "chart", "agent_zero_path", "/a0/usr/charts/result.png"),
        ("assistant", "pixel", "data_uri", "data:image/png;base64,cG5nLWJ5dGVz"),
    ]


def test_extracts_same_origin_image_get_reference() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 3,
        "event": "assistant_message",
        "data": {
            "text": "![chart](https://agent.test/api/image_get?path=%2Fa0%2Fusr%2Fcharts%2Fresult.png)"
        },
    }

    refs = extract_image_references(event, base_url="https://agent.test")

    assert [(ref.source, ref.value) for ref in refs] == [
        ("agent_zero_path", "/a0/usr/charts/result.png")
    ]


def test_rejects_external_url_parent_paths_and_oversized_data() -> None:
    oversized_payload = "A" * (4 * ((25 * 1024 * 1024) // 3 + 1))
    event = {
        "context_id": "ctx-1",
        "sequence": 4,
        "event": "assistant_message",
        "data": {
            "text": (
                "![remote](https://other.test/image.png) "
                "![wrong-origin](https://agent.test:444/api/image_get?path=%2Fa0%2Ftmp%2Fscreen.png) "
                "![parent](img:///a0/tmp/../secret.png) "
                f"![large](data:image/png;base64,{oversized_payload})"
            )
        },
    }

    assert extract_image_references(event, base_url="https://agent.test") == ()


def test_rejects_unsupported_or_invalid_data_image() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 4,
        "event": "assistant_message",
        "data": {
            "text": (
                "![vector](data:image/svg+xml;base64,PHN2Zy8+) "
                "![tiff](data:image/tiff;base64,SUkqAA==) "
                "![invalid](data:image/png;base64,abcde)"
            )
        },
    }

    assert extract_image_references(event, base_url="https://agent.test") == ()


def test_deduplicates_metadata_and_markdown_references_in_source_order() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 3,
        "event": "assistant_message",
        "data": {
            "text": "![duplicate](img:///a0/usr/charts/result.png)",
            "meta": {"image": "img:///a0/usr/charts/result.png"},
        },
    }

    refs = extract_image_references(event, base_url="https://agent.test")

    assert [(ref.owner, ref.caption, ref.value) for ref in refs] == [
        ("assistant", "Assistant image", "/a0/usr/charts/result.png")
    ]


def test_cache_buster_does_not_change_cache_key() -> None:
    first = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_output",
        "data": {
            "meta": {"tool_name": "browser", "Screenshot": "img:///a0/tmp/screen.jpg&t=1"}
        },
    }
    second = {
        "context_id": "ctx-1",
        "sequence": 9,
        "event": "tool_output",
        "data": {
            "meta": {"tool_name": "browser", "Screenshot": "img:///a0/tmp/screen.jpg&t=2"}
        },
    }

    assert extract_image_references(first, base_url="http://agent.test")[0].cache_key == (
        extract_image_references(second, base_url="http://agent.test")[0].cache_key
    )


def test_malformed_raw_url_is_ignored() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 4,
        "event": "assistant_message",
        "data": {"text": "![broken](https://[::1/image.png)"},
    }

    assert extract_image_references(event, base_url="https://agent.test") == ()


def test_malformed_base_url_is_ignored() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 4,
        "event": "assistant_message",
        "data": {
            "text": "![image](https://agent.test/api/image_get?path=%2Fa0%2Ftmp%2Fscreen.png)"
        },
    }

    assert extract_image_references(event, base_url="https://[agent.test") == ()


def test_browser_snapshot_falls_back_after_invalid_first_reference() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_output",
        "data": {
            "meta": {
                "tool_name": "browser",
                "browser_snapshot": {
                    "uri": "https://other.test/screenshot.png",
                    "a0_path": "img:///a0/tmp/fallback.png",
                },
            }
        },
    }

    refs = extract_image_references(event, base_url="https://agent.test")

    assert [ref.value for ref in refs] == ["/a0/tmp/fallback.png"]


def test_browser_snapshot_accepts_direct_a0_paths() -> None:
    snapshots = [
        ({"a0_path": "/a0/tmp/a0-path.png"}, "/a0/tmp/a0-path.png"),
        ({"path": "/a0/tmp/path.png"}, "/a0/tmp/path.png"),
    ]

    for snapshot, expected in snapshots:
        event = {
            "context_id": "ctx-1",
            "sequence": 8,
            "event": "tool_output",
            "data": {"meta": {"tool_name": "browser", "browser_snapshot": snapshot}},
        }

        refs = extract_image_references(event, base_url="https://agent.test")

        assert [ref.value for ref in refs] == [expected]


def test_non_browser_tool_output_does_not_extract_browser_images() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_output",
        "data": {
            "meta": {
                "tool_name": "code_execution_tool",
                "Screenshot": "img:///a0/tmp/unrelated.png",
                "browser_snapshot": {"a0_path": "/a0/tmp/unrelated.png"},
            }
        },
    }

    assert extract_image_references(event, base_url="https://agent.test") == ()


def test_tool_thought_metadata_does_not_extract_browser_images() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_thought",
        "data": {
            "meta": {
                "_tool_name": "browser",
                "Screenshot": "img:///a0/tmp/unrelated.png",
                "browser_snapshot": {"a0_path": "/a0/tmp/unrelated.png"},
            }
        },
    }

    assert extract_image_references(event, base_url="https://agent.test") == ()


def test_keeps_ephemeral_browser_snapshot_visible_but_unavailable() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_output",
        "data": {
            "meta": {
                "tool_name": "browser",
                "browser_snapshot": {"ephemeral_ref": "a0-ephemeral-image://ctx/ref"},
            }
        },
    }

    refs = extract_image_references(event, base_url="http://agent.test")

    assert [(ref.owner, ref.source, ref.value, ref.caption) for ref in refs] == [
        (
            "browser",
            "unavailable",
            "ephemeral screenshot is not fetchable",
            "Browser screenshot",
        )
    ]
