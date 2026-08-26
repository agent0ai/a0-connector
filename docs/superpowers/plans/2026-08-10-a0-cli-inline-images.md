# A0 CLI Inline Images Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render browser screenshots and user/assistant history images as expandable inline thumbnails in the A0 Textual transcript.

**Architecture:** Normalize eligible connector metadata into immutable image references, fetch and validate Agent Zero-hosted payloads through the existing authenticated client, and render a bounded cached Pillow surface through an A0-owned `textual-image` adapter. Every transcript sequence becomes a small container that preserves its existing text/status widget and owns zero or more `ImageEntry` children, allowing late browser screenshot metadata to update the same tool entry.

**Tech Stack:** Python 3.10+, Textual 8.2.8, Rich 15, httpx 0.28, Pillow 12, `textual-image` 0.8.x/0.13.x, pytest/anyio.

## Global Constraints

- Preserve Python 3.10+; do not raise the Python floor.
- Ask before installing new dependencies. If the compatibility proof fails, stop and revise the design instead of vendoring a renderer or dropping native protocol support.
- Ask before creating implementation commits or pushing; an execution-mode choice does not authorize a push.
- Run the applicable DOX pass after every task and include any newly true ownership/verification contract in that task's commit instead of deferring all AGENTS updates to the end.
- `A0_CLI_IMAGE_MODE` accepts exactly `auto`, `tgp`, `sixel`, `halfcell`, or `off`; it is not persisted.
- `auto` prefers TGP, then Sixel, then half-cell. Tests, non-TTY sessions, and `textual-serve` use half-cell.
- Thumbnail maximum is 36 columns by 12 rows. Expanded maximum is 96 columns by 32 rows and may shrink to the transcript width. Images preserve complete aspect ratio and are never cropped.
- Automatically fetch only authenticated Agent Zero image paths. Do not fetch arbitrary cross-origin URLs.
- Accept PNG, JPEG, WebP, GIF, and BMP; render the first animation frame; leave SVG as a labeled placeholder.
- Enforce 25 MiB encoded, 32-megapixel decoded, four-concurrent-fetch/load,
  single-full-resolution-decoder, and 64 MiB memory-cache limits. Downsample
  before applying orientation and color conversion.
- Keep fetched and decoded image data in process memory only. Never log or copy cookies, base64, payload bytes, or cache contents.
- Image failures must not interrupt chat events, context replay, browser actions, headless mode, or gateway mode.
- Browser screenshots stay subject to existing browser consent and privacy gates.
- Headless and gateway startup paths must not import `textual-image` or emit terminal graphics sequences.
- Use a fake renderer in automated Textual tests; native protocol bytes do not belong in snapshots.

---

## File Structure

### New files

- `src/agent_zero_cli/image_render.py` — terminal capability selection, cell sizing, `textual-image` version adapter, widget construction, and cleanup.
- `src/agent_zero_cli/media_refs.py` — pure extraction and normalization of eligible event image references.
- `src/agent_zero_cli/image_store.py` — authenticated/data-URI loading, Pillow validation, request coalescing, concurrency, cancellation, and LRU cache.
- `src/agent_zero_cli/widgets/image_entry.py` — focusable image state machine, thumbnail/expanded interaction, resize debounce, placeholder, and protocol cleanup.
- `tests/test_image_render.py` — mode selection, size fitting, compatibility import, widget factory, and cleanup tests.
- `tests/test_media_refs.py` — browser/user/assistant/data reference extraction and rejection tests.
- `tests/test_image_store.py` — decode, limits, retry boundary, deduplication, concurrency, LRU, and cancellation tests.
- `tests/test_chat_log_images.py` — transcript ownership, toggling, lazy loading, copying, and cleanup tests with a fake renderer.

### Modified files

- `requirements/a0-runtime.in`, `constraints/a0-runtime.txt`, `pyproject.toml` — unconditional Pillow plus conditional `textual-image` release lines; generated locks remain tool-owned.
- `src/agent_zero_cli/__main__.py` — initialize the renderer only on the interactive TUI path before `App.run()`.
- `src/agent_zero_cli/app.py` — own the renderer/store and service `ImageEntry.LoadRequested` messages.
- `src/agent_zero_cli/client.py` — bounded authenticated `/api/image_get` retrieval.
- `src/agent_zero_cli/event_handlers.py` — attach extracted images after normal event/status rendering for snapshots and live updates.
- `src/agent_zero_cli/widgets/chat_log.py` — sequence-level `TranscriptEntry` wrapper, image upsert, nearby-entry scheduling, semantic copy, and cleanup.
- `src/agent_zero_cli/widgets/__init__.py` — export `ImageEntry` for application message handling.
- `src/agent_zero_cli/styles/app.tcss` — transcript container, caption, focus, placeholder, and surface layout.
- `src/agent_zero_cli/chat_commands.py`, `src/agent_zero_cli/connection.py` — cancel or clear image work at clear/context/host/disconnect/exit boundaries.
- `devtools/preview_launcher.py`, `devtools/snapshot.py`, `devtools/README.md` — force deterministic half-cell rendering in browser/SVG previews.
- `tests/test_client.py`, `tests/test_app.py`, `tests/test_entrypoint.py`, `tests/test_devtools.py` — fetch, orchestration, lifecycle, lazy-import, and preview regression coverage.
- `README.md`, `docs/architecture.md`, `docs/configuration.md`, `docs/tui-frontend.md` — user behavior, data path, override, compatibility, and troubleshooting.
- `AGENTS.md`, `src/agent_zero_cli/AGENTS.md`, `src/agent_zero_cli/widgets/AGENTS.md`, `tests/AGENTS.md`, `devtools/AGENTS.md`, `requirements/AGENTS.md` — durable ownership and verification contracts if the implemented behavior changes their current scope descriptions.

## Shared Interfaces

The tasks below use these names consistently.

`src/agent_zero_cli/image_render.py` defines:

- `ImageMode = Literal["tgp", "sixel", "halfcell", "off"]`.
- Immutable `RendererSelection(mode: ImageMode, notice: str = "")`.
- Immutable `CellBox(columns: int, rows: int)`.
- `ImageRenderer.disabled() -> ImageRenderer` and `ImageRenderer.for_test(*, mode: ImageMode, cell_pixels: tuple[int, int]) -> ImageRenderer`.
- Read-only `ImageRenderer.mode`, `notice`, and `max_surface_pixels`.
- `ImageRenderer.fit_box(image_size: tuple[int, int], *, available_columns: int, expanded: bool) -> CellBox`.
- `ImageRenderer.create_widget(image: PILImage.Image, box: CellBox) -> Widget`.
- `ImageRenderer.create_halfcell_widget(image: PILImage.Image, box: CellBox) -> Widget`.
- `ImageRenderer.cleanup_widget(widget: Widget | None) -> None`.
- `select_image_mode(requested: str, *, is_tty: bool, tgp_supported: bool, sixel_supported: bool, force_halfcell: bool = False) -> RendererSelection`.
- `initialize_image_renderer(*, environ: Mapping[str, str] | None = None, force_halfcell: bool = False) -> ImageRenderer`.

`src/agent_zero_cli/media_refs.py` defines:

- `ImageOwner = Literal["browser", "user", "assistant"]`.
- `ImageSource = Literal["agent_zero_path", "data_uri", "unavailable"]`.
- Immutable `ImageReference` fields: `entry_key`, `cache_key`, `context_id`, `sequence`, `owner`, `caption`, `source`, and `value`.
- Read-only `ImageReference.copy_text -> str`.
- `extract_image_references(event: Mapping[str, object], *, base_url: str) -> tuple[ImageReference, ...]`.

