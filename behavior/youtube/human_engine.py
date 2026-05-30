"""
Human-emulation primitives: smart-wait, keystroke typing, exact-match discovery.

No tab.get(), no .value= / set_value — CDP keystrokes only.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from nodriver import cdp
from nodriver.core.element import Element
from nodriver.core.tab import Tab

from behavior.youtube.types import ElementNotFoundError, VideoTarget

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FAIL_SCREENSHOT_DIR = PROJECT_ROOT / "logs" / "human_failures"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _extract_str(result: Any) -> str:
    """Safely extract string from tab.evaluate() result — handles RemoteObject or plain str."""
    if isinstance(result, str):
        return result
    if hasattr(result, "value") and isinstance(result.value, str):
        return result.value
    return ""


async def _js_find_text(tab: Tab, text: str) -> bool:
    """
    Check if text is visible in any button/link in DOM — IIFE, instant, never hangs.
    Returns True if found, False otherwise.
    """
    text_json = json.dumps(text.lower())
    try:
        result = await tab.evaluate(
            f"""
            (() => {{
                var want = {text_json};
                var els = document.querySelectorAll('button, a, tp-yt-paper-button, [role="button"]');
                for (var i = 0; i < els.length; i++) {{
                    var t = (els[i].innerText || els[i].textContent || '').trim().toLowerCase();
                    if (t && t.startsWith(want) && els[i].offsetParent !== null) return true;
                }}
                return false;
            }})()
            """,
            return_by_value=True,
        )
        if isinstance(result, bool):
            return result
        if hasattr(result, "value"):
            return bool(result.value)
        return False
    except Exception:
        return False


async def _js_find_xpath(tab: Tab, xpath: str) -> bool:
    """Check if xpath matches any visible element — IIFE, instant, never hangs."""
    xpath_json = json.dumps(xpath)
    try:
        result = await tab.evaluate(
            f"""
            (() => {{
                try {{
                    var r = document.evaluate({xpath_json}, document, null,
                        XPathResult.FIRST_ORDERED_NODE_TYPE, null);
                    var el = r.singleNodeValue;
                    return el && el.offsetParent !== null ? true : false;
                }} catch(e) {{ return false; }}
            }})()
            """,
            return_by_value=True,
        )
        if isinstance(result, bool):
            return result
        if hasattr(result, "value"):
            return bool(result.value)
        return False
    except Exception:
        return False


async def _js_find_selector(tab: Tab, selectors: tuple[str, ...]) -> str:
    """
    Single JS IIFE call — checks all selectors in browser, returns first matching one.
    Never hangs Python side. Returns empty string if none found.
    IIFE form used intentionally — arrow function form '() =>' returns RemoteObject(function).
    """
    sels_json = json.dumps(list(selectors))
    try:
        result = await tab.evaluate(
            f"""
            (() => {{
                var sels = {sels_json};
                for (var i = 0; i < sels.length; i++) {{
                    try {{
                        var el = document.querySelector(sels[i]);
                        if (el && el.offsetWidth > 0 && el.offsetHeight > 0) return sels[i];
                    }} catch(e) {{}}
                }}
                return '';
            }})()
            """,
            return_by_value=True,
        )
        return _extract_str(result)
    except Exception:
        return ""


async def _is_visible_clickable(element: Element) -> bool:
    try:
        result = await element.apply(
            """
            (el) => {
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


async def wait_for_element(
    tab: Tab,
    selector: str,
    *,
    timeout: float = 20.0,
    label: str = "",
    log: Optional[Callable[[str], None]] = None,
) -> Element:
    """
    Poll DOM every 500ms until element is present, visible, and clickable.
    On timeout: screenshot + ElementNotFoundError.
    """
    deadline = time.monotonic() + timeout
    last_error = ""

    while time.monotonic() < deadline:
        matched = await _js_find_selector(tab, (selector,))
        if matched:
            try:
                # Element confirmed by JS — tab.select should return immediately
                element = await tab.select(selector)
                if element and await _is_visible_clickable(element):
                    if log:
                        log(f"Smart-wait OK | {label or selector}")
                    return element
                last_error = "present but not clickable"
            except Exception as exc:
                last_error = str(exc)
        else:
            last_error = "not in DOM"
        await asyncio.sleep(0.5)

    FAIL_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = FAIL_SCREENSHOT_DIR / f"wait_fail_{label or 'element'}_{stamp}.png"
    try:
        await tab.save_screenshot(shot, format="png")
    except Exception:
        shot = Path("screenshot_failed")

    raise ElementNotFoundError(
        f"Smart-wait timeout ({timeout}s) | label={label!r} selector={selector!r} "
        f"last={last_error} screenshot={shot}"
    )


async def wait_for_any_element(
    tab: Tab,
    selectors: tuple[str, ...],
    *,
    timeout: float = 20.0,
    label: str = "",
    log: Optional[Callable[[str], None]] = None,
) -> tuple[Element, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Single JS call checks ALL selectors at once — never hangs Python
        matched_sel = await _js_find_selector(tab, selectors)
        if matched_sel:
            try:
                # Element confirmed by JS — tab.select should return immediately
                element = await tab.select(matched_sel)
                if element and await _is_visible_clickable(element):
                    if log:
                        log(f"Smart-wait OK | {label or matched_sel}")
                    return element, matched_sel
            except Exception:
                pass
        await asyncio.sleep(0.5)

    FAIL_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = FAIL_SCREENSHOT_DIR / f"wait_fail_{label or 'any'}_{stamp}.png"
    try:
        await tab.save_screenshot(shot, format="png")
    except Exception:
        pass

    raise ElementNotFoundError(
        f"Smart-wait timeout ({timeout}s) | label={label!r} selectors={selectors} screenshot={shot}"
    )


async def send_keys_human(
    tab: Tab,
    text: str,
    rng: Any,
    *,
    element: Optional[Element] = None,
) -> None:
    """
    Human-Symmetry typing via CDP char events.

    Per-char delay: 80–250ms (Gaussian around 140ms).
    Word-boundary pause: 200–600ms after each space (~40% chance).
    Mid-word think pause: 1.0–2.5s every 8–15 chars (~12% chance).
    Typo+correction: ~8% per alphabetic char — wrong char → backspace → correct char.
    Pre-type focus delay: 150–450ms.
    """
    if element is not None:
        try:
            await element.focus()
        except Exception:
            pass
        await asyncio.sleep(rng.uniform(0.15, 0.45))

    chars = list(text)
    i = 0
    chars_since_pause = 0

    while i < len(chars):
        char = chars[i]

        # Word-boundary pause: after space, human hesitates before next word
        if char == " " and rng.random() < 0.40:
            await tab.send(cdp.input_.dispatch_key_event("char", text=char))
            await asyncio.sleep(rng.uniform(0.20, 0.60))
            i += 1
            chars_since_pause = 0
            continue

        # Mid-word think pause every 8-15 chars
        chars_since_pause += 1
        if chars_since_pause >= rng.randint(8, 15) and rng.random() < 0.12:
            await asyncio.sleep(rng.uniform(1.0, 2.5))
            chars_since_pause = 0

        # Typo + correction: ~8% for alphabetic chars
        if (
            char.isalpha()
            and i > 0
            and rng.random() < 0.08
            and char.lower() != chars[i - 1].lower()
        ):
            # Type a wrong adjacent key
            wrong = rng.choice("abcdefghijklmnopqrstuvwxyz")
            await tab.send(cdp.input_.dispatch_key_event("char", text=wrong))
            # Brief pause before noticing the mistake
            await asyncio.sleep(rng.uniform(0.09, 0.22))
            # Backspace to erase it
            await tab.send(cdp.input_.dispatch_key_event(
                "keyDown", key="Backspace", code="Backspace", windows_virtual_key_code=8
            ))
            await tab.send(cdp.input_.dispatch_key_event(
                "keyUp", key="Backspace", code="Backspace", windows_virtual_key_code=8
            ))
            # Correction delay (noticed mistake — slower retype)
            await asyncio.sleep(rng.uniform(0.15, 0.40))

        # Type the correct char with Gaussian-distributed delay
        await tab.send(cdp.input_.dispatch_key_event("char", text=char))
        # Per-char delay: Gaussian centred at 140ms, clamped 80–250ms
        raw_delay = rng.gauss(0.140, 0.040)
        await asyncio.sleep(max(0.080, min(0.250, raw_delay)))
        i += 1


async def press_enter(tab: Tab) -> None:
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyDown", key="Enter", code="Enter", windows_virtual_key_code=13
        )
    )
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyUp", key="Enter", code="Enter", windows_virtual_key_code=13
        )
    )


