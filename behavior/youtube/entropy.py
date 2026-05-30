"""
Behavioral Entropy System — Anti-Detection Navigation Engine
============================================================
YouTube के AI track करता है behavioral clusters. अगर 100 profiles same path
follow करें तो flag हो जाते हैं। यह module हर profile को एक unique
'personality' देता है और तीन अलग entry paths में से randomly चुनता है।

Path A: Keyword Search  → random keyword pool → search → scroll → click target
Path B: Channel Search  → search channel name → channel page → find video
Path C: Homepage Browse → homepage → organic scroll → title match → click

Extra features:
- 20% chance 'Human Mistake' — wrong video first, 15-30s watch, back
- Strict metadata verification before click (video_id + channel name)
- Interaction jitter: typing speed, scroll amount, pause timing
- Per-profile deterministic personality (from profile_id hash)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from nodriver.core.tab import Tab

from behavior.youtube.human_engine import (
    _extract_str,
    _js_find_selector,
    _js_find_text,
    exact_match,
    slow_scroll,
)
from behavior.youtube.types import VideoTarget, YouTubeManagerError

# ---------------------------------------------------------------------------
# KEYWORD POOL — 15 different keyword variants for the target video
# यहाँ generic pool है; caller target-specific keywords inject करता है
# ---------------------------------------------------------------------------
GENERIC_KEYWORD_POOL: tuple[str, ...] = (
    "{title}",
    "{title} {channel}",
    "{channel} {title}",
    "{channel} latest video",
    "{channel} new video",
    "{title} full video",
    "{channel} YouTube",
    "{title} watch online",
    "{channel} official",
    "{channel} channel",
    "{channel} videos",
    "watch {title}",
    "{title} {channel} video",
    "{channel}",
    "{title} strategy",
)

# Channel-specific search terms for Path B
CHANNEL_SEARCH_VARIATIONS: tuple[str, ...] = (
    "{channel}",
    "{channel} channel",
    "{channel} YouTube channel",
    "{channel} official",
    "{channel} videos",
)


class EntryPathKind(str, Enum):
    KEYWORD_SEARCH = "path_a_keyword"
    CHANNEL_SEARCH = "path_b_channel"
    HOMEPAGE_BROWSE = "path_c_homepage"


class PersonalityType(str, Enum):
    IMPATIENT = "impatient"   # Fast typer, minimal scroll, direct
    CAUTIOUS = "cautious"     # Slow, lots of scroll, hesitates before click
    CURIOUS = "curious"       # Scrolls a lot, reads descriptions, explores
    DIRECT = "direct"         # Efficient, straight to search, quick click


# Path weights per personality type — (A, B, C)
# NOTE: Path C (homepage) is a "bonus organic" path — it tries homepage first
# but always falls back to Path A if video not found there.
# Weights reflect initial path CHOICE, not final success path.
_PERSONALITY_PATH_WEIGHTS: dict[PersonalityType, tuple[float, float, float]] = {
    PersonalityType.IMPATIENT: (0.75, 0.20, 0.05),
    PersonalityType.CAUTIOUS:  (0.50, 0.30, 0.20),
    PersonalityType.CURIOUS:   (0.50, 0.20, 0.30),
    PersonalityType.DIRECT:    (0.70, 0.25, 0.05),
}


@dataclass
class BehavioralPersonality:
    """Per-profile personality — deterministically derived from profile_id hash."""
    profile_id: str
    personality_type: PersonalityType
    path_weights: tuple[float, float, float]  # (A, B, C)
    typing_speed: str       # "fast" | "normal" | "slow"
    scroll_speed: str       # "fast" | "normal" | "slow"
    mistake_probability: float  # 0.0 - 0.35
    pre_click_pause: tuple[float, float]  # (min, max) seconds
    search_linger: tuple[float, float]    # time to spend on results page before clicking

    @classmethod
    def from_profile_id(cls, profile_id: str) -> "BehavioralPersonality":
        """Derive stable personality from profile_id SHA-256 hash."""
        digest = hashlib.sha256(profile_id.encode()).hexdigest()
        # Use different slices for different attributes — stable across runs
        p_byte = int(digest[0:2], 16)   # 0-255
        t_byte = int(digest[2:4], 16)
        s_byte = int(digest[4:6], 16)
        m_byte = int(digest[6:8], 16)
        l_byte = int(digest[8:10], 16)

        personality_types = list(PersonalityType)
        personality = personality_types[p_byte % len(personality_types)]

        typing_speeds = ["fast", "fast", "normal", "normal", "normal", "slow"]
        scroll_speeds = ["fast", "normal", "normal", "normal", "slow", "slow"]
        typing_speed = typing_speeds[t_byte % len(typing_speeds)]
        scroll_speed = scroll_speeds[s_byte % len(scroll_speeds)]

        # mistake_probability: 10% to 30% range
        mistake_prob = 0.10 + (m_byte / 255.0) * 0.20

        # pre_click_pause: 0.5s to 4.5s
        pause_min = 0.5 + (l_byte / 255.0) * 2.0
        pause_max = pause_min + 1.0 + (p_byte / 255.0) * 1.5

        # search_linger: time on results page before clicking (2s to 18s)
        linger_min = 2.0 + (t_byte / 255.0) * 6.0
        linger_max = linger_min + 2.0 + (s_byte / 255.0) * 10.0

        path_weights = _PERSONALITY_PATH_WEIGHTS[personality]

        return cls(
            profile_id=profile_id,
            personality_type=personality,
            path_weights=path_weights,
            typing_speed=typing_speed,
            scroll_speed=scroll_speed,
            mistake_probability=mistake_prob,
            pre_click_pause=(round(pause_min, 2), round(pause_max, 2)),
            search_linger=(round(linger_min, 2), round(linger_max, 2)),
        )


def _build_keyword_variants(target: VideoTarget) -> list[str]:
    """
    Build keyword list from GENERIC_KEYWORD_POOL + target-specific keywords.
    Shuffled so each profile picks differently.
    """
    title = target.title_hint or target.video_id or "video"
    channel = target.channel_name or "channel"
    base = target.search_keywords or title

    variants: list[str] = []
    for template in GENERIC_KEYWORD_POOL:
        kw = template.format(title=title, channel=channel)
        if kw and kw not in variants:
            variants.append(kw)

    # Always include the raw search_keywords as first priority
    if base and base not in variants:
        variants.insert(0, base)

    return variants


def _build_channel_keyword_variants(target: VideoTarget) -> list[str]:
    channel = target.channel_name or ""
    if not channel:
        return [target.search_keywords or target.title_hint or ""]
    variants = []
    for template in CHANNEL_SEARCH_VARIATIONS:
        kw = template.format(channel=channel)
        if kw and kw not in variants:
            variants.append(kw)
    return variants


# ---------------------------------------------------------------------------
# STRICT METADATA VERIFICATION
# ---------------------------------------------------------------------------

def _verify_card(card: dict, target: VideoTarget) -> bool:
    """
    Strict two-factor verification:
    1. video_id must be in href (if target has video_id)
    2. channel name must match (if target has channel_name)
    Returns True only when BOTH pass (or when field is not provided).
    """
    href = card.get("href", "")
    title = card.get("title", "")
    channel = card.get("channel", "")

    # Factor 1: Video ID check
    id_ok = True
    if target.video_id:
        id_ok = target.video_id in href

    # Factor 2: Channel name check
    channel_ok = True
    if target.channel_name:
        target_ch_norm = target.channel_name.strip().lower()
        card_ch_norm = channel.strip().lower()
        # Allow partial match (channel names can have slight variations)
        channel_ok = (
            target_ch_norm in card_ch_norm or
            card_ch_norm in target_ch_norm or
            _fuzzy_channel_match(target_ch_norm, card_ch_norm)
        )

    # Factor 3: Title hint check (soft — only if ID not available)
    title_ok = True
    if not target.video_id and target.title_hint:
        title_ok = exact_match(title, channel, target)

    return id_ok and channel_ok and title_ok


def _fuzzy_channel_match(a: str, b: str) -> bool:
    """Check if two channel names share significant words."""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return False
    overlap = words_a & words_b
    return len(overlap) / min(len(words_a), len(words_b)) >= 0.5


# ---------------------------------------------------------------------------
# JS UTILITIES — card scraping, navigation wait
# ---------------------------------------------------------------------------

async def _js_get_cards(tab: Tab) -> list[dict]:
    """Single IIFE — collect all visible result cards as JSON string."""
    import json as _json
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
                    for (var i = 0; i < els.length && out.length < 40; i++) {
                        var el = els[i];
                        var titleEl = el.querySelector(
                            '#video-title, a#video-title, a#video-title-link'
                        );
                        var channelEl = el.querySelector(
                            '#channel-name a, ytd-channel-name a, ' +
                            'yt-formatted-string.ytd-channel-name'
                        );
                        var link = (titleEl && titleEl.tagName === 'A' ? titleEl : null)
                            || el.querySelector('a[href*="/watch"]');
                        var href = link
                            ? (link.href || link.getAttribute('href') || '')
                            : '';
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        out.push({
                            title: (titleEl
                                ? (titleEl.getAttribute('title') ||
                                   titleEl.innerText || '').trim()
                                : ''),
                            channel: (channelEl
                                ? (channelEl.innerText ||
                                   channelEl.textContent || '').trim()
                                : ''),
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
    except Exception:
        pass
    return []


async def _js_get_channel_videos(tab: Tab) -> list[dict]:
    """Scrape video cards from a channel page."""
    import json as _json
    try:
        result = await tab.evaluate(
            """
            (() => {
                var out = [];
                var els = document.querySelectorAll(
                    'ytd-grid-video-renderer, ytd-rich-item-renderer, ytd-video-renderer'
                );
                for (var i = 0; i < els.length && out.length < 30; i++) {
                    var el = els[i];
                    var titleEl = el.querySelector(
                        '#video-title, a#video-title, a#video-title-link, h3 a'
                    );
                    var href = titleEl
                        ? (titleEl.href || titleEl.getAttribute('href') || '')
                        : '';
                    if (!href) continue;
                    out.push({
                        title: (titleEl
                            ? (titleEl.getAttribute('title') ||
                               titleEl.innerText || '').trim()
                            : ''),
                        channel: '',
                        href: href
                    });
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
    except Exception:
        pass
    return []


async def _js_click_by_href(tab: Tab, video_id: str, href: str) -> bool:
    """Click video link via JS — by video_id first, fallback direct nav."""
    clicked = False
    if video_id:
        try:
            result = await tab.evaluate(
                f"""
                (() => {{
                    var a = document.querySelector('a[href*="{video_id}"]');
                    if (a) {{ a.click(); return true; }}
                    return false;
                }})()
                """,
                return_by_value=True,
            )
            raw = getattr(result, "value", result)
            clicked = bool(raw)
        except Exception:
            pass

    if not clicked and href:
        try:
            full = href if href.startswith("http") else f"https://www.youtube.com{href}"
            await asyncio.wait_for(tab.get(full), timeout=20.0)
            clicked = True
        except Exception:
            pass

    return clicked


async def _wait_for_watch_url(tab: Tab, timeout: float = 20.0) -> bool:
    """Poll tab.url until /watch appears or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = str(tab.url or "")
        if "/watch" in url:
            return True
        await asyncio.sleep(0.5)
    return False


async def _wait_for_channel_url(tab: Tab, timeout: float = 15.0) -> bool:
    """Poll tab.url until /@channel or /channel/ appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        url = str(tab.url or "")
        if "/@" in url or "/channel/" in url or "/c/" in url:
            return True
        await asyncio.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# JITTER HELPERS
# ---------------------------------------------------------------------------

def _jitter_scroll_amount(rng: Any, personality: BehavioralPersonality) -> int:
    """Random scroll amount 200-1200px based on personality."""
    if personality.scroll_speed == "fast":
        return rng.randint(400, 1200)
    elif personality.scroll_speed == "slow":
        return rng.randint(200, 500)
    else:
        return rng.randint(250, 800)


async def _jitter_scroll(tab: Tab, rng: Any, personality: BehavioralPersonality,
                          passes: int = 1) -> None:
    """Scroll with jitter — random amount and speed."""
    for _ in range(passes):
        px = _jitter_scroll_amount(rng, personality)
        try:
            await tab.evaluate(
                f"(() => window.scrollBy({{top: {px}, behavior: 'smooth'}}))()",
                return_by_value=True,
            )
        except Exception:
            try:
                await tab.evaluate(
                    f"(() => window.scrollBy(0, {px}))()",
                    return_by_value=True,
                )
            except Exception:
                pass
        delay = rng.uniform(0.6, 2.5) if personality.scroll_speed == "slow" else rng.uniform(0.3, 1.2)
        await asyncio.sleep(delay)


async def _jitter_pause_before_click(rng: Any, personality: BehavioralPersonality) -> None:
    """Variable pause before clicking a video — simulates 'reading the title'."""
    mn, mx = personality.pre_click_pause
    await asyncio.sleep(rng.uniform(mn, mx))


async def _jitter_linger_on_results(rng: Any, personality: BehavioralPersonality) -> None:
    """Time spent on results page browsing before target click."""
    mn, mx = personality.search_linger
    linger = rng.uniform(mn, mx)
    await asyncio.sleep(linger)


# ---------------------------------------------------------------------------
# WRONG VIDEO (Human Mistake) — 20% chance
# ---------------------------------------------------------------------------

async def _do_wrong_video_detour(
    tab: Tab,
    rng: Any,
    strategy: Any,  # YouTubeInteraction
    log: Any,
) -> None:
    """
    Click a random wrong video, watch 15-30s, then go back.
    Called with 20% probability before the real target click.
    """
    log("HumanMistake | picking wrong video to click first")
    cards = await _js_get_cards(tab)
    if not cards:
        log("HumanMistake | no cards found, skipping detour")
        return

    # Pick a random card (prefer ones in first 5 results — more natural)
    pool = cards[:min(5, len(cards))]
    wrong = rng.choice(pool)
    w_href = wrong.get("href", "")
    w_title = wrong.get("title", "unknown")

    if not w_href:
        log("HumanMistake | no href on wrong card, skipping")
        return

    log(f"HumanMistake | clicking wrong video: {w_title!r}")

    # Click it
    w_vid_id = ""
    if "/watch?v=" in w_href:
        w_vid_id = w_href.split("/watch?v=")[-1].split("&")[0][:11]

    clicked = await _js_click_by_href(tab, w_vid_id, w_href)
    if not clicked:
        log("HumanMistake | click failed, aborting detour")
        return

    # Wait for video to start
    await _wait_for_watch_url(tab, timeout=12.0)
    await asyncio.sleep(rng.uniform(1.5, 3.0))

    # Watch 15-30 seconds
    watch_time = rng.uniform(15.0, 30.0)
    log(f"HumanMistake | watching wrong video for {watch_time:.0f}s")
    await asyncio.sleep(watch_time)

    # Go back to search results
    log("HumanMistake | going back to search results")
    try:
        await tab.evaluate("(() => window.history.back())()", return_by_value=True)
    except Exception:
        pass
    await asyncio.sleep(rng.uniform(2.0, 4.0))

    # Verify we're back on search/results page
    url = str(tab.url or "")
    log(f"HumanMistake | returned | url={url[:60]}")


# ---------------------------------------------------------------------------
# MAIN ENGINE
# ---------------------------------------------------------------------------

class BehavioralEntropyEngine:
    """
    Orchestrates unpredictable navigation paths for each profile.
    हर profile का अपना unique behavioral pattern होता है।
    """

    def __init__(
        self,
        profile_id: str,
        strategy: Any,   # YouTubeInteraction (DesktopInteraction / MobileInteraction)
        rng: Any,
        log: Any,
    ) -> None:
        self._profile_id = profile_id
        self._strategy = strategy
        self._rng = rng
        self._log = log
        self._personality = BehavioralPersonality.from_profile_id(profile_id)
        self._log(
            f"[Entropy] Personality={self._personality.personality_type.value} "
            f"typing={self._personality.typing_speed} "
            f"scroll={self._personality.scroll_speed} "
            f"mistake_prob={self._personality.mistake_probability:.0%}"
        )

    def select_path(self) -> EntryPathKind:
        """Pick entry path based on personality weights."""
        paths = [
            EntryPathKind.KEYWORD_SEARCH,
            EntryPathKind.CHANNEL_SEARCH,
            EntryPathKind.HOMEPAGE_BROWSE,
        ]
        weights = list(self._personality.path_weights)
        chosen = self._rng.choices(paths, weights=weights, k=1)[0]
        self._log(f"[Entropy] Entry path selected: {chosen.value}")
        return chosen

    async def execute(self, tab: Tab, target: VideoTarget) -> bool:
        """
        Main entry — pick path and execute. Returns True if target video found & clicked.

        Fallback chain:
          Path B → Path A (if channel search fails)
          Path C → Path A (if homepage browse fails — very common for specific videos)
          Path A → returns False (caller will try legacy search)
        """
        path = self.select_path()

        if path == EntryPathKind.KEYWORD_SEARCH:
            self._log("[Entropy] Executing Path A (Keyword Search)")
            return await self._path_a_keyword_search(tab, target)

        elif path == EntryPathKind.CHANNEL_SEARCH:
            self._log("[Entropy] Executing Path B (Channel Search)")
            result = await self._path_b_channel_search(tab, target)
            if not result:
                self._log("[Entropy] Path B failed — navigating home, falling back to Path A")
                await self._ensure_home(tab)
                return await self._path_a_keyword_search(tab, target)
            return result

        else:  # Path C
            self._log("[Entropy] Executing Path C (Homepage Browse)")
            result = await self._path_c_homepage_browse(tab, target)
            if not result:
                self._log("[Entropy] Path C failed — video not on homepage, falling back to Path A")
                await self._ensure_home(tab)
                return await self._path_a_keyword_search(tab, target)
            return result

    async def _ensure_home(self, tab: Tab) -> None:
        """Navigate back to YouTube homepage before fallback search."""
        try:
            url = str(tab.url or "")
            if "youtube.com" in url and "/watch" not in url and "/results" not in url:
                return  # already on homepage-like page
            await asyncio.wait_for(tab.get("https://www.youtube.com"), timeout=15.0)
            await asyncio.sleep(self._rng.uniform(1.5, 3.0))
        except Exception as e:
            self._log(f"[Entropy] _ensure_home error: {e}", level=30)

    # -----------------------------------------------------------------------
    # PATH A: Keyword Search
    # -----------------------------------------------------------------------

    async def _path_a_keyword_search(self, tab: Tab, target: VideoTarget) -> bool:
        """
        Random keyword from pool → search → jitter scroll → strict verify → click.
        """
        variants = _build_keyword_variants(target)
        # Shuffle so different profiles try different keywords first
        shuffled = list(variants)
        self._rng.shuffle(shuffled)

        # Pick 1 primary keyword + 2 fallbacks
        primary_kw = shuffled[0]
        fallbacks = shuffled[1:3]

        self._log(f"[PathA] Primary keyword: {primary_kw!r}")
        self._log(f"[PathA] Fallback keywords: {fallbacks}")

        for attempt, kw in enumerate([primary_kw] + fallbacks, start=1):
            self._log(f"[PathA] Search attempt {attempt}: {kw!r}")

            # Use strategy's search method to type and submit
            success = await self._search_with_keyword(tab, kw, target)
            if success:
                return True

            self._log(f"[PathA] Attempt {attempt} failed — trying next keyword")
            # Go back to YouTube home before next attempt
            try:
                await tab.evaluate("(() => window.history.back())()", return_by_value=True)
                await asyncio.sleep(self._rng.uniform(1.5, 3.0))
            except Exception:
                pass

        return False

    async def _search_with_keyword(self, tab: Tab, keyword: str, target: VideoTarget) -> bool:
        """
        Type keyword → submit → jitter on results → maybe wrong video → find target.
        """
        from behavior.youtube.desktop import DESKTOP_SEARCH_SELECTORS, DESKTOP_RESULT_SELECTORS
        from behavior.youtube.types import ElementNotFoundError

        # Find search bar
        try:
            search_el, matched_sel = await self._strategy.smart_wait_any(
                tab, DESKTOP_SEARCH_SELECTORS, timeout=15.0, label="entropy_search_bar"
            )
        except ElementNotFoundError:
            self._log("[PathA] Search bar not found")
            return False

        # Click → clear → type with jitter typing speed
        await self._strategy.human_click(search_el)
        await asyncio.sleep(self._rng.uniform(0.3, 0.7))
        await self._strategy._clear_input(tab)
        await asyncio.sleep(self._rng.uniform(0.1, 0.3))

        # Jitter typing speed via character delay
        await self._jitter_type(tab, keyword)
        await asyncio.sleep(self._rng.uniform(0.4, 1.2))

        # Submit
        submitted = await self._strategy._submit_search(tab, search_el)
        if not submitted:
            return False

        # Wait for results
        results_ok = await self._strategy._wait_for_results_url(tab, timeout=15.0)
        if not results_ok:
            self._log("[PathA] Results URL not confirmed, checking DOM anyway")

        try:
            await self._strategy.smart_wait_any(
                tab, DESKTOP_RESULT_SELECTORS, timeout=15.0, label="entropy_results"
            )
        except ElementNotFoundError:
            self._log("[PathA] Results DOM not found")
            return False

        # Linger on results page (human reads titles)
        await _jitter_linger_on_results(self._rng, self._personality)

        # Random scroll before looking for target
        scroll_passes = self._rng.randint(0, 3)
        if scroll_passes > 0:
            self._log(f"[PathA] Pre-target scroll: {scroll_passes} passes")
            await _jitter_scroll(tab, self._rng, self._personality, passes=scroll_passes)

        # 20% chance: click wrong video first
        if self._rng.random() < self._personality.mistake_probability:
            await _do_wrong_video_detour(tab, self._rng, self._strategy, self._log)
            # After detour, wait for results page to return
            await asyncio.sleep(self._rng.uniform(1.0, 2.5))

        # Now find the real target — multi-pass scroll with strict verification
        return await self._find_target_in_results(tab, target)

    async def _find_target_in_results(self, tab: Tab, target: VideoTarget) -> bool:
        """Scroll results page, strictly verify each card, click target."""
        max_passes = self._rng.randint(5, 12)
        seen_hrefs: set[str] = set()

        for pass_num in range(1, max_passes + 1):
            self._log(f"[PathA] Scanning results pass {pass_num}/{max_passes}")
            cards = await _js_get_cards(tab)

            for card in cards:
                href = card.get("href", "")
                if not href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                # STRICT VERIFICATION: video_id + channel name
                if not _verify_card(card, target):
                    continue

                self._log(
                    f"[PathA] Target verified | title={card.get('title')!r} "
                    f"channel={card.get('channel')!r} href={href[:60]}"
                )

                # Pause before click (reading title)
                await _jitter_pause_before_click(self._rng, self._personality)

                vid_id = target.video_id or ""
                clicked = await _js_click_by_href(tab, vid_id, href)

                if clicked:
                    nav_ok = await _wait_for_watch_url(tab, timeout=20.0)
                    self._log(f"[PathA] Clicked & navigated | watch_url={nav_ok}")
                    await asyncio.sleep(self._rng.uniform(1.0, 2.5))
                    return True

            # Scroll down for more results
            await _jitter_scroll(tab, self._rng, self._personality, passes=1)

        self._log(f"[PathA] Target not found after {max_passes} passes")
        return False

    async def _jitter_type(self, tab: Tab, text: str) -> None:
        """
        Type text using strategy's human_type_tab (CDP keystrokes — works in YouTube search bar).
        Personality-based delay is applied via pre/post pauses since human_type_tab has its own jitter.
        """
        # Add pre-typing hesitation based on personality
        if self._personality.typing_speed == "slow":
            await asyncio.sleep(self._rng.uniform(0.5, 1.2))
        elif self._personality.typing_speed == "fast":
            await asyncio.sleep(self._rng.uniform(0.05, 0.2))

        # Use strategy's CDP-based typing (reliable in React inputs)
        await self._strategy.human_type_tab(tab, text)

        # Post-typing pause — slow typers pause more before submitting
        if self._personality.typing_speed == "slow":
            await asyncio.sleep(self._rng.uniform(0.4, 1.0))
        elif self._rng.random() < 0.15:
            # Occasional pause as if re-reading what was typed
            await asyncio.sleep(self._rng.uniform(0.3, 0.8))

    # -----------------------------------------------------------------------
    # PATH B: Channel Search
    # -----------------------------------------------------------------------

    async def _path_b_channel_search(self, tab: Tab, target: VideoTarget) -> bool:
        """
        Search for channel name → click channel from results → find video on channel page.
        """
        self._log(f"[PathB] Channel search | channel={target.channel_name!r}")
        if not target.channel_name:
            self._log("[PathB] No channel_name in target — skipping")
            return False

        channel_variants = _build_channel_keyword_variants(target)
        channel_kw = self._rng.choice(channel_variants)
        self._log(f"[PathB] Searching channel: {channel_kw!r}")

        from behavior.youtube.desktop import DESKTOP_SEARCH_SELECTORS, DESKTOP_RESULT_SELECTORS
        from behavior.youtube.types import ElementNotFoundError

        # Find and use search bar
        try:
            search_el, _ = await self._strategy.smart_wait_any(
                tab, DESKTOP_SEARCH_SELECTORS, timeout=15.0, label="pathb_search_bar"
            )
        except ElementNotFoundError:
            self._log("[PathB] Search bar not found")
            return False

        await self._strategy.human_click(search_el)
        await asyncio.sleep(self._rng.uniform(0.3, 0.6))
        await self._strategy._clear_input(tab)
        await self._jitter_type(tab, channel_kw)
        await asyncio.sleep(self._rng.uniform(0.5, 1.0))

        submitted = await self._strategy._submit_search(tab, search_el)
        if not submitted:
            return False

        await asyncio.sleep(self._rng.uniform(1.5, 3.0))

        # Look for channel result and click it
        channel_clicked = await self._click_channel_from_results(tab, target)
        if not channel_clicked:
            self._log("[PathB] Channel not found in results")
            return False

        # Wait for channel page
        await asyncio.sleep(self._rng.uniform(2.0, 4.0))
        channel_page_ok = await _wait_for_channel_url(tab, timeout=15.0)
        self._log(f"[PathB] Channel page loaded: {channel_page_ok} | url={str(tab.url)[:60]}")

        # Navigate to Videos tab
        await self._navigate_to_videos_tab(tab)
        await asyncio.sleep(self._rng.uniform(1.5, 3.0))

        # Scroll and find target video
        return await self._find_target_on_channel_page(tab, target)

    async def _click_channel_from_results(self, tab: Tab, target: VideoTarget) -> bool:
        """Find and click the channel card in search results."""
        target_ch = (target.channel_name or "").strip().lower()
        deadline = time.monotonic() + 12.0

        while time.monotonic() < deadline:
            try:
                result = await tab.evaluate(
                    f"""
                    (() => {{
                        var channelEls = document.querySelectorAll(
                            'ytd-channel-renderer, ytd-vertical-list-item-renderer'
                        );
                        var target = {json.dumps(target_ch)};
                        for (var i = 0; i < channelEls.length; i++) {{
                            var el = channelEls[i];
                            var name = (el.querySelector('#channel-title, #text, .channel-name')
                                || el).innerText || '';
                            if (name.toLowerCase().includes(target) || target.includes(name.toLowerCase().trim())) {{
                                var link = el.querySelector('a#main-link, a[href*="/@"], a[href*="/channel/"]');
                                if (link) {{ link.click(); return true; }}
                            }}
                        }}
                        return false;
                    }})()
                    """,
                    return_by_value=True,
                )
                raw = getattr(result, "value", result)
                if raw:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.8)

        return False

    async def _navigate_to_videos_tab(self, tab: Tab) -> None:
        """Click the Videos tab on a channel page."""
        try:
            result = await tab.evaluate(
                """
                (() => {
                    var tabs = document.querySelectorAll(
                        'yt-tab-shape, ytd-tab-renderer, [tab-title], #tabsContent tp-yt-paper-tab'
                    );
                    for (var i = 0; i < tabs.length; i++) {
                        var t = tabs[i].innerText || tabs[i].getAttribute('tab-title') || '';
                        if (t.toLowerCase().trim() === 'videos') {
                            tabs[i].click();
                            return true;
                        }
                    }
                    return false;
                })()
                """,
                return_by_value=True,
            )
            raw = getattr(result, "value", result)
            self._log(f"[PathB] Videos tab click: {raw}")
        except Exception as e:
            self._log(f"[PathB] Videos tab error: {e}", level=30)
        await asyncio.sleep(self._rng.uniform(1.0, 2.5))

    async def _find_target_on_channel_page(self, tab: Tab, target: VideoTarget) -> bool:
        """Scroll channel video grid to find and click target."""
        max_passes = self._rng.randint(4, 8)
        seen_hrefs: set[str] = set()

        for pass_num in range(1, max_passes + 1):
            self._log(f"[PathB] Channel video scan pass {pass_num}")
            cards = await _js_get_channel_videos(tab)

            for card in cards:
                href = card.get("href", "")
                if not href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                # Verify by video_id in href (title may differ on channel page)
                id_match = target.video_id and target.video_id in href
                title_match = (not target.video_id) and exact_match(
                    card.get("title", ""), "", target
                )

                if not (id_match or title_match):
                    continue

                self._log(f"[PathB] Target found on channel | href={href[:60]}")
                await _jitter_pause_before_click(self._rng, self._personality)

                clicked = await _js_click_by_href(tab, target.video_id or "", href)
                if clicked:
                    nav_ok = await _wait_for_watch_url(tab, timeout=20.0)
                    self._log(f"[PathB] Clicked | watch_url={nav_ok}")
                    await asyncio.sleep(self._rng.uniform(1.0, 2.5))
                    return True

            await _jitter_scroll(tab, self._rng, self._personality, passes=1)
            await asyncio.sleep(self._rng.uniform(0.8, 2.0))

        self._log("[PathB] Target not found on channel page")
        return False

    # -----------------------------------------------------------------------
    # PATH C: Homepage Browse
    # -----------------------------------------------------------------------

    async def _path_c_homepage_browse(self, tab: Tab, target: VideoTarget) -> bool:
        """
        Start at homepage → organic scroll → find target by title match → click.
        """
        self._log("[PathC] Homepage browse path")

        # Ensure we are on YouTube homepage
        home_url = "https://www.youtube.com"
        current = str(tab.url or "")
        if "youtube.com" not in current or "/watch" in current or "/results" in current:
            try:
                await asyncio.wait_for(tab.get(home_url), timeout=20.0)
                await asyncio.sleep(self._rng.uniform(2.0, 4.0))
            except Exception as e:
                self._log(f"[PathC] Homepage nav error: {e}", level=30)
                return False

        self._log(f"[PathC] On homepage | url={str(tab.url)[:60]}")

        # Organic scrolling — simulate browsing
        initial_scroll = self._rng.randint(1, 4)
        self._log(f"[PathC] Initial organic scroll: {initial_scroll} passes")
        await _jitter_scroll(tab, self._rng, self._personality, passes=initial_scroll)
        await asyncio.sleep(self._rng.uniform(2.0, 5.0))

        # Scan homepage feed for target
        max_passes = self._rng.randint(6, 14)
        seen_hrefs: set[str] = set()

        for pass_num in range(1, max_passes + 1):
            self._log(f"[PathC] Homepage scan pass {pass_num}/{max_passes}")
            cards = await _js_get_cards(tab)

            for card in cards:
                href = card.get("href", "")
                if not href or href in seen_hrefs:
                    continue
                seen_hrefs.add(href)

                if not _verify_card(card, target):
                    continue

                self._log(
                    f"[PathC] Target found on homepage | title={card.get('title')!r}"
                )

                # Linger — simulate reading surrounding content
                await asyncio.sleep(self._rng.uniform(1.5, 4.0))
                await _jitter_pause_before_click(self._rng, self._personality)

                vid_id = target.video_id or ""
                clicked = await _js_click_by_href(tab, vid_id, href)
                if clicked:
                    nav_ok = await _wait_for_watch_url(tab, timeout=20.0)
                    self._log(f"[PathC] Clicked | watch_url={nav_ok}")
                    await asyncio.sleep(self._rng.uniform(1.0, 2.5))
                    return True

            # Scroll to load more feed items
            await _jitter_scroll(tab, self._rng, self._personality, passes=1)
            await asyncio.sleep(self._rng.uniform(1.0, 3.0))

        self._log("[PathC] Target not found on homepage")
        return False