`src/agent_zero_cli/image_store.py` defines:

- `ImageClient(Protocol)` with `fetch_image(path: str) -> Awaitable[tuple[bytes, str]]`.
- Mutable `ImageAsset(cache_key, mime_type, image, width, height, cost_bytes)` with `clone() -> ImageAsset` and `close() -> None`.
- `ImageUnavailableError(reason: str)` whose public `reason` is safe for transcript display.
- `ImageStore(client: ImageClient, *, max_surface_pixels, max_cache_bytes=64 * 1024 * 1024, max_concurrent=4)`, with four fetch/load permits and one full-resolution decode permit.
- `ImageStore.load(reference: ImageReference) -> Awaitable[ImageAsset]`.
- `ImageStore.cancel_pending() -> None`, `ImageStore.clear() -> None`, and read-only `cache_bytes -> int`.

`src/agent_zero_cli/widgets/image_entry.py` defines:

- `ImageEntry(Vertical)` with immutable `reference`, state in `pending|loading|rendered|unavailable|disabled`, and read-only `expanded`.
- Bubbling `ImageEntry.LoadRequested(entry, reference, generation)`.
- `ImageEntry.request_load()`, `set_asset(generation, asset)`, `set_unavailable(generation, reason)`, `release_surface()`, and `copy_text()`.

`src/agent_zero_cli/widgets/chat_log.py` defines:

- `TranscriptEntry(Vertical)` with `sequence`, `primary`, `set_primary(widget_type)`, `upsert_images(references, renderer)`, and `copy_text()`.
- `ChatLog.append_or_update_images(sequence, references, *, prepend=False) -> None`.
- `ChatLog.request_nearby_images() -> None`.

---

### Task 1: Prove Dependencies and Add the Renderer Adapter

**Files:**
- Create: `src/agent_zero_cli/image_render.py`
- Create: `tests/test_image_render.py`
- Modify: `requirements/a0-runtime.in`
- Generate: `constraints/a0-runtime.txt`
- Generate: `pyproject.toml`
- Modify: `src/agent_zero_cli/__main__.py`
- Modify: `src/agent_zero_cli/app.py`
- Modify: `tests/test_entrypoint.py`
- Modify: `src/agent_zero_cli/AGENTS.md`
- Modify: `requirements/AGENTS.md`

**Interfaces:**
- Consumes: Textual `Widget`, Pillow `Image`, `textual_image.widget.TGPImage`, `SixelImage`, and `HalfcellImage`.
- Produces: `RendererSelection`, `CellBox`, `ImageRenderer`, `select_image_mode()`, and `initialize_image_renderer()` exactly as declared in Shared Interfaces.

- [ ] **Step 1: Write failing selection, sizing, and compatibility tests**

Create tests that establish the protocol order and the stable adapter surface:

```python
from PIL import Image as PILImage

from agent_zero_cli.image_render import (
    CellBox,
    ImageRenderer,
    select_image_mode,
)


def test_auto_prefers_tgp_before_sixel() -> None:
    selected = select_image_mode(
        "auto",
        is_tty=True,
        tgp_supported=True,
        sixel_supported=True,
    )
    assert selected.mode == "tgp"
    assert selected.notice == ""


def test_explicit_unsupported_native_mode_falls_back_once() -> None:
    selected = select_image_mode(
        "sixel",
        is_tty=True,
        tgp_supported=True,
        sixel_supported=False,
    )
    assert selected.mode == "halfcell"
    assert selected.notice == "Sixel images are unavailable; using half-cell images."


def test_non_tty_forces_halfcell() -> None:
    assert select_image_mode(
        "auto",
        is_tty=False,
        tgp_supported=True,
        sixel_supported=True,
    ).mode == "halfcell"


def test_renderer_fits_complete_thumbnail_and_expanded_boxes() -> None:
    renderer = ImageRenderer.for_test(mode="halfcell", cell_pixels=(1, 2))
    assert renderer.fit_box((1600, 900), available_columns=120, expanded=False) == CellBox(36, 10)
    assert renderer.fit_box((1600, 900), available_columns=80, expanded=True) == CellBox(80, 22)


def test_installed_textual_image_branch_constructs_each_widget() -> None:
    from textual_image.widget import HalfcellImage, SixelImage, TGPImage

    image = PILImage.new("RGB", (4, 4), "#123456")
    for widget_type in (TGPImage, SixelImage, HalfcellImage):
        widget = widget_type(image)
        assert widget.image is image
        widget.image = None
```

Also update `test_run_app_installs_textual_input_decoder_guard` so the expected order is renderer initialization, app construction, then `run()`, and assert that the renderer is passed as the `image_renderer` keyword argument.

- [ ] **Step 2: Run the focused tests and verify the new module is missing**

Run:

```bash
./.venv/bin/python -m pytest tests/test_image_render.py tests/test_entrypoint.py::test_run_app_installs_textual_input_decoder_guard -v
```

Expected: collection fails because `agent_zero_cli.image_render` does not exist.

- [ ] **Step 3: Implement mode selection, cell fitting, lazy library imports, and cleanup**

Implement the public interface with these exact rules:

```python
VALID_REQUESTED_MODES = frozenset({"auto", "tgp", "sixel", "halfcell", "off"})
THUMBNAIL_MAX = CellBox(36, 12)
EXPANDED_MAX = CellBox(96, 32)


def select_image_mode(requested: str, *, is_tty: bool, tgp_supported: bool,
                      sixel_supported: bool, force_halfcell: bool = False) -> RendererSelection:
    normalized = str(requested or "auto").strip().lower()
    invalid = normalized not in VALID_REQUESTED_MODES
    if invalid:
        normalized = "auto"
    if normalized == "off":
        return RendererSelection("off")
    if force_halfcell or not is_tty:
        if invalid:
            notice = "Invalid A0_CLI_IMAGE_MODE; using half-cell images."
        elif normalized == "tgp":
            notice = "TGP images are unavailable; using half-cell images."
        elif normalized == "sixel":
            notice = "Sixel images are unavailable; using half-cell images."
        else:
            notice = ""
        return RendererSelection("halfcell", notice)
    if normalized == "tgp":
        return RendererSelection("tgp") if tgp_supported else RendererSelection(
            "halfcell", "TGP images are unavailable; using half-cell images."
        )
    if normalized == "sixel":
        return RendererSelection("sixel") if sixel_supported else RendererSelection(
            "halfcell", "Sixel images are unavailable; using half-cell images."
        )
    if normalized == "halfcell":
        return RendererSelection("halfcell")
    mode: ImageMode = "tgp" if tgp_supported else "sixel" if sixel_supported else "halfcell"
    notice = "Invalid A0_CLI_IMAGE_MODE; using automatic image detection." if invalid else ""
    return RendererSelection(mode, notice)
```

`initialize_image_renderer()` must read `A0_CLI_IMAGE_MODE`, query TGP before Sixel, capture terminal cell pixel size before `App.run()`, import widget classes only inside the function, and retain a half-cell factory for per-image fallback. `ImageRenderer.disabled()` and `ImageRenderer.for_test()` must not import `textual_image`.

Use this sizing calculation so the complete image fits both limits:

```python
max_box = EXPANDED_MAX if expanded else THUMBNAIL_MAX
max_columns = max(1, min(max_box.columns, available_columns))
scale = min(
    max_columns * cell_width / image_width,
    max_box.rows * cell_height / image_height,
)
columns = max(1, min(max_columns, round(image_width * scale / cell_width)))
rows = max(1, min(max_box.rows, round(image_height * scale / cell_height)))
return CellBox(columns, rows)
```

