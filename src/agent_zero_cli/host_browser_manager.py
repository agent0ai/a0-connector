from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path
import sys
from typing import Any, Awaitable, Callable

from agent_zero_cli.config import (
    CLIConfig,
    normalize_host_browser_relaunch_preference,
    save_host_browser_enabled,
    save_host_browser_profile,
    save_host_browser_relaunch_preference,
)
from agent_zero_cli.host_browser_common import (
    RELAUNCH_CONTEXT_ID,
    _SUPPORTED_ACTIONS,
    BrowserCandidate,
    BrowserProfile,
    ProfileLockState,
    base_browser_family,
    detect_browser_candidates,
    discover_profiles,
    discover_remote_debugging_profiles,
    is_a0_managed_family,
    is_remote_debugging_family,
    normalize_host_browser_selection,
    normalize_remote_debugging_endpoint,
    normalize_action,
    parse_content_helper_payload,
    parse_dom_helper_payload,
    playwright_python_install_command,
    playwright_python_install_commands,
    profile_lock_state_for_profile,
    remote_debugging_endpoint_label,
    remote_debugging_restriction_reason,
    _run_install_command,
    _trim_install_output,
)
from agent_zero_cli.host_browser_session import HostBrowserSession, ProfileLockedError


def normalize_host_browser_profile_mode(value: object, *, default: str = "") -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return default
    if normalized in {"agent", "clean", "clean_agent", "a0", "dedicated"}:
        return "agent"
    if normalized in {"existing", "user", "personal", "current"}:
        return "existing"
    return default


def _is_python_pip_install_command(command: list[str]) -> bool:
    return len(command) >= 5 and command[1:4] == ["-m", "pip", "install"]


def _pip_module_missing(output: str) -> bool:
    normalized = str(output or "").lower()
    return "no module named pip" in normalized or "no module named 'pip'" in normalized


def _format_install_attempts(attempts: list[tuple[list[str], int, str]]) -> str:
    return "; ".join(
        f"{' '.join(command)} exited {returncode}: {_trim_install_output(output)}"
        for command, returncode, output in attempts
    )


