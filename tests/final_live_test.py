"""
Full end-to-end live test: Orchestrator setup → Search-to-Watch → human action → 2–3 min watch.

Uses a Multilogin profile from .env or data/platform_profiles.json.
Logs every step to the console so you can observe the browser in action.

Usage:
  python tests/final_live_test.py
  python tests/final_live_test.py --platform windows
  set LIVE_TEST_PROFILE_ID=your-uuid && python tests/final_live_test.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from behavior.YouTubeManager import VideoTarget, YouTubeManager, YouTubeManagerError
from behavior.youtube.types import ProfileConfig
from core.Orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# Finance video under test
# ---------------------------------------------------------------------------

VIDEO_ID = "KjNyAVwtAUg"
SEARCH_KEYWORDS = "Best Credit Card 2026-My 1000$ monthly earn strategy ultagtaplay"
TITLE_HINT = "Best Credit Card 2026-My 1000$ monthly earn strategy"
CHANNEL_NAME = "ultagtaplay"   # used by Entropy Path B (channel search)
MIN_WATCH_SECONDS = 20
MAX_WATCH_SECONDS = 30

PROFILES_PATH = PROJECT_ROOT / "data" / "platform_profiles.json"
DEFAULT_PLATFORM = "windows"


def setup_console_logging() -> None:
    """Mirror pipeline steps to stdout."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    root = logging.getLogger("mmb.final_live_test")
    root.setLevel(logging.INFO)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
        )
        root.addHandler(handler)


def log_step(logger: logging.Logger, step: str, detail: str = "") -> None:
    msg = f"[STEP] {step}"
    if detail:
        msg = f"{msg} | {detail}"
    logger.info(msg)
    print(msg, flush=True)


def resolve_profile(platform: str) -> tuple[str, str]:
    """
    Profile ID resolution order:
      1. LIVE_TEST_PROFILE_ID env var (overrides everything)
      2. Platform-based hardcoded profiles
    Windows  → 15a65f05-da47-473f-b52a-9f615a54e4f3
    Android  → 005028b7-5d09-43d9-b252-0df11fe92f8e
    Mac      → 1ad77258-a617-4f8c-9de0-a036e9c84073
    """
    # 1. Env var override
    env_id = (os.getenv("LIVE_TEST_PROFILE_ID") or "").strip()
    if env_id:
        return env_id, platform

    # 2. Hardcoded platform defaults
    PROFILES = {
        "windows": ("15a65f05-da47-473f-b52a-9f615a54e4f3", "windows"),
        "android": ("005028b7-5d09-43d9-b252-0df11fe92f8e", "android"),
        "macos":   ("1ad77258-a617-4f8c-9de0-a036e9c84073", "macos"),
        "mac":     ("1ad77258-a617-4f8c-9de0-a036e9c84073", "macos"),
    }
    key = platform.lower().strip()
    if key in PROFILES:
        return PROFILES[key]
    # fallback to windows
    return PROFILES["windows"]


async def wait_for_playback_start(manager: YouTubeManager, tab, logger: logging.Logger, timeout: float = 90.0) -> None:
    """
    Wait until the HTML5 player reports progress (video OR ad is playing).
    Handles: autoplay blocked → click to play, ad stuck at 0 → skip, buffering.
    """
    log_step(logger, "OBSERVE", "Waiting for video playback to start...")
    deadline = time.monotonic() + timeout
    last_time: float | None = None
    zero_count = 0   # consecutive time=0 checks → trigger click-to-play
    skip_tried = False

    # First wait for /watch in URL (up to 20s) — check both tab.url and JS
    url_deadline = time.monotonic() + 20.0
    while time.monotonic() < url_deadline:
        current_url = str(tab.url or "")
        if not current_url or "/watch" not in current_url:
            # Fallback: JS-based URL read (more reliable on mobile)
            try:
                r = await tab.evaluate("(() => window.location.href)()", return_by_value=True)
                js_url = r if isinstance(r, str) else getattr(r, "value", "")
                if js_url and "/watch" in js_url:
                    current_url = js_url
            except Exception:
                pass
        if "/watch" in current_url:
            log_step(logger, "OBSERVE", f"Watch URL confirmed | {current_url[:70]}")
            break
        await asyncio.sleep(0.8)
    else:
        current_url = str(tab.url or "")
        log_step(logger, "OBSERVE", f"URL check done | url={current_url[:70]}")

    while time.monotonic() < deadline:
        try:
            await manager.strategy.wait_player_ready(tab, timeout=5.0)
        except Exception:
            pass

        # Try ad skip on every check
        if not skip_tried:
            skipped = await manager.strategy.skip_ad_if_present(tab)
            if skipped:
                log_step(logger, "OBSERVE", "Ad skipped during playback wait")
                skip_tried = False  # allow retry next round
                await asyncio.sleep(1.5)
                continue

        current = await manager.strategy.get_current_time(tab)
        duration = await manager.strategy.get_video_duration(tab)

        log_step(logger, "OBSERVE", f"Player check | time={current} duration={duration}")

        # Success: time advancing
        if current is not None and current > 0.1:
            log_step(logger, "OBSERVE",
                f"Playback started | position={manager.strategy.format_timestamp(current)} "
                f"duration={manager.strategy.format_timestamp(duration)}")
            return

        if current is not None and last_time is not None and current > last_time:
            log_step(logger, "OBSERVE", f"Playback advancing | t={current:.1f}s")
            return

        # time=0 & duration=0 — autoplay blocked → force play via YouTube API
        if (current == 0.0 or current is None) and (duration == 0.0 or duration is None):
            zero_count += 1
            if zero_count % 3 == 0:
                log_step(logger, "OBSERVE", f"time=0 x{zero_count} — forcing playVideo()...")
                try:
                    await tab.evaluate(
                        """
                        (() => {
                            // YouTube player API (most reliable)
                            var p = document.querySelector('#movie_player');
                            if (p && p.playVideo) { p.playVideo(); return 'playVideo'; }
                            // HTML5 video element
                            var v = document.querySelector('video');
                            if (v) { v.play().catch(()=>{}); return 'video.play'; }
                            // Click large play button overlay
                            var btn = document.querySelector('.ytp-large-play-button, .ytp-play-button');
                            if (btn) { btn.click(); return 'btn_clicked'; }
                            return 'no_element';
                        })()
                        """,
                        return_by_value=True,
                    )
                except Exception:
                    pass
                await asyncio.sleep(1.0)
                continue

        # time=0 but duration exists — ad loading, wait
        if current == 0.0 and duration and duration > 0:
            zero_count += 1
            if zero_count % 4 == 0:
                log_step(logger, "OBSERVE", "duration>0 but time=0 — trying ad skip...")
                await manager.strategy.skip_ad_if_present(tab)

        last_time = current
        skip_tried = False
        await asyncio.sleep(2.0)

    raise YouTubeManagerError("Video did not start playing within timeout.")