`max_surface_pixels` is `(96 * cell_width, 32 * cell_height)` for TGP/Sixel, `(96, 64)` for half-cell, and `(1, 1)` for off. `create_widget()` must set `widget.styles.width` and `widget.styles.height` to the selected cell box. `cleanup_widget()` must set the library widget's `image` property to `None` before removal so TGP cleanup runs.

Add an optional `image_renderer: ImageRenderer | None = None` keyword to `AgentZeroCLI.__init__`, defaulting to `ImageRenderer.disabled()`. In `_run_app`, call `initialize_image_renderer()` after the Linux input guard and before constructing `AgentZeroCLI`, then pass the result into the app. On mount, surface `renderer.notice` exactly once through `_show_notice`.

- [ ] **Step 4: Add dependency intent and regenerate locks**

After obtaining the repository-required approval to install dependencies, replace the conditional Pillow line in `requirements/a0-runtime.in` with:

```text
pillow>=10.3.0
textual-image>=0.8.5,<0.9; python_version < "3.12"
textual-image>=0.13.2,<0.14; python_version >= "3.12"
```

Regenerate the generated files rather than editing them:

```bash
./.venv/bin/python devtools/lock_dependencies.py
./.venv/bin/python devtools/lock_dependencies.py --check
```

Expected: `constraints/a0-runtime.txt` and `pyproject.toml` contain the two mutually exclusive `textual-image` markers and unconditional Pillow; the check exits 0.

- [ ] **Step 5: Run focused renderer and entrypoint tests**

Run:

```bash
./.venv/bin/python -m pytest tests/test_image_render.py tests/test_entrypoint.py -v
```

Expected: all tests pass, including real construction of the dependency branch installed for the current Python interpreter.

- [ ] **Step 6: Run the compatibility gate on every supported Python branch**

Use isolated uv environments so the conditional dependency branches are actually resolved:

```bash
uv run --python 3.10 --with-editable . pytest tests/test_image_render.py tests/test_entrypoint.py -v
uv run --python 3.11 --with-editable . pytest tests/test_image_render.py tests/test_entrypoint.py -v
uv run --python 3.12 --with-editable . pytest tests/test_image_render.py tests/test_entrypoint.py -v
uv run --python 3.13 --with-editable . pytest tests/test_image_render.py tests/test_entrypoint.py -v
```

Expected: all four commands pass. If either dependency line fails against Textual 8.2.8, stop this plan and return to design review.

- [ ] **Step 7: Commit the compatibility proof and adapter**

Before staging, update `src/agent_zero_cli/AGENTS.md` to own the interactive-only renderer adapter and `requirements/AGENTS.md` to require the two Python-marked `textual-image` lines plus unconditional Pillow.

```bash
git add requirements/a0-runtime.in constraints/a0-runtime.txt pyproject.toml src/agent_zero_cli/image_render.py src/agent_zero_cli/__main__.py src/agent_zero_cli/app.py src/agent_zero_cli/AGENTS.md requirements/AGENTS.md tests/test_image_render.py tests/test_entrypoint.py
git commit -m "feat: add terminal image renderer adapter"
```

---

### Task 2: Normalize Browser and Chat Image References

**Files:**
- Create: `src/agent_zero_cli/media_refs.py`
- Create: `tests/test_media_refs.py`
- Modify: `src/agent_zero_cli/AGENTS.md`

**Interfaces:**
- Consumes: normalized connector event dictionaries and the current `A0Client.base_url`.
- Produces: `ImageReference` and `extract_image_references()` exactly as declared in Shared Interfaces.

- [ ] **Step 1: Write failing extraction and rejection tests**

Cover each approved source and stable deduplication:

```python
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


def test_extracts_assistant_markdown_and_bounded_data_image() -> None:
    event = {
        "context_id": "ctx-1",
        "sequence": 3,
        "event": "assistant_message",
        "data": {"text": "![chart](img:///a0/usr/charts/result.png)"},
    }
    refs = extract_image_references(event, base_url="http://agent.test")
    assert refs[0].owner == "assistant"
    assert refs[0].caption == "chart"
    assert refs[0].value == "/a0/usr/charts/result.png"


def test_rejects_external_url_parent_paths_and_oversized_data() -> None:
    external = {
        "context_id": "ctx-1",
        "sequence": 4,
        "event": "assistant_message",
        "data": {"text": "![remote](https://other.test/image.png)"},
    }
    assert extract_image_references(external, base_url="https://agent.test") == ()


def test_cache_buster_does_not_change_cache_key() -> None:
    first = {
        "context_id": "ctx-1",
        "sequence": 8,
        "event": "tool_output",
        "data": {"meta": {"Screenshot": "img:///a0/tmp/screen.jpg&t=1"}},
    }
    second = {
        "context_id": "ctx-1",
        "sequence": 9,
        "event": "tool_output",
        "data": {"meta": {"Screenshot": "img:///a0/tmp/screen.jpg&t=2"}},
    }
    assert extract_image_references(first, base_url="http://agent.test")[0].cache_key == (
        extract_image_references(second, base_url="http://agent.test")[0].cache_key
    )
```

Also cover `browser_snapshot.uri`, `browser_snapshot.a0_path`, same-origin `/api/image_get?path=...`, attachment dictionaries containing `path`, duplicate references in metadata/Markdown, unsupported data MIME, invalid base64 length, and filenames containing `/`, `\\`, `?`, or `#`.

Add a structured snapshot with only `ephemeral_ref: "a0-ephemeral-image://ctx/ref"` and assert it produces one browser `ImageReference` with `source="unavailable"` and `value="ephemeral screenshot is not fetchable"`; this keeps transcript placement visible without inventing a Core fetch route.

- [ ] **Step 2: Run the extraction tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_media_refs.py -v
```

Expected: collection fails because `agent_zero_cli.media_refs` does not exist.

- [ ] **Step 3: Implement pure normalization with source and entry identities**

Use `urllib.parse`, `hashlib.sha256`, and bounded string checks only; this module must not import httpx, Pillow, Textual, or Rich.

Normalize `img://` references by stripping the prefix and the Core-style `&t=` suffix. Normalize `/api/image_get` references by extracting the `path` query value. Accept an absolute `/api/image_get` URL only when its `(scheme, hostname, port)` matches `base_url`.

Build identities exactly this way:

```python
source_identity = f"{source}:{value}".encode("utf-8")
cache_key = hashlib.sha256(source_identity).hexdigest()
entry_key = f"{sequence}:{cache_key}"
```

Sanitize attachment names with `replace("\\", "/")`, remove query and fragment text, take the final basename, reject `""`, `"."`, and `".."`, and map accepted names to `/a0/usr/uploads/<basename>`.

For data URIs, accept only a `;base64,` payload whose declared MIME is in the approved raster set. Estimate decoded size before returning a reference:

```python
padding = payload[-2:].count("=")
decoded_size = (len(payload) * 3) // 4 - padding
if decoded_size > 25 * 1024 * 1024:
    return None
```

Deduplicate by `cache_key` while preserving source order. Caption rules are `Browser screenshot`, `User attachment — <basename>`, the Markdown alt text when present, and `Assistant image` otherwise.

For `browser_snapshot`, try `uri`, then `a0_path`, then `path`. If it contains only an `ephemeral_ref`, return the unavailable reference described in Step 1. Do not pass `a0-ephemeral-image://` into `/api/image_get`.

