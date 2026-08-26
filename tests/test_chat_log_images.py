from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from PIL import Image as PILImage
import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.selection import SELECT_ALL
from textual.widget import Widget
from textual.widgets import Static

from agent_zero_cli.image_render import CellBox
from agent_zero_cli.image_store import ImageAsset
from agent_zero_cli.media_refs import ImageReference
from agent_zero_cli.widgets.chat_log import (
    ChatLog,
    SelectableStatic,
    StatusEntry,
    TranscriptEntry,
)
from agent_zero_cli.widgets.image_entry import ImageEntry


pytestmark = pytest.mark.anyio


class FakeRenderer:
    mode = "halfcell"
    notice = ""
    max_surface_pixels = (192, 64)

    def __init__(self, *, fail_native: bool = False) -> None:
        self.fail_native = fail_native
        self.created: list[tuple[str, CellBox]] = []
        self.cleaned: list[Widget] = []
        self.redrawn: list[Widget] = []

    def fit_box(
        self,
        image_size: tuple[int, int],
        *,
        available_columns: int,
        expanded: bool,
    ) -> CellBox:
        del image_size
        return CellBox(min(80, available_columns), 24) if expanded else CellBox(36, 12)

    def create_widget(self, image: PILImage.Image, box: CellBox) -> Static:
        if self.fail_native:
            raise RuntimeError("native rendering failed")
        self.created.append(("native", box))
        return Static(f"surface {image.width}x{image.height} at {box.columns}x{box.rows}")

    def cleanup_widget(self, widget: Widget | None) -> None:
        if widget is not None:
            self.cleaned.append(widget)
            widget.remove()

    def redraw_widget(self, widget: Widget | None) -> None:
        if self.mode == "sixel" and widget is not None:
            self.redrawn.append(widget)


class FakeTimer:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


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


def user_ref(*, sequence: int = 2, name: str = "scan.png") -> ImageReference:
    return ImageReference(
        entry_key=f"{sequence}:user-{name}",
        cache_key=f"user-{name}",
        context_id="ctx-1",
        sequence=sequence,
        owner="user",
        caption=f"User attachment — {name}",
        source="agent_zero_path",
        value=f"/a0/usr/uploads/{name}",
    )


def image_asset() -> ImageAsset:
    image = PILImage.new("RGB", (72, 24), "#123456")
    return ImageAsset(
        cache_key="browser-cache",
        mime_type="image/png",
        image=image,
        width=image.width,
        height=image.height,
        cost_bytes=image.width * image.height * 3,
    )


@pytest.mark.parametrize(
    "reference",
    (
        browser_ref(),
        user_ref(),
        replace(
            user_ref(name="answer.png"),
            owner="assistant",
            caption="Assistant image",
        ),
    ),
    ids=("browser", "user", "assistant"),
)
def test_transcript_images_start_expanded(reference: ImageReference) -> None:
    assert ImageEntry(reference, FakeRenderer()).expanded


class ImageEntryHarness(App[None]):
    def __init__(self, entry: ImageEntry) -> None:
        super().__init__()
        self.entry = entry
        self.load_requests: list[ImageEntry.LoadRequested] = []

    def compose(self) -> ComposeResult:
        yield Vertical(self.entry)

    def on_image_entry_load_requested(self, message: ImageEntry.LoadRequested) -> None:
        self.load_requests.append(message)


class TranscriptImageApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.renderer = FakeRenderer()
        self.load_requests: list[ImageEntry.LoadRequested] = []

    def compose(self) -> ComposeResult:
        yield ChatLog(image_renderer=self.renderer, id="chat-log")

    def on_image_entry_load_requested(self, message: ImageEntry.LoadRequested) -> None:
        self.load_requests.append(message)


async def test_browser_image_stays_inside_same_sequence_entry() -> None:
    app = TranscriptImageApp()

    async with app.run_test(size=(100, 30)) as pilot:
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
    app = TranscriptImageApp()

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        ref = browser_ref(sequence=8)
        log.append_or_update_status(8, "Using tool", "click")
        log.append_or_update_images(8, (ref,))
        log.append_or_update_images(8, (ref,))
        await pilot.pause()

        assert len(log.query(TranscriptEntry)) == 1
        assert len(log.query(ImageEntry)) == 1