async def address_bar_navigate(tab: Tab, host: str, rng: Any) -> None:
    """Ctrl+L -> clear -> type host -> Enter. Never tab.get()."""
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyDown", key="l", code="KeyL", modifiers=2, windows_virtual_key_code=76
        )
    )
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyUp", key="l", code="KeyL", modifiers=2, windows_virtual_key_code=76
        )
    )
    await asyncio.sleep(rng.uniform(0.35, 0.75))

    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyDown", key="a", code="KeyA", modifiers=2, windows_virtual_key_code=65
        )
    )
    await tab.send(
        cdp.input_.dispatch_key_event(
            "keyUp", key="a", code="KeyA", modifiers=2, windows_virtual_key_code=65
        )
    )
    await asyncio.sleep(rng.uniform(0.08, 0.2))

    await tab.send(
        cdp.input_.dispatch_key_event("keyDown", key="Backspace", code="Backspace")
    )
    await tab.send(
        cdp.input_.dispatch_key_event("keyUp", key="Backspace", code="Backspace")
    )
    await asyncio.sleep(rng.uniform(0.12, 0.3))

    await send_keys_human(tab, host, rng)
    await asyncio.sleep(rng.uniform(0.3, 0.7))
    await press_enter(tab)


def exact_match(title: str, channel: str, target: VideoTarget) -> bool:
    """Require title_hint AND channel_name when both are provided."""
    result_title = _normalize(title)
    result_channel = _normalize(channel)
    want_title = _normalize(target.title_hint or "")
    want_channel = _normalize(target.channel_name or "")

    if target.video_id and target.video_id in (title or channel or ""):
        if not want_title and not want_channel:
            return True

    title_ok = not want_title
    if want_title:
        title_ok = want_title in result_title or result_title in want_title

    channel_ok = not want_channel
    if want_channel:
        channel_ok = (
            want_channel in result_channel
            or result_channel in want_channel
            or all(w in result_channel for w in want_channel.split() if len(w) > 2)
        )

    if want_title and want_channel:
        return title_ok and channel_ok
    if want_title:
        return title_ok
    if want_channel:
        return channel_ok
    if target.video_id:
        combined = f"{title} {channel}"
        return target.video_id in combined
    return False


async def slow_scroll(tab: Tab, rng: Any, *, delta_px: Optional[int] = None) -> None:
    px = delta_px or rng.randint(180, 420)
    try:
        await tab.evaluate(f"(() => window.scrollBy(0, {px}))()", return_by_value=True)
    except Exception:
        pass
    await asyncio.sleep(rng.uniform(0.6, 1.4))


async def wait_for_player(tab: Tab, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ready = await tab.evaluate(
                """
                (() => {
                    const v = document.querySelector('video');
                    if (!v) return false;
                    return v.readyState >= 3 && isFinite(v.duration) && v.duration > 0;
                })()
                """,
                return_by_value=True,
            )
            raw = getattr(ready, "value", ready) if ready is not None else False
            if raw:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False