- [ ] **Step 4: Run the pure tests**

```bash
./.venv/bin/python -m pytest tests/test_media_refs.py -v
```

Expected: all tests pass without network or Textual startup.

- [ ] **Step 5: Commit reference extraction**

Add `media_refs.py` ownership and the same-origin/no-I/O normalization contract to `src/agent_zero_cli/AGENTS.md`.

```bash
git add src/agent_zero_cli/media_refs.py src/agent_zero_cli/AGENTS.md tests/test_media_refs.py
git commit -m "feat: extract transcript image references"
```

---

### Task 3: Add Authenticated Fetching, Validation, and the Memory Store

**Files:**
- Create: `src/agent_zero_cli/image_store.py`
- Create: `tests/test_image_store.py`
- Modify: `src/agent_zero_cli/client.py`
- Modify: `tests/test_client.py`
- Modify: `src/agent_zero_cli/AGENTS.md`

**Interfaces:**
- Consumes: `ImageReference`, `ImageRenderer.max_surface_pixels`, the existing authenticated `A0Client.http` session, and `/api/image_get?path=...`.
- Produces: `A0Client.fetch_image()`, `ImageAsset`, `ImageUnavailableError`, and `ImageStore` exactly as declared in Shared Interfaces.

- [ ] **Step 1: Write failing authenticated fetch tests**

Extend the existing `FakeResponse` with `content: bytes`. Add tests for the exact route, headers, MIME, size, redirect, and one retry:

```python
async def test_fetch_image_uses_authenticated_core_endpoint() -> None:
    client = A0Client("http://agent.test")
    client.http = Mock()
    client.http.get = AsyncMock(
        return_value=FakeResponse(
            content=b"png-bytes",
            headers={"content-type": "image/png"},
        )
    )

    content, mime = await client.fetch_image("/a0/usr/uploads/scan.png")

    assert (content, mime) == (b"png-bytes", "image/png")
    client.http.get.assert_awaited_once_with(
        "http://agent.test/api/image_get",
        params={"path": "/a0/usr/uploads/scan.png"},
        headers={"Origin": "http://agent.test", "Referer": "http://agent.test/"},
        follow_redirects=False,
    )
```

Use `httpx.ConnectError` as the first `side_effect` and a valid response as the second to prove exactly one retry. Add a `503` response followed by success and assert it also retries once. Assert that a second transport failure, a second `502|503|504`, login redirect, other HTTP error, non-image MIME, and content longer than 25 MiB raise `A0ProtocolError` without including response bytes.

- [ ] **Step 2: Write failing image store tests**

Use in-memory images generated by Pillow and a fake client:

```python
pytestmark = pytest.mark.anyio


def png_bytes(size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    image = PILImage.new("RGB", size, "#123456")
    image.save(output, format="PNG")
    return output.getvalue()


def image_reference(key: str) -> ImageReference:
    return ImageReference(
        entry_key=f"1:{key}",
        cache_key=key,
        context_id="ctx-1",
        sequence=1,
        owner="assistant",
        caption="Assistant image",
        source="agent_zero_path",
        value=f"/a0/usr/uploads/{key}.png",
    )


class FakeImageClient:
    def __init__(self, payload: bytes, mime: str = "image/png") -> None:
        self.payload = payload
        self.mime = mime
        self.calls = 0

    async def fetch_image(self, path: str) -> tuple[bytes, str]:
        assert path.startswith("/a0/")
        self.calls += 1
        return self.payload, self.mime


class BlockingImageClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.active = 0
        self.maximum_active = 0
        self.four_started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_image(self, path: str) -> tuple[bytes, str]:
        assert path.startswith("/a0/")
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == 4:
            self.four_started.set()
        try:
            await self.release.wait()
            return self.payload, "image/png"
        finally:
            self.active -= 1


async def test_store_coalesces_same_source_and_returns_oriented_surface() -> None:
    client = FakeImageClient(png_bytes((40, 20)))
    store = ImageStore(client, max_surface_pixels=(96, 64))
    first, second = await asyncio.gather(
        store.load(image_reference("same")),
        store.load(image_reference("same")),
    )
    assert first is not second
    assert first.image is not second.image
    assert client.calls == 1
    assert (first.width, first.height) == (40, 20)


async def test_store_limits_concurrency_to_four() -> None:
    client = BlockingImageClient(png_bytes((4, 4)))
    store = ImageStore(client, max_surface_pixels=(96, 64), max_concurrent=4)
    tasks = [
        asyncio.create_task(store.load(image_reference(str(index))))
        for index in range(5)
    ]
    await client.four_started.wait()
    assert client.maximum_active == 4
    client.release.set()
    await asyncio.gather(*tasks)


async def test_store_evicts_lru_and_closes_surface() -> None:
    client = FakeImageClient(png_bytes((4, 4)))
    store = ImageStore(
        client,
        max_surface_pixels=(4, 4),
        max_cache_bytes=60,
    )
    first = await store.load(image_reference("first"))
    second = await store.load(image_reference("second"))
    reloaded = await store.load(image_reference("first"))
    assert client.calls == 3
    assert store.cache_bytes <= 60
    first.close()
    second.close()
    reloaded.close()
```

Also test data URI decoding, MIME mismatch, unsupported SVG, corrupt bytes, first GIF frame, EXIF transpose, alpha compositing against `#17181a`, a monkeypatched 32-megapixel limit, `cancel_pending()`, and `clear()`.

- [ ] **Step 3: Run both focused files and verify missing behavior**

```bash
./.venv/bin/python -m pytest tests/test_client.py -k fetch_image -v
./.venv/bin/python -m pytest tests/test_image_store.py -v
```

Expected: `A0Client.fetch_image` and `agent_zero_cli.image_store` are missing.

- [ ] **Step 4: Implement bounded image retrieval on `A0Client`**

Add:

```python
async def fetch_image(
    self,
    path: str,
    *,
    max_bytes: int = 25 * 1024 * 1024,
) -> tuple[bytes, str]:
```

Reject values that are empty or do not begin with `/`. GET `_core_api_url("image_get")` with `params={"path": path}`, `_browser_headers()`, and `follow_redirects=False`. Retry once for `httpx.TransportError` or status `502`, `503`, or `504`; do not retry authentication, validation, or other HTTP errors. Apply `_is_login_redirect`, HTTP status, `Content-Length`, actual byte length, and normalized `image/*` checks. Return `(response.content, content_type_without_parameters)`.

- [ ] **Step 5: Implement validation, resizing, coalescing, and LRU cleanup**

Use an `asyncio.Semaphore(4)` for fetch/load tasks, a separate
`asyncio.Semaphore(1)` for full-resolution decoding, an
`OrderedDict[str, ImageAsset]`, and a
`dict[str, asyncio.Task[ImageAsset]]`. `load()` first checks the cache, then
shares an in-flight task by `cache_key`. The task fetches data, holds the decode
permit while Pillow work runs through `asyncio.to_thread` (including while a
cancelled caller waits for that non-cancellable thread to finish), inserts the
completed cache master, and evicts least-recently-used masters until
`cache_bytes <= max_cache_bytes`. Return `master.clone()` to every caller so an
entry owns and closes its display surface without sharing the cache master's
Pillow object.

Define the safe error type exactly:

```python
class ImageUnavailableError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "image unavailable")
        super().__init__(self.reason)
```

The synchronous decoder must follow this sequence:

