"""
Legacy-proven search flows ported from MMB-AGENT--final (searchEngine.cjs).

Warm-up → Search → Filter (browse/scroll) → Click escalation sequence.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any, Callable, Optional

from nodriver import cdp
from nodriver.core.tab import Tab

from behavior.youtube.types import VideoTarget

STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "and", "or", "but", "not", "this", "that", "it", "its",
    "how", "what", "which", "who", "when", "where", "why", "do", "does", "did",
    "will", "would", "could", "should", "can", "may", "might",
    "you", "your", "my", "our", "their", "his", "her",
})

WARMUP_QUERY_SEEDS = (
    "best personal loan rates",
    "car insurance comparison",
    "home mortgage rates today",
    "best credit cards cashback",
    "investment portfolio tips",
    "tax filing online free",
    "best savings account uk",
    "compare life insurance quotes",
    "student loan refinancing",
    "banking apps best 2026",
    "travel rewards credit card",
    "auto insurance quotes",
    "health insurance plans usa",
    "refinance home loan calculator",
    "best etf to invest in",
)


def _normalize_eval(value: Any) -> Any:
    """
    nodriver returns RemoteObject when return_by_value result is falsy (0, [], false).
    Unwrap so callers always get the primitive/list/dict.
    """
    if value is None or isinstance(value, (bool, str, int, float, list, dict)):
        return value
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "deep_serialized_value") and value.deep_serialized_value is not None:
        dsv = value.deep_serialized_value
        if hasattr(dsv, "value"):
            return dsv.value
    return value


def _normalize_query(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clean_title(title: str) -> str:
    cleaned = re.sub(r"[()[\]{}|:!?—–\-]", " ", title)
    cleaned = re.sub(r"\b\d{4}\b", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def generate_escalation_queries(video_title: str, channel_name: str = "") -> list[str]:
    """Port of generateEscalationQueries + buildYouTubeSearchPlan (searchEngine.cjs)."""
    title = _normalize_query(video_title)
    channel = _normalize_query(channel_name)
    clean = _clean_title(title)
    keywords = [
        w.lower()
        for w in clean.split()
        if len(w) > 2 and w.lower() not in STOP_WORDS
    ]

    core = keywords[: min(4, len(keywords))]
    level1 = " ".join(core)
    level2 = " ".join(keywords[: min(6, len(keywords))])
    level3 = f"{channel} {' '.join(core[:3])}".strip() if channel else level2
    level4 = " ".join(clean.split()[:10])
    level5 = title

    queries = [level1, level2, level3, level4, level5]
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        n = _normalize_query(q)
        if len(n) < 4:
            continue
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def build_search_plan(
    video_title: str,
    channel_name: str = "",
    search_seed: str = "",
    max_attempts: int = 6,
) -> list[str]:
    """Port of buildYouTubeSearchPlan."""
    seed = _normalize_query(search_seed or video_title)
    title = _normalize_query(video_title)
    channel = _normalize_query(channel_name)

    from_seed = generate_escalation_queries(seed, channel)
    from_title = generate_escalation_queries(title, channel)

    channel_plus: list[str] = []
    if channel and title:
        clean = _clean_title(title)
        words = [w for w in clean.split() if len(w) > 2 and w.lower() not in STOP_WORDS]
        core3 = " ".join(words[:3])
        core5 = " ".join(words[:5])
        near_full = " ".join(clean.split()[:12])
        if core3:
            channel_plus.append(f"{channel} {core3}")
        if core5 and core5 != core3:
            channel_plus.append(f"{channel} {core5}")
        if near_full:
            channel_plus.append(f"{channel} {near_full}")
        channel_plus.append(f"{channel} {title}"[:120])

    title_exact = from_title[-1] if from_title else title
    title_near = from_title[-2] if len(from_title) > 1 else title_exact

    merged = [
        *(from_seed[:2]),
        *channel_plus,
        title_near,
        title_exact,
    ]
    seen: set[str] = set()
    out: list[str] = []
    for q in merged:
        n = _normalize_query(q)
        if len(n) < 4:
            continue
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out[: max(1, max_attempts)]


def verify_video_match(
    result_title: str,
    result_channel: str,
    expected_title: str,
    expected_channel: str = "",
) -> bool:
    """Port of verifyVideoMatch (videoMatch.cjs)."""
    expected_words = [
        w
        for w in expected_title.lower().split()
        if len(w) > 2 and w not in STOP_WORDS
    ]
    result_words = [w for w in result_title.lower().split() if len(w) > 2]
    matched = sum(
        1
        for w in expected_words
        if any(rw in w or w in rw for rw in result_words)
    )
    title_pct = matched / len(expected_words) if expected_words else 0.0

    score = 0
    if title_pct >= 0.65:
        score += 55
    elif title_pct >= 0.5:
        score += 42
    elif title_pct >= 0.4:
        score += 28
    else:
        return False

    need_channel = bool(expected_channel.strip())
    channel_ok = not need_channel
    if need_channel and result_channel:
        exp_ch = expected_channel.lower().strip()
        res_ch = result_channel.lower().strip()
        if exp_ch in res_ch or res_ch in exp_ch:
            score += 35
            channel_ok = True
        else:
            exp_parts = [w for w in exp_ch.split() if len(w) > 2]
            res_parts = [w for w in res_ch.split() if len(w) > 2]
            ratio = (
                sum(1 for w in exp_parts if any(r in w or w in r for r in res_parts))
                / len(exp_parts)
                if exp_parts
                else 0
            )
            if ratio >= 0.6:
                score += 28
                channel_ok = True

    if need_channel:
        return channel_ok and title_pct >= 0.45 and score >= 62
    return title_pct >= 0.80 and score >= 55


async def is_mobile_youtube(tab: Tab) -> bool:
    try:
        url = tab.url or ""
        if "m.youtube.com" in url:
            return True
        return bool(
            await tab.evaluate(
                """
                () => !!document.querySelector('ytm-app, ytm-browse, ytm-watch')
                    || /android|mobile/i.test(navigator.userAgent || '')
                """,
                return_by_value=True,
            )
        )
    except Exception:
        return False


async def dismiss_consent_legacy(tab: Tab, log: Callable[[str], None]) -> None:
    """Port of dismissSiteOverlays + YoutubeUi consent patterns."""
    try:
        clicked = _normalize_eval(
            await tab.evaluate(
            """
            (() => {
                const gBtn = document.querySelector('#L2AGLb, button#L2AGLb');
                if (gBtn) { gBtn.click(); return 'google-consent'; }
                const want = ['accept all', 'accept', 'i agree', 'reject all', 'agree', 'got it', 'allow all'];
                for (const el of document.querySelectorAll(
                    'button, .VfPpkd-LgbsSe, tp-yt-paper-button, ytd-button-renderer button, input[type="submit"]'
                )) {
                    const t = (el.textContent || el.value || '').toLowerCase().trim();
                    if (!t || t.length > 48) continue;
                    if (want.some(w => t === w || t.includes(w))) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 8 && r.height > 8) { el.click(); return t.slice(0, 24); }
                    }
                }
                return null;
            })()
            """,
            return_by_value=True,
            )
        )
        if clicked:
            log(f"Dismissed overlay ({clicked})")
            await asyncio.sleep(random.uniform(0.8, 1.5))
    except Exception:
        pass


async def type_in_search_bar(
    tab: Tab,
    query: str,
    human_type_fn: Callable,
    *,
    mobile: bool = False,
) -> bool:
    """
    Port of typeInSearchBar + clickSearchAndType (searchEngine.cjs / YoutubeUi.cjs).
    Primary selectors: input#search, #search-icon-legacy, button[aria-label="Search"], '/' shortcut.
    """
    if mobile:
        mobile_ok = await _type_in_mobile_search_bar(tab, query, human_type_fn)
        if mobile_ok:
            return True

    for attempt in range(1, 4):
        try:
            await tab.send(
                cdp.input_.dispatch_key_event("keyDown", key="/", code="Slash")
            )
            await tab.send(cdp.input_.dispatch_key_event("keyUp", key="/", code="Slash"))
            await asyncio.sleep(0.6)
            focused = await tab.evaluate(
                """
                () => document.activeElement?.id === 'search'
                    || document.activeElement?.tagName === 'INPUT'
                """,
                return_by_value=True,
            )
            if focused:
                await tab.send(
                    cdp.input_.dispatch_key_event(
                        "keyDown", key="a", code="KeyA", modifiers=2
                    )
                )
                await tab.send(
                    cdp.input_.dispatch_key_event(
                        "keyUp", key="a", code="KeyA", modifiers=2
                    )
                )
                await asyncio.sleep(0.1)
                await tab.send(
                    cdp.input_.dispatch_key_event("keyDown", key="Backspace", code="Backspace")
                )
                await tab.send(
                    cdp.input_.dispatch_key_event("keyUp", key="Backspace", code="Backspace")
                )
                await asyncio.sleep(0.2)
                await human_type_fn(query)
                return True
        except Exception:
            pass

        for selector in ("input#search",):
            try:
                el = await tab.select(selector, timeout=2)
                if el:
                    await el.click()
                    await asyncio.sleep(0.4)
                    await human_type_fn(query)
                    return True
            except Exception:
                pass

        for selector in ("#search-icon-legacy", 'button[aria-label="Search"]'):
            try:
                el = await tab.select(selector, timeout=2)
                if el:
                    await el.click()
                    await asyncio.sleep(0.6)
                    await human_type_fn(query)
                    return True
            except Exception:
                pass

        if attempt < 3:
            await asyncio.sleep(1.5)

    return False


async def _type_in_mobile_search_bar(
    tab: Tab,
    query: str,
    human_type_fn: Callable,
) -> bool:
    """Port of typeInMobileSearchBar (searchEngine.cjs)."""
    search_buttons = (
        'button[aria-label*="Search" i]',
        'ytm-topbar-menu-button-renderer button',
        '.mobile-topbar-header-content button',
    )
    for sel in search_buttons:
        try:
            btn = await tab.select(sel, timeout=2)
            if not btn:
                continue
            label = await btn.apply(
                "(el) => (el.getAttribute('aria-label') || el.innerText || '').toLowerCase()",
                return_by_value=True,
            )
            if "search" not in str(label or "") and "topbar" not in sel:
                continue
            await btn.click()
            await asyncio.sleep(0.7)
            break
        except Exception:
            continue

    input_selectors = (
        'input[type="search"]',
        'input[name="search_query"]',
        'input[placeholder*="Search" i]',
        'input[aria-label*="Search" i]',
        'ytm-searchbox input',
        '.searchbox-input input',
    )
    for sel in input_selectors:
        try:
            inp = await tab.select(sel, timeout=2)
            if not inp:
                continue
            await inp.click()
            await asyncio.sleep(0.3)
            await human_type_fn(query)
            return True
        except Exception:
            continue
    return False


async def browse_results_filter(tab: Tab, variant: int = 0) -> None:
    """Port of browseResults — scroll filter before click (TrafficRouter.cjs pattern)."""
    v = variant % 5
    patterns = [
        [(200, 420), (90, 220), (-120, -350)],
        [(250, 550), (100, 280), (-80, -200), (-100, -250)],
        [(150, 380), (-150, -400)],
        [(120, 300), (120, 300), (-200, -450)],
        [(300, 650), (60, 180), (-180, -480)],
    ]
    for delta_range in patterns[v]:
        lo, hi = delta_range
        delta = random.randint(abs(lo), abs(hi)) * (1 if lo >= 0 else -1)
        await tab.evaluate(f"() => window.scrollBy(0, {delta})")
        await asyncio.sleep(random.uniform(0.7, 2.8))


async def wait_for_search_results(tab: Tab, timeout: float = 12.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        count = _normalize_eval(
            await tab.evaluate(
                """
                () => document.querySelectorAll(
                    'ytd-video-renderer, ytm-compact-video-renderer, ytm-video-with-context-renderer'
                ).length
                """,
                return_by_value=True,
            )
        )
        if isinstance(count, (int, float)) and int(count) > 0:
            return True
        await asyncio.sleep(0.45)
    return False


async def find_and_click_desktop_result(
    tab: Tab,
    target: VideoTarget,
    *,
    max_scrolls: int = 2,
    log: Callable[[str], None],
) -> bool:
    """Port of findAndVerifyVideo for desktop ytd-video-renderer results."""
    expected_title = target.title_hint or target.search_keywords or ""
    expected_channel = ""

    for scroll in range(max_scrolls):
        if target.video_id:
            vid = target.video_id.replace("'", "\\'")
            opened = _normalize_eval(
                await tab.evaluate(
                f"""
                () => {{
                    const vid = '{vid}';
                    for (const a of document.querySelectorAll('ytd-video-renderer a#video-title')) {{
                        if ((a.getAttribute('href') || '').includes(vid)) {{
                            a.click();
                            return true;
                        }}
                    }}
                    return false;
                }}
                """,
                return_by_value=True,
                )
            )
            if opened is True:
                log(f"Clicked result by video ID {target.video_id}")
                await asyncio.sleep(random.uniform(1.2, 2.2))
                return True

        results = _normalize_eval(
            await tab.evaluate(
            """
            () => {
                const videos = document.querySelectorAll('ytd-video-renderer');
                const matches = [];
                for (let i = 0; i < Math.min(videos.length, 20); i++) {
                    const el = videos[i];
                    const titleEl = el.querySelector('a#video-title');
                    const channelEl = el.querySelector(
                        'ytd-channel-name a, .ytd-channel-name, ytd-channel-name yt-formatted-string'
                    );
                    matches.push({
                        index: i,
                        title: titleEl?.getAttribute('title') || titleEl?.textContent?.trim() || '',
                        channel: (channelEl?.textContent || '').replace(/\\s+/g, ' ').trim(),
                    });
                }
                return matches;
            }
            """,
            return_by_value=True,
            )
        )

        if isinstance(results, list):
            for result in results:
                if verify_video_match(
                    str(result.get("title", "")),
                    str(result.get("channel", "")),
                    expected_title,
                    expected_channel,
                ):
                    idx = int(result["index"])
                    clicked = _normalize_eval(
                        await tab.evaluate(
                        f"""
                        () => {{
                            const videos = document.querySelectorAll('ytd-video-renderer');
                            const el = videos[{idx}];
                            if (!el) return false;
                            const titleEl = el.querySelector('a#video-title');
                            if (!titleEl) return false;
                            titleEl.click();
                            return true;
                        }}
                        """,
                        return_by_value=True,
                        )
                    )
                    if clicked is True:
                        log(f"Matched and clicked desktop result index {idx}")
                        await asyncio.sleep(random.uniform(1.2, 2.2))
                        return True

        if scroll < max_scrolls - 1:
            await tab.evaluate("() => window.scrollBy(0, 450)")
            await asyncio.sleep(random.uniform(0.5, 0.9))

    return False


async def mobile_url_search(
    tab: Tab,
    video_title: str,
    channel_name: str,
    target: VideoTarget,
    log: Callable[[str], None],
) -> bool:
    """Port of mobileYouTubeSearch (YoutubeUi.cjs) — URL escalation with card scoring."""
    queries: list[str] = []
    if channel_name:
        queries.append(f"{channel_name} {video_title}")
    queries.append(video_title)
    short = " ".join(video_title.split()[:5])
    if short != video_title:
        queries.append(f"{channel_name} {short}".strip() if channel_name else short)

    home = "https://m.youtube.com"
    for query in queries:
        url = f"{home}/results?search_query={query.replace(' ', '+')}"
        log(f"[Mobile legacy] URL search: {query[:60]}")
        try:
            await tab.get(url)
            await asyncio.sleep(random.uniform(2.5, 4.0))
            await dismiss_consent_legacy(tab, log)

            payload = json.dumps({
                "titleTarget": video_title,
                "channelTarget": channel_name or "",
                "stopWords": list(STOP_WORDS),
            })
            href = _normalize_eval(
                await tab.evaluate(
                f"""
                () => {{
                    const args = {payload};
                    const {{ titleTarget, channelTarget, stopWords }} = args;
                    const stopSet = new Set(stopWords);
                    function wordMatch(text, target) {{
                        const words = target.toLowerCase().split(/\\s+/)
                            .filter(w => w.length > 2 && !stopSet.has(w));
                        if (!words.length) return 0;
                        return words.filter(w => text.toLowerCase().includes(w)).length / words.length;
                    }}
                    const cardSelectors = [
                        'ytm-compact-video-renderer',
                        'ytm-video-with-context-renderer',
                    ];
                    let bestHref = null;
                    let bestScore = 0;
                    for (const cardSel of cardSelectors) {{
                        for (const card of document.querySelectorAll(cardSel)) {{
                            const cardText = (card.textContent || '').toLowerCase();
                            let score = wordMatch(cardText, titleTarget);
                            if (channelTarget) {{
                                const ct = channelTarget.toLowerCase().trim();
                                if (cardText.includes(ct)) score += 0.35;
                            }}
                            if (score > bestScore) {{
                                const link = card.querySelector('a[href*="/watch?v="]')
                                    || card.querySelector('a[href*="/watch"]');
                                if (link) {{
                                    const h = link.getAttribute('href');
                                    if (h && h.includes('/watch')) {{
                                        bestScore = score;
                                        bestHref = h;
                                    }}
                                }}
                            }}
                        }}
                    }}
                    const minScore = channelTarget ? 0.58 : 0.48;
                    return bestScore >= minScore ? bestHref : null;
                }}
                """,
                return_by_value=True,
                )
            )

            if href:
                if target.video_id and target.video_id not in str(href):
                    if target.video_id:
                        href = f"/watch?v={target.video_id}"
                full = href if str(href).startswith("http") else f"{home}{href}"
                log(f"[Mobile legacy] Navigating to matched video: {full[:80]}")
                await tab.get(full)
                await asyncio.sleep(random.uniform(2.0, 4.0))
                return True
        except Exception as exc:
            log(f"[Mobile legacy] Query failed: {exc}")
        await asyncio.sleep(random.uniform(1.0, 2.0))

    return False


async def run_search_warmup(
    tab: Tab,
    video_title: str,
    channel_name: str,
    profile_id: str,
    human_type_fn: Callable,
    log: Callable[[str], None],
    *,
    enabled: bool = True,
    min_queries: int = 3,
    max_queries: int = 5,
) -> None:
    """Port of runSearchWarmup — related searches before exact video search."""
    if not enabled:
        log("[SearchWarmup] skipped")
        return

    seed = sum(ord(c) for c in profile_id) & 0xFFFFFFFF
    rng = random.Random(seed)
    pool = list(WARMUP_QUERY_SEEDS)
    rng.shuffle(pool)
    count = min_queries + rng.randint(0, max(0, max_queries - min_queries))
    queries = pool[:count]

    log(f"[SearchWarmup] starting {len(queries)} related searches")
    cap_start = asyncio.get_event_loop().time()

    for i, query in enumerate(queries):
        if asyncio.get_event_loop().time() - cap_start > 90:
            log("[SearchWarmup] time cap reached")
            break
        log(f"[SearchWarmup] attempt {i + 1}/{len(queries)}: {query}")
        try:
            url = tab.url or ""
            if "youtube.com" in url:
                await tab.evaluate("() => window.scrollTo(0, 0)")
                await asyncio.sleep(random.uniform(0.2, 0.4))
                typed = await type_in_search_bar(tab, query, human_type_fn, mobile=await is_mobile_youtube(tab))
                if typed:
                    await tab.send(
                        cdp.input_.dispatch_key_event("keyDown", key="Enter", code="Enter")
                    )
                    await tab.send(
                        cdp.input_.dispatch_key_event("keyUp", key="Enter", code="Enter")
                    )
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    await tab.evaluate(f"() => window.scrollBy(0, {random.randint(200, 500)})")
                    await asyncio.sleep(random.uniform(1.0, 2.5))
            else:
                await tab.get(f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}")
                await asyncio.sleep(random.uniform(2.0, 3.5))
        except Exception as exc:
            log(f"[SearchWarmup] attempt {i + 1} failed: {exc}")

    log("[SearchWarmup] complete → exact video search")