async def _js_eval(tab, code: str):
    """Safe JS evaluation — IIFE form, never hangs."""
    try:
        return await tab.evaluate(f"(() => {{ {code} }})()", return_by_value=True)
    except Exception:
        return None


async def _read_video_info(tab) -> dict:
    """Read title, channel, description via JS — no element needed."""
    try:
        result = await tab.evaluate(
            """
            (() => {
                var title = (document.querySelector('h1.ytd-video-primary-info-renderer yt-formatted-string')
                    || document.querySelector('h1 .yt-formatted-string')
                    || document.querySelector('h1')) ?.innerText || '';
                var channel = (document.querySelector('#channel-name a')
                    || document.querySelector('ytd-channel-name a')) ?.innerText || '';
                var desc = (document.querySelector('#description-inline-expander yt-formatted-string')
                    || document.querySelector('#description .yt-formatted-string')
                    || document.querySelector('#description')) ?.innerText || '';
                var likes = (document.querySelector('#segmented-like-button button[aria-label]')
                    || document.querySelector('.ytd-toggle-button-renderer[aria-label*="like"]'))
                    ?.getAttribute('aria-label') || '';
                return JSON.stringify({title: title.trim(), channel: channel.trim(),
                    desc: desc.slice(0, 300).trim(), likes: likes});
            })()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", None)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


async def _scroll_to_comments_section(tab) -> None:
    """Scroll directly to #comments element so YouTube lazy-loads them."""
    try:
        await tab.evaluate(
            """
            (() => {
                var c = document.querySelector('#comments, ytd-comments#comments');
                if (c) {
                    c.scrollIntoView({behavior: 'smooth', block: 'start'});
                } else {
                    window.scrollBy(0, 2500);
                }
            })()
            """,
            return_by_value=True,
        )
    except Exception:
        pass


async def _read_top_comments(tab) -> list:
    """Read top 5 comments via JS — tries multiple selectors for robustness."""
    SELECTORS = [
        'ytd-comment-renderer #content-text',
        '#content-text.ytd-comment-renderer',
        'yt-formatted-string#content-text',
        'ytd-comment-thread-renderer #content-text',
        '#comments ytd-comment-renderer yt-formatted-string',
        '#contents ytd-comment-renderer span',
    ]
    for sel in SELECTORS:
        try:
            sel_escaped = sel.replace("'", "\\'")
            result = await tab.evaluate(
                f"""
                (() => {{
                    var els = document.querySelectorAll('{sel_escaped}');
                    var out = [];
                    for (var i = 0; i < Math.min(els.length, 5); i++) {{
                        var t = (els[i].innerText || els[i].textContent || '').trim();
                        if (t) out.push(t.slice(0, 120));
                    }}
                    return JSON.stringify(out);
                }})()
                """,
                return_by_value=True,
            )
            raw = result if isinstance(result, str) else getattr(result, "value", None)
            if raw:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    return data
        except Exception:
            continue
    return []


async def _read_related_videos(tab) -> list:
    """Read top 5 related video titles from sidebar — tries multiple selectors."""
    try:
        result = await tab.evaluate(
            """
            (() => {
                var out = [];
                // Try multiple selector patterns for related/sidebar videos
                var patterns = [
                    'ytd-compact-video-renderer #video-title',
                    'ytd-compact-video-renderer span#video-title',
                    '#related ytd-compact-video-renderer a#thumbnail',
                    'ytd-watch-next-secondary-results-renderer ytd-compact-video-renderer #video-title',
                    '#secondary ytd-compact-video-renderer #video-title',
                    'ytd-compact-video-renderer a[href*="/watch"]',
                ];
                for (var p = 0; p < patterns.length && out.length === 0; p++) {
                    var els = document.querySelectorAll(patterns[p]);
                    for (var i = 0; i < Math.min(els.length, 5); i++) {
                        var el = els[i];
                        var t = (el.getAttribute('title') || el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim();
                        if (t && t.length > 2) out.push(t.slice(0, 100));
                    }
                }
                return JSON.stringify(out);
            })()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", None)
        if raw:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


async def _curved_scroll(tab, total_px: int, steps: int = None) -> None:
    """
    Human-like curved scroll — easing in/out, random micro-pauses, not straight.
    Simulates natural finger/mouse movement with acceleration + deceleration.
    """
    if steps is None:
        steps = random.randint(6, 14)
    # Ease-in-out curve: sin wave gives slow→fast→slow rhythm
    import math
    scrolled = 0
    for i in range(steps):
        t = i / max(steps - 1, 1)
        # Ease in-out sine curve
        ease = (1 - math.cos(math.pi * t)) / 2
        # Next point on curve
        next_pos = int(total_px * ease)
        delta = next_pos - scrolled
        if delta <= 0:
            continue
        scrolled = next_pos
        # Add small random jitter ±20px for natural feel
        jitter = random.randint(-20, 20)
        actual_delta = max(1, delta + jitter)
        try:
            await tab.evaluate(
                f"(() => window.scrollBy({{top: {actual_delta}, behavior: 'auto'}}))()",
                return_by_value=True,
            )
        except Exception:
            pass
        # Variable pause between scroll steps — mimics human hand movement
        await asyncio.sleep(random.uniform(0.04, 0.18))
    # Final micro-pause after scroll completes
    await asyncio.sleep(random.uniform(0.3, 0.8))


async def _skip_ad_if_present(tab, logger: logging.Logger) -> bool:
    """
    Detect and skip YouTube ad if skip button is visible.
    Checks multiple skip button selectors — returns True if ad was skipped.
    """
    try:
        result = await tab.evaluate(
            """
            (() => {
                var skipSels = [
                    '.ytp-skip-ad-button',
                    '.ytp-ad-skip-button',
                    'button.ytp-skip-ad-button',
                    '.ytp-ad-skip-button-modern',
                    '[class*="skip-ad"]',
                    '[class*="skip_ad"]'
                ];
                for (var i = 0; i < skipSels.length; i++) {
                    var el = document.querySelector(skipSels[i]);
                    if (el && el.offsetParent !== null) {
                        el.click();
                        return 'skipped:' + skipSels[i];
                    }
                }
                // Check if ad is playing at all
                var adBadge = document.querySelector('.ytp-ad-badge, .ytp-ad-text, .ad-showing');
                return adBadge ? 'ad_present_no_skip' : 'no_ad';
            })()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", "")
        if raw and "skipped" in raw:
            log_step(logger, "HUMAN", f"Ad skipped | selector={raw}")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            return True
        elif raw == "ad_present_no_skip":
            log_step(logger, "HUMAN", "Ad playing — no skip button yet, waiting...")
            # Wait up to 6s for skip button
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                r2 = await tab.evaluate(
                    "(() => { var el = document.querySelector('.ytp-skip-ad-button,.ytp-ad-skip-button,.ytp-ad-skip-button-modern'); return el && el.offsetParent ? 'skip' : 'wait'; })()",
                    return_by_value=True,
                )
                r2v = r2 if isinstance(r2, str) else getattr(r2, "value", "")
                if r2v == "skip":
                    await tab.evaluate(
                        "(() => { var el = document.querySelector('.ytp-skip-ad-button,.ytp-ad-skip-button,.ytp-ad-skip-button-modern'); if(el) el.click(); })()",
                        return_by_value=True,
                    )
                    log_step(logger, "HUMAN", "Ad skipped (after wait)")
                    await asyncio.sleep(1.0)
                    return True
        return False
    except Exception as e:
        log_step(logger, "HUMAN", f"Ad check error: {e}")
        return False


async def _click_bell_icon(tab, logger: logging.Logger) -> bool:
    """Click bell notification icon after subscribe."""
    try:
        result = await tab.evaluate(
            """
            (() => {
                var sels = [
                    'ytd-subscription-notification-toggle-button-renderer button',
                    '#notification-preference-button button',
                    'button[aria-label*="notification"]',
                    'button[aria-label*="Notification"]',
                    'button[aria-label*="bell"]',
                    '.ytd-subscription-notification-toggle-button-renderer',
                    'ytd-bell-icon-button-renderer button',
                ];
                for (var i = 0; i < sels.length; i++) {
                    var el = document.querySelector(sels[i]);
                    if (el && el.offsetParent !== null) {
                        el.click();
                        return 'clicked:' + sels[i];
                    }
                }
                return 'not_found';
            })()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", "")
        if raw and "clicked" in raw:
            log_step(logger, "HUMAN", f"Bell icon clicked | {raw}")
            await asyncio.sleep(random.uniform(1.0, 2.0))
            # Click "All" notifications option if menu appeared
            await tab.evaluate(
                """
                (() => {
                    var items = document.querySelectorAll('ytd-menu-service-item-renderer, tp-yt-paper-item');
                    for (var i = 0; i < items.length; i++) {
                        var t = (items[i].innerText || '').toLowerCase().trim();
                        if (t === 'all' || t === 'all notifications') { items[i].click(); return; }
                    }
                })()
                """,
                return_by_value=True,
            )
            return True
        log_step(logger, "HUMAN", "Bell icon not found")
        return False
    except Exception as e:
        log_step(logger, "HUMAN", f"Bell icon error: {e}")
        return False


async def _semantic_find_and_click(tab, action_name: str, logger: logging.Logger) -> str:
    """
    Semantic button search — 4-tier approach:
    1. CSS selectors (primary)
    2. aria-label contains search (case-insensitive)
    3. Button innerText match
    4. Shadow DOM / web component search
    Returns selector that worked or 'not_found'.
    """
    action_lower = action_name.lower()
    is_dislike = "dislike" in action_lower
    is_like = action_lower == "like"

    try:
        result = await tab.evaluate(
            f"""
            (() => {{
                var action = {json.dumps(action_lower)};
                var isDislike = {json.dumps(is_dislike)};
                var isLike = {json.dumps(is_like)};

                // Tier 1: Specific CSS selectors
                var tier1 = isDislike ? [
                    'dislike-button-view-model button',
                    '#segmented-dislike-button button',
                    'button[aria-label*="dislike" i]',
                    'ytd-segmented-like-dislike-button-renderer button:nth-child(2)',
                ] : [
                    'like-button-view-model button',
                    '#segmented-like-button button',
                    'button[aria-label*="like this video" i]',
                    'ytd-segmented-like-dislike-button-renderer button:first-child',
                    'ytd-like-button-renderer button',
                ];
                for (var i = 0; i < tier1.length; i++) {{
                    var el = document.querySelector(tier1[i]);
                    if (el && el.offsetParent !== null && !el.disabled) {{
                        // Extra check: for like button, skip if aria-label contains 'dislike'
                        var lbl = (el.getAttribute('aria-label') || '').toLowerCase();
                        if (isLike && lbl.includes('dislike')) continue;
                        el.click();
                        return 'tier1:' + tier1[i];
                    }}
                }}

                // Tier 2: aria-label semantic search
                var allBtns = document.querySelectorAll('button, [role="button"]');
                for (var j = 0; j < allBtns.length; j++) {{
                    var btn = allBtns[j];
                    var ariaLbl = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (!ariaLbl || btn.offsetParent === null || btn.disabled) continue;
                    if (isDislike && ariaLbl.includes('dislike') && !ariaLbl.includes('undo')) {{
                        btn.click(); return 'tier2_aria_dislike:' + ariaLbl;
                    }}
                    if (isLike && ariaLbl.includes('like') && !ariaLbl.includes('dislike') && !ariaLbl.includes('undo')) {{
                        btn.click(); return 'tier2_aria_like:' + ariaLbl;
                    }}
                }}

                // Tier 3: innerText match
                for (var k = 0; k < allBtns.length; k++) {{
                    var btn2 = allBtns[k];
                    var txt = (btn2.innerText || btn2.textContent || '').trim().toLowerCase();
                    if (btn2.offsetParent === null || btn2.disabled) continue;
                    if (isDislike && txt === 'dislike') {{ btn2.click(); return 'tier3_text:dislike'; }}
                    if (isLike && txt === 'like') {{ btn2.click(); return 'tier3_text:like'; }}
                }}

                // Tier 4: web component deep search
                var wcSels = isDislike
                    ? ['dislike-button-view-model', 'ytd-toggle-button-renderer#dislike-button']
                    : ['like-button-view-model', 'ytd-toggle-button-renderer#like-button'];
                for (var m = 0; m < wcSels.length; m++) {{
                    var wc = document.querySelector(wcSels[m]);
                    if (wc) {{
                        var inner = wc.querySelector('button, [role="button"]');
                        if (inner && inner.offsetParent !== null) {{
                            inner.click(); return 'tier4_wc:' + wcSels[m];
                        }}
                    }}
                }}

                return 'not_found';
            }})()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", "not_found")
        return raw or "not_found"
    except Exception as e:
        return f"error:{e}"


async def _post_like_dislike(tab, logger: logging.Logger, strategy) -> dict:
    """
    Semantic Like + Dislike — 4-tier search, ad-aware.
    Waits for video to be playing (not ad) before clicking.
    """
    results = {"like": False, "dislike": False}

    # Ensure real video is playing before like/dislike
    ad_ok = await strategy.wait_for_video_start(tab, timeout=20.0, skip_ads=True)
    if not ad_ok:
        log_step(logger, "HUMAN", "Like/Dislike deferred — waiting for video content...")
        await asyncio.sleep(3.0)

    # LIKE — semantic search
    log_step(logger, "HUMAN", "Like button — semantic search (4-tier)...")
    like_result = await _semantic_find_and_click(tab, "like", logger)
    results["like"] = "not_found" not in like_result and "error" not in like_result
    log_step(logger, "HUMAN", f"Like | result={like_result} success={results['like']}")
    await asyncio.sleep(random.uniform(2.0, 4.0))

    # DISLIKE — semantic search
    log_step(logger, "HUMAN", "Dislike button — semantic search (4-tier)...")
    dislike_result = await _semantic_find_and_click(tab, "dislike", logger)
    results["dislike"] = "not_found" not in dislike_result and "error" not in dislike_result
    log_step(logger, "HUMAN", f"Dislike | result={dislike_result} success={results['dislike']}")

    return results


async def _leave_comment(tab, logger: logging.Logger) -> bool:
    """Scroll to comments, click comment box, type a comment, submit."""
    comment_texts = [
        "Very informative video! Thanks for sharing 👍",
        "Great strategy, will definitely try this!",
        "This is exactly what I was looking for, thank you!",
        "Amazing content as always 🔥",
        "Super helpful, subscribed for more!",
    ]
    comment = random.choice(comment_texts)
    try:
        # Click comment input box
        result = await tab.evaluate(
            """
            (() => {
                var sels = [
                    '#simplebox-placeholder',
                    '#placeholder-area',
                    'ytd-comment-simplebox-renderer #simplebox-placeholder',
                    '#contenteditable-root',
                ];
                for (var i = 0; i < sels.length; i++) {
                    var el = document.querySelector(sels[i]);
                    if (el && el.offsetParent !== null) {
                        el.click();
                        return 'clicked:' + sels[i];
                    }
                }
                return 'not_found';
            })()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", "")
        if "not_found" in (raw or ""):
            log_step(logger, "HUMAN", "Comment box not found — may need more scroll")
            return False

        log_step(logger, "HUMAN", f"Comment box clicked | {raw}")
        await asyncio.sleep(random.uniform(0.8, 1.5))

        # Type comment character by character in the active element
        await tab.evaluate(
            f"""
            (() => {{
                var el = document.querySelector('#contenteditable-root, #simplebox-placeholder');
                if (el) el.focus();
            }})()
            """,
            return_by_value=True,
        )
        await asyncio.sleep(0.3)

        # Type via CDP key events
        for char in comment:
            try:
                await tab.evaluate(
                    f"(() => document.execCommand('insertText', false, {json.dumps(char)}))()",
                    return_by_value=True,
                )
                await asyncio.sleep(random.uniform(0.04, 0.12))
            except Exception:
                pass

        log_step(logger, "HUMAN", f"Comment typed: {comment!r}")
        await asyncio.sleep(random.uniform(1.0, 2.0))

        # Click Submit button
        submit_result = await tab.evaluate(
            """
            (() => {
                var sels = [
                    '#submit-button button',
                    'ytd-button-renderer#submit-button button',
                    'button[aria-label="Comment"]',
                ];
                for (var i = 0; i < sels.length; i++) {
                    var el = document.querySelector(sels[i]);
                    if (el && el.offsetParent !== null && !el.disabled) {
                        el.click();
                        return 'submitted';
                    }
                }
                return 'submit_not_found';
            })()
            """,
            return_by_value=True,
        )
        s_raw = submit_result if isinstance(submit_result, str) else getattr(submit_result, "value", "")
        ok = "submitted" in (s_raw or "")
        log_step(logger, "HUMAN", f"Comment submit | result={s_raw} ok={ok}")
        return ok
    except Exception as e:
        log_step(logger, "HUMAN", f"Comment error: {e}")
        return False


async def _autoplay_on_then_off(tab, logger: logging.Logger) -> dict:
    """Toggle autoplay ON, wait, then toggle OFF. Returns status."""
    results = {"autoplay_on": False, "autoplay_off": False}

    async def _toggle_autoplay():
        try:
            result = await tab.evaluate(
                """
                (() => {
                    var sels = [
                        'ytd-toggle-button-renderer.ytd-player-legacy-desktop-watch-ads-renderer button',
                        '#toggleButton',
                        'button.ytp-button[data-tooltip-target-id="ytp-autonav-toggle-button"]',
                        '.ytp-autonav-toggle-button',
                        '[aria-label*="autoplay"]',
                        '[aria-label*="Autoplay"]',
                        'ytd-menu-renderer .ytd-player-legacy-desktop-watch-ads-renderer',
                    ];
                    for (var i = 0; i < sels.length; i++) {
                        var el = document.querySelector(sels[i]);
                        if (el && el.offsetParent !== null) {
                            var state = el.getAttribute('aria-checked') || el.getAttribute('aria-pressed') || '';
                            el.click();
                            return 'toggled:' + sels[i] + ':was=' + state;
                        }
                    }
                    return 'not_found';
                })()
                """,
                return_by_value=True,
            )
            raw = result if isinstance(result, str) else getattr(result, "value", "")
            return raw or "not_found"
        except Exception as e:
            return f"error:{e}"

    # Toggle ON
    log_step(logger, "HUMAN", "Autoplay — turning ON...")
    r1 = await _toggle_autoplay()
    results["autoplay_on"] = "toggled" in r1
    log_step(logger, "HUMAN", f"Autoplay ON | result={r1}")
    await asyncio.sleep(random.uniform(2.0, 4.0))

    # Toggle OFF
    log_step(logger, "HUMAN", "Autoplay — turning OFF...")
    r2 = await _toggle_autoplay()
    results["autoplay_off"] = "toggled" in r2
    log_step(logger, "HUMAN", f"Autoplay OFF | result={r2}")
    await asyncio.sleep(random.uniform(1.0, 2.0))

    return results


async def _volume_interaction(tab, logger: logging.Logger) -> bool:
    """
    Human-like volume interaction:
    - 40% chance: mute → wait 2-4s → unmute
    - 60% chance: reduce volume → wait → increase back
    """
    try:
        action = random.random()
        if action < 0.40:
            # Mute → unmute via 'm' key
            log_step(logger, "HUMAN", "Volume — muting via 'M' key...")
            await tab.evaluate(
                "(() => { document.querySelector('.html5-video-player, #movie_player')?.focus(); })()",
                return_by_value=True,
            )
            await asyncio.sleep(0.3)
            # Dispatch 'm' keypress on player
            await tab.evaluate(
                """
                (() => {
                    var player = document.querySelector('.html5-video-player, #movie_player');
                    if (player) {
                        player.dispatchEvent(new KeyboardEvent('keydown', {key: 'm', keyCode: 77, bubbles: true}));
                        return 'muted';
                    }
                    var v = document.querySelector('video');
                    if (v) { v.muted = true; return 'muted_direct'; }
                    return 'not_found';
                })()
                """,
                return_by_value=True,
            )
            log_step(logger, "HUMAN", "Muted — waiting 2-4s...")
            await asyncio.sleep(random.uniform(2.0, 4.0))
            # Unmute
            await tab.evaluate(
                """
                (() => {
                    var player = document.querySelector('.html5-video-player, #movie_player');
                    if (player) {
                        player.dispatchEvent(new KeyboardEvent('keydown', {key: 'm', keyCode: 77, bubbles: true}));
                        return 'unmuted';
                    }
                    var v = document.querySelector('video');
                    if (v) { v.muted = false; return 'unmuted_direct'; }
                    return 'not_found';
                })()
                """,
                return_by_value=True,
            )
            log_step(logger, "HUMAN", "Unmuted ✓")
        else:
            # Reduce volume using volume slider click or arrow keys
            log_step(logger, "HUMAN", "Volume — reducing via Down arrow (3x)...")
            for _ in range(3):
                await tab.evaluate(
                    """
                    (() => {
                        var v = document.querySelector('video');
                        if (v && v.volume > 0.1) { v.volume = Math.max(0, v.volume - 0.1); return v.volume; }
                        return null;
                    })()
                    """,
                    return_by_value=True,
                )
                await asyncio.sleep(random.uniform(0.2, 0.5))
            log_step(logger, "HUMAN", "Volume reduced — waiting 2-3s...")
            await asyncio.sleep(random.uniform(2.0, 3.5))
            # Restore volume
            await tab.evaluate(
                "(() => { var v = document.querySelector('video'); if(v) v.volume = 1.0; })()",
                return_by_value=True,
            )
            log_step(logger, "HUMAN", "Volume restored ✓")
        return True
    except Exception as e:
        log_step(logger, "HUMAN", f"Volume interaction error: {e}")
        return False


async def _playlist_share_hover(tab, logger: logging.Logger) -> bool:
    """
    Hover over Share / Save-to-playlist buttons — no click, just mouse-over.
    Simulates user reading options without committing.
    """
    try:
        result = await tab.evaluate(
            """
            (() => {
                var hovered = [];
                // Share button
                var shareSels = [
                    'button[aria-label="Share"]',
                    'ytd-button-renderer:has(yt-icon[icon="share"])',
                    '.yt-spec-button-shape-next[aria-label="Share"]',
                    'button.yt-spec-button-shape-next[aria-label*="Share"]',
                ];
                for (var i = 0; i < shareSels.length; i++) {
                    var el = document.querySelector(shareSels[i]);
                    if (el && el.offsetParent !== null) {
                        el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                        hovered.push('share');
                        break;
                    }
                }
                // Save / playlist button
                var saveSels = [
                    'button[aria-label*="Save"]',
                    'button[aria-label*="playlist"]',
                    'ytd-button-renderer:has(yt-icon[icon="playlist_add"])',
                    '.yt-spec-button-shape-next[aria-label*="Save"]',
                ];
                for (var j = 0; j < saveSels.length; j++) {
                    var el2 = document.querySelector(saveSels[j]);
                    if (el2 && el2.offsetParent !== null) {
                        el2.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                        el2.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
                        hovered.push('save_playlist');
                        break;
                    }
                }
                return hovered.join(',') || 'not_found';
            })()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", "")
        log_step(logger, "HUMAN", f"Share/Playlist hover | hovered={raw}")
        await asyncio.sleep(random.uniform(1.5, 3.0))

        # Move mouse away (simulate human looking then moving on)
        await tab.evaluate(
            """
            (() => {
                var player = document.querySelector('#movie_player, .html5-video-player');
                if (player) player.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
            })()
            """,
            return_by_value=True,
        )
        return "not_found" not in (raw or "")
    except Exception as e:
        log_step(logger, "HUMAN", f"Share/Playlist hover error: {e}")
        return False


async def perform_full_human_session(manager: YouTubeManager, tab, logger: logging.Logger) -> dict:
    """
    Complete human behavior session — ALL interactions:
    1.  Skip ad if present
    2.  Read video info (title, channel, description)
    3.  Pause → wait → Resume
    4.  Seek forward 10s
    5.  Quality change (360p)
    6.  Like + Dislike test
    7.  Subscribe → Bell icon
    8.  Curved scroll → description expand
    9.  Curved scroll → comments → read + leave comment
    10. Scroll related videos (sidebar)
    11. Autoplay ON → wait → Autoplay OFF
    12. Post-watch human activity (before close)
    """
    results = {}
    strategy = manager.strategy

    # ── 0. Skip ad if present ───────────────────────────────────────────────
    log_step(logger, "HUMAN", "Checking for ads...")
    ad_skipped = await _skip_ad_if_present(tab, logger)
    results["ad_skipped"] = ad_skipped
    await strategy.human_delay(1.0, 2.0)

    # ── 1. Read video info ──────────────────────────────────────────────────
    log_step(logger, "HUMAN", "Reading video metadata (title, channel, description)...")
    await strategy.human_delay(2.0, 3.5)
    info = await _read_video_info(tab)
    if info:
        log_step(logger, "HUMAN", f"Title: {info.get('title', 'N/A')!r}")
        log_step(logger, "HUMAN", f"Channel: {info.get('channel', 'N/A')!r}")
        desc = info.get('desc', '')
        log_step(logger, "HUMAN", f"Description preview: {desc[:150]!r}")
    results["video_info"] = bool(info)

    # ── 2. Pause → Resume (single call handles both internally) ─────────────
    log_step(logger, "HUMAN", "Pause/Resume test...")
    pause_ok = await strategy.pause_playback(tab)
    log_step(logger, "HUMAN", f"Pause/Resume | success={pause_ok}")
    results["pause_resume"] = pause_ok

    # ── 3. Seek forward 10s ─────────────────────────────────────────────────
    log_step(logger, "HUMAN", "Seeking forward 10 seconds...")
    before = await strategy.get_current_time(tab)
    seek_ok = await strategy.seek_relative(tab, 10)
    await asyncio.sleep(0.5)
    after = await strategy.get_current_time(tab)
    log_step(logger, "HUMAN",
        f"Seek +10s | before={strategy.format_timestamp(before or 0)} "
        f"after={strategy.format_timestamp(after or 0)} success={seek_ok}")
    results["seek_forward"] = seek_ok

    # ── 4. Ad-Aware: wait for real video before quality/settings ────────────
    log_step(logger, "HUMAN", "Ad-check before settings — waiting for real video content...")
    video_ready = await strategy.wait_for_video_start(tab, timeout=30.0, skip_ads=True)
    log_step(logger, "HUMAN", f"Video ready for settings | confirmed={video_ready}")

    # ── 5. Quality change (360p) — only after video confirmed ───────────────
    log_step(logger, "HUMAN", "Setting quality to 360p (ad-aware)...")
    quality_ok = await strategy.change_settings(tab, quality="360p")
    log_step(logger, "HUMAN", f"Quality 360p | success={quality_ok}")
    results["quality"] = quality_ok
    await strategy.human_delay(0.5, 1.0)

    # ── 6. Like + Dislike — semantic 4-tier search ───────────────────────────
    log_step(logger, "HUMAN", "Testing Like & Dislike (semantic 4-tier)...")
    ld_results = await _post_like_dislike(tab, logger, strategy)
    results.update(ld_results)
    await strategy.human_delay(0.5, 1.0)

    # ── 6. Subscribe → Bell icon ─────────────────────────────────────────────
    log_step(logger, "HUMAN", "Attempting Subscribe...")
    sub_ok = await strategy.subscribe(tab)
    log_step(logger, "HUMAN", f"Subscribe | success={sub_ok}")
    results["subscribe"] = sub_ok
    await strategy.human_delay(0.5, 1.0)

    if sub_ok:
        log_step(logger, "HUMAN", "Clicking Bell notification icon...")
        bell_ok = await _click_bell_icon(tab, logger)
        results["bell_icon"] = bell_ok
        await strategy.human_delay(0.5, 1.0)
    else:
        results["bell_icon"] = False

    # ── 7. Curved scroll → description ──────────────────────────────────────
    log_step(logger, "HUMAN", "Curved scroll down to description...")
    await _curved_scroll(tab, total_px=random.randint(400, 700))
    await asyncio.sleep(0.5)
    # Expand description
    await _js_eval(tab, """
        var btn = document.querySelector(
            '#expand, #description #expand, tp-yt-paper-button#expand,
             ytd-text-inline-expander #expand-sizer'
        );
        if (btn) btn.click();
    """)
    await asyncio.sleep(0.8)
    log_step(logger, "HUMAN", "Description expanded")

    # ── 8. Curved scroll → comments → read + post ───────────────────────────
    log_step(logger, "HUMAN", "Curved scroll to comments section...")
    # First: curved scroll downward naturally
    for _ in range(random.randint(2, 3)):
        await _curved_scroll(tab, total_px=random.randint(350, 700))
        await asyncio.sleep(random.uniform(0.8, 1.5))

    # Then: directly scroll into view the #comments element so YT lazy-loads them
    await _scroll_to_comments_section(tab)
    log_step(logger, "HUMAN", "Waiting for comments to lazy-load...")
    await asyncio.sleep(2.0)

    comments = await _read_top_comments(tab)
    if comments:
        log_step(logger, "HUMAN", f"Comments read | count={len(comments)}")
        for i, c in enumerate(comments[:3], 1):
            log_step(logger, "HUMAN", f"  Comment {i}: {c[:80]!r}")
    else:
        log_step(logger, "HUMAN", "Comments not yet loaded — scrolling deeper + waiting...")
        for _ in range(3):
            await _curved_scroll(tab, total_px=random.randint(300, 500))
            await asyncio.sleep(1.2)
        await asyncio.sleep(3.0)
        comments = await _read_top_comments(tab)
        log_step(logger, "HUMAN", f"Comments after deep scroll | count={len(comments)}")
    results["comments_read"] = len(comments)

    # Leave a comment
    log_step(logger, "HUMAN", "Attempting to leave a comment...")
    comment_ok = await _leave_comment(tab, logger)
    results["comment_posted"] = comment_ok

    # ── 9. Related videos from sidebar (always visible, no scroll-to-top needed) ──
    log_step(logger, "HUMAN", "Reading related videos from sidebar...")
    # Sidebar is rendered on page load — just query it directly
    related = await _read_related_videos(tab)
    if related:
        log_step(logger, "HUMAN", f"Related videos | count={len(related)}")
        for i, r in enumerate(related[:3], 1):
            log_step(logger, "HUMAN", f"  Related {i}: {r!r}")
    else:
        log_step(logger, "HUMAN", "Related videos not found — scrolling to top to expose sidebar...")
        try:
            await tab.evaluate("(() => window.scrollTo({top: 0, behavior: 'smooth'}))()", return_by_value=True)
        except Exception:
            pass
        await asyncio.sleep(2.5)
        related = await _read_related_videos(tab)
        if not related:
            # Last resort: check if secondary panel exists and force-load via scroll
            try:
                await tab.evaluate(
                    """
                    (() => {
                        var s = document.querySelector('#secondary, #related, ytd-watch-next-secondary-results-renderer');
                        if (s) { s.scrollTop = 0; s.dispatchEvent(new Event('scroll')); }
                    })()
                    """,
                    return_by_value=True,
                )
            except Exception:
                pass
            await asyncio.sleep(2.0)
            related = await _read_related_videos(tab)
        log_step(logger, "HUMAN", f"Related after retry | count={len(related)}")
    results["related_videos"] = len(related)

    # ── 10. Autoplay ON → OFF (ad-aware — only after video confirmed) ─────────
    log_step(logger, "HUMAN", "Testing Autoplay (ON then OFF) — ad-aware...")
    # Quick ad check before autoplay toggle
    if await strategy.is_ad_playing(tab):
        log_step(logger, "HUMAN", "Ad still playing — waiting before autoplay toggle...")
        await strategy.wait_for_video_start(tab, timeout=20.0)
    ap_results = await _autoplay_on_then_off(tab, logger)
    results.update(ap_results)
    await strategy.human_delay(1.5, 2.5)

    # ── 11. Volume interaction — ad-aware ────────────────────────────────────
    log_step(logger, "HUMAN", "Volume interaction (ad-aware)...")
    if await strategy.is_ad_playing(tab):
        log_step(logger, "HUMAN", "Ad playing — skipping volume interaction")
        results["volume"] = False
    else:
        vol_ok = await _volume_interaction(tab, logger)
        results["volume"] = vol_ok
    await strategy.human_delay(1.0, 2.0)

    # ── 12. Share / Playlist button hover (no click) ─────────────────────────
    log_step(logger, "HUMAN", "Hovering over Share & Save-to-playlist buttons...")
    hover_ok = await _playlist_share_hover(tab, logger)
    results["share_playlist_hover"] = hover_ok
    await strategy.human_delay(1.5, 2.5)

    return results


async def watch_for_duration(
    manager: YouTubeManager,
    tab,
    logger: logging.Logger,
    seconds: float,
) -> float:
    """
    Watch with periodic position logs + micro-interactions.
    After 80% watch time → post-watch human activity (10-20s) before returning.
    """
    log_step(logger, "WATCH", f"Watching for {seconds:.0f}s ({seconds / 60:.1f} min)...")
    started = time.monotonic()
    tick = 0
    post_watch_done = False

    while True:
        elapsed = time.monotonic() - started
        if elapsed >= seconds:
            break

        await asyncio.sleep(min(10.0, seconds - elapsed))
        tick += 1
        elapsed = time.monotonic() - started
        current = await manager.strategy.get_current_time(tab)
        stamp = manager.strategy.format_timestamp(current or 0)
        log_step(logger, "WATCH", f"tick={tick} elapsed={elapsed:.0f}s position={stamp}")

        if tick % 3 == 0 and random.random() < 0.40:
            await manager.strategy.micro_interaction(tab)

        # After 80% watch time — do human activity while video still plays
        if not post_watch_done and elapsed >= seconds * 0.80:
            post_watch_done = True
            log_step(logger, "WATCH", "80% watch reached — doing human post-watch activity...")
            await _post_watch_human_activity(tab, logger)

    actual = time.monotonic() - started
    log_step(logger, "WATCH", f"Completed | actual={actual:.0f}s")
    return actual


async def _click_related_video_after_task(tab, logger: logging.Logger) -> bool:
    """
    Task poora hone ke BAAD — ek related video pe click karo, 30-60 sec dekho, wapas aao.
    Ye bilkul natural lagta hai YouTube ko — ek video dekha, curiosity se aur dekha.
    """
    log_step(logger, "RELATED", "Main task done — checking sidebar for related video...")

    # Step 1: Scroll to top so sidebar is visible
    try:
        await tab.evaluate("(() => window.scrollTo({top: 0, behavior: 'smooth'}))()", return_by_value=True)
    except Exception:
        pass
    await asyncio.sleep(2.5)

    # Step 2: Find related video links
    try:
        result = await tab.evaluate(
            """
            (() => {
                var patterns = [
                    '#related ytd-compact-video-renderer a#thumbnail',
                    'ytd-compact-video-renderer a#thumbnail',
                    '#secondary ytd-compact-video-renderer a[href*="/watch"]',
                    'ytd-watch-next-secondary-results-renderer a[href*="/watch?v="]',
                    '#related a[href*="/watch?v="]',
                ];
                for (var p = 0; p < patterns.length; p++) {
                    var els = document.querySelectorAll(patterns[p]);
                    if (els.length > 0) {
                        var links = [];
                        for (var i = 0; i < Math.min(els.length, 5); i++) {
                            var href = els[i].getAttribute('href') || '';
                            if (href && href.includes('/watch')) links.push(href);
                        }
                        if (links.length > 0) return JSON.stringify(links);
                    }
                }
                return JSON.stringify([]);
            })()
            """,
            return_by_value=True,
        )
        raw = result if isinstance(result, str) else getattr(result, "value", None)
        links = json.loads(raw) if raw else []
    except Exception:
        links = []

    if not links:
        log_step(logger, "RELATED", "No related video found in sidebar — skipping")
        return False

    # Step 3: Pick one (not first, random from top 3 — human choice)
    chosen = random.choice(links[:3])
    if not chosen.startswith("http"):
        chosen = "https://www.youtube.com" + chosen

    log_step(logger, "RELATED", f"Clicking related video → {chosen}")

    # Step 4: Human pause before click (curiosity → decision)
    await asyncio.sleep(random.uniform(1.5, 3.0))

    try:
        await tab.get(chosen)
    except Exception as e:
        log_step(logger, "RELATED", f"Navigation failed: {e}")
        return False

    await asyncio.sleep(random.uniform(2.0, 3.5))

    # Step 5: Watch 30-60 seconds
    watch_sec = random.uniform(30.0, 60.0)
    log_step(logger, "RELATED", f"Watching related video for {watch_sec:.0f}s...")

    # Try to play if paused
    try:
        await tab.evaluate("(() => { var v = document.querySelector('video'); if(v && v.paused) v.play(); })()", return_by_value=True)
    except Exception:
        pass

    # Skip any ad on related video too
    for _ in range(4):
        await asyncio.sleep(3.0)
        try:
            skip_btn_js = """
            (() => {
                var btns = document.querySelectorAll('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
                for(var i=0;i<btns.length;i++){if(btns[i].offsetParent!==null){btns[i].click();return true;}}
                return false;
            })()
            """
            r = await tab.evaluate(skip_btn_js, return_by_value=True)
            skipped = r if isinstance(r, bool) else getattr(r, "value", False)
            if skipped:
                log_step(logger, "RELATED", "Ad skipped on related video")
                break
        except Exception:
            pass

    # Actually watch
    start = time.monotonic()
    while time.monotonic() - start < watch_sec:
        await asyncio.sleep(5.0)
        elapsed = time.monotonic() - start
        log_step(logger, "RELATED", f"Related watch tick | elapsed={elapsed:.0f}s / {watch_sec:.0f}s")

    log_step(logger, "RELATED", f"Related video watched {watch_sec:.0f}s — natural session end ✓")
    return True


async def _post_watch_human_activity(tab, logger: logging.Logger) -> None:
    """
    10-20 seconds of natural human activity after 80% watch.
    Simulates: checking comments, hovering over related video, subtle scroll.
    Browser stays open — this happens BEFORE close.
    """
    activity_time = random.uniform(10.0, 20.0)
    log_step(logger, "WATCH", f"Post-watch activity for {activity_time:.0f}s (video still playing)...")
    start = time.monotonic()

    activities = [
        # Scroll down slightly to glance at comments
        lambda: _js_eval(tab, f"window.scrollBy({{top: {random.randint(150, 350)}, behavior: 'smooth'}})"),
        # Scroll back up
        lambda: _js_eval(tab, f"window.scrollBy({{top: -{random.randint(100, 250)}, behavior: 'smooth'}})"),
        # Hover over related video (simulate mouse movement)
        lambda: tab.evaluate(
            "(() => { var el = document.querySelector('ytd-compact-video-renderer'); if(el) el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true})); })()",
            return_by_value=True,
        ),
        # Read like count
        lambda: tab.evaluate(
            "(() => { var el = document.querySelector('#segmented-like-button button'); return el ? el.getAttribute('aria-label') : ''; })()",
            return_by_value=True,
        ),
        # Move mouse to progress bar area (simulate checking time)
        lambda: tab.evaluate(
            "(() => { var el = document.querySelector('.ytp-progress-bar'); if(el) el.dispatchEvent(new MouseEvent('mouseover', {bubbles:true})); })()",
            return_by_value=True,
        ),
    ]
    random.shuffle(activities)

    for act in activities:
        if time.monotonic() - start >= activity_time:
            break
        try:
            await act()
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.5, 4.0))

    # Final pause — just watching/idle
    remaining = activity_time - (time.monotonic() - start)
    if remaining > 0:
        await asyncio.sleep(remaining)

    log_step(logger, "WATCH", "Post-watch activity complete — session ending naturally")


async def run_live_test(platform: str, args=None) -> None:
    import types as _types
    if args is None:
        args = _types.SimpleNamespace(search=False, entry="direct")
    load_dotenv(PROJECT_ROOT / ".env")
    setup_console_logging()
    logger = logging.getLogger("mmb.final_live_test")

    profile_id, resolved_platform = resolve_profile(platform)

    platform_identity: dict = {}
    if PROFILES_PATH.exists():
        with PROFILES_PATH.open(encoding="utf-8") as handle:
            cfg = json.load(handle)
        entry = cfg.get(resolved_platform) or {}
        if str(entry.get("profile_id")) == profile_id:
            platform_identity = dict(entry.get("identity") or {})
            platform_identity["profile_id"] = profile_id
            platform_identity["screen_resolution"] = "1920x1080"
            platform_identity["os_type"] = "windows"
            platform_identity.pop("mobile_first", None)

    log_step(logger, "SETUP", "Initializing Orchestrator...")
    orchestrator = Orchestrator()
    log_step(
        logger,
        "SETUP",
        f"Orchestrator ready | jobs={len(orchestrator._config.jobs)} "
        f"provider={orchestrator._config.provider} pool={orchestrator._profile_pool.size}",
    )

    if platform_identity:
        orchestrator._identity_manager.store_identity(profile_id, platform_identity)
        log_step(
            logger,
            "SETUP",
            f"Identity pinned | screen={platform_identity.get('screen_resolution')} os={platform_identity.get('os_type')}",
        )

    log_step(
        logger,
        "SETUP",
        f"YouTubeManager | profile={profile_id} platform={resolved_platform} video={VIDEO_ID}",
    )

    entry_path = getattr(args, "entry", "search")
    config_entry = "search" if entry_path == "direct" else entry_path
    profile_config = ProfileConfig(
        entry_path=config_entry,
        own_channel_ids=["UCultagtaplay"],
    )

    manager = YouTubeManager(
        profile_id=profile_id,
        country_code=orchestrator._config.country_code,
        profile_platform=resolved_platform,
        browser_manager=orchestrator._browser_manager,
        identity_manager=orchestrator._identity_manager,
        behavior_profile="serious_learner",
        referrer_search=os.getenv("LIVE_TEST_REFERRER", "false").lower() in ("1", "true", "yes"),
        profile_config=profile_config,
    )

    log_step(
        logger,
        "SETUP",
        f"Strategy={manager.platform.value} os_type={manager.identity.get('os_type')}",
    )

    tab = None
    try:
        log_step(logger, "SESSION", "Opening browser session (Multilogin -> nodriver -> YouTube)...")
        tab = await manager.open_session()
        log_step(logger, "SESSION", f"Session open | tab url={tab.url or 'pending'}")

        # ── Navigation: entry path se decide hoga ───────────────────────────────
        entry_mode = getattr(args, "entry", "direct")
        if entry_mode == "search" or getattr(args, "search", False):
            log_step(logger, "NAVIGATE", f"[PATH=SEARCH] keywords='{SEARCH_KEYWORDS}'")
            target = VideoTarget(
                video_id=VIDEO_ID,
                title_hint=TITLE_HINT,
                channel_name=CHANNEL_NAME,
                search_keywords=SEARCH_KEYWORDS,
            )
        elif entry_mode == "homepage":
            log_step(logger, "NAVIGATE", f"[PATH=HOMEPAGE] channel={CHANNEL_NAME}")
            target = VideoTarget(
                video_id=VIDEO_ID,
                title_hint=TITLE_HINT,
                channel_name=CHANNEL_NAME,
                search_keywords=SEARCH_KEYWORDS,  # fallback if not on homepage
            )
        elif entry_mode == "notification":
            log_step(logger, "NAVIGATE", f"[PATH=NOTIFICATION] video={VIDEO_ID}")
            target = VideoTarget(
                video_id=VIDEO_ID,
                title_hint=TITLE_HINT,
                channel_name=CHANNEL_NAME,
                search_keywords=SEARCH_KEYWORDS,  # fallback
            )
        else:
            DIRECT_VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
            log_step(logger, "NAVIGATE", f"[PATH=DIRECT] → {DIRECT_VIDEO_URL}")
            target = VideoTarget(
                video_id=VIDEO_ID,
                title_hint=TITLE_HINT,
                channel_name=CHANNEL_NAME,
                direct_url=DIRECT_VIDEO_URL,
            )
        route = await manager.navigate_to_video(tab, target)
        log_step(logger, "NAVIGATE", f"Arrived | route={route} url={tab.url or ''}")

        await wait_for_playback_start(manager, tab, logger)

        # System health check before interactions
        log_step(logger, "HUMAN", "Running system health check...")
        health = await manager.strategy.verify_system_health(tab)
        log_step(logger, "HUMAN", f"Health check | {health}")

        # Full human behavior session
        log_step(logger, "HUMAN", "Starting full human behavior session...")
        behavior_results = await perform_full_human_session(manager, tab, logger)
        log_step(logger, "HUMAN", f"Behavior session done | results={behavior_results}")

        watch_target = random.uniform(MIN_WATCH_SECONDS, MAX_WATCH_SECONDS)
        watched = await watch_for_duration(manager, tab, logger, watch_target)

        # ── Main task COMPLETE — ab related video click karo (task ke BAAD) ──
        log_step(logger, "RELATED", "Main video task complete — exploring related video naturally...")
        related_clicked = await _click_related_video_after_task(tab, logger)
        behavior_results["related_video_clicked"] = related_clicked

        log_step(
            logger,
            "VERIFY",
            f"PASS | route={route} watched={watched:.0f}s video={VIDEO_ID} | {behavior_results}",
        )
        print("\n=== LIVE TEST PASSED ===", flush=True)

    except YouTubeManagerError as exc:
        log_step(logger, "VERIFY", f"FAIL | {exc}")
        print(f"\n=== LIVE TEST FAILED ===\n{exc}", flush=True)
        raise
    except Exception as exc:
        log_step(logger, "VERIFY", f"FAIL | unexpected: {exc}")
        print(f"\n=== LIVE TEST FAILED ===\n{exc}", flush=True)
        raise
    finally:
        log_step(logger, "TEARDOWN", "Closing session cleanly...")
        await manager.close_session()
        log_step(logger, "TEARDOWN", "Browser stopped and profile released.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Full E2E live test for MMB-Agent-v2")
    parser.add_argument(
        "--platform",
        default=os.getenv("LIVE_TEST_PLATFORM", DEFAULT_PLATFORM),
        choices=("windows", "macos", "android"),
        help="Platform profile to use from data/platform_profiles.json (default: windows)",
    )
    parser.add_argument(
        "--search",
        action="store_true",
        default=False,
        help="Use search path instead of direct URL (tests Behavioral Entropy Engine)",
    )
    parser.add_argument(
        "--entry",
        default="search",
        choices=("search", "homepage", "notification", "direct"),
        help="Entry path: search / homepage / notification / direct",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_live_test(args.platform, args))