```python
with PILImage.open(io.BytesIO(content)) as probe:
    probe.verify()
with PILImage.open(io.BytesIO(content)) as decoded:
    if decoded.format not in {"PNG", "JPEG", "WEBP", "GIF", "BMP"}:
        raise ImageUnavailableError("unsupported format")
    if decoded.width * decoded.height > 32_000_000:
        raise ImageUnavailableError("image dimensions are too large")
    decoded.seek(0)
    orientation = decoded.getexif().get(274, 1)
    pre_orientation_target = (
        (max_surface_pixels[1], max_surface_pixels[0])
        if orientation in {5, 6, 7, 8}
        else max_surface_pixels
    )
    decoded.thumbnail(pre_orientation_target, PILImage.Resampling.LANCZOS)
    oriented = ImageOps.exif_transpose(decoded)
    rgba = oriented.convert("RGBA")
    background = PILImage.new("RGBA", rgba.size, (23, 24, 26, 255))
    background.alpha_composite(rgba)
    surface = background.convert("RGB")
    surface.thumbnail(max_surface_pixels, PILImage.Resampling.LANCZOS)
    surface.load()
```

Wrap both opens in `warnings.catch_warnings()` with
`PILImage.DecompressionBombWarning` promoted to an error, and convert Pillow
bomb/corruption exceptions to safe `ImageUnavailableError` reasons. Normalize
`image/jpg` to `image/jpeg` and require the declared MIME to agree with the
decoded format using `PNG -> image/png`, `JPEG -> image/jpeg`,
`WEBP -> image/webp`, `GIF -> image/gif`, and `BMP -> image/bmp`. Set
`cost_bytes = surface.width * surface.height * 3`. `ImageAsset.clone()` copies
and fully loads the Pillow surface; `close()` calls `image.close()`.
`cancel_pending()` cancels and clears in-flight tasks but retains cached masters.
`clear()` cancels pending work, closes every cached master, and resets
accounting.

- [ ] **Step 6: Run service tests and the client regression file**

```bash
./.venv/bin/python -m pytest tests/test_image_store.py tests/test_client.py -v
```

Expected: all tests pass and no test uses a live Agent Zero server.

- [ ] **Step 7: Commit the authenticated image service**

Record `image_store.py` ownership, authenticated same-origin loading, memory-only caching, and numeric limits in `src/agent_zero_cli/AGENTS.md`.

```bash
git add src/agent_zero_cli/client.py src/agent_zero_cli/image_store.py src/agent_zero_cli/AGENTS.md tests/test_client.py tests/test_image_store.py
git commit -m "feat: load and validate Agent Zero images"
```

---

### Task 4: Build the Focusable Image Widget

**Files:**
- Create: `src/agent_zero_cli/widgets/image_entry.py`
- Create: `tests/test_chat_log_images.py`
- Modify: `src/agent_zero_cli/widgets/__init__.py`
- Modify: `src/agent_zero_cli/styles/app.tcss`
- Modify: `src/agent_zero_cli/widgets/AGENTS.md`
- Modify: `tests/AGENTS.md`

**Interfaces:**
- Consumes: `ImageReference`, `ImageAsset`, `CellBox`, and `ImageRenderer`.
- Produces: `ImageEntry` and `ImageEntry.LoadRequested` exactly as declared in Shared Interfaces.

- [ ] **Step 1: Write failing widget state and interaction tests with a fake renderer**

Define a fake that emits Textual `Static` widgets instead of terminal controls:

```python
pytestmark = pytest.mark.anyio


class FakeRenderer:
    mode = "halfcell"
    notice = ""
    max_surface_pixels = (192, 64)

    def fit_box(self, image_size: tuple[int, int], *, available_columns: int, expanded: bool) -> CellBox:
        del image_size
        return CellBox(min(80, available_columns), 24) if expanded else CellBox(36, 12)

    def create_widget(self, image: PILImage.Image, box: CellBox) -> Static:
        return Static(f"surface {image.width}x{image.height} at {box.columns}x{box.rows}")

    def create_halfcell_widget(self, image: PILImage.Image, box: CellBox) -> Static:
        return self.create_widget(image, box)

    def cleanup_widget(self, widget: Widget | None) -> None:
        if widget is not None:
            widget.remove()


def browser_ref(*, sequence: int = 8, context_id: str = "ctx-1") -> ImageReference:
    return ImageReference(
        entry_key=f"{sequence}:browser-cache",
        cache_key="browser-cache",
        context_id=context_id,
        sequence=sequence,
        owner="browser",
        caption="Browser screenshot",
        source="agent_zero_path",
        value="/a0/tmp/browser/history.jpg",
    )


def user_ref(*, sequence: int = 2, context_id: str = "ctx-1") -> ImageReference:
    return ImageReference(
        entry_key=f"{sequence}:user-cache",
        cache_key="user-cache",
        context_id=context_id,
        sequence=sequence,
        owner="user",
        caption="User attachment — scan.png",
        source="agent_zero_path",
        value="/a0/usr/uploads/scan.png",
    )


def image_asset(cache_key: str = "browser-cache") -> ImageAsset:
    image = PILImage.new("RGB", (72, 24), "#123456")
    return ImageAsset(
        cache_key=cache_key,
        mime_type="image/png",
        image=image,
        width=image.width,
        height=image.height,
        cost_bytes=image.width * image.height * 3,
    )
```

Test that a new fetchable entry is pending, an `unavailable` reference starts with its stable reason and never posts, `request_load()` posts one message and changes to loading, a matching generation accepts an asset, stale generations close their unused asset, Enter/Space/click toggle expansion, toggle does not issue a second load, resize preserves expansion, an error displays a stable captioned placeholder and does not repost in the same mounted entry, `release_surface()` returns a rendered off-screen entry to pending, `copy_text()` returns only the semantic placeholder, and unmount invokes renderer and Pillow cleanup.

- [ ] **Step 2: Run the widget tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_chat_log_images.py -k image_entry -v
```

Expected: collection fails because `agent_zero_cli.widgets.image_entry` does not exist.

- [ ] **Step 3: Implement the widget state machine and renderer replacement**

Implement `ImageEntry` as a focusable `Vertical` with Enter and Space bindings. Compose a `.image-caption`, `.image-placeholder`, and `.image-surface-host`. The caption suffix is `Enter/Space to expand` in thumbnail state and `Enter/Space to collapse` in expanded state; mouse users receive the same toggle through click. Do not perform network calls.

`request_load()` increments a generation only when state is pending, sets loading text, and posts:

```python
self.post_message(self.LoadRequested(self, self.reference, self._generation))
```

`set_asset()` takes ownership of the caller's cloned asset. It accepts only the current generation, closes stale assets, hides the placeholder, and mounts a renderer widget sized by `fit_box()`. Catch native construction failure once and call `create_halfcell_widget()`; if that also fails, close the asset and call `set_unavailable(generation, "renderer failed")`.

`action_toggle()` flips `_expanded`, remounts from the owned asset, refreshes layout, and calls `scroll_visible(animate=False)`. `on_click()` ignores clicks during text selection. `on_resize()` replaces any existing timer and calls the same remount function after 0.1 seconds. `release_surface()` cleans the renderer child, closes the owned asset, clears it, and returns rendered entries to pending. `on_unmount()` cancels the timer and calls `release_surface()`.

- [ ] **Step 4: Add stable TCSS**

Add rules with these layout contracts:

```css
ImageEntry {
    height: auto;
    margin-top: 1;
    padding: 0 0 0 4;
}

ImageEntry:focus {
    background: #1b232d;
}

.image-caption,
.image-placeholder {
    height: auto;
    color: #9aa7b4;
}

