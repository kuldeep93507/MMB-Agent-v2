"""
Mobile YouTube interaction — human-emulation layer (Android).

Uses ytm- selectors, touch taps, and finger swipes.
No set_value — smart-wait + send_keys + tab.get() fallback where needed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from urllib.parse import quote_plus

from nodriver import cdp
from nodriver.core.element import Element
from nodriver.core.tab import Tab

from behavior.youtube.base import YouTubeInteraction
from behavior.youtube.human_engine import exact_match, press_enter, wait_for_player
from behavior.youtube.types import ElementNotFoundError, InteractionContext, PlatformKind, VideoTarget

# Semantic first, then specific mobile selectors
MOBILE_SEARCH_INPUT = (
    'input[role="combobox"]',
    'input[aria-label="Search YouTube"]',
    'input[aria-label*="Search" i]',
    'input[type="search"]',
    'input[name="search_query"]',
    'ytm-searchbox input',
)

MOBILE_RESULT_SELECTORS = (
    'ytm-video-with-context-renderer',
    'ytm-compact-video-renderer',
    '[data-style="COMPACT"] a[href*="/watch"]',
)


class MobileInteraction(YouTubeInteraction):
    """Android YouTube — hybrid nav, tap/swipe, exact-match discovery."""

    platform = PlatformKind.MOBILE

    def __init__(self, ctx: InteractionContext) -> None:
        super().__init__(ctx)

    # ------------------------------------------------------------------
    # BULLETPROOF NAVIGATION
    # Address bar attempt → 10s poll → fallback tab.get() → verify
    # ------------------------------------------------------------------
    async def _navigate_to_url(self, tab: Tab, url: str, *, label: str = "") -> bool:
        """Android-safe nav — tab.get() only, NO address bar (causes CDP crash)."""
        self._log(f"Mobile nav [{label}] | tab.get({url})")

        try:
            await asyncio.wait_for(tab.get(url), timeout=30.0)
            await asyncio.sleep(self._rng.uniform(2.0, 3.0))
        except asyncio.TimeoutError:
            self._log(f"Mobile nav [{label}] | tab.get timeout", level=30)
        except Exception as exc:
            self._log(f"Mobile nav [{label}] | tab.get error: {exc}", level=30)

        # JS URL verify
        current_url = await self._js_get_url(tab)
        if "youtube.com" in current_url:
            self._log(f"Mobile nav [{label}] | success | url={current_url[:60]}")
            return True

        self._log(f"Mobile nav [{label}] | FAILED | url={current_url[:50]!r}", level=40)
        return False

    # ------------------------------------------------------------------
    # ROBUST CONSENT DISMISSAL
    # Three-layer: JS IIFE → resolver → text-find. Verifies overlay gone.
    # ------------------------------------------------------------------
    async def dismiss_consent(self, tab: Tab) -> None:
        # Layer 1: JS IIFE
        try:
            clicked = await tab.evaluate(
                """
                (() => {
                    const texts = ['Accept all', 'I agree', 'Accept', 'Agree', 'Reject all'];
                    const dialogs = [...document.querySelectorAll(
                        'ytm-consent-bump-renderer, [class*="consent"], ytm-dialog-renderer'
                    )];
                    const pool = dialogs.length > 0
                        ? dialogs.flatMap(d => [...d.querySelectorAll('button')])
                        : [...document.querySelectorAll('button')];
                    for (const t of texts) {
                        const btn = pool.find(b => (b.innerText || '').trim().startsWith(t));
                        if (btn && btn.offsetParent !== null) { btn.click(); return t; }
                    }
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
                self._log(f"Mobile consent dismissed via JS | button={clicked!r}")
                await self.human_delay(1.2, 2.0)
                if await self._consent_overlay_gone(tab):
                    return
        except Exception:
            pass

        # Layer 2: resolver
        button = await self._resolver.find(tab, "consent_accept", timeout=3.0)
        if button:
            self._log("Dismissing mobile consent via resolver")
            try:
                await self.tap(button)
                await self.human_delay(1.2, 2.2)
                if await self._consent_overlay_gone(tab):
                    return
            except Exception as exc:
                self._log(f"Mobile consent tap failed: {exc}", level=30)

        # Layer 3: text-find
        for text in ("Accept all", "I agree", "Accept", "Agree"):
            try:
                el = await tab.find(text, best_match=True, timeout=2)
                if el:
                    await self.tap(el)
                    await self.human_delay(1.0, 2.0)
                    if await self._consent_overlay_gone(tab):
                        self._log(f"Mobile consent dismissed via text-find | text={text!r}")
                        return
            except Exception:
                continue

    async def _consent_overlay_gone(self, tab: Tab, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                present = await tab.evaluate(
                    """
                    () => {
                        const sels = ['ytm-consent-bump-renderer', '[class*="consent"]'];
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
                return True
            await asyncio.sleep(0.4)
        return False

    # ------------------------------------------------------------------
    # SEARCH — Direct Query Navigation (Stability-First)
    # Flow: ensure_home → thinking pause → direct results URL → slow scroll
    #       → verify search_query in URL → _discover_exact_match
    # NOTE: Button/input search ABANDONED — causes CDP hangs on Android.
    # ------------------------------------------------------------------
    async def search(self, tab: Tab, keywords: str, target: VideoTarget) -> bool:
        await self._ensure_youtube_home_robust(tab)

        queries = self._build_keyword_escalation(keywords, target)
        self._log(f"Mobile Direct-Query search | {len(queries)} keyword variants")

        for attempt, query in enumerate(queries, start=1):
            self._log(f"Mobile DQ attempt {attempt}/{len(queries)}: {query!r}")

            await self.dismiss_consent(tab)

            # Human "thinking time" on homepage before navigating
            think_time = self._rng.uniform(2.0, 4.0)
            self._log(f"Mobile DQ | thinking pause {think_time:.1f}s")
            await asyncio.sleep(think_time)

            # Direct results URL — no button click, no input, pure stability
            results_url = f"https://m.youtube.com/results?search_query={quote_plus(query)}"
            self._log(f"Mobile DQ | navigating to results: {results_url}")
            try:
                await asyncio.wait_for(tab.get(results_url), timeout=30.0)
            except asyncio.TimeoutError:
                self._log("Mobile DQ | tab.get timeout (30s) — next query", level=30)
                continue
            except Exception as exc:
                self._log(f"Mobile DQ | tab.get failed: {exc} — next query", level=30)
                continue

            await asyncio.sleep(self._rng.uniform(2.0, 3.5))

            # Verify search_query in URL (JS-only — no tab.url for Android)
            try:
                js_url = await tab.evaluate(
                    "(() => window.location.href)()", return_by_value=True
                )
                js_url = js_url if isinstance(js_url, str) else getattr(js_url, "value", "")
                if "search_query=" not in js_url:
                    self._log(f"Mobile DQ | URL verify failed | url={js_url[:80]!r} — next query", level=30)
                    continue
                self._log(f"Mobile DQ | URL verified | {js_url[:80]}")
            except Exception as exc:
                self._log(f"Mobile DQ | URL JS eval error: {exc} — proceeding anyway", level=30)

            # Slow scroll — hides direct URL jump, human-like
            scroll_px = int(self._rng.uniform(300, 700))
            self._log(f"Mobile DQ | slow scroll {scroll_px}px")
            try:
                await tab.evaluate(
                    f"(() => window.scrollBy({{top: {scroll_px}, behavior: 'smooth'}}))();",
                    return_by_value=True,
                )
            except Exception:
                pass
            await asyncio.sleep(self._rng.uniform(1.0, 2.0))

            # Wait for results DOM
            try:
                await self.smart_wait_any(
                    tab, MOBILE_RESULT_SELECTORS, timeout=20.0, label="dq_results"
                )
            except ElementNotFoundError:
                await self.dismiss_consent(tab)
                await asyncio.sleep(1.5)
                try:
                    await self.smart_wait_any(
                        tab, MOBILE_RESULT_SELECTORS, timeout=10.0, label="dq_results_retry"
                    )
                except ElementNotFoundError:
                    self._log("Mobile DQ | results DOM not found — next query")
                    continue

            found = await self._discover_exact_match(tab, target)
            if found:
                await self.wait_player_ready(tab)
                return True

            self._log("Mobile DQ | exact match not found — next query")

        await self.record_selector_failure(
            "video_link",
            f"mobile DQ search failed title={target.title_hint!r} channel={target.channel_name!r}",
        )
        raise ElementNotFoundError(
            f"Mobile Direct-Query search failed after {len(queries)} keywords."
        )

    async def _js_get_url(self, tab: Tab) -> str:
        """JS-only URL read — avoids CDP tab.url RemoteObject bug on Android."""
        try:
            result = await tab.evaluate("(() => window.location.href)()", return_by_value=True)
            url = result if isinstance(result, str) else getattr(result, "value", "")
            return url or ""
        except Exception:
            return str(tab.url or "")

    async def _ensure_youtube_home_robust(self, tab: Tab) -> None:
        """Bulletproof home check — JS-only URL to avoid Android RemoteObject bug."""
        current_url = await self._js_get_url(tab)
        self._log(f"mobile ensure_home | url={current_url[:60]!r}")

        needs_nav = "youtube.com" not in current_url
        on_video = "/watch" in current_url

        if needs_nav:
            success = await self._navigate_to_url(tab, "https://m.youtube.com", label="ensure_home")
            if not success:
                try:
                    await asyncio.wait_for(tab.get("https://m.youtube.com"), timeout=30.0)
                    await asyncio.sleep(3.0)
                except Exception as exc:
                    raise ElementNotFoundError(f"Cannot navigate to mobile YouTube: {exc}")
            await self.human_delay(1.5, 2.5)
            await self.dismiss_consent(tab)

        elif on_video:
            # Android: NEVER use address bar — causes CDP crash [-32001]
            # Use tab.get() directly
            self._log("mobile ensure_home | on /watch → tab.get(m.youtube.com)")
            try:
                await asyncio.wait_for(tab.get("https://m.youtube.com"), timeout=25.0)
            except Exception as exc:
                self._log(f"mobile ensure_home | tab.get error: {exc}", level=30)
            await self.human_delay(2.0, 3.0)

        else:
            self._log("mobile ensure_home | already on YouTube — skipping nav")

        # Verify YouTube DOM loaded (light check — search bar not required for Direct Query)
        try:
            await self.smart_wait_any(
                tab,
                ('ytm-app', 'ytm-pivot-bar-renderer', 'ytm-searchbox', 'input[name="search_query"]'),
                timeout=8.0,
                label="yt_dom_verify",
            )
            self._log("mobile ensure_home | YouTube DOM verified")
        except ElementNotFoundError:
            self._log("mobile ensure_home | YouTube DOM soft-verify failed — continuing anyway", level=30)

    async def _open_search_input(self, tab: Tab) -> Element | None:
        """Try to get visible search input; tap search icon if needed."""
        # First: direct look for input
        try:
            el, _ = await self.smart_wait_any(
                tab, MOBILE_SEARCH_INPUT, timeout=6.0, label="search_input_direct"
            )
            return el
        except ElementNotFoundError:
            pass

        # Tap search icon to reveal input
        icon = await self._find_search_icon(tab)
        if icon:
            await self.tap(icon)
            await self.human_delay(0.9, 1.8)
            try:
                el, _ = await self.smart_wait_any(
                    tab, MOBILE_SEARCH_INPUT, timeout=12.0, label="search_input_after_icon"
                )
                return el
            except ElementNotFoundError:
                pass

        # Semantic text-find fallback
        try:
            el = await tab.find("Search", best_match=True, timeout=3)
            if el:
                tag = await el.apply("(e) => e.tagName.toLowerCase()", return_by_value=True)
                if str(tag) == "input":
                    return el
                # It's an icon button — tap it
                await self.tap(el)
                await self.human_delay(0.8, 1.5)
                try:
                    result, _ = await self.smart_wait_any(
                        tab, MOBILE_SEARCH_INPUT, timeout=10.0, label="search_after_text_find"
                    )
                    return result
                except ElementNotFoundError:
                    pass
        except Exception:
            pass

        return None

    async def _find_search_icon(self, tab: Tab) -> Element | None:
        icon = await self._resolver.find(tab, "search_open", timeout=3.0)
        if icon:
            return icon
        # aria-label based
        try:
            el = await tab.select('[aria-label="Search"]', timeout=2)
            if el:
                return el
        except Exception:
            pass
        return None

    async def _clear_input(self, tab: Tab) -> None:
        """Select all + delete to clear focused input."""
        await tab.send(cdp.input_.dispatch_key_event(
            "keyDown", key="a", code="KeyA", modifiers=2, windows_virtual_key_code=65
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            "keyUp", key="a", code="KeyA", modifiers=2, windows_virtual_key_code=65
        ))
        await asyncio.sleep(self._rng.uniform(0.06, 0.14))
        await tab.send(cdp.input_.dispatch_key_event(
            "keyDown", key="Backspace", code="Backspace", windows_virtual_key_code=8
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            "keyUp", key="Backspace", code="Backspace", windows_virtual_key_code=8
        ))
        await asyncio.sleep(self._rng.uniform(0.08, 0.18))

    async def _submit_search(self, tab: Tab, search_input: Element) -> None:
        """Submit via search button tap (if visible) or Enter on input."""
        search_btn = await self._resolver.find(tab, "search_button", timeout=3.0)
        if search_btn:
            await self.tap(search_btn)
            self._log("Mobile search submitted via button tap")
            return
        # Fallback: focus + Enter
        try:
            await search_input.focus()
            await asyncio.sleep(self._rng.uniform(0.15, 0.3))
        except Exception:
            pass
        await press_enter(tab)
        self._log("Mobile search submitted via Enter")

    async def _wait_for_results_url(self, tab: Tab, timeout: float = 15.0) -> bool:
        """Poll tab.url until 'search_query' appears — confirms search navigation."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            url = str(tab.url or "")
            if "search_query" in url:
                self._log(f"Mobile results URL confirmed | url={url[:80]}")
                return True
            await asyncio.sleep(0.5)
        self._log(f"Mobile results URL not seen | stuck={str(tab.url)[:60]!r}", level=30)
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
        return result[:5] or [base or title or "youtube"]

    # ------------------------------------------------------------------
    # EXACT MATCH DISCOVERY — swipe scan
    # ------------------------------------------------------------------
    async def _discover_exact_match(self, tab: Tab, target: VideoTarget) -> bool:
        """
        Surgical Scan: Scroll → collect cards → match title_hint + channel_name → tap.

        Match priority:
          1. exact_match(title, channel, target) — semantic title + channel check
          2. video_id in href — direct ID match (no title needed if no title_hint set)

        Bug fix: previously skipped video_id match when title_hint was set, even
        if the card title also matched — now title+id both required when title_hint set.
        """
        max_passes = self._rng.randint(8, 14)
        seen: set[str] = set()

        for pass_num in range(1, max_passes + 1):
            self._log(f"Mobile surgical scan pass {pass_num}/{max_passes}")

            # Single JS call to get all card data — avoids RemoteObject bug
            cards_json = await self._js_get_all_mobile_cards(tab)

            for item in cards_json:
                href    = item.get("href", "")
                title   = item.get("title", "")
                channel = item.get("channel", "")
                card_key = href or title[:40]

                if not card_key or card_key in seen:
                    continue
                seen.add(card_key)

                # Priority 1: semantic exact match (title + channel)
                semantic_match = exact_match(title, channel, target)

                # Priority 2: video_id direct match
                id_match = bool(target.video_id and href and target.video_id in href)

                # Decision:
                # - If title_hint set: require semantic OR (id_match AND title loosely present)
                # - If no title_hint: id_match alone is enough
                is_match = False
                if semantic_match:
                    is_match = True
                elif id_match:
                    if target.title_hint:
                        # Still require some title similarity when title_hint set
                        hint_words = target.title_hint.lower().split()[:3]
                        title_lower = title.lower()
                        if any(w in title_lower for w in hint_words):
                            is_match = True
                    else:
                        is_match = True

                if not is_match:
                    continue

                self._log(
                    f"Mobile exact match | title={title!r} channel={channel!r} href={href[:50]}"
                )

                # Click via JS href (most reliable — no element reference needed)
                clicked = False
                if target.video_id:
                    try:
                        result = await tab.evaluate(
                            f"""
                            (() => {{
                                var a = document.querySelector('a[href*="{target.video_id}"]');
                                if (a) {{ a.click(); return true; }}
                                return false;
                            }})()
                            """,
                            return_by_value=True,
                        )
                        clicked = bool(result) if isinstance(result, bool) else bool(
                            getattr(result, "value", False)
                        )
                    except Exception:
                        pass

                if not clicked and href:
                    # Fallback: direct tab.get navigation
                    try:
                        full_href = href if href.startswith("http") else f"https://m.youtube.com{href}"
                        await asyncio.wait_for(tab.get(full_href), timeout=20.0)
                        clicked = True
                    except Exception as exc:
                        self._log(f"Mobile fallback nav failed: {exc}", level=30)

                if clicked:
                    await self.human_delay(2.0, 4.0)
                    if await wait_for_player(tab, timeout=22.0):
                        return True
                    self._log("Player not ready after tap — continuing scan", level=30)

            await self.swipe_up(tab, distance=self._rng.randint(220, 420))
            await self.human_delay(1.2, 2.6)

        return False

    async def _js_get_all_mobile_cards(self, tab: Tab) -> list[dict]:
        """
        Single JS IIFE: collect all mobile result card data as JSON string.
        Avoids RemoteObject bug — returns parsed list of {title, channel, href}.
        """
        import json as _json
        try:
            result = await tab.evaluate(
                """
                (() => {
                    var sels = [
                        'ytm-video-with-context-renderer',
                        'ytm-compact-video-renderer',
                        'ytm-shelf-renderer ytm-compact-video-renderer',
                    ];
                    var seen = new Set();
                    var out = [];
                    for (var s = 0; s < sels.length; s++) {
                        var els = document.querySelectorAll(sels[s]);
                        for (var i = 0; i < els.length && out.length < 25; i++) {
                            var el = els[i];
                            var link = el.querySelector('a[href*="/watch"]')
                                || el.closest('a[href*="/watch"]');
                            var href = link ? (link.href || link.getAttribute('href') || '') : '';
                            if (!href || seen.has(href)) continue;
                            seen.add(href);
                            var titleEl = el.querySelector(
                                '.media-item-headline, h3, .compact-media-item-headline, ' +
                                'span[role="text"], [class*="headline"], [class*="title"]'
                            );
                            var chEl = el.querySelector(
                                '.subhead, .media-item-meta, [class*="byline"], [class*="channel"]'
                            );
                            out.push({
                                href: href,
                                title: (
                                    titleEl ? (titleEl.innerText || titleEl.textContent || '').trim().split('\\n')[0]
                                    : link ? (link.getAttribute('aria-label') || '').trim()
                                    : ''
                                ),
                                channel: chEl
                                    ? (chEl.innerText || chEl.textContent || '').trim().split('\\n')[0]
                                    : '',
                            });
                        }
                    }
                    return JSON.stringify(out);
                })()
                """,
                return_by_value=True,
            )
            from behavior.youtube.human_engine import _extract_str
            json_str = _extract_str(result)
            if json_str:
                data = _json.loads(json_str)
                if isinstance(data, list):
                    return data
        except Exception as exc:
            self._log(f"_js_get_all_mobile_cards error: {exc}", level=30)
        return []

    async def _collect_result_cards(self, tab: Tab) -> list[Element]:
        cards: list[Element] = []
        seen: set[int] = set()
        for sel in MOBILE_RESULT_SELECTORS:
            try:
                found = await tab.select_all(sel, timeout=2)
                for item in found or []:
                    if id(item) not in seen:
                        seen.add(id(item))
                        cards.append(item)
            except Exception:
                continue
        return cards

    async def _card_key(self, card: Element) -> str:
        try:
            key = await card.apply(
                """
                (el) => {
                    const link = el.querySelector('a[href*="/watch"]') || el.closest('a[href*="/watch"]');
                    const href = link?.href || link?.getAttribute('href') || '';
                    const title = (el.innerText || el.textContent || '').trim().slice(0, 80);
                    return href + '::' + title;
                }
                """,
                return_by_value=True,
            )
            return str(key or id(card))
        except Exception:
            return str(id(card))

    async def _read_mobile_card(self, card: Element) -> dict[str, str]:
        try:
            data = await card.apply(
                """
                (el) => {
                    const root = el.closest(
                        'ytm-video-with-context-renderer, ytm-compact-video-renderer'
                    ) || el;
                    const link = root.querySelector('a[href*="/watch"]') || el.closest('a[href*="/watch"]');
                    const titleNode = root.querySelector(
                        '.media-item-headline, h3, .compact-media-item-headline, ' +
                        'span[role="text"], [class*="headline"]'
                    );
                    const channelNode = root.querySelector(
                        '.subhead, .media-item-meta, .ytm-badge-and-byline-renderer, ' +
                        '[class*="byline"], [class*="channel"]'
                    );
                    return {
                        title: (
                            titleNode?.innerText || titleNode?.textContent ||
                            link?.getAttribute('aria-label') || root.innerText || ''
                        ).trim().split('\\n')[0],
                        channel: (channelNode?.innerText || channelNode?.textContent || '')
                            .trim().split('\\n')[0],
                        href: link?.href || link?.getAttribute('href') || '',
                    };
                }
                """,
                return_by_value=True,
            )
            if isinstance(data, dict):
                return {k: str(v or "") for k, v in data.items()}
        except Exception:
            pass
        return {"title": "", "channel": "", "href": ""}

    async def _touch_click_center(self, tab: Tab, element: Element) -> None:
        try:
            position = await element.get_position()
            if position and position.center:
                x, y = position.center
                x += self._rng.uniform(-6, 6)
                y += self._rng.uniform(-4, 4)
                await tab.send(cdp.input_.dispatch_touch_event(
                    type_="touchStart",
                    touch_points=[cdp.input_.TouchPoint(x=x, y=y)],
                ))
                await asyncio.sleep(self._rng.uniform(0.05, 0.12))
                await tab.send(cdp.input_.dispatch_touch_event(
                    type_="touchEnd", touch_points=[]
                ))
                await asyncio.sleep(self._rng.uniform(0.08, 0.18))
                return
        except Exception:
            pass
        await self.tap(element)

    # ------------------------------------------------------------------
    # HOMEPAGE / SUGGESTED BROWSE
    # ------------------------------------------------------------------
    async def browse_homepage(self, tab: Tab, target: VideoTarget) -> bool:
        self._log("Scanning mobile homepage feed")
        await self._ensure_youtube_home_robust(tab)

        for pass_num in range(1, self._rng.randint(4, 8) + 1):
            self._log(f"Mobile homepage pass {pass_num}")
            if await self._scan_feed_exact(tab, target, "homepage_feed_video"):
                return True
            await self.swipe_up(tab)
            await self.human_delay(1.2, 2.8)
        return False

    async def browse_suggested(self, tab: Tab, target: VideoTarget) -> bool:
        self._log("Mobile suggested-video path")
        await self._ensure_youtube_home_robust(tab)

        decoy = await self.pick_random_video(tab, "homepage_feed_video")
        if not decoy:
            return False

        await self._touch_click_center(tab, decoy)
        await self.human_delay(3.0, 5.5)
        await self.wait_player_ready(tab)
        await self._watch_brief(tab, self._rng.uniform(6.0, 18.0))

        for pass_num in range(1, self._rng.randint(3, 6) + 1):
            self._log(f"Mobile related pass {pass_num}")
            if await self._scan_feed_exact(tab, target, "suggested_video"):
                return True
            await self.swipe_up(tab, distance=self._rng.randint(280, 520))
            await self.human_delay(1.0, 2.2)
        return False

    async def _scan_feed_exact(self, tab: Tab, target: VideoTarget, link_key: str) -> bool:
        links = await self._resolver.find_all_links(tab, link_key)
        for link in links:
            href = await self.read_href(link)
            title = await self.read_title(link)
            channel = await self._channel_near_link(link)
            if self.matches_target(href, title, target, channel):
                await self._touch_click_center(tab, link)
                await self.human_delay(2.0, 4.0)
                return True
        return False

    async def _channel_near_link(self, link: Element) -> str:
        try:
            ch = await link.apply(
                """
                (el) => {
                    const root = el.closest(
                        'ytm-video-with-context-renderer, ytm-compact-video-renderer'
                    ) || el.parentElement;
                    const ch = root?.querySelector(
                        '.subhead, .media-item-meta, .ytm-badge-and-byline-renderer, [class*="byline"]'
                    );
                    return (ch?.innerText || ch?.textContent || '').trim().split('\\n')[0];
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
        Gaussian watch-time with ad gate + Gaussian chunk sizes.
        Engagement fires at natural timestamps with ±jitter.
        """
        # Ad gate: wait for real video, skip any pre-roll ads
        await self.wait_for_video_start(tab, timeout=45.0, skip_ads=True)

        elapsed = 0.0
        next_idx = 0
        chunk_mean  = (self._ctx.watch_chunk_min + self._ctx.watch_chunk_max) / 2
        chunk_sigma = (self._ctx.watch_chunk_max - self._ctx.watch_chunk_min) / 4

        while elapsed < planned_seconds:
            raw_chunk = self._rng.gauss(chunk_mean, chunk_sigma)
            chunk = min(
                max(self._ctx.watch_chunk_min, raw_chunk),
                planned_seconds - elapsed,
            )
            await self._watch_brief(tab, chunk)
            elapsed += chunk

            while next_idx < len(engagement_plan) and engagement_plan[next_idx]["at"] <= elapsed:
                jitter = self._rng.uniform(0.5, 2.5)
                await asyncio.sleep(jitter)
                await self._run_engagement(tab, engagement_plan[next_idx], result_callbacks)
                next_idx += 1

            if self._rng.random() < 0.25:
                await self.micro_interaction(tab)

    async def _watch_brief(self, tab: Tab, seconds: float) -> None:
        remaining = seconds
        pause_threshold = self._ctx.pause_probability
        while remaining > 0:
            step = min(remaining, self._rng.uniform(2.0, 7.0))
            await asyncio.sleep(step)
            remaining -= step

            roll = self._rng.random()
            if roll < pause_threshold:
                await self.pause_playback(tab)
            elif roll < pause_threshold + 0.10:
                delta = self._rng.choice([-15, -8, 8, 15])
                await self.seek_relative(tab, delta)
            elif roll < 0.30:
                speed = self._rng.choice((1.0, 1.0, 1.25))
                await self.change_settings(tab, playback_speed=speed)
            elif roll < 0.36:
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
            await self.human_delay(0.7, 1.8)
            callbacks["liked"] = await self.like(tab)
        elif action_type == "subscribe" and not callbacks.get("subscribed"):
            await self.human_delay(1.0, 2.5)
            callbacks["subscribed"] = await self.subscribe(tab)
        elif action_type == "bell" and not callbacks.get("bell"):
            await self.human_delay(0.8, 2.0)
            callbacks["bell"] = await self.toggle_bell(tab)
        elif action_type == "comment" and not callbacks.get("commented"):
            text = str(action.get("text", ""))
            if text:
                await self.human_delay(0.8, 2.0)
                callbacks["commented"] = await self.post_comment(tab, text)

    # ------------------------------------------------------------------
    # ENGAGEMENT
    # ------------------------------------------------------------------
    async def like(self, tab: Tab) -> bool:
        # Ad gate: real video must be playing before engagement
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self._tap_player_for_controls(tab)
        await self.human_delay(0.6, 1.6)
        button = await self._resolver.find(tab, "like_button", timeout=6.0)
        if not button:
            await self.swipe_up(tab, distance=200)
            button = await self._resolver.find(tab, "like_button", timeout=4.0)
        if not button:
            await self.record_selector_failure("like_button")
            return False
        await self.tap(button)
        self._log("Liked video (tap)")
        return True

    async def dislike(self, tab: Tab) -> bool:
        # Ad gate
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self._tap_player_for_controls(tab)
        await self.human_delay(0.6, 1.6)
        button = await self._resolver.find(tab, "dislike_button", timeout=6.0)
        if not button:
            await self.swipe_up(tab, distance=200)
            button = await self._resolver.find(tab, "dislike_button", timeout=4.0)
        if not button:
            await self.record_selector_failure("dislike_button")
            return False
        await self.tap(button)
        self._log("Disliked video (tap)")
        return True

    async def subscribe(self, tab: Tab) -> bool:
        # Ad gate
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self.human_delay(0.8, 2.0)
        button = await self._resolver.find(tab, "subscribe_button", timeout=6.0)
        if not button:
            await self.swipe_up(tab, distance=180)
            button = await self._resolver.find(tab, "subscribe_button", timeout=4.0)
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

        await self.tap(button)
        self._log("Subscribed (tap)")
        return True

    async def toggle_bell(self, tab: Tab) -> bool:
        # Ad gate
        await self.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
        await self.human_delay(0.7, 1.5)
        button = await self._resolver.find(tab, "bell_button", timeout=5.0)
        if not button:
            await self.record_selector_failure("bell_button")
            return False
        await self.tap(button)
        self._log("Toggled notification bell (tap)")
        return True

    async def change_settings(
        self,
        tab: Tab,
        *,
        playback_speed: Optional[float] = None,
        quality: Optional[str] = None,
        toggle_autoplay: Optional[bool] = None,
    ) -> bool:
        # Ad gate: settings only after real video starts
        await self.wait_for_video_start(tab, timeout=45.0, skip_ads=True)
        await self._tap_player_for_controls(tab)
        await self.human_delay(0.5, 1.0)
        settings = await self._resolver.find(tab, "settings_button", timeout=5.0)
        if not settings:
            await self.record_selector_failure("settings_button")
            return False

        await self.tap(settings)
        await self.human_delay(0.6, 1.2)

        if playback_speed is not None:
            speed_menu = await self._resolver.find(tab, "playback_speed_menu", timeout=4.0)
            if speed_menu:
                await self.tap(speed_menu)
                await self.human_delay(0.6, 1.2)
                label = f"{playback_speed}x" if playback_speed != 1.0 else "Normal"
                await self._tap_menu_text(tab, label)
                self._log(f"Mobile playback speed via menu: {playback_speed}x")
                return True

        if quality:
            await self._tap_menu_text(tab, "Quality")
            await self.human_delay(0.5, 1.0)
            await self._tap_menu_text(tab, quality)
            return True

        return True

    async def _tap_menu_text(self, tab: Tab, text: str) -> bool:
        try:
            hit = await tab.find(text, best_match=True, timeout=3)
            if hit:
                await self.tap(hit)
                return True
        except Exception:
            pass
        return False

    async def pause_playback(self, tab: Tab) -> bool:
        await self._tap_player_for_controls(tab)
        current = await self.get_current_time(tab)
        stamp = self.format_timestamp(current or 0)

        player = await self._resolver.find(tab, "player", timeout=2.0)
        if player:
            await self.tap(player)
            await asyncio.sleep(self._rng.uniform(0.3, 0.6))

        paused = await tab.evaluate(
            "() => { const v = document.querySelector('video'); return v ? v.paused : null; }",
            return_by_value=True,
        )
        if not paused:
            if player:
                await self.tap(player)
            paused = True

        if paused:
            pause_len = self._rng.uniform(1.5, 5.5)
            self._log(f"Paused video at {stamp} (tap toggle)")
            await asyncio.sleep(pause_len)
            if player:
                await self.tap(player)
            self._log(f"Resumed after {pause_len:.1f}s")
        return True

    async def seek_relative(self, tab: Tab, delta_seconds: int) -> bool:
        before = await self.get_current_time(tab)
        await self._tap_player_for_controls(tab)

        width = int(self._ctx.identity.get("screen_width") or 390)
        height = int(self._ctx.identity.get("screen_height") or 844)
        y = height * 0.45

        if delta_seconds > 0:
            x = width * 0.78
            taps = max(1, abs(delta_seconds) // 10)
            for _ in range(taps):
                await self._touch_at(tab, x, y)
                await asyncio.sleep(self._rng.uniform(0.15, 0.35))
                await self._touch_at(tab, x, y)
                await asyncio.sleep(self._rng.uniform(0.2, 0.4))
        else:
            x = width * 0.22
            taps = max(1, abs(delta_seconds) // 10)
            for _ in range(taps):
                await self._touch_at(tab, x, y)
                await asyncio.sleep(self._rng.uniform(0.15, 0.35))
                await self._touch_at(tab, x, y)
                await asyncio.sleep(self._rng.uniform(0.2, 0.4))

        after = await self.get_current_time(tab)
        direction = "forward" if delta_seconds > 0 else "backward"
        self._log(
            f"Seek {direction} via double-tap x{taps} | "
            f"{self.format_timestamp(before or 0)} -> {self.format_timestamp(after or 0)}"
        )
        return True

    async def _touch_at(self, tab: Tab, x: float, y: float) -> None:
        await tab.send(cdp.input_.dispatch_touch_event(
            type_="touchStart",
            touch_points=[cdp.input_.TouchPoint(x=x, y=y)],
        ))
        await asyncio.sleep(self._rng.uniform(0.04, 0.09))
        await tab.send(cdp.input_.dispatch_touch_event(type_="touchEnd", touch_points=[]))

    async def adjust_volume(self, tab: Tab, direction: str) -> bool:
        await self._tap_player_for_controls(tab)
        steps = self._rng.randint(1, 2)
        for _ in range(steps):
            await self.swipe_up(tab, distance=80 if direction == "up" else -80)
            await asyncio.sleep(self._rng.uniform(0.15, 0.3))
        self._log(f"Volume gesture {direction} x{steps}")
        return True

    async def scroll_feed(self, tab: Tab, scrolls: int) -> None:
        for index in range(scrolls):
            await self.swipe_up(tab)
            self._log(f"Swipe scroll ({index + 1}/{scrolls})")
            await self.human_delay(0.7, 1.8)

    async def swipe_up(self, tab: Tab, *, distance: Optional[int] = None) -> None:
        dist = distance or self._rng.randint(320, 580)
        width = int(self._ctx.identity.get("screen_width") or 390)
        height = int(self._ctx.identity.get("screen_height") or 844)

        start_x = self._rng.uniform(width * 0.35, width * 0.65)
        start_y = self._rng.uniform(height * 0.62, height * 0.78)
        end_x = start_x + self._rng.uniform(-18, 18)
        end_y = max(40, start_y - dist)

        await tab.send(cdp.input_.dispatch_touch_event(
            type_="touchStart",
            touch_points=[cdp.input_.TouchPoint(x=start_x, y=start_y)],
        ))
        steps = self._rng.randint(8, 16)
        for step in range(1, steps + 1):
            ratio = step / steps
            cx = start_x + (end_x - start_x) * ratio
            cy = start_y + (end_y - start_y) * ratio
            await tab.send(cdp.input_.dispatch_touch_event(
                type_="touchMove",
                touch_points=[cdp.input_.TouchPoint(x=cx, y=cy)],
            ))
            await asyncio.sleep(self._rng.uniform(0.008, 0.02))

        await tab.send(cdp.input_.dispatch_touch_event(type_="touchEnd", touch_points=[]))

    async def post_comment(self, tab: Tab, text: str) -> bool:
        self._log(f"Mobile comment | len={len(text)}")
        header = await self._resolver.find(tab, "comments_section", timeout=5.0)
        if header:
            await self.tap(header)
            await self.human_delay(1.0, 2.0)

        box = await self._resolver.find(tab, "comment_box", timeout=6.0)
        if box:
            await self.tap(box)

        input_el = await self._resolver.find(tab, "comment_input", timeout=8.0)
        if not input_el:
            await self.record_selector_failure("comment_input")
            return False

        await self.tap(input_el)
        await self.human_type(input_el, text)
        await self.human_delay(0.8, 1.8)

        submit = await self._resolver.find(tab, "comment_submit", timeout=5.0)
        if not submit:
            return False
        await self.tap(submit)
        self._log("Mobile comment submitted")
        return True

    async def _tap_player_for_controls(self, tab: Tab) -> None:
        player = await self._resolver.find(tab, "player", timeout=3.0)
        if player:
            await self.tap(player)
        else:
            await self._touch_at(
                tab,
                int(self._ctx.identity.get("screen_width") or 390) * 0.5,
                int(self._ctx.identity.get("screen_height") or 844) * 0.35,
            )
        await asyncio.sleep(self._rng.uniform(0.3, 0.7))