async def test_disabled_renderer_preserves_pre_image_transcript() -> None:
    app = TranscriptImageApp()
    app.renderer.mode = "off"

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        log.append_or_update(2, Text("See this"))
        log.append_or_update_images(2, (user_ref(sequence=2),))
        await pilot.pause()

        assert len(log.query(ImageEntry)) == 0
        assert log.copyable_text(visible_only=False).strip() == "See this"
        assert app.load_requests == []


async def test_primary_type_replacement_preserves_image_children() -> None:
    app = TranscriptImageApp()

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        log.append_or_update_status(8, "Using tool", "click")
        log.append_or_update_images(8, (browser_ref(sequence=8),))
        await pilot.pause()

        owner = log._seq_to_widget[8]
        image = owner.query_one(ImageEntry)
        log.append_or_update(8, Text("Tool finished"), scroll=False)
        await pilot.pause()

        assert type(owner.primary) is SelectableStatic
        assert owner.query_one(ImageEntry) is image
        assert image.parent is owner


def test_image_scan_scheduling_coalesces_one_callback_per_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = ChatLog(image_renderer=FakeRenderer())
    callbacks: list[object] = []
    scans: list[str] = []
    monkeypatch.setattr(
        log,
        "call_after_refresh",
        lambda callback, *args, **kwargs: callbacks.append(callback),
    )
    monkeypatch.setattr(log, "request_nearby_images", lambda: scans.append("scan"))

    for _ in range(100):
        log._schedule_nearby_images()

    assert len(callbacks) == 1
    callback = callbacks.pop()
    assert callable(callback)
    callback()
    assert scans == ["scan"]

    for _ in range(100):
        log._schedule_nearby_images()
    assert len(callbacks) == 1


async def test_scroll_watcher_refreshes_textual_viewport_before_scheduling_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TranscriptImageApp()

    async with app.run_test(size=(100, 30)):
        log = app.query_one(ChatLog)
        calls: list[str] = []
        log.show_vertical_scrollbar = True
        monkeypatch.setattr(log, "_refresh_scroll", lambda: calls.append("refresh"))
        monkeypatch.setattr(
            log,
            "_schedule_nearby_images",
            lambda: calls.append("scan"),
        )

        log.watch_scroll_y(2.0, 9.0)

        assert log.vertical_scrollbar.position == 9.0
        assert calls == ["refresh", "scan"]


async def test_copy_uses_semantic_image_placeholder() -> None:
    app = TranscriptImageApp()

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        log.append_or_update(2, Text("See this"))
        log.append_or_update_images(2, (user_ref(sequence=2),))
        await pilot.pause()

        copied = log.copyable_text(visible_only=False)
        assert "See this" in copied
        assert "[image: User attachment — scan.png]" in copied
        assert "data:image" not in copied


async def test_browser_status_media_metadata_never_renders_or_copies_raw_references() -> None:
    app = TranscriptImageApp()
    data_uri = "data:image/png;base64,AAAA"
    image_path = "img:///a0/tmp/browser/private.jpg&t=123"

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        log.append_or_update_status(
            8,
            "Using browser",
            "click",
            {
                "tool_name": "browser",
                "Screenshot": image_path,
                "browser_snapshot": {"uri": data_uri},
                "artifact": data_uri,
                "thoughts": [f"captured {data_uri}"],
            },
        )
        log.append_or_update_images(8, (browser_ref(sequence=8),))
        await pilot.pause()

        status = log._seq_to_widget[8].primary
        assert isinstance(status, StatusEntry)
        status.action_toggle()
        await pilot.pause()

        rendered = status.render().plain
        copied = log.copyable_text(visible_only=False)
        combined = f"{rendered}\n{copied}"
        assert image_path not in combined
        assert data_uri not in combined
        assert "img://" not in combined
        assert "data:image" not in combined
        assert "/a0/tmp/browser/private.jpg" not in combined
        assert "[image: Browser screenshot]" in copied


def test_image_surface_change_schedules_bottom_follow_only_while_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = ChatLog(image_renderer=FakeRenderer())
    scheduled: list[str] = []
    monkeypatch.setattr(log, "_schedule_scroll_end", lambda: scheduled.append("follow"))
    monkeypatch.setattr(log, "_schedule_nearby_images", lambda: scheduled.append("scan"))

    log._auto_follow = True
    log.on_image_entry_surface_changed(ImageEntry.SurfaceChanged())
    log._auto_follow = False
    log.on_image_entry_surface_changed(ImageEntry.SurfaceChanged())

    assert scheduled == ["follow", "scan", "scan"]