.image-surface-host {
    width: auto;
    height: auto;
}
```

Export `ImageEntry` from `widgets/__init__.py`.

- [ ] **Step 5: Run focused interaction tests**

```bash
./.venv/bin/python -m pytest tests/test_chat_log_images.py -k image_entry -v
```

Expected: all `ImageEntry` tests pass without emitting a native escape sequence.

- [ ] **Step 6: Commit the standalone widget**

Update the widget DOX with the no-network, semantic-copy, and cleanup contracts, and the test DOX with the fake-renderer/no-native-control rule.

```bash
git add src/agent_zero_cli/widgets/image_entry.py src/agent_zero_cli/widgets/__init__.py src/agent_zero_cli/styles/app.tcss src/agent_zero_cli/widgets/AGENTS.md tests/test_chat_log_images.py tests/AGENTS.md
git commit -m "feat: add expandable transcript image widget"
```

---

### Task 5: Group Transcript Content and Lazily Schedule Images

**Files:**
- Modify: `src/agent_zero_cli/widgets/chat_log.py`
- Modify: `src/agent_zero_cli/app.py`
- Modify: `src/agent_zero_cli/styles/app.tcss`
- Modify: `tests/test_chat_log_images.py`
- Modify: `tests/test_app.py`
- Modify: `src/agent_zero_cli/widgets/AGENTS.md`

**Interfaces:**
- Consumes: existing `SelectableStatic`, `StatusEntry`, and `CodeEntry`; `ImageEntry`; and `ImageRenderer`.
- Produces: `TranscriptEntry`, `ChatLog.append_or_update_images()`, and `ChatLog.request_nearby_images()` exactly as declared in Shared Interfaces.

- [ ] **Step 1: Write failing ownership, copy, and lazy-load tests**

Add a test app that yields `ChatLog(image_renderer=FakeRenderer())`. Establish these behaviors:

```python
async def test_browser_image_stays_inside_same_sequence_entry() -> None:
    async with TranscriptImageApp().run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        log.append_or_update_status(8, "Using tool", "click", {"tool_name": "browser"})
        log.append_or_update_images(8, (browser_ref(sequence=8),))
        await pilot.pause()

        owner = log._seq_to_widget[8]
        assert isinstance(owner, TranscriptEntry)
        assert isinstance(owner.primary, StatusEntry)
        assert len(owner.query(ImageEntry)) == 1
        assert owner.query_one(ImageEntry).parent is owner


async def test_late_image_upsert_does_not_duplicate_owner_or_image() -> None:
    async with TranscriptImageApp().run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        ref = browser_ref(sequence=8)
        log.append_or_update_status(8, "Using tool", "click")
        log.append_or_update_images(8, (ref,))
        log.append_or_update_images(8, (ref,))
        await pilot.pause()
        assert len(log.query(TranscriptEntry)) == 1
        assert len(log.query(ImageEntry)) == 1


async def test_copy_uses_semantic_image_placeholder() -> None:
    async with TranscriptImageApp().run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        log.append_or_update(2, Text("See this"))
        log.append_or_update_images(2, (user_ref(sequence=2),))
        await pilot.pause()
        copied = log.copyable_text(visible_only=False)
        assert "See this" in copied
        assert "[image: User attachment — scan.png]" in copied
        assert "data:image" not in copied
```

Add a two-reference user entry and assert its `ImageEntry` children remain in tuple/source order and are vertically stacked under the primary message. Mount 100 sequence entries with 50 image references in a 30-row test viewport. Collect `LoadRequested` messages and assert that fewer than 50 are posted initially and no more than entries in the visible viewport plus one viewport above/below are requested. Scroll to the end and assert newly near-visible entries request once; assert surfaces farther than two viewports are released.

- [ ] **Step 2: Run grouping tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_chat_log_images.py -k "sequence_entry or late_image or copy_uses or lazy" -v
```

Expected: `TranscriptEntry` and `append_or_update_images` are missing.

- [ ] **Step 3: Introduce `TranscriptEntry` without changing existing render output**

Change `_seq_to_widget` to `dict[int, TranscriptEntry]`. Add a private `_entry(sequence, prepend)` factory. Each existing append method obtains the wrapper and updates only its primary child:

```python
entry = self._entry(sequence, prepend=prepend)
widget = entry.set_primary(StatusEntry)
assert isinstance(widget, StatusEntry)
widget.set_status(
    label,
    detail,
    meta,
    active=active,
    shimmer_phase=self._shimmer_phase,
    shimmer_frame=self._shimmer_frame,
)
```

`set_primary()` keeps the current widget when its exact type matches, or removes and replaces it while leaving image children intact. Preserve the current `append_or_update`, status, code, prepend, shimmer, history cursor, and autoscroll public APIs.

`TranscriptEntry.copy_text()` concatenates primary `copy_text()` output and each `ImageEntry.copy_text()` with blank lines. Change ChatLog's copy helpers to accept direct child `Widget` instances with callable `copy_text`, not only `Static`.

- [ ] **Step 4: Add image upsert and viewport scheduling**

`upsert_images()` keys children by `reference.entry_key`, never removes an existing image merely because a later event lacks metadata, and preserves each child's expansion state.

After mounting/upserting, use `call_after_refresh(request_nearby_images)`. A candidate is near-visible when its screen region intersects the ChatLog content region grown vertically by one viewport height. Call `entry.request_load()` only for pending entries; unavailable entries remain stable until history is reloaded into a fresh widget. When a rendered entry is farther than two viewport heights from the content region, call `release_surface()` so its decoded display copy is reclaimed; returning near the viewport loads a fresh clone from cache or refetches after LRU eviction.

Call `request_nearby_images()` after mouse wheel scrolling, keyboard line/page/home/end scrolling, resize, and history prepend. Do not use sleeps or eagerly walk into the network layer.

Replace the direct-child margin rules with the wrapper-aware layout:

```css
#chat-log > TranscriptEntry {
    height: auto;
    margin-top: 1;
}

TranscriptEntry > SelectableStatic,
TranscriptEntry > StatusEntry,
TranscriptEntry > CodeEntry {
    height: auto;
}
```

Pass `self.image_renderer` into `ChatLog` from `AgentZeroCLI.compose()`.

- [ ] **Step 5: Update existing ChatLog test assertions and fake APIs**

Where tests previously asserted `_seq_to_widget[sequence]` was a `StatusEntry`, assert its `.primary` instead. Add `append_or_update_images()` storage to `FakeChatLog` in `tests/test_app.py`:

```python
def append_or_update_images(self, sequence: int, references: tuple[ImageReference, ...], *, prepend: bool = False) -> None:
    del prepend
    self.image_entries[sequence] = references
```

Initialize `self.image_entries` in the fake constructor and let the fake `append_or_update_status()` accept and ignore the existing `prepend` keyword so prepended media/status replay can share the production call shape. Do not weaken assertions for current text/status/code output.

- [ ] **Step 6: Run all ChatLog and application regressions**

```bash
./.venv/bin/python -m pytest tests/test_chat_log_images.py tests/test_app.py -v
```

Expected: existing transcript behavior and new grouping/lazy tests pass.

- [ ] **Step 7: Commit sequence-level media ownership**

Update `widgets/AGENTS.md` with `TranscriptEntry` sequence ownership, lazy near-viewport loading, off-screen surface release, and semantic-copy behavior.

```bash
git add src/agent_zero_cli/widgets/chat_log.py src/agent_zero_cli/app.py src/agent_zero_cli/styles/app.tcss src/agent_zero_cli/widgets/AGENTS.md tests/test_chat_log_images.py tests/test_app.py
git commit -m "feat: group images with transcript entries"
```

