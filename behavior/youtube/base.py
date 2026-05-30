"""
Abstract YouTube interaction strategy and shared human-input primitives.
"""

from __future__ import annotations

import asyncio
import json as _json_mod
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional


def _json_dumps_inline(comma_str: str) -> str:
    """Convert 'sel1, sel2, sel3' string into JSON array literal for JS injection."""
    parts = [s.strip() for s in comma_str.split(",") if s.strip()]
    return _json_mod.dumps(parts)

from nodriver import cdp
from nodriver.core.element import Element
from nodriver.core.tab import Tab

from behavior.youtube.human_engine import (
    address_bar_navigate,
    exact_match,
    press_enter,
    send_keys_human,
    slow_scroll,
    wait_for_any_element,
    wait_for_element,
    wait_for_player,
)
from behavior.youtube.resolver import SemanticResolver
from behavior.youtube.types import ElementNotFoundError, InteractionContext, PlatformKind, VideoTarget


class YouTubeInteraction(ABC):
    """
    Platform-specific strategy for YouTube UI automation.

    Subclasses implement search, watch, like, subscribe, and settings flows
    using the input modality appropriate to desktop or mobile.
    """

    platform: PlatformKind

    def __init__(self, ctx: InteractionContext) -> None:
        self._ctx = ctx
        self._rng = ctx.rng
        self._logger = ctx.logger
        self._resolver = SemanticResolver(
            platform=ctx.platform,
            logger=ctx.logger,
            on_failure=getattr(ctx, "record_selector_failure", None),
        )

    @property
    def resolver(self) -> SemanticResolver:
        return self._resolver

    def _log(self, message: str, *, level: int = 20) -> None:
        self._logger.log(level, f"[{self.platform.value}] {message}")

    # ------------------------------------------------------------------
    # Abstract contract
    # ------------------------------------------------------------------

    @abstractmethod
    async def search(self, tab: Tab, keywords: str, target: VideoTarget) -> bool:
        """Search for keywords and click the target video."""

    @abstractmethod
    async def browse_homepage(self, tab: Tab, target: VideoTarget) -> bool:
        """Scroll the homepage feed until the target video is found."""

    @abstractmethod
    async def browse_suggested(self, tab: Tab, target: VideoTarget) -> bool:
        """Open a decoy video then locate the target in suggestions."""

    @abstractmethod
    async def watch(
        self,
        tab: Tab,
        planned_seconds: float,
        engagement_plan: list[dict[str, Any]],
        result_callbacks: dict[str, Any],
    ) -> None:
        """Simulate organic watch time with micro-interactions."""

    @abstractmethod
    async def like(self, tab: Tab) -> bool:
        """Like the current video."""

    @abstractmethod
    async def subscribe(self, tab: Tab) -> bool:
        """Subscribe to the current channel."""

    @abstractmethod
    async def change_settings(
        self,
        tab: Tab,
        *,
        playback_speed: Optional[float] = None,
        quality: Optional[str] = None,
        toggle_autoplay: Optional[bool] = None,
    ) -> bool:
        """Adjust player settings (playback speed, quality, autoplay) via UI menus."""

    @abstractmethod
    async def pause_playback(self, tab: Tab) -> bool:
        """Toggle pause state."""

    @abstractmethod
    async def seek_relative(self, tab: Tab, delta_seconds: int) -> bool:
        """Seek forward or backward."""

    @abstractmethod
    async def adjust_volume(self, tab: Tab, direction: str) -> bool:
        """Raise or lower volume (direction: 'up' | 'down')."""

    @abstractmethod
    async def scroll_feed(self, tab: Tab, scrolls: int) -> None:
        """Scroll a results or feed page."""

    @abstractmethod
    async def dismiss_consent(self, tab: Tab) -> None:
        """Close cookie/consent overlays if present."""

    @abstractmethod
    async def post_comment(self, tab: Tab, text: str) -> bool:
        """Type and submit a comment."""

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def human_delay(self, minimum: float = 1.0, maximum: float = 3.0) -> None:
        await asyncio.sleep(self._rng.uniform(minimum, maximum))

    async def smart_wait(
        self,
        tab: Tab,
        selector: str,
        *,
        timeout: float = 20.0,
        label: str = "",
    ) -> Element:
        return await wait_for_element(
            tab,
            selector,
            timeout=timeout,
            label=label,
            log=lambda msg: self._log(msg),
        )

    async def smart_wait_any(
        self,
        tab: Tab,
        selectors: tuple[str, ...],
        *,
        timeout: float = 20.0,
        label: str = "",
    ) -> tuple[Element, str]:
        return await wait_for_any_element(
            tab,
            selectors,
            timeout=timeout,
            label=label,
            log=lambda msg: self._log(msg),
        )

    async def human_type(self, element: Element, text: str, *, variable: bool = False) -> None:
        await send_keys_human(
            element.tab,
            text,
            self._rng,
            element=element,
        )

    async def human_type_tab(self, tab: Tab, text: str) -> None:
        await send_keys_human(tab, text, self._rng)

    async def navigate_address_bar(self, tab: Tab, host: str) -> None:
        await address_bar_navigate(tab, host, self._rng)

    async def ensure_youtube_home(self, tab: Tab) -> None:
        url = tab.url or ""
        host = "m.youtube.com" if self._ctx.is_mobile else "youtube.com"
        if host not in url:
            await self.navigate_address_bar(tab, host)
            await self.human_delay(2.0, 4.0)
            await self.dismiss_consent(tab)
        search_selectors = self._search_bar_selectors()
        await self.smart_wait_any(tab, search_selectors, timeout=20.0, label="search_bar")

    def _search_bar_selectors(self) -> tuple[str, ...]:
        spec = self._resolver.selector_map.get("search_bar", {})
        css = spec.get("css", ())
        return tuple(css) if css else ('input#search', 'input[name="search_query"]')

    async def record_selector_failure(self, key: str, detail: str = "") -> None:
        recorder = getattr(self._ctx, "record_selector_failure", None)
        if recorder:
            recorder(key, detail or key)
        self._log(f"Selector failure | key={key} detail={detail}", level=30)

    async def is_clickable(self, element: Element) -> bool:
        try:
            result = await element.apply(
                """
                function(el) {
                    if (!el || el.disabled) return false;
                    if (el.getAttribute('aria-disabled') === 'true') return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return false;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const top = document.elementFromPoint(cx, cy);
                    return top && (el === top || el.contains(top) || top.contains(el));
                }
                """,
                return_by_value=True,
            )
            return bool(result)
        except Exception:
            return False

    async def wait_clickable(self, element: Element, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await element.update()
            except Exception:
                pass
            if await self.is_clickable(element):
                return True
            await asyncio.sleep(0.25)
        return False

    async def human_click(self, element: Element, *, jitter: bool = True) -> None:
        clickable = await self.wait_clickable(element, timeout=6.0)

        if not clickable:
            self._log("Soft click fallback (element not hit-testable)")
            try:
                await element.scroll_into_view()
            except Exception:
                pass
            try:
                await element.focus()
                await asyncio.sleep(self._rng.uniform(0.15, 0.35))
                await element.click()
                await self.human_delay(0.4, 1.0)
                return
            except Exception:
                await element.apply("(el) => { el.click(); return true; }")
                await self.human_delay(0.4, 1.0)
                return

        await self.human_delay(0.25, 0.85)

        try:
            position = await element.get_position()
            if position and position.center:
                x, y = position.center
                if jitter:
                    x += self._rng.uniform(-4.0, 4.0)
                    y += self._rng.uniform(-3.0, 3.0)
                await element.tab.mouse_move(x, y, steps=self._rng.randint(6, 16))
                await asyncio.sleep(self._rng.uniform(0.05, 0.16))
                await element.tab.mouse_click(x, y)
            else:
                await element.mouse_click()
        except Exception:
            await element.click()

        await self.human_delay(0.4, 1.2)

    async def tap(self, element: Element) -> None:
        """Mobile-friendly tap via mouse_click at element center."""
        await self.human_click(element, jitter=False)

    async def send_key(self, tab: Tab, key: str) -> None:
        await tab.send(cdp.input_.dispatch_key_event("keyDown", key=key, code=key))
        await tab.send(cdp.input_.dispatch_key_event("keyUp", key=key, code=key))

    async def click_target_video(
        self,
        tab: Tab,
        target: VideoTarget,
        link_key: str = "video_link",
    ) -> bool:
        links = await self._resolver.find_all_links(tab, link_key)
        if not links:
            return False

        self._rng.shuffle(links)
        candidates: list[Element] = []
        for link in links:
            href = await self.read_href(link)
            title = await self.read_title(link)
            if self.matches_target(href, title, target):
                candidates.append(link)

        if not candidates and target.video_id:
            for link in links:
                href = await self.read_href(link)
                if href and target.video_id in href:
                    candidates.append(link)

        if not candidates:
            return False

        chosen = self._rng.choice(candidates)
        title = await self.read_title(chosen)
        self._log(f"Clicking target video | title={title!r}")
        await self.human_click(chosen)
        await self.human_delay(2.0, 4.5)
        return True

    async def pick_random_video(self, tab: Tab, link_key: str) -> Element | None:
        links = await self._resolver.find_all_links(tab, link_key)
        if not links:
            return None
        return self._rng.choice(links[: min(12, len(links))])

    @staticmethod
    def matches_target(
        href: Optional[str],
        title: Optional[str],
        target: VideoTarget,
        channel: Optional[str] = None,
    ) -> bool:
        if target.video_id and href and target.video_id in href:
            if target.title_hint or getattr(target, "channel_name", None):
                return exact_match(title or "", channel or "", target)
            return True
        return exact_match(title or "", channel or "", target)

    @staticmethod
    async def read_href(element: Element) -> Optional[str]:
        try:
            href = await element.apply(
                "(el) => el.href || el.getAttribute('href') || ''",
                return_by_value=True,
            )
            return str(href) if href else None
        except Exception:
            return None

    @staticmethod
    async def read_title(element: Element) -> Optional[str]:
        try:
            title = await element.apply(
                """
                (el) => {
                    const t = el.getAttribute('title') ||
                        el.getAttribute('aria-label') ||
                        el.innerText || el.textContent || '';
                    return t.trim();
                }
                """,
                return_by_value=True,
            )
            return str(title) if title else None
        except Exception:
            return None

    @staticmethod
    def extract_video_id(url: str) -> Optional[str]:
        match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url)
        return match.group(1) if match else None

    async def get_current_time(self, tab: Tab) -> Optional[float]:
        try:
            value = await tab.evaluate(
                "(() => { const v = document.querySelector('video'); return v ? v.currentTime : null; })()",
                return_by_value=True,
            )
            if value is None:
                return None
            # Handle RemoteObject — extract .value if needed
            raw = getattr(value, "value", value)
            return float(raw) if raw is not None else None
        except Exception:
            return None

    async def get_video_duration(self, tab: Tab) -> float:
        try:
            value = await tab.evaluate(
                """
                (() => {
                    const v = document.querySelector('video');
                    if (!v || !v.duration || !isFinite(v.duration)) return 0;
                    return v.duration;
                })()
                """,
                return_by_value=True,
            )
            raw = getattr(value, "value", value) if value is not None else 0
            return float(raw or 0)
        except Exception:
            return 0.0

    async def wait_player_ready(self, tab: Tab, timeout: float = 20.0) -> None:
        if not await wait_for_player(tab, timeout=timeout):
            self._log("Player ready timeout; continuing", level=30)

    @staticmethod
    def format_timestamp(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, secs = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    async def micro_interaction(self, tab: Tab) -> None:
        choice = self._rng.choice(("comments", "scroll", "idle"))
        if choice == "comments":
            self._log("Micro-interaction: scrolling comments")
            section = await self._resolver.find(tab, "comments_section", timeout=3.0)
            if section:
                try:
                    await section.scroll_into_view()
                except Exception:
                    pass
            await self.scroll_feed(tab, self._rng.randint(1, 3))
        elif choice == "scroll":
            self._log("Micro-interaction: feed scroll")
            await self.scroll_feed(tab, 1)
        else:
            await self.human_delay(1.0, 2.5)

    # ------------------------------------------------------------------
    # AD-AWARE METHODS
    # ------------------------------------------------------------------

    async def is_ad_playing(self, tab: Tab) -> bool:
        """
        Detect if a YouTube ad is currently playing.
        Checks multiple ad-specific DOM elements — returns True if any found.
        """
        try:
            result = await tab.evaluate(
                """
                (() => {
                    var adSelectors = [
                        '.ad-interrupting',
                        '.ytp-ad-player-overlay',
                        '.ytp-ad-player-overlay-instream-info',
                        '.ytp-ad-module',
                        '.ytp-ad-badge',
                        '.ytp-ad-text',
                        '.video-ads',
                        '.ytp-ad-overlay-container',
                        '[class*="ad-showing"]',
                        '.ytp-ad-skip-button',
                        '.ytp-ad-skip-button-modern',
                    ];
                    for (var i = 0; i < adSelectors.length; i++) {
                        var el = document.querySelector(adSelectors[i]);
                        if (el && el.offsetParent !== null) return true;
                    }
                    // Check player class list for 'ad-showing'
                    var player = document.querySelector('.html5-video-player, #movie_player');
                    if (player && player.classList.contains('ad-showing')) return true;
                    return false;
                })()
                """,
                return_by_value=True,
            )
            raw = getattr(result, "value", result)
            return bool(raw)
        except Exception:
            return False

    async def skip_ad_if_present(self, tab: Tab) -> bool:
        """
        Check for ad and auto-click skip button if available.
        Returns True if ad was skipped, False if no ad or no skip button.
        """
        try:
            result = await tab.evaluate(
                """
                (() => {
                    var skipSels = [
                        '.ytp-skip-ad-button',
                        '.ytp-ad-skip-button',
                        '.ytp-ad-skip-button-modern',
                        'button.ytp-skip-ad-button',
                        '[class*="skip-ad"]',
                        '[class*="skip_ad"]',
                    ];
                    for (var i = 0; i < skipSels.length; i++) {
                        var el = document.querySelector(skipSels[i]);
                        if (el && el.offsetParent !== null) {
                            el.click();
                            return 'skipped:' + skipSels[i];
                        }
                    }
                    return 'no_skip_button';
                })()
                """,
                return_by_value=True,
            )
            raw = result if isinstance(result, str) else getattr(result, "value", "")
            skipped = "skipped" in (raw or "")
            if skipped:
                self._log(f"Ad skip button clicked | {raw}")
                await asyncio.sleep(1.0)
            return skipped
        except Exception:
            return False

    async def wait_for_video_start(
        self,
        tab: Tab,
        *,
        timeout: float = 60.0,
        skip_ads: bool = True,
    ) -> bool:
        """
        Wait until the ACTUAL video content is playing (not an ad).

        Flow:
        1. Detect ad → try skip → wait for ad to end
        2. Once no ad detected and currentTime > 0 → video has started
        3. Log: 'Ad detected → Skipping → Video started → Applying settings'

        Returns True when video is confirmed playing, False on timeout.
        """
        deadline = time.monotonic() + timeout
        ad_was_detected = False
        skip_attempted = False

        while time.monotonic() < deadline:
            ad_playing = await self.is_ad_playing(tab)

            if ad_playing:
                if not ad_was_detected:
                    self._log("Ad detected → waiting/skipping...")
                    ad_was_detected = True

                if skip_ads and not skip_attempted:
                    skipped = await self.skip_ad_if_present(tab)
                    if skipped:
                        self._log("Ad detected → Skipping → waiting for video...")
                        skip_attempted = True
                        await asyncio.sleep(2.0)
                        continue
                    else:
                        # Skip button not ready yet — wait
                        skip_attempted = False  # reset to retry skip next loop

                await asyncio.sleep(1.5)
                continue

            # No ad — check if real video is playing
            current_time = await self.get_current_time(tab)
            duration = await self.get_video_duration(tab)

            if current_time is not None and current_time > 0.5 and duration > 10.0:
                if ad_was_detected:
                    self._log(
                        "Ad detected → Skipped → Video started → Applying settings ✓"
                    )
                else:
                    self._log("Video started (no ad) → Applying settings ✓")
                return True

            await asyncio.sleep(1.0)

        self._log("wait_for_video_start timeout — proceeding anyway", level=30)
        return False

    # ------------------------------------------------------------------
    # SELF-HEALING: safe_click with fallback + HTML snippet logging
    # ------------------------------------------------------------------

    async def safe_click(
        self,
        tab: Tab,
        selector_key: str,
        *,
        extra_selectors: tuple[str, ...] = (),
        timeout: float = 5.0,
        label: str = "",
    ) -> bool:
        """
        Self-healing click with 3-tier fallback + HTML snippet logging.

        Tier 1: Primary selectors from SelectorMap (css + aria_labels)
        Tier 2: extra_selectors provided by caller
        Tier 3: Text-search fallback (button innerText match)
        On total failure: logs HTML snippet to selector_failures.json
        Returns True if click succeeded.
        """
        import json as _json
        import datetime

        from behavior.youtube.selectors import DESKTOP_SELECTORS, MOBILE_SELECTORS
        sel_map = DESKTOP_SELECTORS if not self._ctx.is_mobile else MOBILE_SELECTORS
        spec = sel_map.get(selector_key, {})

        # Build all candidate selectors
        primary_css = list(spec.get("css", ()))
        aria_labels = spec.get("aria_labels", ())
        aria_css = [f'button[aria-label*="{lbl}" i]' for lbl in aria_labels]
        all_selectors = primary_css + aria_css + list(extra_selectors)

        label = label or selector_key

        # Try each selector via JS
        for sel in all_selectors:
            try:
                result = await tab.evaluate(
                    f"""
                    (() => {{
                        var el = document.querySelector({_json.dumps(sel)});
                        if (el && el.offsetParent !== null && !el.disabled) {{
                            el.click();
                            return 'clicked';
                        }}
                        return 'not_found';
                    }})()
                    """,
                    return_by_value=True,
                )
                raw = result if isinstance(result, str) else getattr(result, "value", "")
                if raw == "clicked":
                    self._log(f"safe_click [{label}] ✓ via {sel!r}")
                    await asyncio.sleep(self._rng.uniform(0.3, 0.8))
                    return True
            except Exception:
                continue

        # Tier 3: text-based search
        text_terms = list(spec.get("text", ()))
        if text_terms:
            for term in text_terms:
                try:
                    result = await tab.evaluate(
                        f"""
                        (() => {{
                            var term = {_json.dumps(term.lower())};
                            var els = document.querySelectorAll('button, [role="button"], tp-yt-paper-button');
                            for (var i = 0; i < els.length; i++) {{
                                var t = (els[i].innerText || els[i].textContent || '').trim().toLowerCase();
                                if (t === term && els[i].offsetParent !== null) {{
                                    els[i].click();
                                    return 'text_clicked:' + t;
                                }}
                            }}
                            return 'not_found';
                        }})()
                        """,
                        return_by_value=True,
                    )
                    raw = result if isinstance(result, str) else getattr(result, "value", "")
                    if "text_clicked" in (raw or ""):
                        self._log(f"safe_click [{label}] ✓ via text={term!r}")
                        await asyncio.sleep(self._rng.uniform(0.3, 0.8))
                        return True
                except Exception:
                    continue

        # All failed — capture screenshot + HTML snippet
        self._log(f"safe_click [{label}] FAILED — capturing evidence", level=30)
        await self._capture_failure_screenshot(tab, label)
        await self._log_selector_failure_html(tab, selector_key, label)
        return False

    async def _log_selector_failure_html(
        self, tab: Tab, selector_key: str, label: str
    ) -> None:
        """Capture surrounding HTML and log to selector_failures.json for LLM analysis."""
        import json as _json
        import datetime
        from behavior.youtube.types import SELECTOR_FAILURE_LOG

        try:
            # Capture relevant portion of DOM
            html_result = await tab.evaluate(
                """
                (() => {
                    var areas = [
                        document.querySelector('#top-level-buttons-computed'),
                        document.querySelector('#actions'),
                        document.querySelector('ytd-video-primary-info-renderer'),
                        document.querySelector('.ytp-chrome-controls'),
                    ];
                    var html = '';
                    for (var i = 0; i < areas.length; i++) {
                        if (areas[i]) {
                            html += areas[i].outerHTML.slice(0, 2000) + '\\n---\\n';
                        }
                    }
                    return html.slice(0, 6000) || document.body.innerHTML.slice(0, 3000);
                })()
                """,
                return_by_value=True,
            )
            html_str = html_result if isinstance(html_result, str) else getattr(html_result, "value", "")

            failure_entry = {
                "timestamp": datetime.datetime.now().isoformat(),
                "selector_key": selector_key,
                "label": label,
                "url": str(tab.url or ""),
                "html_snippet": html_str[:4000],
                "llm_hint": f"Find the button/element for '{label}' in the HTML above and suggest a reliable CSS selector.",
            }

            SELECTOR_FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if SELECTOR_FAILURE_LOG.exists():
                try:
                    existing = _json.loads(SELECTOR_FAILURE_LOG.read_text(encoding="utf-8"))
                except Exception:
                    existing = []

            existing.append(failure_entry)
            # Keep last 100 entries
            SELECTOR_FAILURE_LOG.write_text(
                _json.dumps(existing[-100:], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._log(f"HTML snippet logged to {SELECTOR_FAILURE_LOG.name}")
        except Exception as exc:
            self._log(f"HTML snippet log error: {exc}", level=30)

    async def _capture_failure_screenshot(self, tab: Tab, label: str) -> None:
        """Save a PNG screenshot when a selector fails — for post-mortem debugging."""
        import datetime
        from pathlib import Path

        PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
        screenshot_dir = PROJECT_ROOT / "logs" / "failure_screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_label = label.replace(" ", "_").replace("/", "_")[:40]
        path = screenshot_dir / f"{ts}_{safe_label}.png"

        try:
            # nodriver screenshot — returns bytes or saves to path
            data = await tab.save_screenshot(path)
            self._log(f"Failure screenshot saved: {path.name}")
        except Exception as exc:
            self._log(f"Screenshot capture failed: {exc}", level=30)

    # ------------------------------------------------------------------
    # HEALTH CHECK
    # ------------------------------------------------------------------

    async def verify_system_health(self, tab: Tab) -> dict:
        """
        Periodically check if core YouTube elements are still detectable.
        Returns health report dict — useful for monitoring across sessions.

        Checks: search_bar, like_button, subscribe_button, video_player, autoplay_toggle
        """
        checks = {
            "search_bar": 'input#search, input[name="search_query"], input[role="combobox"]',
            "like_button": 'like-button-view-model button, button[aria-label*="like" i]:not([aria-label*="dislike" i])',
            "subscribe_button": '#subscribe-button button, ytd-subscribe-button-renderer button',
            "video_player": 'video.html5-main-video, video',
            "autoplay_toggle": '.ytp-autonav-toggle-button, button[data-tooltip-target-id="ytp-autonav-toggle-button"]',
        }
        report = {}
        for name, selectors in checks.items():
            try:
                result = await tab.evaluate(
                    f"""
                    (() => {{
                        var sels = {_json_dumps_inline(selectors)};
                        for (var i = 0; i < sels.length; i++) {{
                            var el = document.querySelector(sels[i]);
                            if (el && el.offsetParent !== null) return 'found:' + sels[i];
                        }}
                        return 'missing';
                    }})()
                    """,
                    return_by_value=True,
                )
                raw = result if isinstance(result, str) else getattr(result, "value", "missing")
                found = "found" in (raw or "")
                report[name] = found
                if not found:
                    self._log(f"Health check WARN | {name} not detectable", level=30)
            except Exception:
                report[name] = False
        self._log(f"Health check | {report}")
        return report