async def test_sequence_entry_keeps_image_source_order_below_primary() -> None:
    app = TranscriptImageApp()

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        first = user_ref(sequence=2, name="first.png")
        second = user_ref(sequence=2, name="second.png")
        log.append_or_update(2, Text("Attachments"))
        log.append_or_update_images(2, (first, second))
        await pilot.pause()

        owner = log._seq_to_widget[2]
        images = list(owner.query(ImageEntry))
        assert [image.reference for image in images] == [first, second]
        assert owner.primary is not None
        assert owner.primary.region.y < images[0].region.y < images[1].region.y


async def test_lazy_images_use_wider_bounded_retention_for_rendered_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TranscriptImageApp()

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        for sequence in range(100):
            log.append_or_update(sequence, Text(f"message {sequence}"), scroll=False)
            if sequence % 2 == 0:
                log.append_or_update_images(sequence, (browser_ref(sequence=sequence),))
        await pilot.pause()
        log.request_nearby_images()
        await pilot.pause()

        assert 0 < len(app.load_requests) < 50
        assert len(app.load_requests) <= log.content_region.height * 3

        first_near = log._seq_to_widget[0].query_one(ImageEntry)
        assert first_near.state == "loading"
        rendered_generation = first_near.generation
        rendered_asset = image_asset()
        closes: list[str] = []
        monkeypatch.setattr(rendered_asset, "close", lambda: closes.append("closed"))
        log._auto_follow = False
        first_near.set_asset(rendered_generation, rendered_asset)
        await pilot.pause()
        rendered_surface = first_near._surface
        assert rendered_surface is not None

        log.scroll_to(
            y=log.content_region.height * 4,
            animate=False,
            immediate=True,
        )
        await pilot.pause()
        log.request_nearby_images()

        assert first_near.state == "rendered"
        assert first_near.generation == rendered_generation
        assert first_near._asset is rendered_asset
        assert first_near._surface is rendered_surface

        log.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        log.request_nearby_images()

        assert len(app.load_requests) > 1
        assert first_near.state == "pending"
        assert first_near.generation > rendered_generation
        assert first_near._asset is None
        assert first_near._surface is None
        assert closes == ["closed"]
        assert rendered_surface in app.renderer.cleaned


async def test_visible_sixel_images_redraw_together_after_viewport_refresh() -> None:
    app = TranscriptImageApp()
    app.renderer.mode = "sixel"

    async with app.run_test(size=(100, 80)) as pilot:
        log = pilot.app.query_one(ChatLog)
        refs = (
            user_ref(sequence=2, name="first.png"),
            user_ref(sequence=2, name="second.png"),
        )
        log.append_or_update(2, Text("Attachments"), scroll=False)
        log.append_or_update_images(2, refs)
        await pilot.pause()

        images = list(log.query(ImageEntry))
        for image in images:
            image.request_load()
            image.set_asset(image.generation, image_asset())
        await pilot.pause()
        app.renderer.redrawn.clear()

        log.request_nearby_images()

        assert [image.state for image in images] == ["rendered", "rendered"]
        assert app.renderer.redrawn == [image._surface for image in images]

        app.renderer.mode = "halfcell"
        app.renderer.redrawn.clear()
        log.request_nearby_images()
        assert app.renderer.redrawn == []


async def test_loading_image_scrolled_far_away_rejects_late_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = TranscriptImageApp()
    late_asset = image_asset()
    closes: list[str] = []
    monkeypatch.setattr(late_asset, "close", lambda: closes.append("closed"))

    async with app.run_test(size=(100, 30)) as pilot:
        log = pilot.app.query_one(ChatLog)
        for sequence in range(100):
            log.append_or_update(sequence, Text(f"message {sequence}"), scroll=False)
            if sequence % 2 == 0:
                log.append_or_update_images(sequence, (browser_ref(sequence=sequence),))
        await pilot.pause()
        log.request_nearby_images()

        first_near = log._seq_to_widget[0].query_one(ImageEntry)
        assert first_near.state == "loading"
        loading_generation = first_near.generation

        log.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        log.request_nearby_images()

        assert first_near.state == "pending"
        assert first_near.generation > loading_generation

        first_near.set_asset(loading_generation, late_asset)
        assert closes == ["closed"]
        assert first_near.state == "pending"
        assert first_near._surface is None