---

### Task 6: Wire Live/Replay Events to App-Level Loading and Lifecycle Cleanup

**Files:**
- Modify: `src/agent_zero_cli/app.py`
- Modify: `src/agent_zero_cli/event_handlers.py`
- Modify: `src/agent_zero_cli/chat_commands.py`
- Modify: `src/agent_zero_cli/connection.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_chat_log_images.py`
- Modify: `tests/test_entrypoint.py`
- Modify: `tests/test_headless.py`
- Modify: `tests/test_gateway.py`
- Modify: `src/agent_zero_cli/AGENTS.md`

**Interfaces:**
- Consumes: `extract_image_references()`, `ImageStore.load()`, `ImageEntry.LoadRequested`, and `ChatLog.append_or_update_images()`.
- Produces: `AgentZeroCLI._load_image_entry()` and complete snapshot/live/lifecycle behavior.

- [ ] **Step 1: Write failing event placement tests**

Extend `FakeChatLog` with `image_entries`. Test all approved event sources:

```python
def test_live_browser_screenshot_attaches_to_status_sequence(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.current_context = "ctx-1"
    dummy_app._handle_context_event(
        {
            "context_id": "ctx-1",
            "sequence": 8,
            "event": "tool_output",
            "data": {
                "meta": {
                    "tool_name": "browser",
                    "Screenshot": "img:///a0/tmp/history.jpg&t=1",
                }
            },
        }
    )
    log = dummy_app._test_widgets["#chat-log"]
    assert log.image_entries[8][0].owner == "browser"


def test_snapshot_attaches_user_and_assistant_history_images(dummy_app: DummyAgentZeroCLI) -> None:
    dummy_app.current_context = "ctx-1"
    dummy_app._handle_context_snapshot(
        {
            "context_id": "ctx-1",
            "events": [
                {
                    "context_id": "ctx-1",
                    "sequence": 2,
                    "event": "user_message",
                    "data": {"text": "scan", "meta": {"attachments": ["scan.png"]}},
                },
                {
                    "context_id": "ctx-1",
                    "sequence": 3,
                    "event": "assistant_message",
                    "data": {"text": "![result](img:///a0/usr/result.png)"},
                },
            ],
        }
    )
    log = dummy_app._test_widgets["#chat-log"]
    assert [log.image_entries[key][0].owner for key in (2, 3)] == ["user", "assistant"]
```

Also test image-only assistant events, prepended history, late status metadata, duplicate updates, and an external Markdown URL remaining text-only.

- [ ] **Step 2: Write failing load, stale-result, and lifecycle tests**

Use a fake `ImageStore` and real `ImageEntry` under `run_test()`:

```python
class BlockingStore:
    def __init__(self, asset: ImageAsset) -> None:
        self.asset = asset
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def load(self, reference: ImageReference) -> ImageAsset:
        assert reference.context_id == "ctx-old"
        self.started.set()
        await self.release.wait()
        return self.asset

    def cancel_pending(self) -> None:
        return None

    def clear(self) -> None:
        return None


class ImageLoadingApp(AgentZeroCLI):
    def __init__(self, store: BlockingStore) -> None:
        super().__init__(
            config=CLIConfig(instance_url="http://agent.test"),
            image_renderer=FakeRenderer(),
        )
        self.image_store = store

    async def _startup(self) -> None:
        return None


async def test_load_result_is_ignored_after_context_switch() -> None:
    store = BlockingStore(image_asset())
    app = ImageLoadingApp(store=store)
    async with app.run_test(size=(100, 30)) as pilot:
        app.current_context = "ctx-old"
        log = app.query_one(ChatLog)
        log.append_or_update_images(8, (browser_ref(context_id="ctx-old"),))
        await pilot.pause()
        entry = log.query_one(ImageEntry)
        entry.request_load()
        await store.started.wait()
        app.current_context = "ctx-new"
        store.release.set()
        await pilot.pause()
        assert entry.state == "loading"
```

Assert `/clear` and context switch call `cancel_pending()` and remove widgets, while disconnect and exit call `clear()`. Assert changing the host clears the store before `client.base_url` changes. Assert an unavailable image never changes `connected`, `agent_active`, or event rendering state. Construct an app with a renderer notice, run mount once, and assert `_show_notice` receives that exact string once.

- [ ] **Step 3: Run the orchestration tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_app.py -k "screenshot or history_images or image_store" -v
./.venv/bin/python -m pytest tests/test_chat_log_images.py -k "load_result or unavailable" -v
```

Expected: no event media helper or app load handler exists.

- [ ] **Step 4: Attach normalized references after every owning event render**

Add one helper in `event_handlers.py`:

```python
def _append_event_images(
    app: AgentZeroCLI,
    log: ChatLog,
    event: dict[str, Any],
    *,
    prepend: bool = False,
) -> None:
    references = extract_image_references(event, base_url=app.client.base_url)
    if not references:
        return
    sequence = references[0].sequence
    log.append_or_update_images(sequence, references, prepend=prepend)
```

For snapshots, invoke it after the normal render/status branch for every event. For live response events, invoke it before the existing early return. For status/tool events, invoke it after `set_active_status()` or `append_or_update_status()` so the primary child exists first. For image-only events, `append_or_update_images()` creates an owner without an empty text panel.

- [ ] **Step 5: Own loading in `AgentZeroCLI`, not in widgets**

Construct:

```python
self.image_store = ImageStore(
    self.client,
    max_surface_pixels=self.image_renderer.max_surface_pixels,
)
```

Handle `ImageEntry.LoadRequested` by stopping the message and starting a named worker. The worker captures `reference.context_id`, awaits `image_store.load(reference)`, and applies the result only when the entry is still attached, its generation still matches, and `self.current_context == reference.context_id`. Close the returned asset before ignoring any stale result. Convert `ImageUnavailableError.reason` to `set_unavailable`; convert unexpected exceptions to `set_unavailable(generation, "load failed")` without logging payload data.

If the renderer mode is `off`, `ImageEntry` begins disabled and does not post a load request.

- [ ] **Step 6: Wire cancellation and cache cleanup into existing boundaries**

Apply these exact ownership rules:

- `chat_commands.cmd_clear`: `cancel_pending()`, then `ChatLog.clear()`; retain the LRU cache.
- `chat_commands.switch_context`: `cancel_pending()` before clearing/mounting the new transcript; retain cache for the same host.
- `connection.begin_connection`: `image_store.clear()` before changing `client.base_url`.
- `connection._reset_disconnected_state`: clear ChatLog first, then `image_store.clear()`.
- `connection.disconnect_and_exit`: clear ChatLog, clear store, then disconnect/exit.
- Widget removal continues to own native protocol cleanup.

- [ ] **Step 7: Prove headless and gateway remain image-renderer-free**

Add subprocess-style import assertions that run the entrypoint with `_run_headless` or `_run_gateway` stubbed and then assert `"textual_image" not in sys.modules`. Keep `image_render` imported only inside `_run_app`; neither `headless/` nor `gateway.py` imports `app.py`, `image_store.py`, or `widgets/image_entry.py`.

Run:

```bash
./.venv/bin/python -m pytest tests/test_app.py tests/test_chat_log_images.py tests/test_entrypoint.py tests/test_headless.py tests/test_gateway.py -v
```

Expected: all tests pass; JSONL and text outputs are unchanged.

- [ ] **Step 8: Commit the end-to-end TUI flow**

Update `src/agent_zero_cli/AGENTS.md` with app-level image loading and the clear/context/host/disconnect/exit lifecycle ownership established by this task.

```bash
git add src/agent_zero_cli/app.py src/agent_zero_cli/event_handlers.py src/agent_zero_cli/chat_commands.py src/agent_zero_cli/connection.py src/agent_zero_cli/AGENTS.md tests/test_app.py tests/test_chat_log_images.py tests/test_entrypoint.py tests/test_headless.py tests/test_gateway.py
git commit -m "feat: display transcript images from connector events"
```

---

### Task 7: Make Preview Behavior Deterministic and Finish Documentation/Acceptance

**Files:**
- Modify: `devtools/preview_launcher.py`
- Modify: `devtools/snapshot.py`
- Modify: `devtools/README.md`
- Modify: `tests/test_devtools.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/configuration.md`
- Modify: `docs/tui-frontend.md`
- Modify: `AGENTS.md`
- Modify: `src/agent_zero_cli/AGENTS.md`
- Modify: `src/agent_zero_cli/widgets/AGENTS.md`
- Modify: `tests/AGENTS.md`
- Modify: `devtools/AGENTS.md`
- Modify: `requirements/AGENTS.md`

**Interfaces:**
- Consumes: the completed image mode, transcript, fetch, and lifecycle contracts.
- Produces: deterministic preview behavior, durable DOX ownership, operational guidance, and final acceptance evidence.

- [ ] **Step 1: Write failing preview-mode tests**

Add tests that monkeypatch `os.execv` and verify `preview_launcher.main()` sets `A0_CLI_IMAGE_MODE=halfcell` before exec. Update the snapshot test to inject or initialize a forced half-cell renderer and assert its mode is half-cell.

```python
def test_preview_launcher_forces_halfcell(monkeypatch: pytest.MonkeyPatch) -> None:
    preview = _load_module("preview_launcher_images", "devtools/preview_launcher.py")
    called: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(preview.os, "execv", lambda executable, argv: called.append((executable, argv)))
    preview.main()
    assert preview.os.environ["A0_CLI_IMAGE_MODE"] == "halfcell"
    assert called[0][1][-2:] == ["-m", "agent_zero_cli"]
