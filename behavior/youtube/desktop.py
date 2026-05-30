"""
Desktop YouTube interaction — human-emulation layer (Windows / macOS).

Uses ytd- selectors, mouse clicks, keyboard shortcuts (j/k/l/f).
No set_value — smart-wait + send_keys + tab.get() fallback where needed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from nodriver import cdp
from nodriver.core.element import Element
from nodriver.core.tab import Tab

from behavior.youtube.base import YouTubeInteraction
from behavior.youtube.human_engine import exact_match, press_enter, slow_scroll
from behavior.youtube.types import ElementNotFoundError, InteractionContext, PlatformKind, VideoTarget

DESKTOP_SHORTCUTS = {
    "pause": "k",
    "forward": "l",
    "rewind": "j",
    "fullscreen": "f",
    "mute": "m",
}

# Ordered by reliability: semantic first, ID-based fallback
DESKTOP_SEARCH_SELECTORS = (
    'input[role="combobox"]',
    'input[aria-label="Search"]',
    '[role="search"] input',
    'input#search',
    'input[name="search_query"]',
    'ytd-searchbox input#search',
    'input[aria-label*="Search"]',
)

DESKTOP_RESULT_SELECTORS = (
    'ytd-video-renderer',
    'ytd-item-section-renderer ytd-video-renderer',
    '#contents ytd-video-renderer',
)

# Consent overlay selectors — multiple YouTube/Google variations
_CONSENT_BUTTON_TEXTS = (
    "Accept all",
    "I agree",
    "Accept",
    "Agree",
    "Reject all",  # clicking Reject is also valid dismissal
)

_CONSENT_OVERLAY_SELECTORS = (
    'ytd-consent-bump-v2-lightbox',
    'tp-yt-paper-dialog[id*="consent"]',
    '.ytd-consent-bump-v2-lightbox',
    '#consent-bump',
    'ytd-backdrop',
)


class DesktopInteraction(YouTubeInteraction):
    """Windows/macOS YouTube — hybrid nav, exact-match discovery, keyboard player."""

    platform = PlatformKind.DESKTOP

    def __init__(self, ctx: InteractionContext) -> None:
        super().__init__(ctx)

    # ------------------------------------------------------------------
    # BULLETPROOF NAVIGATION
    # Strategy: address bar → 10s poll → fallback tab.get() → verify
    # ------------------------------------------------------------------
    async def _navigate_to_url(self, tab: Tab, url: str, *, label: str = "") -> bool:
        """
        Hybrid nav: try address-bar typing first. If tab.url doesn't change in 10s,
        fall back to direct tab.get() with 30s timeout. Returns True when confirmed.
        tab.url is used (not tab.evaluate) — reliable, no RemoteObject risk.
        """
        host = "youtube.com"
        self._log(f"Nav [{label}] | address-bar → {url}")

        # Step 1: address bar Ctrl+L → type → Enter
        await self.navigate_address_bar(tab, host)
        await asyncio.sleep(self._rng.uniform(1.5, 2.5))

        # Step 2: poll tab.url for 10s (reliable — CDP-tracked property)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            current_url = str(tab.url or "")
            if host in current_url:
                self._log(f"Nav [{label}] | address-bar success | url={current_url[:60]}")
                return True
            await asyncio.sleep(0.5)

        # Step 3: fallback tab.get() with 30s timeout
        self._log(f"Nav [{label}] | address-bar timeout → fallback tab.get({url}) | stuck={str(tab.url)[:50]!r}")
        try:
            await asyncio.wait_for(tab.get(url), timeout=30.0)
            await asyncio.sleep(self._rng.uniform(2.0, 3.5))
        except asyncio.TimeoutError:
            self._log(f"Nav [{label}] | tab.get timeout (30s)", level=40)
            return False
        except Exception as exc:
            self._log(f"Nav [{label}] | tab.get failed: {exc}", level=30)
            return False

        # Step 4: verify via tab.url
        final_url = str(tab.url or "")
        if host in final_url:
            self._log(f"Nav [{label}] | fallback success | url={final_url[:60]}")
            return True

        self._log(f"Nav [{label}] | FAILED — not on youtube.com | url={final_url[:50]!r}", level=40)
        return False

    # ------------------------------------------------------------------
    # ROBUST CONSENT DISMISSAL
    # Three-layer strategy: JS IIFE → semantic resolver → text-find
    # Verifies overlay is actually gone before returning.
    # ------------------------------------------------------------------
    async def dismiss_consent(self, tab: Tab) -> None:
        # Layer 1: JS IIFE — fastest, handles hidden/lazy-rendered buttons
        try:
            clicked = await tab.evaluate(
                """
                (() => {
                    const texts = ['Accept all', 'I agree', 'Accept', 'Agree', 'Reject all'];
                    // Search within consent dialogs first
                    const dialogs = [
                        ...document.querySelectorAll(
                            'ytd-consent-bump-v2-lightbox, tp-yt-paper-dialog, ' +
                            '#consent-bump, ytd-backdrop, [id*="consent"]'
                        )
                    ];
                    const pool = dialogs.length > 0
                        ? dialogs.flatMap(d => [...d.querySelectorAll('button, tp-yt-paper-button')])
                        : [...document.querySelectorAll('button, tp-yt-paper-button')];
                    for (const t of texts) {
                        const btn = pool.find(b => (b.innerText || '').trim().startsWith(t));
                        if (btn && btn.offsetParent !== null) { btn.click(); return t; }
                    }
                    // Fallback: form submit on consent page
                    const form = document.querySelector('form[action*="consent"]');
                    if (form) {
                        const submit = form.querySelector('button[type="submit"], button');
                        if (submit) { submit.click(); return 'form-submit'; }
                    }
                    return null;
                })()
                """,
                return_by_value=True,
            )
            if clicked:
                self._log(f"Consent dismissed via JS | button={clicked!r}")
                await self.human_delay(1.2, 2.0)
                # Verify overlay gone
                if await self._consent_overlay_gone(tab):
                    return
        except Exception:
            pass

        # Layer 2: Semantic resolver
        button = await self._resolver.find(tab, "consent_accept", timeout=3.0)
        if button:
            self._log("Dismissing consent via resolver")
            try:
                await self.human_click(button)
                await self.human_delay(1.2, 2.2)
                if await self._consent_overlay_gone(tab):
                    return
            except Exception as exc:
                self._log(f"Consent resolver click failed: {exc}", level=30)

        # Layer 3: Text-based find (last resort)
        for text in ("Accept all", "I agree", "Accept", "Agree"):
            try:
                from behavior.youtube.human_engine import _js_find_text
                if not await _js_find_text(tab, text):
                    continue
                el = await tab.find(text, best_match=True)
                if el:
                    await self.human_click(el)
                    await self.human_delay(1.0, 2.0)
                    if await self._consent_overlay_gone(tab):
                        self._log(f"Consent dismissed via text-find | text={text!r}")
                        return
            except Exception:
                continue

    async def _consent_overlay_gone(self, tab: Tab, timeout: float = 3.0) -> bool:
        """Poll until no consent overlay is visible in DOM."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                present = await tab.evaluate(
                    """
                    () => {
                        const sels = [
                            'ytd-consent-bump-v2-lightbox',
                            'tp-yt-paper-dialog[id*="consent"]',
                            '#consent-bump',
                        ];
                        return sels.some(s => {
                            const el = document.querySelector(s);
                            return el && el.offsetParent !== null;
                        });
                    }
                    """,
                    return_by_value=True,
                )
                if not present:
                    return True
            except Exception:
                return True  # If can't check, assume gone
            await asyncio.sleep(0.4)
        return False

    # ------------------------------------------------------------------
    # SEARCH — Main method
    # Flow: ensure_home → dismiss_consent → find search bar (semantic) →
    #       clear → type → submit → verify results URL → exact match scroll
    # ------------------------------------------------------------------
    async def search(self, tab: Tab, keywords: str, target: VideoTarget) -> bool:
        # Ensure we're on YouTube homepage with search bar visible
        await self._ensure_youtube_home_robust(tab)

        queries = self._build_keyword_escalation(keywords, target)
        self._log(f"Search plan | {len(queries)} keyword variants")

        for attempt, query in enumerate(queries, start=1):
            self._log(f"Search attempt {attempt}/{len(queries)}: {query!r}")

            # Dismiss consent before each attempt (may reappear after nav)
            await self.dismiss_consent(tab)
            await asyncio.sleep(self._rng.uniform(0.3, 0.6))

            # Find search bar with semantic selectors
            try:
                search_el, matched_sel = await self.smart_wait_any(
                    tab, DESKTOP_SEARCH_SELECTORS, timeout=20.0, label="search_bar"
                )
                self._log(f"Search bar found | selector={matched_sel!r}")
            except ElementNotFoundError:
                self._log("Search bar not found — trying scroll + retry", level=30)
                await self.scroll_feed(tab, 1)
                await self.record_selector_failure("search_bar", f"attempt {attempt}")
                continue

            # Click → clear → type
            await self.human_click(search_el)
            await self.human_delay(0.3, 0.6)
            await self._clear_input(tab)
            await self.human_delay(0.1, 0.25)
            await self.human_type_tab(tab, query)
            await self.human_delay(0.5, 1.0)

            # Submit — 55% via search button click, 45% via Enter
            submitted = await self._submit_search(tab, search_el)
            if not submitted:
                self._log("Submit failed — next keyword", level=30)
                continue

            # Wait for results URL to contain "search_query"
            results_loaded = await self._wait_for_results_url(tab, timeout=15.0)
            if not results_loaded:
                self._log("Results URL not detected — checking DOM anyway")

            # Wait for DOM results
            try:
                await self.smart_wait_any(
                    tab, DESKTOP_RESULT_SELECTORS, timeout=20.0, label="search_results"
                )
            except ElementNotFoundError:
                # One more consent check — overlay may have blocked results
                await self.dismiss_consent(tab)
                await asyncio.sleep(1.5)
                try:
                    await self.smart_wait_any(
                        tab, DESKTOP_RESULT_SELECTORS, timeout=10.0, label="search_results_retry"
                    )
                except ElementNotFoundError:
                    self._log("Results not loaded — next keyword")
                    continue

            # Exact-match discovery in results
            found = await self._discover_exact_match(tab, target)
            if found:
                await self.wait_player_ready(tab)
                return True

            self._log("Exact match not in results — next keyword")

        await self.record_selector_failure(
            "video_link",
            f"exact match failed title={target.title_hint!r} channel={target.channel_name!r}",
        )
        raise ElementNotFoundError(
            f"Desktop exact-match search failed after {len(queries)} keywords."
        )

    async def _ensure_youtube_home_robust(self, tab: Tab) -> None:
        """
        Bulletproof home check — uses tab.url (not tab.evaluate) to avoid RemoteObject bug.
        Only navigates if NOT already on YouTube homepage. Waits for search bar to confirm.
        """
        # Use tab.url — reliable, CDP-tracked, never returns RemoteObject
        current_url = str(tab.url or "")
        self._log(f"ensure_home | current tab.url={current_url[:60]!r}")

        needs_nav = "youtube.com" not in current_url
        on_video = "/watch" in current_url

        if needs_nav:
            # Not on YouTube at all — navigate
            success = await self._navigate_to_url(tab, "https://www.youtube.com", label="ensure_home")
            if not success:
                try:
                    await asyncio.wait_for(tab.get("https://www.youtube.com"), timeout=30.0)
                    await asyncio.sleep(3.0)
                except Exception as exc:
                    raise ElementNotFoundError(f"Cannot navigate to YouTube: {exc}")
            await self.human_delay(1.5, 2.5)
            await self.dismiss_consent(tab)

        elif on_video:
            # On a watch page — click logo to go home (no full nav needed)
            self._log("ensure_home | on /watch page → clicking logo")
            try:
                from behavior.youtube.human_engine import _js_find_selector
                _logo_sels = ('a#logo', '#logo a', 'ytd-logo a')
                _found_logo = await _js_find_selector(tab, _logo_sels)
                logo = await tab.select(_found_logo) if _found_logo else None
                if logo:
                    await self.human_click(logo)
                    await asyncio.sleep(self._rng.uniform(1.5, 2.5))
                else:
                    raise Exception("no logo")
            except Exception:
                # Logo not found — use address bar
                await self.navigate_address_bar(tab, "youtube.com")
                await self.human_delay(2.0, 3.0)

        else:
            # Already on YouTube homepage — no navigation needed
            self._log("ensure_home | already on YouTube homepage — skipping nav")

        # Verify search bar visible (confirms page actually loaded)
        await self.smart_wait_any(tab, DESKTOP_SEARCH_SELECTORS, timeout=20.0, label="search_bar_verify")

    async def _clear_input(self, tab: Tab) -> None:
        """Select all + delete — clears any previous text in focused input."""
        # Ctrl+A
        await tab.send(cdp.input_.dispatch_key_event(
            "keyDown", key="a", code="KeyA", modifiers=2, windows_virtual_key_code=65
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            "keyUp", key="a", code="KeyA", modifiers=2, windows_virtual_key_code=65
        ))
        await asyncio.sleep(self._rng.uniform(0.06, 0.14))
        # Delete
        await tab.send(cdp.input_.dispatch_key_event(
            "keyDown", key="Backspace", code="Backspace", windows_virtual_key_code=8
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            "keyUp", key="Backspace", code="Backspace", windows_virtual_key_code=8
        ))
        await asyncio.sleep(self._rng.uniform(0.08, 0.18))

    async def _submit_search(self, tab: Tab, search_el: Element) -> bool:
        """Submit search — try button click (55%), else Enter on search input."""
        search_btn = await self._resolver.find(tab, "search_button", timeout=2.0)
        if search_btn and self._rng.random() < 0.55:
            try:
                await self.human_click(search_btn)
                self._log("Search submitted via button click")
                return True
            except Exception:
                pass

        # Ensure search element has focus then press Enter
        try:
            await search_el.focus()
            await asyncio.sleep(self._rng.uniform(0.15, 0.3))
        except Exception:
            pass
        await press_enter(tab)
        self._log("Search submitted via Enter key")
        await self.human_delay(0.5, 1.0)
        return True

    async def _wait_for_results_url(self, tab: Tab, timeout: float = 15.0) -> bool:
        """Poll tab.url until it contains 'search_query' — confirms search navigation succeeded."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            url = str(tab.url or "")
            if "search_query" in url:
                self._log(f"Results URL confirmed | url={url[:80]}")
                return True
            await asyncio.sleep(0.5)
        self._log(f"Results URL not seen within {timeout}s | stuck={str(tab.url)[:60]!r}", level=30)
        return False

    def _build_keyword_escalation(self, keywords: str, target: VideoTarget) -> list[str]:
        base = (keywords or target.search_keywords or target.title_hint or "").strip()
        title = (target.title_hint or "").strip()
        words = base.split()
        out: list[str] = []
        for length in range(len(words), 0, -1):
            out.append(" ".join(words[:length]))
        if title and title not in out:
            out.append(title)
        if base and base not in out:
            out.insert(0, base)
        seen: set[str] = set()
        result: list[str] = []
        for q in out:
            q = q.strip()
            if q and q not in seen:
                seen.add(q)
                result.append(q)
        return result[:6] or [base or title or "youtube"]

    # ------------------------------------------------------------------
    # EXACT MATCH DISCOVERY
    # Single JS call returns all card data as JSON string — bypasses
    # card.apply() RemoteObject bug. Clicking via video_id in href (JS click).
    # ------------------------------------------------------------------
    async def _discover_exact_match(self, tab: Tab, target: VideoTarget) -> bool:
        import json as _json

        max_passes = self._rng.randint(6, 12)
        seen_hrefs: set[str] = set()

        for pass_num in range(1, max_passes + 1):
            self._log(f"Scanning results pass {pass_num}/{max_passes}")

            # Single JS call — returns JSON string (avoids RemoteObject for complex objects)
            cards_data = await self._js_get_all_cards(tab)

            for item in cards_data:
                title = item.get("title", "")
                channel = item.get("channel", "")
                href = item.get("href", "")

                if not href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                # Check exact match OR video_id match in href
                is_match = exact_match(title, channel, target)
                if not is_match and target.video_id:
                    is_match = target.video_id in href

                if not is_match:
                    continue

                self._log(f"Exact match | title={title!r} channel={channel!r} href={href[:60]}")
                # Click via JS href — most reliable, no element reference needed
                vid_id = target.video_id or ""
                clicked = False
                if vid_id:
                    try:
                        result = await tab.evaluate(
                            f"""
                            (() => {{
                                var a = document.querySelector('a[href*="{vid_id}"]');
                                if (a) {{ a.click(); return true; }}
                                return false;
                            }})()
                            """,
                            return_by_value=True,
                        )
                        clicked = bool(result) if isinstance(result, bool) else (
                            bool(getattr(result, "value", False))
                        )
                    except Exception:
                        pass
                if not clicked and href:
                    # Fallback: navigate directly
                    try:
                        full_href = href if href.startswith("http") else f"https://www.youtube.com{href}"
                        await asyncio.wait_for(tab.get(full_href), timeout=15.0)
                        clicked = True
                    except Exception:
                        pass
                if clicked:
                    # Wait for tab.url to contain /watch (JS click triggers navigation)
                    self._log("JS click fired — waiting for /watch in tab.url...")
                    nav_deadline = time.monotonic() + 20.0
                    while time.monotonic() < nav_deadline:
                        current_url = str(tab.url or "")
                        if "/watch" in current_url:
                            self._log(f"Navigation confirmed | url={current_url[:70]}")
                            break
                        await asyncio.sleep(0.5)
                    else:
                        # JS click did not navigate — try direct tab.get
                        self._log("JS click navigation timeout — forcing tab.get()")
                        full_href = href if href.startswith("http") else f"https://www.youtube.com{href}"
                        try:
                            await asyncio.wait_for(tab.get(full_href), timeout=20.0)
                            self._log(f"Direct nav done | url={str(tab.url)[:70]}")
                        except Exception as e:
                            self._log(f"Direct nav error: {e}", level=30)
                    await self.human_delay(1.5, 3.0)
                    return True

            await slow_scroll(tab, self._rng)
            await self.human_delay(0.8, 1.6)

        return False

    async def _js_get_all_cards(self, tab: Tab) -> list[dict]:
        """
        Single JS IIFE that collects all result card data and returns as JSON string.
        String return → Python str → json.loads() — avoids RemoteObject for complex objects.
        """
        import json as _json
        from behavior.youtube.human_engine import _extract_str
        try:
            result = await tab.evaluate(
                """
                (() => {
                    var sels = [
                        'ytd-video-renderer',
                        'ytd-rich-item-renderer',
                        'ytd-compact-video-renderer'
                    ];
                    var seen = new Set();
                    var out = [];
                    for (var s = 0; s < sels.length; s++) {
                        var els = document.querySelectorAll(sels[s]);
                        for (var i = 0; i < els.length && out.length < 30; i++) {
                            var el = els[i];
                            var titleEl = el.querySelector('#video-title, a#video-title, a#video-title-link');
                            var channelEl = el.querySelector('#channel-name a, ytd-channel-name a');
                            var link = (titleEl && titleEl.tagName === 'A' ? titleEl : null)
                                || el.querySelector('a[href*="/watch"]');
                            var href = link ? (link.href || link.getAttribute('href') || '') : '';
                            if (!href || seen.has(href)) continue;
                            seen.add(href);
                            out.push({
                                title: (titleEl ? (titleEl.getAttribute('title') || titleEl.innerText || '').trim() : ''),
                                channel: (channelEl ? (channelEl.innerText || channelEl.textContent || '').trim() : ''),
                                href: href
                            });
                        }
                    }
                    return JSON.stringify(out);
                })()
                """,
                return_by_value=True,
            )
            json_str = _extract_str(result)
            if json_str:
                data = _json.loads(json_str)
                if isinstance(data, list):
                    return data
        except Exception as exc:
            self._log(f"_js_get_all_cards error: {exc}", level=30)
        return []

    # ------------------------------------------------------------------
    # HOMEPAGE / SUGGESTED BROWSE
    # ------------------------------------------------------------------
    async def browse_homepage(self, tab: Tab, target: VideoTarget) -> bool:
        self._log("Scanning desktop homepage feed")
        await self._ensure_youtube_home_robust(tab)

        for pass_num in range(1, self._rng.randint(4, 9) + 1):
            self._log(f"Homepage scan pass {pass_num}")
            if await self._scan_feed_exact(tab, target, "homepage_feed_video"):
                return True
            await self.scroll_feed(tab, self._rng.randint(1, 3))
            await self.human_delay(1.5, 3.5)
        return False

    async def browse_suggested(self, tab: Tab, target: VideoTarget) -> bool:
        self._log("Suggested-video discovery (desktop)")
        await self._ensure_youtube_home_robust(tab)

        decoy = await self.pick_random_video(tab, "homepage_feed_video")
        if not decoy:
            return False

        self._log("Opening decoy video")
        await self.human_click(decoy)
        await self.human_delay(3.0, 6.0)
        await self.wait_player_ready(tab)
        await self._watch_brief(tab, self._rng.uniform(8.0, 25.0))

        for pass_num in range(1, self._rng.randint(3, 7) + 1):
            self._log(f"Suggested sidebar scan {pass_num}")
            if await self._scan_feed_exact(tab, target, "suggested_video"):
                return True
            await tab.evaluate(
                """
                () => {
                    const el = document.querySelector('#related') ||
                        document.querySelector('ytd-watch-next-secondary-results-renderer');
                    if (el) el.scrollTop += 450;
                }
                """,
                return_by_value=True,
            )
            await self.human_delay(1.0, 2.5)
        return False

    async def _scan_feed_exact(self, tab: Tab, target: VideoTarget, link_key: str) -> bool:
        links = await self._resolver.find_all_links(tab, link_key)
        for link in links:
            href = await self.read_href(link)
            title = await self.read_title(link)
            channel = await self._channel_near_link(link)
            if self.matches_target(href, title, target, channel):
                await self.human_click(link)
                await self.human_delay(2.0, 4.5)
                return True
        return False

    async def _channel_near_link(self, link: Element) -> str:
        try:
            ch = await link.apply(
                """
                (el) => {
                    const root = el.closest(
                        'ytd-video-renderer, ytd-rich-item-renderer, ytd-compact-video-renderer'
                    ) || el.parentElement;
                    const ch = root?.querySelector('#channel-name, ytd-channel-name, .ytd-channel-name');
                    return (ch?.innerText || ch?.textContent || '').trim();
                }
                """,
                return_by_value=True,
            )
            return str(ch or "")
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # WATCH
    # ------------------------------------------------------------------
    async def watch(
        self,
        tab: Tab,
        planned_seconds: float,
        engagement_plan: list[dict[str, Any]],
        result_callbacks: dict[str, Any],
    ) -> None:
        """
        Gaussian watch-time with random engagement at natural timestamps.

        Chunk sizes are Gaussian-distributed around watch_chunk_min + (max-min)/2
        so the session feels organic — not a uniform metronome.
        Engagement events fire at their planned 'at' timestamp ± small jitter.
        """
        # Wait for real video to start before beginning the watch session
        await self.wait_for_video_start(tab, timeout=45.0, skip_ads=True)

        elapsed = 0.0
        next_idx = 0
        chunk_mean = (self._ctx.watch_chunk_min + self._ctx.watch_chunk_max) / 2
        chunk_sigma = (self._ctx.watch_chunk_max - self._ctx.watch_chunk_min) / 4

        while elapsed < planned_seconds:
            # Gaussian chunk size — clamped to [min, remaining]
            raw_chunk = self._rng.gauss(chunk_mean, chunk_sigma)
            chunk = min(
                max(self._ctx.watch_chunk_min, raw_chunk),
                planned_seconds - elapsed,
            )
            await self._watch_brief(tab, chunk)
            elapsed += chunk

            # Fire all engagement actions due at this elapsed time
            while next_idx < len(engagement_plan) and engagement_plan[next_idx]["at"] <= elapsed:
                # Small jitter: ±1-3s on engagement timestamp (human imprecision)
                jitter = self._rng.uniform(0.5, 2.5)
                await asyncio.sleep(jitter)
                await self._run_engagement(tab, engagement_plan[next_idx], result_callbacks)
                next_idx += 1

            # 22% chance of organic micro-interaction between chunks
            if self._rng.random() < 0.22:
                await self.micro_interaction(tab)

    async def _watch_brief(self, tab: Tab, seconds: float) -> None:
        remaining = seconds
        pause_threshold = self._ctx.pause_probability
        while remaining > 0:
            step = min(remaining, self._rng.uniform(2.5, 8.0))
            await asyncio.sleep(step)
            remaining -= step

            roll = self._rng.random()
            if roll < pause_threshold:
                await self.pause_playback(tab)
            elif roll < pause_threshold + 0.08:
                delta = self._rng.choice([-10, -5, 5, 10, 10])
                await self.seek_relative(tab, delta)
            elif roll < 0.24:
                speed = self._rng.choice((1.0, 1.0, 1.25))
                await self.change_settings(tab, playback_speed=speed)
            elif roll < 0.30:
                direction = self._rng.choice(("up", "down"))
                await self.adjust_volume(tab, direction)

            current = await self.get_current_time(tab)
            if current is not None:
                self._log(f"Playback position {self.format_timestamp(current)}")

    async def _run_engagement(
        self,
        tab: Tab,
        action: dict[str, Any],
        callbacks: dict[str, Any],
    ) -> None:
        action_type = action["type"]
        stamp = self.format_timestamp(action["at"])
        self._log(f"Stealth engagement at ~{stamp} | {action_type}")

        if action_type == "like" and not callbacks.get("liked"):
            await self.human_delay(0.8, 2.0)
            callbacks["liked"] = await self.like(tab)
        elif action_type == "subscribe" and not callbacks.get("subscribed"):
            await self.human_delay(1.0, 3.0)
            callbacks["subscribed"] = await self.subscribe(tab)
        elif action_type == "bell" and not callbacks.get("bell"):
            await self.human_delay(1.0, 2.5)
            callbacks["bell"] = await self.toggle_bell(tab)
        elif action_type == "comment" and not callbacks.get("commented"):
            text = str(action.get("text", ""))
            if text:
                await self.human_delay(1.0, 2.5)
                callbacks["commented"] = await self.post_comment(tab, text)

    # ------------------------------------------------------------------
    # ENGAGEMENT
    # ------------------------------------------------------------------
    async def like(self, tab: Tab) -> bool:
        # Ad gate: wait for real video to start before engaging
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self._focus_player(tab)
        await self.human_delay(0.6, 1.8)
        button = await self._resolver.find(tab, "like_button", timeout=5.0)
        if not button:
            await self.record_selector_failure("like_button")
            return False
        await self.human_click(button)
        self._log("Liked video")
        return True

    async def dislike(self, tab: Tab) -> bool:
        # Ad gate
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self._focus_player(tab)
        await self.human_delay(0.6, 1.8)
        button = await self._resolver.find(tab, "dislike_button", timeout=5.0)
        if not button:
            await self.record_selector_failure("dislike_button")
            return False
        await self.human_click(button)
        self._log("Disliked video")
        return True

    async def subscribe(self, tab: Tab) -> bool:
        # Ad gate
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self.human_delay(0.8, 2.0)
        button = await self._resolver.find(tab, "subscribe_button", timeout=5.0)
        if not button:
            await self.record_selector_failure("subscribe_button")
            return False

        label = await button.apply(
            "(el) => (el.getAttribute('aria-label') || el.innerText || '').trim()",
            return_by_value=True,
        )
        if label and "subscribed" in str(label).lower():
            self._log("Already subscribed")
            return False

        await self.human_click(button)
        self._log("Subscribed to channel")
        return True

    async def toggle_bell(self, tab: Tab) -> bool:
        # Ad gate
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self.human_delay(0.7, 1.6)
        button = await self._resolver.find(tab, "bell_button", timeout=4.0)
        if not button:
            await self.record_selector_failure("bell_button")
            return False
        await self.human_click(button)
        self._log("Toggled notification bell")
        return True

    async def change_settings(
        self,
        tab: Tab,
        *,
        playback_speed: Optional[float] = None,
        quality: Optional[str] = None,
        toggle_autoplay: Optional[bool] = None,
    ) -> bool:
        # Ad gate: ALL settings changes happen ONLY after real video starts
        await self.wait_for_video_start(tab, timeout=45.0, skip_ads=True)
        await self._focus_player(tab)
        await self.human_delay(0.5, 1.2)
        settings = await self._resolver.find(tab, "settings_button", timeout=4.0)
        if not settings:
            await self.record_selector_failure("settings_button")
            return False

        await self.human_click(settings)
        await self.human_delay(0.6, 1.2)

        if playback_speed is not None:
            speed_item = await self._resolver.find(tab, "playback_speed_menu", timeout=3.0)
            if speed_item:
                await self.human_click(speed_item)
                await self.human_delay(0.5, 1.0)
                label = f"{playback_speed}x" if playback_speed != 1.0 else "Normal"
                await self._click_menu_by_text(tab, label)
                self._log(f"Playback speed via menu: {playback_speed}x")
                return True

        if quality:
            await self._click_menu_by_text(tab, "Quality")
            await self.human_delay(0.5, 1.0)
            await self._click_menu_by_text(tab, quality)
            self._log(f"Quality set via menu: {quality}")
            return True

        if toggle_autoplay is not None:
            autoplay = await self._resolver.find(tab, "autoplay_toggle", timeout=2.0)
            if autoplay:
                await self.human_click(autoplay)
                self._log("Autoplay toggled via menu")
                return True

        await tab.send(cdp.input_.dispatch_key_event("keyDown", key="Escape", code="Escape"))
        await tab.send(cdp.input_.dispatch_key_event("keyUp", key="Escape", code="Escape"))
        return True

    async def _click_menu_by_text(self, tab: Tab, text: str) -> bool:
        try:
            clicked = await tab.evaluate(
                f"""
                (() => {{
                    const want = {text!r}.toLowerCase();
                    const items = [...document.querySelectorAll('.ytp-menuitem, .ytp-menuitem-label')];
                    const hit = items.find(i => (i.innerText || '').toLowerCase().includes(want));
                    if (hit) {{ hit.click(); return true; }}
                    return false;
                }})()
                """,
                return_by_value=True,
            )
            return bool(clicked)
        except Exception:
            return False

    async def pause_playback(self, tab: Tab) -> bool:
        await self._focus_player(tab)
        await self.send_key(tab, DESKTOP_SHORTCUTS["pause"])
        current = await self.get_current_time(tab)
        stamp = self.format_timestamp(current or 0)

        paused = await tab.evaluate(
            "(() => { const v = document.querySelector('video'); return v ? v.paused : null; })()",
            return_by_value=True,
        )
        if hasattr(paused, "value"):
            paused = paused.value
        if paused:
            pause_len = self._rng.uniform(1.0, 2.5)
            self._log(f"Paused video at {stamp} (shortcut k)")
            await asyncio.sleep(pause_len)
            await self.send_key(tab, DESKTOP_SHORTCUTS["pause"])
            self._log(f"Resumed after {pause_len:.1f}s")
        return True

    async def seek_relative(self, tab: Tab, delta_seconds: int) -> bool:
        await self._focus_player(tab)
        presses = max(1, abs(delta_seconds) // 10)
        key = DESKTOP_SHORTCUTS["forward"] if delta_seconds > 0 else DESKTOP_SHORTCUTS["rewind"]
        before = await self.get_current_time(tab)

        for _ in range(presses):
            await self.send_key(tab, key)
            await asyncio.sleep(self._rng.uniform(0.08, 0.18))

        after = await self.get_current_time(tab)
        direction = "forward" if delta_seconds > 0 else "backward"
        self._log(
            f"Seek {direction} via {key} x{presses} | "
            f"{self.format_timestamp(before or 0)} -> {self.format_timestamp(after or 0)}"
        )
        return True

    async def adjust_volume(self, tab: Tab, direction: str) -> bool:
        await self._focus_player(tab)
        arrow = "ArrowUp" if direction == "up" else "ArrowDown"
        steps = self._rng.randint(1, 3)
        for _ in range(steps):
            await tab.send(cdp.input_.dispatch_key_event("keyDown", key=arrow, code=arrow))
            await tab.send(cdp.input_.dispatch_key_event("keyUp", key=arrow, code=arrow))
            await asyncio.sleep(self._rng.uniform(0.06, 0.14))
        self._log(f"Volume {direction} x{steps} (arrow keys)")
        return True

    async def scroll_feed(self, tab: Tab, scrolls: int) -> None:
        for index in range(scrolls):
            delta = self._rng.randint(4, 14)
            if self._rng.random() < 0.85:
                try:
                    await tab.evaluate(
                        f"() => window.scrollBy(0, {delta * 80})", return_by_value=True
                    )
                except Exception:
                    await tab.scroll_down(delta)
            else:
                try:
                    await tab.evaluate(
                        f"() => window.scrollBy(0, -{self._rng.randint(2, 6) * 80})",
                        return_by_value=True,
                    )
                except Exception:
                    await tab.scroll_up(self._rng.randint(2, 6))
            self._log(f"Smooth scroll ({index + 1}/{scrolls}) delta={delta}")
            await self.human_delay(0.8, 2.2)

    async def post_comment(self, tab: Tab, text: str) -> bool:
        self._log(f"Posting comment | len={len(text)}")
        section = await self._resolver.find(tab, "comments_section", timeout=4.0)
        if section:
            try:
                await section.scroll_into_view()
            except Exception:
                await tab.scroll_down(self._rng.randint(8, 16))

        box = await self._resolver.find(tab, "comment_box", timeout=6.0)
        if box:
            await self.human_click(box)

        input_el = await self._resolver.find(tab, "comment_input", timeout=8.0)
        if not input_el:
            await self.record_selector_failure("comment_input")
            return False

        await self.human_click(input_el)
        await self.human_type(input_el, text)
        await self.human_delay(0.8, 2.0)

        submit = await self._resolver.find(tab, "comment_submit", timeout=5.0)
        if not submit:
            return False
        await self.human_click(submit)
        self._log("Comment submitted")
        return True

    async def _focus_player(self, tab: Tab) -> None:
        player = await self._resolver.find(tab, "player", timeout=3.0)
        if player:
            try:
                await self.human_click(player)
            except Exception:
                pass
        else:
            await tab.evaluate(
                "() => document.querySelector('.html5-video-player')?.click()",
                return_by_value=True,
            )
