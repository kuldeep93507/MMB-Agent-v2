"""
Cross-platform 'Immortal' YouTube orchestrator.

Detects profile OS from IdentityManager, assigns DesktopInteraction or
MobileInteraction via the Strategy pattern, and coordinates session lifecycle.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Optional

import nodriver as uc
from dotenv import load_dotenv
from nodriver.core.browser import Browser
from nodriver.core.tab import Tab

from behavior.youtube.base import YouTubeInteraction
from behavior.youtube.desktop import DesktopInteraction
from behavior.youtube.human_engine import address_bar_navigate, wait_for_any_element
from behavior.youtube.mobile import MobileInteraction
from behavior.youtube.resolver import load_selector_failures, persist_selector_failure
from behavior.youtube.types import (
    DEFAULT_ENV_PATH,
    DEFAULT_LOG_PATH,
    AdStrategy,
    ElementNotFoundError,
    EntryPath,
    EngagementIntensity,
    InteractionContext,
    NavigationRoute,
    PlatformKind,
    ProfileConfig,
    TRAFFIC_MIX,
    VideoTarget,
    WatchSessionResult,
    YouTubeManagerError,
)
from providers.BrowserManager import BrowserManager, BrowserProviderError
from services.IdentityManager import IdentityManager

__all__ = [
    "YouTubeManager",
    "YouTubeManagerError",
    "ElementNotFoundError",
    "VideoTarget",
    "WatchSessionResult",
    "PlatformKind",
    "NavigationRoute",
    "ProfileConfig",
    "AdStrategy",
    "EntryPath",
    "EngagementIntensity",
    "YouTubeInteraction",
    "DesktopInteraction",
    "MobileInteraction",
]


def resolve_platform(identity: dict[str, Any]) -> PlatformKind:
    """
    Infer desktop vs mobile from IdentityManager profile payload.

    Checks ``mobile_first``, ``os_type``, ``device_platform``, and
    ``operator_system_id`` (MoreLogin: 3=Android, 4=iOS).
    """
    if identity.get("mobile_first"):
        return PlatformKind.MOBILE

    os_type = str(identity.get("os_type") or "").lower()
    if os_type in {"android", "ios"}:
        return PlatformKind.MOBILE

    device = str(identity.get("device_platform") or "").lower()
    if device in {"android", "ios"}:
        return PlatformKind.MOBILE

    operator_id = identity.get("operator_system_id")
    if operator_id in (3, 4):
        return PlatformKind.MOBILE

    return PlatformKind.DESKTOP


def build_interaction(ctx: InteractionContext) -> YouTubeInteraction:
    """Factory: return the platform strategy for the current profile."""
    if ctx.platform == PlatformKind.MOBILE:
        return MobileInteraction(ctx)
    return DesktopInteraction(ctx)


class YouTubeManager:
    """
    Immortal cross-platform YouTube session orchestrator.

    Example::

        manager = YouTubeManager(profile_id="abc-123", country_code="US")
        tab = await manager.open_session()
        target = VideoTarget(
            video_id="dQw4w9WgXcQ",
            search_keywords="never gonna give you up",
            title_hint="Never Gonna Give You Up",
        )
        route = await manager.navigate_to_video(tab, target)
        result = await manager.watch_video(tab, perform_engagement=True)
        await manager.close_session()
    """

    def __init__(
        self,
        profile_id: str,
        *,
        country_code: Optional[str] = None,
        force_mobile: Optional[bool] = None,
        env_path: Optional[Path | str] = None,
        log_path: Optional[Path | str] = None,
        browser_manager: Optional[BrowserManager] = None,
        identity_manager: Optional[IdentityManager] = None,
        behavior_profile: Optional[str] = None,
        referrer_search: bool = False,
        profile_platform: Optional[str] = None,
        profile_config: Optional[ProfileConfig] = None,
    ) -> None:
        load_dotenv(env_path or DEFAULT_ENV_PATH)

        self._profile_id = profile_id.strip()
        if not self._profile_id:
            raise ValueError("profile_id must be a non-empty string.")

        self._behavior_profile = (behavior_profile or "default").strip().lower()
        self._referrer_search = referrer_search

        self._country_code = (
            country_code or os.getenv("DEFAULT_COUNTRY_CODE", "US")
        ).upper()
        self._browser_manager = browser_manager or BrowserManager(
            env_path=str(env_path or DEFAULT_ENV_PATH)
        )
        self._identity_manager = identity_manager or IdentityManager(env_path=env_path)
        self._identity = self._identity_manager.generate_identity(
            country_code=self._country_code,
            profile_id=self._profile_id,
        )
        self._identity["profile_id"] = self._profile_id
        self._apply_profile_platform(profile_platform, force_mobile)

        self._platform = resolve_platform(self._identity)
        self._rng = self._build_profile_rng()
        self._watch_mean = self._derive_watch_mean()
        behavior = self._behavior_tuning()
        self._logger = self._configure_logger(log_path or DEFAULT_LOG_PATH)
        self._selector_failures = load_selector_failures()

        self._ctx = InteractionContext(
            identity=self._identity,
            rng=self._rng,
            logger=self._logger,
            watch_mean=self._watch_mean,
            platform=self._platform,
            behavior_profile=self._behavior_profile,
            pause_probability=behavior["pause_probability"],
            watch_chunk_min=behavior["watch_chunk_min"],
            watch_chunk_max=behavior["watch_chunk_max"],
        )
        setattr(self._ctx, "record_selector_failure", self._persist_selector_failure)
        self._strategy = build_interaction(self._ctx)

        # ── Profile config (user-configurable behaviour) ──────────────────────
        self._config = profile_config or ProfileConfig()

        self._browser: Optional[Browser] = None
        self._tab: Optional[Tab] = None
        self._current_video_id: Optional[str] = None
        self._current_route: Optional[str] = None
        self._guardian = None          # PlaybackGuardian instance (set in open_session)
        self._watched_video_ids: set[str] = set()  # session-level watch history

        self._log(
            f"Initialized | platform={self._platform.value} "
            f"os_type={self._identity.get('os_type', 'desktop')} "
            f"mobile_first={self._identity.get('mobile_first', False)} "
            f"behavior={self._behavior_profile}"
        )

    @property
    def profile_config(self) -> ProfileConfig:
        return self._config

    @property
    def identity(self) -> dict[str, Any]:
        return dict(self._identity)

    @property
    def platform(self) -> PlatformKind:
        return self._platform

    @property
    def strategy(self) -> YouTubeInteraction:
        return self._strategy

    @property
    def profile_id(self) -> str:
        return self._profile_id

    def _apply_profile_platform(
        self,
        profile_platform: Optional[str],
        force_mobile: Optional[bool],
    ) -> None:
        """Map jobs.json profile platform tag to identity OS — no global mobile bleed."""
        if profile_platform:
            platform = profile_platform.strip().lower()
            if platform == "android":
                self._identity = self._identity_manager.apply_mobile_fingerprint(
                    self._identity,
                    provider=self._browser_manager.provider_name,
                )
            elif platform in {"macos", "mac"}:
                self._identity.pop("mobile_first", None)
                self._identity["os_type"] = "macos"
                self._identity.pop("device_platform", None)
            elif platform == "windows":
                self._identity.pop("mobile_first", None)
                self._identity["os_type"] = "windows"
                self._identity.pop("device_platform", None)
        elif force_mobile is True:
            self._identity = self._identity_manager.apply_mobile_fingerprint(
                self._identity,
                provider=self._browser_manager.provider_name,
            )
        self._identity["profile_id"] = self._profile_id

    def _rebuild_strategy(self) -> None:
        behavior = self._behavior_tuning()
        self._ctx = InteractionContext(
            identity=self._identity,
            rng=self._rng,
            logger=self._logger,
            watch_mean=self._watch_mean,
            platform=self._platform,
            behavior_profile=self._behavior_profile,
            pause_probability=behavior["pause_probability"],
            watch_chunk_min=behavior["watch_chunk_min"],
            watch_chunk_max=behavior["watch_chunk_max"],
        )
        setattr(self._ctx, "record_selector_failure", self._persist_selector_failure)
        self._strategy = build_interaction(self._ctx)

    def _build_profile_rng(self) -> random.Random:
        digest = hashlib.sha256(
            f"youtube-universal:{self._profile_id}:{self._country_code}".encode()
        ).hexdigest()
        return random.Random(int(digest[:16], 16))

    def _derive_watch_mean(self) -> float:
        """Profile personality: Gaussian mean between 40% and 100%."""
        if self._behavior_profile == "serious_learner":
            return self._rng.uniform(0.78, 0.96)

        bucket = self._rng.choice(("skimmer", "casual", "engaged", "completionist"))
        ranges = {
            "skimmer": (0.40, 0.52),
            "casual": (0.52, 0.72),
            "engaged": (0.68, 0.88),
            "completionist": (0.85, 1.00),
        }
        lo, hi = ranges[bucket]
        return self._rng.uniform(lo, hi)

    def _behavior_tuning(self) -> dict[str, float]:
        if self._behavior_profile == "serious_learner":
            return {
                "pause_probability": 0.24,
                "watch_chunk_min": 6.0,
                "watch_chunk_max": 22.0,
            }
        return {
            "pause_probability": 0.10,
            "watch_chunk_min": 4.0,
            "watch_chunk_max": 18.0,
        }

    @staticmethod
    def _configure_logger(log_path: Path | str) -> logging.Logger:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("mmb.youtube_universal")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if not logger.handlers:
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            logger.addHandler(handler)

        return logger

    def _persist_selector_failure(self, key: str, selector: str) -> None:
        persist_selector_failure(self._selector_failures, key, selector)

    def _log(self, message: str, *, level: int = logging.INFO) -> None:
        self._logger.log(level, message)

    @property
    def _youtube_home_url(self) -> str:
        return self._ctx.youtube_home

    def _search_bar_selectors(self) -> tuple[str, ...]:
        if self._platform == PlatformKind.MOBILE:
            return (
                'input[type="search"]',
                'input[name="search_query"]',
                'ytm-searchbox input',
                'input[aria-label*="Search" i]',
            )
        return (
            'input[role="combobox"]',
            'input[aria-label="Search"]',
            '[role="search"] input',
            'input#search',
            'input[name="search_query"]',
            'ytd-searchbox input#search',
            'input[aria-label*="Search"]',
            'input[placeholder*="Search"]',
        )

    async def _read_page_state(self, tab: Tab) -> tuple[str, str]:
        # Primary: tab.url
        url = ""
        try:
            url = str(tab.url or "")
        except Exception:
            pass

        # Fallback for mobile: tab.url is often '' — use JS window.location
        if not url:
            try:
                result = await tab.evaluate("(() => window.location.href)()", return_by_value=True)
                js_url = result if isinstance(result, str) else getattr(result, "value", "")
                if js_url and js_url != "about:blank":
                    url = js_url
            except Exception:
                pass

        # Title derived from URL
        if "youtube.com" in url:
            title = "YouTube"
        elif url:
            title = url
        else:
            title = ""
        return url, title

    @staticmethod
    def _is_youtube_loaded(url: str, title: str) -> bool:
        lowered_url = (url or "").lower()
        lowered_title = (title or "").lower()
        return "youtube.com" in lowered_url or "youtube" in lowered_title

    async def _wait_until_youtube_loaded(
        self,
        tab: Tab,
        *,
        start_url: str,
        timeout: float = 10.0,
    ) -> bool:
        """Poll every 500ms until URL or title indicates YouTube loaded."""
        deadline = time.monotonic() + timeout
        transition_logged = False

        while time.monotonic() < deadline:
            url, title = await self._read_page_state(tab)
            if self._is_youtube_loaded(url, title):
                if url != start_url and not transition_logged:
                    self._log(
                        f"Navigation transition | {start_url!r} -> {url!r} | title={title!r}"
                    )
                    transition_logged = True
                elif not transition_logged:
                    self._log(f"YouTube detected | url={url!r} | title={title!r}")
                    transition_logged = True
                return True
            await asyncio.sleep(0.5)

        return False

    async def _has_search_bar(self, tab: Tab) -> bool:
        from behavior.youtube.human_engine import _js_find_selector
        matched = await _js_find_selector(tab, tuple(self._search_bar_selectors()))
        if matched:
            return True
        # Fallback: try tab.select on the most reliable selector only
        try:
            element = await tab.select('input#search')
            return element is not None
        except Exception:
            return False

    async def _bulletproof_navigate_youtube(
        self,
        tab: Tab,
        *,
        context: str = "navigate",
    ) -> None:
        """
        Hybrid navigation: address-bar typing first, wait-until-loaded, tab.get fallback.

        1. Type host in address bar + Enter
        2. Poll 500ms for youtube.com in URL or YouTube in title (10s max)
        3. Fallback: tab.get(home_url) if still stuck on chrome://newtab/ etc.
        """
        home = self._youtube_home_url

        # Wait for tab to have a URL — mobile profiles need extra time to boot
        wait_rounds = 20 if self._platform == PlatformKind.MOBILE else 10
        for _ in range(wait_rounds):
            start_url, start_title = await self._read_page_state(tab)
            if start_url:
                break
            await asyncio.sleep(0.5)
        else:
            start_url, start_title = "", ""

        self._log(
            f"Bulletproof nav [{context}] | start_url={start_url!r} target={home}"
        )

        if self._is_youtube_loaded(start_url, start_title) and await self._has_search_bar(tab):
            self._log(f"Bulletproof nav [{context}] | already on YouTube with search bar")
            return

        # ── Android/Mobile: Stability-First navigation ────────────────────────
        if self._platform == PlatformKind.MOBILE:
            self._log(f"Bulletproof nav [{context}] | android — checking current state")

            # Step 1: JS URL check — already on YouTube?
            try:
                r = await tab.evaluate("(() => window.location.href)()", return_by_value=True)
                js_url = r if isinstance(r, str) else getattr(r, "value", "")
                if js_url and "youtube" in js_url.lower():
                    self._log(f"Bulletproof nav [{context}] | android already on YouTube | url={js_url[:60]!r}")
                    return
            except Exception:
                js_url = ""

            # Step 2: DOM check — any YouTube element present?
            try:
                r = await tab.evaluate(
                    "(() => !!(document.querySelector('ytm-app')||"
                    "document.querySelector('ytm-searchbox')||"
                    "document.querySelector('ytm-pivot-bar-renderer')))()",
                    return_by_value=True,
                )
                v = r if isinstance(r, bool) else getattr(r, "value", False)
                if v:
                    self._log(f"Bulletproof nav [{context}] | android DOM already present — skip nav")
                    return
            except Exception:
                pass

            # Step 3: Single tab.get() — no loops, no retries
            self._log(f"Bulletproof nav [{context}] | android tab.get({home!r})")
            try:
                await asyncio.wait_for(tab.get(home), timeout=30.0)
                await asyncio.sleep(3.0)
            except asyncio.TimeoutError:
                self._log(f"Bulletproof nav [{context}] | android tab.get timeout — checking DOM anyway", level=logging.WARNING)
            except Exception as exc:
                self._log(f"Bulletproof nav [{context}] | android tab.get error: {type(exc).__name__} — checking DOM anyway", level=logging.WARNING)

            # Step 4: Final JS + DOM check — accept either
            for _ in range(6):  # 12s total
                await asyncio.sleep(2.0)
                try:
                    r = await tab.evaluate("(() => window.location.href)()", return_by_value=True)
                    v = r if isinstance(r, str) else getattr(r, "value", "")
                    if v and "youtube" in v.lower():
                        self._log(f"Bulletproof nav [{context}] | android JS URL ✓ | {v[:60]!r}")
                        return
                except Exception:
                    pass
                try:
                    r = await tab.evaluate(
                        "(() => !!(document.querySelector('ytm-app')||"
                        "document.querySelector('ytm-searchbox')||"
                        "document.querySelector('ytm-pivot-bar-renderer')))()",
                        return_by_value=True,
                    )
                    v = r if isinstance(r, bool) else getattr(r, "value", False)
                    if v:
                        self._log(f"Bulletproof nav [{context}] | android DOM ✓")
                        return
                except Exception:
                    pass

            self._log(
                f"Bulletproof nav [{context}] | android could not confirm YouTube — proceeding anyway",
                level=logging.WARNING,
            )
            return  # soft-fail: let navigate_to_video handle it

        host = "youtube.com"
        self._log(f"Bulletproof nav [{context}] | address-bar typing {host!r}")
        await address_bar_navigate(tab, host, self._rng)

        if await self._wait_until_youtube_loaded(tab, start_url=start_url, timeout=10.0):
            loaded_url, loaded_title = await self._read_page_state(tab)
            self._log(
                f"Bulletproof nav [{context}] | address-bar success | "
                f"url={loaded_url!r} title={loaded_title!r}"
            )
            return

        stuck_url, _ = await self._read_page_state(tab)
        self._log(
            f"Bulletproof nav [{context}] | address-bar timeout (10s) — "
            f"fallback tab.get({home!r}) | stuck_url={stuck_url!r}",
            level=logging.WARNING,
        )
        try:
            await asyncio.wait_for(tab.get(home), timeout=25.0)
        except asyncio.TimeoutError:
            self._log(f"Bulletproof nav [{context}] | tab.get timeout (25s)", level=logging.WARNING)
        except Exception as exc:
            self._log(f"Bulletproof nav [{context}] | tab.get error: {exc}", level=logging.WARNING)
        await asyncio.sleep(2.0)

        if await self._wait_until_youtube_loaded(tab, start_url=stuck_url, timeout=15.0):
            final_url, final_title = await self._read_page_state(tab)
            self._log(
                f"Navigation transition | fallback tab.get -> {final_url!r} | title={final_title!r}"
            )
            return

        final_url, final_title = await self._read_page_state(tab)
        raise YouTubeManagerError(
            f"Bulletproof navigation failed [{context}] | "
            f"url={final_url!r} title={final_title!r}"
        )

    async def _require_youtube_homepage(self, tab: Tab, *, context: str = "session") -> None:
        """
        Gate: do not proceed until YouTube homepage is loaded and search bar exists.
        Mobile: DOM-based check only (URL unreliable on Android nodriver).
        """
        url, title = await self._read_page_state(tab)
        has_bar = await self._has_search_bar(tab)
        if not self._is_youtube_loaded(url, title) or not has_bar:
            await self._bulletproof_navigate_youtube(tab, context=f"{context}_navigate")

        await self._strategy.dismiss_consent(tab)

        # Mobile/Android: light DOM check only — no search bar required
        if self._platform == PlatformKind.MOBILE:
            mobile_any = (
                'ytm-app', 'ytm-pivot-bar-renderer', 'ytm-topbar-renderer',
                'ytm-searchbox', 'input[name="search_query"]',
            )
            try:
                _, matched = await wait_for_any_element(
                    tab, mobile_any, timeout=8.0,
                    label="mobile_yt_dom", log=lambda msg: self._log(msg),
                )
                self._log(f"YouTube mobile DOM verified [{context}] | matched={matched!r}")
            except ElementNotFoundError:
                self._log(
                    f"YouTube mobile DOM soft-verify [{context}] — continuing anyway",
                    level=logging.WARNING,
                )
            return

        try:
            _, matched = await wait_for_any_element(
                tab,
                self._search_bar_selectors(),
                timeout=20.0,
                label="youtube_home_search_bar",
                log=lambda msg: self._log(msg),
            )
        except ElementNotFoundError as exc:
            fail_url, fail_title = await self._read_page_state(tab)
            raise YouTubeManagerError(
                f"YouTube homepage not verified — search bar missing | "
                f"url={fail_url!r} title={fail_title!r} | {exc}"
            ) from exc

        verified_url, verified_title = await self._read_page_state(tab)
        self._log(
            f"YouTube homepage verified [{context}] | search_bar={matched!r} "
            f"url={verified_url!r} title={verified_title!r}"
        )

    async def open_session(self) -> Tab:
        """
        Launch profile, apply viewport, bulletproof-navigate to YouTube, verify homepage.

        Order (stability-first):
        1. Connect CDP (no navigation)
        2. Apply adaptive viewport BEFORE any navigation
        3. Hybrid address-bar -> wait-until-loaded -> tab.get fallback
        4. Mandatory search-bar verification before session is ready
        """
        is_android = (self._platform == PlatformKind.MOBILE)
        self._log(
            f"Opening session | profile={self._profile_id} platform={self._platform.value} android={is_android}"
        )
        try:
            self._browser = await self._browser_manager.get_browser_instance(
                self._profile_id,
                identity=self._identity,
                apply_viewport=False,
            )
        except BrowserProviderError as exc:
            raise YouTubeManagerError(f"Failed to start browser profile: {exc}") from exc

        if is_android:
            # Android: skip viewport setup here — browser still settling.
            # Get main tab directly, no address-bar, no viewport.
            self._tab = self._browser.main_tab
            if self._tab is None:
                raise YouTubeManagerError("Android browser connected but no main tab available.")
            self._log("Android session | skipping viewport + address-bar — using direct tab.get()")
        else:
            self._tab = await self._browser_manager.prepare_session_tab(self._browser, self._identity)
            await self._strategy.human_delay(0.5, 1.5)

        await self._bulletproof_navigate_youtube(self._tab, context="open_session")
        await self._require_youtube_homepage(self._tab, context="open_session")

        final_url, _ = await self._read_page_state(self._tab)
        self._log(f"Session ready | url={final_url!r} home={self._youtube_home_url}")

        # ── Start Playback Guardian (background loop) ─────────────────────────
        from behavior.youtube.guardian import PlaybackGuardian
        self._guardian = PlaybackGuardian(
            self._tab,
            log=self._log,
            check_interval=10.0,
            autoplay_lock=True,
        )
        await self._guardian.start()

        return self._tab

    async def close_session(self) -> None:
        profile_id = self._profile_id

        # Stop guardian first
        if self._guardian is not None:
            try:
                await self._guardian.stop()
            except Exception:
                pass
            self._guardian = None

        if self._browser is not None:
            try:
                self._browser.stop()
            except Exception as exc:
                self._log(f"Browser stop warning: {exc}", level=logging.WARNING)
            self._browser = None
            self._tab = None

        self._browser_manager.stop_profile(profile_id)
        self._log(f"Session closed | profile={profile_id}")

    async def _configure_mobile_tab(self, tab: Tab) -> None:
        """Deprecated — viewport is enforced in BrowserManager before navigation."""
        await self._browser_manager.apply_viewport_to_tab(tab, self._identity)

    def _choose_route(self, force_route: Optional[str] = None) -> NavigationRoute:
        if force_route:
            normalized = force_route.strip().lower()
            try:
                return NavigationRoute(normalized)
            except ValueError as exc:
                raise YouTubeManagerError(
                    f"Invalid force_route={force_route!r}. "
                    "Use search, homepage, suggested, or direct."
                ) from exc

        routes, weights = zip(*TRAFFIC_MIX)
        chosen = self._rng.choices(list(routes), weights=list(weights), k=1)[0]
        return NavigationRoute(chosen)

    async def navigate_to_video(
        self,
        tab: Optional[Tab] = None,
        target: VideoTarget | None = None,
        *,
        force_route: Optional[str] = None,
        use_entropy: bool = True,
    ) -> str:
        """
        Reach a video organically via Behavioral Entropy Engine.

        When use_entropy=True (default), the Behavioral Entropy Engine picks
        a random entry path (A/B/C) based on the profile's unique personality,
        applies interaction jitter, and optionally inserts a 'human mistake'
        (wrong video first). This ensures NO TWO PROFILES follow the same path.

        When use_entropy=False, falls back to the legacy route-based system.
        """
        page = tab or self._tab
        if page is None:
            raise YouTubeManagerError("No active tab. Call open_session() first.")

        if target is None:
            target = VideoTarget()
        target.validate()

        # ── Direct URL — always bypass entropy ──────────────────────────────
        if target.direct_url:
            direct = target.direct_url.strip()
            self._log(f"Direct navigation | url={direct}")
            start_url, _ = await self._read_page_state(page)

            # Mobile: address_bar_navigate (keyboard) unreliable — use tab.get() directly
            if self._platform == PlatformKind.MOBILE:
                self._log("Direct nav mobile — using tab.get()")
                try:
                    await asyncio.wait_for(page.get(direct), timeout=30.0)
                except Exception as exc:
                    self._log(f"Direct nav mobile tab.get error: {exc}", level=logging.WARNING)
                await asyncio.sleep(3.0)
            else:
                # Try address_bar_navigate first
                await address_bar_navigate(
                    page,
                    direct.replace("https://", "").replace("http://", ""),
                    self._rng,
                )
                await asyncio.sleep(2.0)
                # Verify we're on the VIDEO page (not just youtube.com homepage)
                nav_url, _ = await self._read_page_state(page)
                video_id_in_url = (target.video_id or "") in (nav_url or "")
                on_watch = "/watch" in (nav_url or "")
                if not (video_id_in_url or on_watch):
                    self._log(
                        f"Direct nav: address_bar didn't reach video | "
                        f"url={nav_url!r} — fallback tab.get()",
                        level=logging.WARNING,
                    )
                    try:
                        await asyncio.wait_for(page.get(direct), timeout=25.0)
                    except Exception as exc:
                        self._log(f"Direct nav tab.get fallback error: {exc}", level=logging.WARNING)
                    await asyncio.sleep(2.0)
                else:
                    self._log(f"Direct nav: address_bar success | url={nav_url!r}")
            await self._strategy.wait_player_ready(page)
            self._current_video_id = YouTubeInteraction.extract_video_id(direct)
            self._current_route = NavigationRoute.DIRECT.value
            return self._current_route

        # ── Optional referrer warmup ─────────────────────────────────────────
        keywords = target.search_keywords or target.title_hint or target.video_id or ""
        if self._referrer_search and keywords and self._rng.random() < 0.40:
            await self._referrer_warmup(page, keywords)

        # ── Stability gate ───────────────────────────────────────────────────
        await self._require_youtube_homepage(page, context="pre_search")

        # ── CONFIG-DRIVEN ENTRY PATH ─────────────────────────────────────────
        entry = self._config.entry_path

        if entry == EntryPath.NOTIFICATION.value:
            from behavior.youtube.notification_path import NotificationPath
            self._log(f"[Config] Entry path: NOTIFICATION")
            npath = NotificationPath(page, log=self._log, rng=self._rng)
            ok = await npath.execute(target)
            if ok:
                self._current_video_id = target.video_id or await self._get_current_video_id(page)
                self._current_route = "notification"
                return self._current_route
            self._log("[Config] Notification path failed → fallback to search")
            # fall through to search

        if entry == EntryPath.HOMEPAGE.value:
            self._log("[Config] Entry path: HOMEPAGE")
            # Entropy path C (homepage browse)
            if use_entropy and self._platform == PlatformKind.DESKTOP:
                return await self._navigate_with_entropy(page, target)
            return await self._navigate_legacy(page, target, "homepage")

        # ── BEHAVIORAL ENTROPY ENGINE (search path) ──────────────────────────
        if use_entropy and self._platform == PlatformKind.DESKTOP:
            return await self._navigate_with_entropy(page, target)

        # ── LEGACY ROUTE FALLBACK ────────────────────────────────────────────
        return await self._navigate_legacy(page, target, force_route)

    # ── Ad Handling ───────────────────────────────────────────────────────────

    async def handle_ads(self, tab: Optional[Any] = None) -> dict[str, Any]:
        """
        Handle ads according to profile_config.ad_strategy:
          - skip_all:     skip every ad immediately
          - click_end_ad: watch end-card ad 3-5s then click it (original tab stays)
          - watch_all:    do nothing, let ads play
        Returns result dict with ad_strategy, skipped, clicked.
        """
        page = tab or self._tab
        strategy = self._config.ad_strategy
        result = {"ad_strategy": strategy, "skipped": False, "clicked": False}

        if strategy == AdStrategy.WATCH_ALL.value:
            self._log("[Ads] Strategy=watch_all — letting ads play")
            return result

        if strategy == AdStrategy.SKIP_ALL.value:
            skipped = await self._strategy.skip_ad_if_present(page)
            result["skipped"] = skipped
            if skipped:
                self._log("[Ads] Ad skipped (skip_all)")
            return result

        if strategy == AdStrategy.CLICK_END_AD.value:
            return await self._handle_click_end_ad(page, result)

        return result

    async def _handle_click_end_ad(self, page: Any, result: dict) -> dict:
        """
        click_end_ad strategy:
        Wait for end-card/overlay ad → watch 3-5s → click → keep original tab playing.
        """
        import time as _time

        # Check if ad is playing
        ad_playing = await self._strategy.is_ad_playing(page)
        if not ad_playing:
            self._log("[Ads] click_end_ad: no ad found")
            return result

        self._log("[Ads] click_end_ad: ad detected — watching 3-5s before click")
        await asyncio.sleep(self._rng.uniform(3.0, 5.0))

        # Try clicking the ad companion / overlay link (opens new tab)
        ad_link_selectors = [
            '.ytp-ad-clickable-area',
            '.ytp-ad-player-overlay',
            '.ytp-ad-overlay-slot a',
            '.ytp-ad-image-overlay a',
            '.ytp-ad-button-text',
        ]
        ad_link_clicked = False
        for sel in ad_link_selectors:
            try:
                r = await page.evaluate(
                    f"""
                    (() => {{
                        var el = document.querySelector('{sel}');
                        if (el && el.offsetParent !== null) {{
                            el.click();
                            return true;
                        }}
                        return false;
                    }})()
                    """,
                    return_by_value=True,
                )
                val = r if isinstance(r, bool) else getattr(r, "value", False)
                if val:
                    self._log(f"[Ads] click_end_ad: ad clicked | selector={sel!r}")
                    ad_link_clicked = True
                    result["clicked"] = True
                    break
            except Exception:
                continue

        # Original video tab: ensure it keeps playing
        await asyncio.sleep(1.5)
        try:
            await page.evaluate(
                "(() => { var v = document.querySelector('video'); "
                "if (v && v.paused) { v.play().catch(()=>{}); } })()",
                return_by_value=True,
            )
        except Exception:
            pass

        if not ad_link_clicked:
            # Fallback: just skip the ad
            await self._strategy.skip_ad_if_present(page)
            result["skipped"] = True

        return result

    # ── Loyal-Fan Discovery ───────────────────────────────────────────────────

    async def find_and_click_loyal_fan_video(
        self,
        tab: Optional[Any] = None,
    ) -> Optional[str]:
        """
        Scan sidebar for a video from own_channel_ids.
        If found and not already watched → click it → return video_id.
        Returns None if nothing found / already watched.
        """
        page = tab or self._tab
        own_ids = self._config.own_channel_ids
        if not own_ids:
            self._log("[LoyalFan] No own_channel_ids configured — skip")
            return None

        self._log(f"[LoyalFan] Scanning sidebar for own channels: {own_ids}")

        # Scroll sidebar to ensure related videos are loaded
        try:
            await page.evaluate(
                "(() => { var s = document.querySelector('#secondary, #related'); "
                "if (s) s.scrollTop += 200; })()",
                return_by_value=True,
            )
        except Exception:
            pass
        await asyncio.sleep(1.5)

        # Get all sidebar video links with channel info
        try:
            result = await page.evaluate(
                """
                (() => {
                    var items = document.querySelectorAll('ytd-compact-video-renderer');
                    var found = [];
                    items.forEach(function(item) {
                        var anchor = item.querySelector('a#thumbnail[href*="/watch"]');
                        var channelEl = item.querySelector(
                            '#channel-name a, .ytd-channel-name a, #byline-container a'
                        );
                        if (!anchor) return;
                        var href = anchor.getAttribute('href') || '';
                        var channelHref = channelEl ? (channelEl.getAttribute('href') || '') : '';
                        var title = (item.querySelector('#video-title') || {}).getAttribute
                                    ? (item.querySelector('#video-title').getAttribute('title') || '') : '';
                        found.push({href: href, channelHref: channelHref, title: title});
                    });
                    return JSON.stringify(found);
                })()
                """,
                return_by_value=True,
            )
            import json as _json
            raw = result if isinstance(result, str) else getattr(result, "value", "[]")
            items = _json.loads(raw or "[]")
        except Exception:
            items = []

        # Match against own_channel_ids
        for item in items:
            channel_href = item.get("channelHref", "")
            video_href = item.get("href", "")
            title = item.get("title", "")

            match = any(cid in channel_href for cid in own_ids)
            if not match:
                continue

            # Extract video_id
            vid_id = None
            if "v=" in video_href:
                vid_id = video_href.split("v=")[-1].split("&")[0]

            if vid_id and vid_id in self._watched_video_ids:
                self._log(f"[LoyalFan] Already watched {vid_id} — skip")
                continue

            self._log(f"[LoyalFan] Found own channel video: {title!r} | vid={vid_id}")

            # Click it
            full_url = f"https://www.youtube.com{video_href}" if not video_href.startswith("http") else video_href
            try:
                await page.get(full_url)
                await asyncio.sleep(2.0)
                if vid_id:
                    self._watched_video_ids.add(vid_id)
                    self._current_video_id = vid_id
                self._log(f"[LoyalFan] Navigated to own channel video: {vid_id}")
                return vid_id
            except Exception as exc:
                self._log(f"[LoyalFan] Navigation failed: {exc}", level=logging.WARNING)

        self._log("[LoyalFan] No unwatched own-channel video found in sidebar")
        return None

    # ── Config-Driven Engagement ──────────────────────────────────────────────

    async def perform_config_engagement(
        self,
        tab: Optional[Any] = None,
        *,
        video_duration: float = 300.0,
    ) -> dict[str, Any]:
        """
        Execute all enabled actions from profile_config.actions at random timestamps
        relative to video_duration. engagement_intensity controls how many extra
        micro-interactions happen.

        Returns dict of action results.
        """
        page = tab or self._tab
        cfg = self._config
        results: dict[str, Any] = {}

        # Build action schedule based on video duration
        schedule: list[dict] = []
        start_window = video_duration * 0.10
        end_window = video_duration * 0.85

        if cfg.action_enabled("like"):
            schedule.append({
                "action": "like",
                "at": self._rng.uniform(start_window, end_window * 0.6),
            })
        if cfg.action_enabled("dislike"):
            schedule.append({
                "action": "dislike",
                "at": self._rng.uniform(start_window * 1.2, end_window * 0.7),
            })
        if cfg.action_enabled("subscribe"):
            schedule.append({
                "action": "subscribe",
                "at": self._rng.uniform(start_window * 1.5, end_window),
            })
        if cfg.action_enabled("bell"):
            schedule.append({
                "action": "bell",
                "at": self._rng.uniform(end_window * 0.3, end_window),
            })
        if cfg.action_enabled("comment") and cfg.comment_text:
            schedule.append({
                "action": "comment",
                "at": self._rng.uniform(end_window * 0.5, end_window),
                "text": cfg.comment_text,
            })

        # Sort by timestamp
        schedule.sort(key=lambda x: x["at"])
        sched_summary = ["%s@%.0fs" % (s["action"], s["at"]) for s in schedule]
        self._log(
            f"[Config Engagement] Scheduled {len(schedule)} actions | "
            f"intensity={cfg.engagement_intensity} | {sched_summary}"
        )

        session_start = time.monotonic()

        for item in schedule:
            action = item["action"]
            trigger_at = item["at"]

            # Wait until trigger time
            elapsed = time.monotonic() - session_start
            wait_needed = trigger_at - elapsed
            if wait_needed > 0:
                self._log(f"[Config Engagement] Waiting {wait_needed:.0f}s → {action}")
                await asyncio.sleep(wait_needed)

            # Check ads before any interaction
            if await self._strategy.is_ad_playing(page):
                self._log(f"[Config Engagement] Ad playing — skipping {action} for now")
                results[action] = "skipped_ad"
                continue

            self._log(f"[Config Engagement] Executing: {action}")
            try:
                if action == "like":
                    ok = await self._strategy.like(page)
                    results["like"] = ok
                elif action == "dislike":
                    ok = await self._strategy.dislike(page)
                    results["dislike"] = ok
                elif action == "subscribe":
                    ok = await self._strategy.subscribe(page)
                    results["subscribe"] = ok
                elif action == "bell":
                    ok = await self._click_bell_notification(page)
                    results["bell"] = ok
                elif action == "comment":
                    ok = await self._strategy.post_comment(page, item.get("text", ""))
                    results["comment"] = ok
            except Exception as exc:
                self._log(f"[Config Engagement] {action} failed: {exc}", level=logging.WARNING)
                results[action] = False

            await self._strategy.human_delay(1.0, 2.5)

        return results

    async def _click_bell_notification(self, page: Any) -> bool:
        """Click the notification preference bell after subscribing."""
        BELL_SELECTORS = [
            '#notification-preference-button button',
            'ytd-subscription-notification-toggle-button-renderer button',
            'button[aria-label*="notification" i]',
        ]
        for sel in BELL_SELECTORS:
            try:
                r = await page.evaluate(
                    f"""
                    (() => {{
                        var el = document.querySelector('{sel}');
                        if (el && el.offsetParent !== null) {{ el.click(); return true; }}
                        return false;
                    }})()
                    """,
                    return_by_value=True,
                )
                val = r if isinstance(r, bool) else getattr(r, "value", False)
                if val:
                    self._log(f"[Bell] Clicked | selector={sel!r}")
                    return True
            except Exception:
                continue
        return False

    async def _navigate_with_entropy(self, page: Tab, target: VideoTarget) -> str:
        """
        Behavioral Entropy Engine navigation.
        Picks Path A / B / C based on profile personality, applies all jitter rules.
        """
        from behavior.youtube.entropy import BehavioralEntropyEngine

        engine = BehavioralEntropyEngine(
            profile_id=self._profile_id,
            strategy=self._strategy,
            rng=self._rng,
            log=self._log,
        )

        page_url, page_title = await self._read_page_state(page)
        self._log(
            f"[Entropy] Starting | profile={self._profile_id} "
            f"video_id={target.video_id} channel={target.channel_name!r} "
            f"url={page_url!r}"
        )

        success = await engine.execute(page, target)

        if not success:
            # Final fallback: legacy search with primary keywords
            self._log("[Entropy] All paths failed — legacy search fallback", level=logging.WARNING)
            kw = target.search_keywords or target.title_hint or target.video_id or ""
            try:
                success = await self._strategy.search(page, kw, target)
            except Exception as exc:
                self._log(f"[Entropy] Legacy fallback error: {exc}", level=logging.WARNING)

        if not success:
            raise YouTubeManagerError(
                f"[Entropy] Target video not found | "
                f"video_id={target.video_id} title={target.title_hint!r}"
            )

        await self._strategy.wait_player_ready(page)
        self._current_video_id = target.video_id or await self._get_current_video_id(page)
        self._current_route = "entropy"
        self._log(f"[Entropy] Arrived | video_id={self._current_video_id} url={str(page.url)[:60]}")
        return self._current_route

    async def _navigate_legacy(
        self,
        page: Tab,
        target: VideoTarget,
        force_route: Optional[str] = None,
    ) -> str:
        """Legacy route-based navigation (kept for mobile and force_route override)."""
        route = self._choose_route(force_route)
        self._current_route = route.value
        page_url, page_title = await self._read_page_state(page)
        self._log(
            f"[Legacy] Navigating via {route.value} | platform={self._platform.value} "
            f"video_id={target.video_id} keywords={target.search_keywords!r} "
            f"url={page_url!r} title={page_title!r}"
        )

        max_scroll_retries = 3
        last_error: Optional[Exception] = None

        for attempt in range(max_scroll_retries):
            try:
                if not await self._has_search_bar(page):
                    self._log(
                        f"Search bar lost before attempt {attempt + 1} — re-verifying homepage",
                        level=logging.WARNING,
                    )
                    await self._require_youtube_homepage(page, context=f"retry_{attempt + 1}")

                success = False
                if route == NavigationRoute.SEARCH:
                    kw = target.search_keywords or target.title_hint or target.video_id or ""
                    success = await self._strategy.search(page, kw, target)
                elif route == NavigationRoute.HOMEPAGE:
                    success = await self._strategy.browse_homepage(page, target)
                elif route == NavigationRoute.SUGGESTED:
                    success = await self._strategy.browse_suggested(page, target)

                if not success:
                    raise YouTubeManagerError(
                        f"Target video not found via {route.value} on {self._platform.value}."
                    )
                break
            except ElementNotFoundError as exc:
                last_error = exc
                if attempt >= max_scroll_retries - 1:
                    raise YouTubeManagerError(str(exc)) from exc
                self._log(
                    f"Element not found — retry-with-scroll {attempt + 1}/{max_scroll_retries}",
                    level=logging.WARNING,
                )
                await self._strategy.scroll_feed(page, self._rng.randint(2, 5))
                await self._strategy.human_delay(2.0, 4.0)
                await self._require_youtube_homepage(page, context=f"post_scroll_{attempt + 1}")
        else:
            if last_error:
                raise YouTubeManagerError(str(last_error)) from last_error

        await self._strategy.wait_player_ready(page)
        self._current_video_id = target.video_id or await self._get_current_video_id(page)
        self._log(f"Arrived on video | id={self._current_video_id}")
        return route.value

    async def watch_video(
        self,
        tab: Optional[Tab] = None,
        *,
        perform_engagement: bool = False,
        comment_text: Optional[str] = None,
        like_probability: float = 0.55,
        subscribe_probability: float = 0.18,
    ) -> WatchSessionResult:
        """Watch with Gaussian duration and stealth engagement on both platforms."""
        page = tab or self._tab
        if page is None:
            raise YouTubeManagerError("No active tab.")

        duration = await self._strategy.get_video_duration(page)
        if duration <= 0:
            duration = float(self._rng.randint(180, 720))
            self._log(f"Fallback duration {duration:.0f}s", level=logging.WARNING)

        watch_fraction = self._sample_watch_fraction()
        planned_watch = max(15.0, duration * watch_fraction)
        self._log(
            f"Watch plan | duration={self._strategy.format_timestamp(duration)} "
            f"fraction={watch_fraction:.0%} target={self._strategy.format_timestamp(planned_watch)}"
        )

        engagement_plan = self._plan_engagement(
            planned_watch,
            perform_engagement=perform_engagement,
            comment_text=comment_text,
            like_probability=like_probability,
            subscribe_probability=subscribe_probability,
        )

        callbacks: dict[str, Any] = {
            "liked": False,
            "subscribed": False,
            "commented": False,
        }
        engagement_events: list[str] = []

        started = time.monotonic()
        await self._strategy.watch(page, planned_watch, engagement_plan, callbacks)
        actual = time.monotonic() - started

        for action in engagement_plan:
            stamp = self._strategy.format_timestamp(action["at"])
            if action["type"] == "like" and callbacks.get("liked"):
                engagement_events.append(f"like@{stamp}")
            elif action["type"] == "subscribe" and callbacks.get("subscribed"):
                engagement_events.append(f"subscribe@{stamp}")
            elif action["type"] == "comment" and callbacks.get("commented"):
                engagement_events.append(f"comment@{stamp}")

        result = WatchSessionResult(
            platform=self._platform.value,
            route=self._current_route or "unknown",
            video_id=self._current_video_id,
            planned_watch_seconds=planned_watch,
            actual_watch_seconds=actual,
            watch_fraction=watch_fraction,
            liked=bool(callbacks.get("liked")),
            subscribed=bool(callbacks.get("subscribed")),
            commented=bool(callbacks.get("commented")),
            engagement_events=engagement_events,
        )
        self._log(
            f"Watch complete | platform={result.platform} "
            f"watched={self._strategy.format_timestamp(actual)} "
            f"liked={result.liked} subscribed={result.subscribed}"
        )
        return result

    async def perform_engagement(
        self,
        tab: Optional[Tab] = None,
        *,
        like: bool = True,
        subscribe: bool = False,
    ) -> dict[str, bool]:
        """Like/subscribe with human pauses between actions."""
        page = tab or self._tab
        if page is None:
            raise YouTubeManagerError("No active tab.")

        outcomes = {"liked": False, "subscribed": False}
        if like:
            await self._strategy.human_delay(0.8, 2.2)
            outcomes["liked"] = await self._strategy.like(page)
        if subscribe:
            await self._strategy.human_delay(1.5, 4.0)
            outcomes["subscribed"] = await self._strategy.subscribe(page)
        return outcomes

    async def post_comment(self, text: str, tab: Optional[Tab] = None) -> bool:
        page = tab or self._tab
        if page is None:
            raise YouTubeManagerError("No active tab.")
        if not text.strip():
            raise YouTubeManagerError("Comment text must not be empty.")
        return await self._strategy.post_comment(page, text.strip())

    async def change_settings(
        self,
        tab: Optional[Tab] = None,
        *,
        playback_speed: Optional[float] = None,
        quality: Optional[str] = None,
        toggle_autoplay: Optional[bool] = None,
    ) -> bool:
        page = tab or self._tab
        if page is None:
            raise YouTubeManagerError("No active tab.")
        return await self._strategy.change_settings(
            page,
            playback_speed=playback_speed,
            quality=quality,
            toggle_autoplay=toggle_autoplay,
        )

    async def _referrer_warmup(self, tab: Tab, keywords: str) -> None:
        """Referrer SERP via address bar, then bulletproof return to YouTube home."""
        engine_host = self._rng.choice(("www.google.com/search?q=", "www.bing.com/search?q="))
        query = keywords.replace(" ", "+")
        self._log(f"Referrer warmup | engine={engine_host.split('/')[0]} query={keywords!r}")
        await address_bar_navigate(tab, f"{engine_host}{query}", self._rng)
        await self._strategy.human_delay(4.0, 9.0)
        await self._bulletproof_navigate_youtube(tab, context="referrer_return")
        await self._require_youtube_homepage(tab, context="referrer_warmup")

    def _sample_watch_fraction(self) -> float:
        sigma = self._rng.uniform(0.06, 0.14)
        sample = self._rng.gauss(self._watch_mean, sigma)
        return max(0.40, min(1.0, sample))

    def _plan_engagement(
        self,
        planned_watch: float,
        *,
        perform_engagement: bool,
        comment_text: Optional[str],
        like_probability: float,
        subscribe_probability: float,
    ) -> list[dict[str, Any]]:
        if not perform_engagement:
            return []

        plan: list[dict[str, Any]] = []
        start = planned_watch * 0.15
        end = planned_watch * 0.88

        if self._rng.random() < like_probability:
            plan.append({"type": "like", "at": self._rng.uniform(start, end * 0.75)})
        if self._rng.random() < subscribe_probability:
            plan.append({"type": "subscribe", "at": self._rng.uniform(start * 1.2, end)})
        if comment_text:
            plan.append({
                "type": "comment",
                "at": self._rng.uniform(end * 0.55, end),
                "text": comment_text,
            })

        plan.sort(key=lambda item: item["at"])
        return plan

    async def _get_current_video_id(self, tab: Tab) -> Optional[str]:
        try:
            url = tab.url or ""
            return YouTubeInteraction.extract_video_id(url)
        except Exception:
            return None


async def _demo() -> None:
    profile_id = os.getenv("YOUTUBE_DEMO_PROFILE_ID", "")
    if not profile_id:
        print("Set YOUTUBE_DEMO_PROFILE_ID to run the demo.")
        return

    manager = YouTubeManager(profile_id=profile_id)
    tab = await manager.open_session()
    try:
        target = VideoTarget(
            video_id="dQw4w9WgXcQ",
            search_keywords="never gonna give you up",
            title_hint="Never Gonna Give You Up",
        )
        route = await manager.navigate_to_video(tab, target)
        print(f"Platform: {manager.platform.value} | Route: {route}")
        result = await manager.watch_video(tab, perform_engagement=False)
        print(f"Watched {result.actual_watch_seconds:.0f}s ({result.watch_fraction:.0%})")
    finally:
        await manager.close_session()


if __name__ == "__main__":
    uc.loop().run_until_complete(_demo())