```

- [ ] **Step 2: Run devtools tests and verify failure**

```bash
./.venv/bin/python -m pytest tests/test_devtools.py -v
```

Expected: preview and snapshot do not yet force/inject half-cell rendering.

- [ ] **Step 3: Force half-cell in browser and SVG preview paths**

Set `os.environ["A0_CLI_IMAGE_MODE"] = "halfcell"` in `preview_launcher.main()` immediately before `os.execv`. In `snapshot.py`, call `initialize_image_renderer(force_halfcell=True)` and pass it to `AgentZeroCLI`.

Document that xterm.js/browser previews deliberately show the same bounded half-cell content used by fallback terminals and do not exercise native protocol cleanup.

- [ ] **Step 4: Update user and architecture documentation**

Document all of the following with exact values:

- Eligible browser, user, and assistant history sources.
- Browser screenshot placement beneath the same tool metadata.
- Thumbnail and expanded cell limits plus click/Enter/Space behavior.
- `A0_CLI_IMAGE_MODE=auto|tgp|sixel|halfcell|off`.
- TGP, Sixel, half-cell, and browser-preview behavior.
- 25 MiB, 32-megapixel, four-fetch/load, single-full-resolution-decoder, and
  64 MiB limits, with downsampling before orientation and color conversion.
- Supported raster formats, first-frame behavior, SVG placeholder, same-origin-only loading, and memory-only caching.
- `/api/image_get` authenticated fetch and unchanged connector event schema for browser screenshots.
- The separate Agent Zero Core attachment metadata correction and its deployment boundary.
- Troubleshooting for forced fallback, unsupported protocol, tmux pass-through, unavailable image, and `off` mode.
- Headless/gateway remaining text/JSONL-only.

Correct the current architecture paragraph that says connector-runtime screenshots remain only in an ephemeral registry: the verified Core connector runtime materializes the host artifact and browser history metadata carries `Screenshot: img://<path>&t=...` plus `browser_snapshot`.

- [ ] **Step 5: Update DOX ownership**

Add `image_render.py`, `media_refs.py`, `image_store.py`, `widgets/image_entry.py`, transcript image ownership, fake-renderer test rules, preview forcing, and conditional dependency verification to the nearest AGENTS files. Do not duplicate full design prose across DOX scopes.

- [ ] **Step 6: Run the full automated verification set**

```bash
./.venv/bin/python devtools/lock_dependencies.py --check
./.venv/bin/python -m pytest tests/ -v
./.venv/bin/python devtools/snapshot.py --output /tmp/a0-inline-images.svg --width 100 --height 36
git diff --check
```

Expected: lock check and full suite pass, snapshot is created with no native protocol bytes, and the diff check is clean.

- [ ] **Step 7: Run real-terminal visual acceptance**

Use a chat containing one browser screenshot, one user attachment, and one assistant image. In each environment, verify thumbnail rendering, click/Enter/Space toggling, complete aspect ratio, scrolling, resizing, `/clear`, context switching, reconnect/history replay, and clean exit:

```bash
read -r -p "Verified Agent Zero URL: " a0_image_test_host
A0_CLI_IMAGE_MODE=tgp a0 --host "$a0_image_test_host" --no-docker-discovery --connect
A0_CLI_IMAGE_MODE=sixel a0 --host "$a0_image_test_host" --no-docker-discovery --connect
A0_CLI_IMAGE_MODE=halfcell a0 --host "$a0_image_test_host" --no-docker-discovery --connect
A0_CLI_IMAGE_MODE=off a0 --host "$a0_image_test_host" --no-docker-discovery --connect
```

Run the native commands only in matching capable terminals. Also run inside tmux; if pass-through is unavailable, verify one notice and a clean half-cell fallback. Record TGP, Sixel, and half-cell results separately from automated tests and from Core runtime deployment.

- [ ] **Step 8: Commit documentation and acceptance tooling**

```bash
git add README.md docs/architecture.md docs/configuration.md docs/tui-frontend.md devtools/preview_launcher.py devtools/snapshot.py devtools/README.md tests/test_devtools.py AGENTS.md src/agent_zero_cli/AGENTS.md src/agent_zero_cli/widgets/AGENTS.md tests/AGENTS.md devtools/AGENTS.md requirements/AGENTS.md
git commit -m "docs: document inline terminal images"
```

---

## Final Verification Checklist

- [ ] Dependency lock check passes and conditional branches pass on Python 3.10, 3.11, 3.12, and 3.13.
- [ ] Browser screenshots render under the existing browser tool metadata without duplicate sequence entries.
- [ ] User and assistant live/replayed images render under their owning messages.
- [ ] TGP and Sixel show raster thumbnails in capable terminals; half-cell is universal and deterministic in previews.
- [ ] Click, Enter, and Space expand/collapse inline while preserving state across updates and resize.
- [ ] Long replay does not eagerly fetch 50 images; concurrency and cache limits are enforced.
- [ ] Invalid, unsupported, oversized, unauthenticated, and unavailable images become stable placeholders.
- [ ] Copy text contains semantic placeholders and no bytes, base64, cookies, or cache paths.
- [ ] `/clear`, context/host changes, disconnect, and exit cancel work and clean native surfaces.
- [ ] Headless and gateway tests prove no image library import or terminal protocol output.
- [ ] CLI automated evidence, Core deployment evidence, and real-terminal visual acceptance are reported as separate surfaces.