class HostBrowserManager:
    def __init__(
        self,
        config: CLIConfig,
        *,
        persist_enabled: bool = True,
        candidate_provider: Callable[[], list[BrowserCandidate]] | None = None,
        playwright_available: bool | None = None,
        playwright_starter: Callable[[], Any] | None = None,
        playwright_installer: Callable[[list[str]], Awaitable[tuple[int, str]]] | None = None,
    ) -> None:
        self.config = config
        self.enabled = bool(config.host_browser_enabled)
        self._persist_enabled = persist_enabled
        self._candidate_provider = candidate_provider or detect_browser_candidates
        self._playwright_available = playwright_available
        self._playwright_starter = playwright_starter
        self._playwright_installer = playwright_installer or _run_install_command
        self._sessions: dict[str, HostBrowserSession] = {}
        self._dom_helper_source: str | None = None
        self._dom_helper_sha256 = ""
        self._content_helper_source: str | None = None
        self._content_helper_sha256 = ""
        self.last_error = ""

    @property
    def supported(self) -> bool:
        return self._support_reason() == ""

    def hello_metadata(
        self,
        *,
        profile_mode: object = "",
        browser_selection: object = "",
    ) -> dict[str, Any]:
        profile = self.selected_profile(
            profile_mode=profile_mode,
            browser_selection=browser_selection,
        )
        status = self.status_snapshot(
            profile=profile,
            profile_mode=profile_mode,
            browser_selection=browser_selection,
        )
        can_repair = not self._has_playwright() and not bool(profile and profile.is_remote_debugging)
        return {
            "supported": bool(status["supported"]),
            "can_prepare": bool(status["can_prepare"]),
            "can_repair": can_repair,
            "enabled": bool(status["enabled"]),
            "status": status["status"],
            "browser_family": profile.family if profile else "",
            "profile_label": profile.profile_label if profile else "",
            "profile_path": profile.profile_path_display if profile else "",
            "cdp_endpoint": profile.cdp_endpoint if profile else "",
            "browser_id": profile.browser_id if profile else "",
            "browser_label": profile.browser_label if profile else "",
            "available_browsers": self.available_browser_metadata(),
            "dom_helper_sha256": self._dom_helper_sha256,
            "content_helper_sha256": self._content_helper_sha256,
            "features": [
                "existing_profile",
                "dedicated_profile",
                "user_authorized_remote_debugging",
                "playwright",
                "artifacts",
                "background_tabs",
                "content_helper_rpc",
                "dom_helper_rpc",
                "local_upload_paths",
                *sorted(_SUPPORTED_ACTIONS),
            ],
            "support_reason": status["support_reason"],
        }

    def metadata(self) -> dict[str, Any]:
        return self.hello_metadata()

    def status_snapshot(
        self,
        profile: BrowserProfile | None = None,
        *,
        profile_mode: object = "",
        browser_selection: object = "",
    ) -> dict[str, Any]:
        mode = normalize_host_browser_profile_mode(profile_mode)
        profile = profile if profile is not None else self.selected_profile(
            profile_mode=mode,
            browser_selection=browser_selection,
        )
        support_reason = self._support_reason(profile)
        supported = not support_reason
        can_prepare = self._can_prepare(
            profile,
            profile_mode=mode,
            browser_selection=browser_selection,
        )
        lock = profile_lock_state_for_profile(profile) if profile else ProfileLockState(False)
        if not supported:
            status = "unsupported"
        elif not self.enabled:
            status = "disabled"
        elif profile is not None and self._active_context_for_profile(profile):
            status = "active"
        elif lock.locked:
            status = "relaunch_required"
        else:
            status = "ready"
        return {
            "supported": supported,
            "can_prepare": can_prepare,
            "enabled": self.enabled and supported,
            "status": status,
            "browser_family": profile.family if profile else "",
            "profile_label": profile.profile_label if profile else "",
            "profile_path": profile.profile_path_display if profile else "",
            "cdp_endpoint": profile.cdp_endpoint if profile else "",
            "browser_id": profile.browser_id if profile else "",
            "browser_label": profile.browser_label if profile else "",
            "profile_locked": lock.locked,
            "lock": lock.as_dict(),
            "support_reason": support_reason,
            "last_error": self.last_error,
            "active_contexts": sorted(self._sessions),
        }

    def status_text(self) -> str:
        status = self.status_snapshot()
        if not status["supported"]:
            if status["can_prepare"]:
                return (
                    "Host browser can be prepared automatically when Agent Zero first uses "
                    "the Browser tool."
                )
            return f"Host browser unsupported: {status['support_reason']}"
        if status["status"] == "disabled":
            return "Host browser is disabled. Use /browser host on to advertise it to Agent Zero."
        profile_text = (
            f"remote debugging browser at {status['cdp_endpoint']}"
            if status.get("cdp_endpoint")
            else f"{status['browser_family']} profile {status['profile_label']} ({status['profile_path']})"
        )
        if status["status"] == "relaunch_required":
            return (
                f"Host browser needs relaunch consent for {profile_text}. "
                "Close that Chromium-family browser, then run /browser relaunch."
            )
        return f"Host browser {status['status']}: {profile_text}."

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.config.host_browser_enabled = self.enabled
        if self._persist_enabled:
            save_host_browser_enabled(self.enabled)

    def set_relaunch_preference(self, preference: str) -> str:
        normalized = normalize_host_browser_relaunch_preference(preference)
        self.config.host_browser_relaunch_preference = normalized
        save_host_browser_relaunch_preference(normalized)
        return normalized

    def has_playwright_dependency(self) -> bool:
        return self._has_playwright()

    def playwright_install_command(self) -> list[str]:
        return playwright_python_install_command(sys.executable)

    async def ensure_playwright_dependency(self) -> dict[str, object]:
        commands = playwright_python_install_commands(sys.executable)
        command = commands[0]
        if self._has_playwright():
            return {"installed": False, "command": command, "output": ""}

        attempts: list[tuple[list[str], int, str]] = []
        installed = False
        for candidate in commands:
            returncode, output = await self._playwright_installer(candidate)
            attempts.append((candidate, returncode, output))
            if returncode == 0:
                command = candidate
                installed = True
                break
            if _pip_module_missing(output) and _is_python_pip_install_command(candidate):
                ensurepip_command = [sys.executable, "-m", "ensurepip", "--upgrade"]
                ensurepip_returncode, ensurepip_output = await self._playwright_installer(ensurepip_command)
                attempts.append((ensurepip_command, ensurepip_returncode, ensurepip_output))
                if ensurepip_returncode == 0:
                    returncode, output = await self._playwright_installer(candidate)
                    attempts.append((candidate, returncode, output))
                    if returncode == 0:
                        command = candidate
                        installed = True
                        break

        importlib.invalidate_caches()
        if self._playwright_available is not True:
            self._playwright_available = None
        if not installed:
            raise RuntimeError(
                "Python Playwright install failed with exit code "
                f"{attempts[-1][1]}: {_format_install_attempts(attempts)}"
            )
        if not self._has_playwright():
            raise RuntimeError(
                "Python Playwright install completed, but the package is still not importable "
                f"from {sys.executable}."
            )
        return {"installed": True, "command": command, "output": _trim_install_output(attempts[-1][2])}

    def selected_profile(
        self,
        profile_mode: object = "",
        *,
        browser_selection: object = "",
    ) -> BrowserProfile | None:
        mode = normalize_host_browser_profile_mode(profile_mode)
        profiles = self.available_profiles()
        selection = normalize_host_browser_selection(browser_selection)
        if selection:
            return self._selected_profile_by_selection(profiles, selection, mode=mode)
        family = str(self.config.host_browser_family or "").strip().lower()
        profile_path = str(self.config.host_browser_profile_path or "").strip()
        profile_label = str(self.config.host_browser_profile_label or "").strip()
        if mode == "agent":
            return self._selected_agent_profile(
                profiles,
                family=family,
                profile_path=profile_path,
                profile_label=profile_label,
            )
        if mode == "existing":
            return self._selected_existing_profile(
                profiles,
                family=family,
                profile_path=profile_path,
                profile_label=profile_label,
            )
        return self._selected_automatic_profile(
            profiles,
            family=family,
            profile_path=profile_path,
            profile_label=profile_label,
        )

    def _selected_automatic_profile(
        self,
        profiles: list[BrowserProfile],
        *,
        family: str,
        profile_path: str,
        profile_label: str,
    ) -> BrowserProfile | None:
        remote_profiles = [profile for profile in profiles if profile.is_remote_debugging]
        if remote_profiles:
            matching_remote = self._matching_remote_profile(
                remote_profiles,
                family=family,
                profile_label=profile_label,
                profile_path=profile_path,
            )
            if matching_remote is not None:
                return matching_remote
        if family or profile_path or profile_label:
            for profile in profiles:
                if family and profile.family != family:
                    continue
                if profile_path and profile.profile_path_display != profile_path:
                    continue
                if profile_label and profile.profile_label != profile_label:
                    continue
                return profile
        for profile in profiles:
            if self._profile_support_reason(profile) == "":
                return profile
        return profiles[0] if profiles else None

    def _selected_existing_profile(
        self,
        profiles: list[BrowserProfile],
        *,
        family: str,
        profile_path: str,
        profile_label: str,
    ) -> BrowserProfile | None:
        remote_profiles = [profile for profile in profiles if profile.is_remote_debugging]
        if remote_profiles:
            matching_remote = self._matching_remote_profile(
                remote_profiles,
                family=family,
                profile_label=profile_label,
                profile_path=profile_path,
            )
            if matching_remote is not None:
                return matching_remote
        if family or profile_path or profile_label:
            for profile in profiles:
                if profile.is_remote_debugging or is_a0_managed_family(profile.family):
                    continue
                if family and profile.family != family:
                    continue
                if profile_path and profile.profile_path_display != profile_path:
                    continue
                if profile_label and profile.profile_label != profile_label:
                    continue
                return profile
        for profile in profiles:
            if profile.is_remote_debugging or is_a0_managed_family(profile.family):
                continue
            if self._profile_support_reason(profile) == "":
                return profile
        return next(
            (
                profile
                for profile in profiles
                if not profile.is_remote_debugging and not is_a0_managed_family(profile.family)
            ),
            None,
        )

    def _selected_agent_profile(
        self,
        profiles: list[BrowserProfile],
        *,
        family: str,
        profile_path: str,
        profile_label: str,
    ) -> BrowserProfile | None:
        agent_profiles = [
            profile
            for profile in profiles
            if not profile.is_remote_debugging and is_a0_managed_family(profile.family)
        ]
        if family or profile_path or profile_label:
            for profile in agent_profiles:
                if family and profile.family != family:
                    continue
                if profile_path and profile.profile_path_display != profile_path:
                    continue
                if profile_label and profile.profile_label != profile_label:
                    continue
                return profile
        for profile in agent_profiles:
            if self._profile_support_reason(profile) == "":
                return profile
        return agent_profiles[0] if agent_profiles else None

    def available_profiles(self) -> list[BrowserProfile]:
        candidates = self._candidate_provider()
        profiles: list[BrowserProfile] = discover_remote_debugging_profiles(candidates)
        for candidate in candidates:
            profiles.extend(discover_profiles(candidate))
        return profiles

    def available_browser_metadata(self) -> list[dict[str, Any]]:
        browsers: list[dict[str, Any]] = []
        for profile in self.available_profiles():
            support_reason = self._support_reason(profile)
            lock = profile_lock_state_for_profile(profile)
            if self._active_context_for_profile(profile):
                status = "active"
            elif support_reason:
                status = "unsupported"
            elif lock.locked:
                status = "relaunch_required"
            else:
                status = "ready"
            browsers.append(
                {
                    "id": profile.browser_id,
                    "family": profile.family,
                    "label": profile.browser_label,
                    "cdp_endpoint": profile.cdp_endpoint,
                    "status": status,
                    "enabled": not support_reason,
                }
            )
        return browsers

    def select_profile(self, family: str, profile_label: str = "", profile_path: str = "") -> BrowserProfile:
        family = str(family or "").strip().lower()
        profile_label = str(profile_label or "").strip()
        profile_path = str(profile_path or "").strip()
        for profile in self.available_profiles():
            if family and profile.family != family:
                continue
            if profile_label and profile.profile_label.lower() != profile_label.lower():
                continue
            if profile_path and profile.profile_path_display != profile_path:
                continue
            self._persist_selected_profile(profile)
            return profile
        raise ValueError("No matching Chromium-family profile was found.")

    async def relaunch(self) -> dict[str, Any]:
        return await self.ensure_available()

    async def ensure_available(
        self,
        *,
        profile_mode: object = "",
        browser_selection: object = "",
    ) -> dict[str, Any]:
        mode = normalize_host_browser_profile_mode(profile_mode)
        selection = normalize_host_browser_selection(browser_selection)
        profile = self._auto_start_profile(profile_mode=mode, browser_selection=selection)
        if profile is None and not self._has_playwright():
            await self.ensure_playwright_dependency()
            profile = self._auto_start_profile(profile_mode=mode, browser_selection=selection)
        if profile is None:
            if selection:
                raise RuntimeError(f"No Chromium-family browser matched selection {selection!r}.")
            raise RuntimeError("No Chromium-family browser profile was found.")
        if not profile.is_remote_debugging and not self._has_playwright():
            await self.ensure_playwright_dependency()
        support_reason = self._support_reason(profile)
        if support_reason:
            raise RuntimeError(support_reason)
        lock = profile_lock_state_for_profile(profile)
        active_context = self._active_context_for_profile(profile)
        if active_context:
            self.set_enabled(True)
            return self.status_snapshot(
                profile=profile,
                profile_mode=mode,
                browser_selection=selection,
            )
        if lock.locked:
            raise ProfileLockedError(
                "The selected profile is still locked. Close the normal browser window first, "
                "then run /browser relaunch again.",
                lock_state=lock,
            )
        self.set_enabled(True)
        session = await self._session(RELAUNCH_CONTEXT_ID, profile=profile)
        await session.ensure_started()
        return self.status_snapshot(
            profile=profile,
            profile_mode=mode,
            browser_selection=selection,
        )

    async def close(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

    async def disconnect(self) -> None:
        await self.close()

    async def handle_op(self, payload: dict[str, Any]) -> dict[str, Any]:
        op_id = str(payload.get("op_id", "") or "").strip()
        action = normalize_action(payload.get("action"))
        context_id = str(payload.get("context_id", "") or "").strip() or "default"
        profile_mode = normalize_host_browser_profile_mode(
            payload.get("profile_mode", payload.get("host_browser_profile_mode")),
            default="existing",
        )
        browser_selection = normalize_host_browser_selection(
            payload.get("browser_selection") or payload.get("host_browser_selection")
        )

        if not op_id:
            return {"op_id": "", "ok": False, "error": "op_id is required", "code": "MISSING_OP_ID"}
        if action not in _SUPPORTED_ACTIONS:
            return self._error(op_id, "UNKNOWN_ACTION", f"Unknown host browser action: {action!r}")
        if action == "status":
            snapshot = self.status_snapshot(
                profile_mode=profile_mode,
                browser_selection=browser_selection,
            )
            snapshot["context_id"] = context_id
            return self._success(op_id, snapshot)
        if not self.enabled:
            return self._error(op_id, "HOST_BROWSER_DISABLED", "Host browser is disabled in the A0 CLI.")
        try:
            self._apply_helper_payloads(payload)
        except ValueError as exc:
            self.last_error = str(exc)
            return self._error(op_id, "HOST_BROWSER_CONTENT_HELPER_INVALID", str(exc))
        if action == "ensure":
            try:
                return self._success(
                    op_id,
                    await self.ensure_available(
                        profile_mode=profile_mode,
                        browser_selection=browser_selection,
                    ),
                )
            except ProfileLockedError as exc:
                self.last_error = str(exc)
                profile = self.selected_profile(
                    profile_mode=profile_mode,
                    browser_selection=browser_selection,
                )
                return self._error(
                    op_id,
                    "HOST_BROWSER_RELAUNCH_REQUIRED",
                    str(exc),
                    result={
                        "lock": exc.lock_state.as_dict(),
                        "profile": profile.as_dict() if profile else None,
                    },
                )
            except Exception as exc:
                self.last_error = str(exc)
                return self._error(op_id, "HOST_BROWSER_ERROR", str(exc))

        profile = self.selected_profile(
            profile_mode=profile_mode,
            browser_selection=browser_selection,
        )
        if profile is None and browser_selection:
            return self._error(
                op_id,
                "HOST_BROWSER_NO_PROFILE",
                f"No Chromium-family browser matched selection {browser_selection!r}.",
            )
        support_reason = self._support_reason(profile)
        if support_reason:
            return self._error(op_id, "HOST_BROWSER_UNSUPPORTED", support_reason)
        if profile is None:
            return self._error(op_id, "HOST_BROWSER_NO_PROFILE", "No Chromium-family browser profile was found.")

        lock = profile_lock_state_for_profile(profile)
        active_context = self._active_context_for_profile(profile)
        if lock.locked and context_id not in self._sessions and active_context != RELAUNCH_CONTEXT_ID:
            if active_context:
                return self._error(
                    op_id,
                    "HOST_BROWSER_CONTEXT_ACTIVE",
                    (
                        "Host browser is already controlled by another Agent Zero browser context. "
                        "Close that browser context before starting a new host-browser context."
                    ),
                    result={"active_context": active_context, "profile": profile.as_dict()},
                )
            return self._error(
                op_id,
                "HOST_BROWSER_RELAUNCH_REQUIRED",
                (
                    "The selected Chromium-family profile is already open. "
                    "Run /browser relaunch after closing that browser to give A0 explicit control."
                ),
                result={"lock": lock.as_dict(), "profile": profile.as_dict()},
            )

        try:
            session = await self._session(context_id, profile=profile)
            result = await session.dispatch(payload)
        except ProfileLockedError as exc:
            self.last_error = str(exc)
            return self._error(
                op_id,
                "HOST_BROWSER_RELAUNCH_REQUIRED",
                str(exc),
                result={"lock": exc.lock_state.as_dict(), "profile": profile.as_dict()},
            )
        except Exception as exc:
            self.last_error = str(exc)
            return self._error(op_id, "HOST_BROWSER_ERROR", str(exc))
        return self._success(op_id, result)

    async def _session(self, context_id: str, *, profile: BrowserProfile) -> HostBrowserSession:
        session = self._sessions.get(context_id)
        if session is not None and session.profile != profile:
            await session.close()
            self._sessions.pop(context_id, None)
            session = None
        if session is None and context_id != RELAUNCH_CONTEXT_ID:
            relaunch_session = self._sessions.get(RELAUNCH_CONTEXT_ID)
            if relaunch_session is not None and relaunch_session.profile == profile:
                self._sessions.pop(RELAUNCH_CONTEXT_ID, None)
                relaunch_session.context_id = context_id
                session = relaunch_session
                self._sessions[context_id] = session
        if session is None:
            session = HostBrowserSession(
                context_id=context_id,
                profile=profile,
                playwright_starter=self._playwright_starter,
            )
            self._sessions[context_id] = session
        if self._dom_helper_source is not None:
            await session.set_dom_helper_source(
                self._dom_helper_source,
                self._dom_helper_sha256,
            )
        if self._content_helper_source is not None:
            await session.set_content_helper_source(
                self._content_helper_source,
                self._content_helper_sha256,
            )
        return session

    def _apply_helper_payloads(self, payload: dict[str, Any]) -> None:
        parsed_dom = parse_dom_helper_payload(payload)
        if parsed_dom is not None:
            source, source_hash = parsed_dom
            self._dom_helper_source = source
            self._dom_helper_sha256 = source_hash

        parsed = parse_content_helper_payload(payload)
        if parsed is None:
            return
        source, source_hash = parsed
        self._content_helper_source = source
        self._content_helper_sha256 = source_hash

    def _active_context_for_profile(self, profile: BrowserProfile) -> str:
        for context_id, session in self._sessions.items():
            if session.profile == profile and session.context is not None:
                return context_id
        return ""

    def _active_profile(self) -> BrowserProfile | None:
        for session in self._sessions.values():
            if session.context is not None:
                return session.profile
        return None

    def _auto_start_profile(
        self,
        *,
        profile_mode: object = "",
        browser_selection: object = "",
    ) -> BrowserProfile | None:
        mode = normalize_host_browser_profile_mode(profile_mode)
        selection = normalize_host_browser_selection(browser_selection)
        active_profile = self._active_profile()
        if (
            active_profile is not None
            and self._profile_matches_mode(active_profile, mode)
            and (not selection or self._profile_matches_selection(active_profile, selection))
        ):
            return active_profile
        profile = self.selected_profile(profile_mode=mode, browser_selection=selection)
        if selection:
            return profile
        if profile is not None and self._profile_support_reason(profile) == "":
            if mode:
                self._persist_selected_profile(profile)
            return profile
        fallback = self._first_supported_profile(profile_mode=mode)
        if fallback is not None:
            self._persist_selected_profile(fallback)
            return fallback
        return profile

    def _first_supported_profile(
        self,
        *,
        profile_mode: object = "",
        browser_selection: object = "",
    ) -> BrowserProfile | None:
        mode = normalize_host_browser_profile_mode(profile_mode)
        selection = normalize_host_browser_selection(browser_selection)
        for profile in self.available_profiles():
            if not self._profile_matches_mode(profile, mode):
                continue
            if selection and not self._profile_matches_selection(profile, selection):
                continue
            if self._profile_support_reason(profile) == "":
                return profile
        return None

    def _selected_profile_by_selection(
        self,
        profiles: list[BrowserProfile],
        selection: str,
        *,
        mode: str,
    ) -> BrowserProfile | None:
        for profile in profiles:
            if self._profile_matches_browser_id(profile, selection):
                return profile
        for profile in profiles:
            if self._profile_matches_mode(profile, mode) and self._profile_matches_family(profile, selection):
                return profile
        endpoint = normalize_remote_debugging_endpoint(selection)
        if endpoint:
            return BrowserProfile(
                family="chrome-cdp",
                family_label="Chromium-family browser (remote debugging)",
                executable_path="",
                user_data_dir=Path(),
                profile_directory=remote_debugging_endpoint_label(endpoint),
                display_name="Remote debugging allowed",
                cdp_endpoint=endpoint,
            )
        return None

    @staticmethod
    def _profile_matches_mode(profile: BrowserProfile, mode: str) -> bool:
        if not mode:
            return True
        if mode == "agent":
            return not profile.is_remote_debugging and is_a0_managed_family(profile.family)
        return profile.is_remote_debugging or not is_a0_managed_family(profile.family)

    @staticmethod
    def _profile_matches_selection(profile: BrowserProfile, selection: str) -> bool:
        return (
            HostBrowserManager._profile_matches_browser_id(profile, selection)
            or HostBrowserManager._profile_matches_family(profile, selection)
        )

    @staticmethod
    def _profile_matches_browser_id(profile: BrowserProfile, selection: str) -> bool:
        return selection in {
            normalize_host_browser_selection(profile.browser_id),
            normalize_host_browser_selection(profile.cdp_endpoint),
        }

    @staticmethod
    def _profile_matches_family(profile: BrowserProfile, selection: str) -> bool:
        return selection in {
            normalize_host_browser_selection(profile.family),
            normalize_host_browser_selection(base_browser_family(profile.family)),
        }

    def _matching_remote_profile(
        self,
        remote_profiles: list[BrowserProfile],
        *,
        family: str,
        profile_label: str,
        profile_path: str,
    ) -> BrowserProfile | None:
        if not family or is_a0_managed_family(family) or is_remote_debugging_family(family):
            if profile_label:
                for profile in remote_profiles:
                    if profile.profile_label.lower() == profile_label.lower():
                        return profile
            return remote_profiles[0]
        base_family = base_browser_family(family)
        for profile in remote_profiles:
            if base_browser_family(profile.family) != base_family:
                continue
            profile_path_matches = profile_path in {profile.profile_path_display, str(profile.user_data_dir)}
            if profile_path and not profile_path_matches:
                continue
            if profile_label and profile_label.lower() != profile.profile_label.lower() and not profile_path_matches:
                continue
            return profile
        return None

    def _can_prepare(
        self,
        profile: BrowserProfile | None = None,
        *,
        profile_mode: object = "",
        browser_selection: object = "",
    ) -> bool:
        mode = normalize_host_browser_profile_mode(profile_mode)
        if profile is not None and self._profile_support_reason(profile) == "":
            return True
        return self._first_supported_profile(
            profile_mode=mode,
            browser_selection=browser_selection,
        ) is not None

    def _persist_selected_profile(self, profile: BrowserProfile) -> None:
        self.config.host_browser_family = profile.family
        self.config.host_browser_profile_path = profile.profile_path_display
        self.config.host_browser_profile_label = profile.profile_label
        save_host_browser_profile(
            family=profile.family,
            profile_path=profile.profile_path_display,
            profile_label=profile.profile_label,
        )

    def _support_reason(self, profile: BrowserProfile | None = None) -> str:
        profile = profile if profile is not None else self.selected_profile()
        if profile is not None and profile.is_remote_debugging:
            return self._profile_support_reason(profile)
        if not self._has_playwright():
            return (
                f"Browser support is incomplete in the A0 CLI host environment ({sys.executable}). "
                "Use the current Browser setup action, or run /browser repair in the A0 CLI. "
                "The Patchright runtime inside the Agent Zero Docker container is used by the "
                "container browser backend and cannot control host Chromium-family profiles."
            )
        return self._profile_support_reason(profile)

    def _profile_support_reason(self, profile: BrowserProfile | None) -> str:
        if profile is None:
            return "No installed Chromium-family browser profile was detected."
        if profile.is_remote_debugging:
            return ""
        if not profile.executable_path or not Path(profile.executable_path).exists():
            return "Selected Chromium-family browser executable was not found."
        restriction_reason = remote_debugging_restriction_reason(profile)
        if restriction_reason:
            return restriction_reason
        return ""

    def _has_playwright(self) -> bool:
        if self._playwright_available is not None:
            return bool(self._playwright_available)
        try:
            return importlib.util.find_spec("playwright.async_api") is not None
        except (ModuleNotFoundError, ValueError):
            return False

    def _success(self, op_id: str, result: Any) -> dict[str, Any]:
        return {"op_id": op_id, "ok": True, "result": result}

    def _error(
        self,
        op_id: str,
        code: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "op_id": op_id,
            "ok": False,
            "code": code,
            "error": message,
        }
        if result is not None:
            payload["result"] = result
        return payload