async def test_image_entry_requests_one_load_and_accepts_current_asset() -> None:
    renderer = FakeRenderer()
    entry = ImageEntry(browser_ref(), renderer)
    app = ImageEntryHarness(entry)

    async with app.run_test(size=(100, 40)) as pilot:
        assert entry.state == "pending"
        assert entry.query_one(".image-caption", Static).render().plain == (
            "Browser screenshot — Enter/Space to collapse"
        )
        entry.request_load()
        await pilot.pause()

        assert entry.state == "loading"
        assert len(app.load_requests) == 1
        assert app.load_requests[0].generation == 1

        entry.set_asset(1, image_asset())
        await pilot.pause()

        assert entry.state == "rendered"
        assert entry.expanded
        assert renderer.created == [("native", CellBox(80, 24))]
        assert not entry.query_one(".image-placeholder", Static).display


async def test_image_entry_unavailable_reference_never_requests_load() -> None:
    renderer = FakeRenderer()
    reference = replace(
        browser_ref(),
        source="unavailable",
        value="ephemeral screenshot is not fetchable",
    )
    entry = ImageEntry(reference, renderer)
    app = ImageEntryHarness(entry)

    async with app.run_test() as pilot:
        await pilot.pause()
        entry.request_load()
        await pilot.pause()

        placeholder = entry.query_one(".image-placeholder", Static)
        assert entry.state == "unavailable"
        assert placeholder.render().plain == "Browser screenshot: ephemeral screenshot is not fetchable"
        assert app.load_requests == []


async def test_image_entry_disabled_renderer_shows_stable_placeholder() -> None:
    renderer = FakeRenderer()
    renderer.mode = "off"
    entry = ImageEntry(browser_ref(), renderer)
    app = ImageEntryHarness(entry)

    async with app.run_test() as pilot:
        await pilot.pause()
        entry.request_load()
        await pilot.pause()

        assert entry.state == "disabled"
        assert entry.query_one(".image-placeholder", Static).render().plain == (
            "Browser screenshot: images disabled"
        )
        assert app.load_requests == []


async def test_image_entry_closes_stale_asset_and_disables_failed_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = FakeRenderer(fail_native=True)
    entry = ImageEntry(browser_ref(), renderer)
    app = ImageEntryHarness(entry)
    stale = image_asset()
    stale_closes: list[str] = []
    monkeypatch.setattr(stale, "close", lambda: stale_closes.append("closed"))

    async with app.run_test(size=(100, 40)) as pilot:
        entry.request_load()
        entry.set_asset(0, stale)
        entry.set_asset(1, image_asset())
        await pilot.pause()

        assert stale_closes == ["closed"]
        assert entry.state == "unavailable"
        assert renderer.created == []


async def test_image_entry_toggle_resize_and_click_do_not_request_again() -> None:
    renderer = FakeRenderer()
    entry = ImageEntry(browser_ref(), renderer)
    app = ImageEntryHarness(entry)

    async with app.run_test(size=(100, 40)) as pilot:
        entry.request_load()
        entry.set_asset(1, image_asset())
        await pilot.pause()

        entry.focus()
        await pilot.press("enter")
        assert not entry.expanded
        entry._surface_host.styles.width = 36
        await pilot.pause()
        assert entry._surface_host.content_region.width == 36
        await pilot.press("space")
        assert entry.expanded
        assert renderer.created[-1] == ("native", CellBox(80, 24))

        stopped: list[bool] = []
        entry.on_click(SimpleNamespace(stop=lambda: stopped.append(True)))
        assert not entry.expanded
        resize_timers: list[FakeTimer] = []
        entry.set_timer = lambda _delay, callback: resize_timers.append(  # type: ignore[method-assign]
            FakeTimer(callback)
        ) or resize_timers[-1]
        entry.on_resize(SimpleNamespace())
        resize_timers[-1].callback()
        await pilot.pause()

        assert stopped == [True]
        assert app.load_requests and len(app.load_requests) == 1
        assert renderer.created[-1] == ("native", CellBox(36, 12))

        app.screen.selections = {entry: SELECT_ALL}
        entry.on_click(SimpleNamespace(stop=lambda: stopped.append(True)))
        assert not entry.expanded
        assert stopped == [True]


async def test_image_entry_error_release_and_unmount_clean_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = FakeRenderer(fail_native=True)
    entry = ImageEntry(browser_ref(), renderer)
    app = ImageEntryHarness(entry)
    failed = image_asset()
    failed_closes: list[str] = []
    monkeypatch.setattr(failed, "close", lambda: failed_closes.append("closed"))

    async with app.run_test() as pilot:
        entry.request_load()
        entry.set_asset(1, failed)
        await pilot.pause()

        assert entry.state == "unavailable"
        assert failed_closes == ["closed"]
        assert entry.query_one(".image-placeholder", Static).render().plain == (
            "Browser screenshot: renderer failed"
        )
        entry.request_load()
        assert len(app.load_requests) == 1
        assert entry.copy_text() == "[image: Browser screenshot]"

    renderer = FakeRenderer()
    entry = ImageEntry(browser_ref(), renderer)
    app = ImageEntryHarness(entry)
    asset = image_asset()
    closes: list[str] = []
    monkeypatch.setattr(asset, "close", lambda: closes.append("closed"))
    async with app.run_test() as pilot:
        entry.request_load()
        entry.set_asset(1, asset)
        await pilot.pause()
        entry.release_surface()
        assert entry.state == "pending"
        assert closes == ["closed"]
        assert len(renderer.cleaned) == 1


def test_image_entry_resize_replaces_timer_and_unmount_cancels_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ImageEntry(browser_ref(), FakeRenderer())
    timers: list[FakeTimer] = []

    def set_timer(_delay: float, callback) -> FakeTimer:
        timer = FakeTimer(callback)
        timers.append(timer)
        return timer

    monkeypatch.setattr(entry, "set_timer", set_timer)
    entry.on_resize(SimpleNamespace())
    entry.on_resize(SimpleNamespace())

    assert len(timers) == 2
    assert timers[0].stopped
    assert not timers[1].stopped

    entry.on_unmount()

    assert timers[1].stopped
    assert entry._resize_timer is None


async def test_image_entry_unmount_cleans_renderer_before_owned_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_order: list[tuple[str, bool]] = []
    base_prunes: list[str] = []

    class OrderedRenderer(FakeRenderer):
        def cleanup_widget(self, widget: Widget | None) -> None:
            if widget is not None:
                cleanup_order.append(("renderer", widget.parent is entry._surface_host))
            super().cleanup_widget(widget)

    renderer = OrderedRenderer()
    entry = ImageEntry(browser_ref(), renderer)
    asset = image_asset()
    monkeypatch.setattr(asset, "close", lambda: cleanup_order.append(("asset", False)))
    original_base_prune = Widget.on_prune

    async def tracked_base_prune(widget: Widget, event) -> None:
        if widget is entry:
            base_prunes.append("base")
        await original_base_prune(widget, event)

    monkeypatch.setattr(Widget, "on_prune", tracked_base_prune)
    app = ImageEntryHarness(entry)

    async with app.run_test() as pilot:
        entry.request_load()
        entry.set_asset(1, asset)
        await pilot.pause()
        await entry.remove()
        await pilot.pause()

    assert cleanup_order == [("renderer", True), ("asset", False)]
    assert base_prunes == ["base"]


async def test_image_entry_unmount_while_loading_rejects_late_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ImageEntry(browser_ref(), FakeRenderer())
    app = ImageEntryHarness(entry)
    late_asset = image_asset()
    closes: list[str] = []
    monkeypatch.setattr(late_asset, "close", lambda: closes.append("closed"))

    async with app.run_test() as pilot:
        entry.request_load()
        await pilot.pause()
        loading_generation = entry.generation

        await entry.remove()
        await pilot.pause()

        assert entry.state == "pending"
        assert entry.generation > loading_generation

        entry.set_asset(loading_generation, late_asset)
        entry.set_unavailable(loading_generation, "late failure")

        assert closes == ["closed"]
        assert entry.state == "pending"
