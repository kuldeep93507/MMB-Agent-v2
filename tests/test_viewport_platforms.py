"""
Sequential platform tests: Windows → macOS → Android.

Opens YouTube by TYPING youtube.com in the address bar (no forced tab.get).

Usage:
  python tests/test_viewport_platforms.py
  python tests/test_viewport_platforms.py windows
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from nodriver import cdp

from providers.BrowserManager import BrowserManager, resolve_viewport_profile

PROFILES_PATH = PROJECT_ROOT / "data" / "platform_profiles.json"
TEST_ORDER = ("windows", "macos", "android")


async def human_address_bar_navigate(tab, url: str, rng: random.Random) -> None:
    """Type a URL in the omnibox like a human — focus bar, clear, type, Enter."""
    await asyncio.sleep(rng.uniform(0.8, 1.6))

    for key, code in (("l", "KeyL"), ("l", "KeyL")):
        mod = 2
        await tab.send(
            cdp.input_.dispatch_key_event(
                "keyDown", key=key, code=code, modifiers=mod, windows_virtual_key_code=76
            )
        )
        await tab.send(
            cdp.input_.dispatch_key_event(
                "keyUp", key=key, code=code, modifiers=mod, windows_virtual_key_code=76
            )
        )
    await asyncio.sleep(rng.uniform(0.5, 1.0))

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
    await asyncio.sleep(rng.uniform(0.15, 0.35))

    for char in url:
        await tab.send(cdp.input_.dispatch_key_event("char", text=char))
        await asyncio.sleep(rng.uniform(0.07, 0.2))

    await asyncio.sleep(rng.uniform(0.4, 0.8))
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


async def read_viewport_metrics(tab) -> dict:
    """Read viewport metrics with per-field fallback."""
    fields = {
        "innerWidth": "window.innerWidth",
        "innerHeight": "window.innerHeight",
        "dpr": "window.devicePixelRatio",
        "href": "location.href",
        "platform": "navigator.platform",
        "ua": "navigator.userAgent.slice(0, 90)",
    }
    metrics: dict = {}
    for name, expr in fields.items():
        try:
            val = await tab.evaluate(f"{expr}", return_by_value=True)
            if val is not None:
                metrics[name] = val
        except Exception:
            pass
    if not metrics.get("href"):
        try:
            metrics["href"] = tab.url or ""
        except Exception:
            metrics["href"] = ""
    return metrics


def load_profiles() -> dict:
    if not PROFILES_PATH.exists():
        raise FileNotFoundError(
            f"{PROFILES_PATH} missing — run: python tests/create_platform_profiles.py"
        )
    with PROFILES_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


async def test_platform(platform: str, cfg: dict, rng: random.Random) -> bool:
    entry = cfg.get(platform, {})
    profile_id = entry.get("profile_id")
    identity = entry.get("identity") or {}

    if not profile_id:
        print(f"  SKIP {platform}: no profile_id ({entry.get('error', 'unknown')})")
        return False

    expected = resolve_viewport_profile(identity)
    youtube_url = "youtube.com" if platform != "android" else "m.youtube.com"

    print(f"\n{'=' * 60}")
    print(f"TEST: {platform.upper()} | profile={profile_id}")
    print(f"Expected viewport: {expected.os_label} {expected.width}x{expected.height}")
    print(f"Opening via address bar: {youtube_url}")
    print(f"{'=' * 60}")

    manager = BrowserManager()
    browser = None

    try:
        browser = await manager.get_browser_instance(profile_id, identity=identity)
        tab = browser.main_tab
        if tab is None:
            tab = await browser.get("about:blank")
        await asyncio.sleep(1.0)
        await manager.apply_viewport_to_tab(tab, identity)

        await asyncio.sleep(rng.uniform(1.0, 2.0))
        await human_address_bar_navigate(tab, youtube_url, rng)
        await asyncio.sleep(rng.uniform(8.0, 12.0))

        metrics = await read_viewport_metrics(tab)
        if not metrics.get("innerWidth"):
            raise RuntimeError(f"Could not read viewport metrics | url={metrics.get('href')}")

        screenshot = PROJECT_ROOT / "logs" / f"viewport_test_{platform}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        await tab.save_screenshot(screenshot, format="png")

        width_ok = abs(metrics["innerWidth"] - expected.width) <= max(50, expected.width * 0.18)
        height_ok = abs(metrics["innerHeight"] - expected.height) <= max(100, expected.height * 0.18)
        yt_ok = "youtube" in str(metrics.get("href", "")).lower()

        print(f"  URL                      : {metrics.get('href')}")
        print(f"  innerWidth x innerHeight : {metrics['innerWidth']} x {metrics['innerHeight']}")
        print(f"  devicePixelRatio         : {metrics['dpr']}")
        print(f"  navigator.platform       : {metrics['platform']}")
        print(f"  screenshot               : {screenshot}")

        ok = width_ok and height_ok and yt_ok
        print(
            f"  RESULT: {'PASS' if ok else 'FAIL'} "
            f"(width={width_ok} height={height_ok} youtube={yt_ok})"
        )
        return ok
    except Exception as exc:
        print(f"  RESULT: FAIL — {exc}")
        traceback.print_exc()
        return False
    finally:
        if browser is not None:
            try:
                browser.stop()
            except Exception:
                pass
            manager.stop_profile(profile_id)
            print(f"  Profile stopped: {profile_id}")
            await asyncio.sleep(5)


async def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    cfg = load_profiles()
    rng = random.Random()

    arg = (sys.argv[1] if len(sys.argv) > 1 else "all").strip().lower()
    platforms = TEST_ORDER if arg in ("all", "") else [arg]

    if arg not in ("all", "") and arg not in TEST_ORDER:
        print(f"Use: windows | macos | android | all")
        sys.exit(1)

    results: dict[str, bool] = {}
    for i, platform in enumerate(platforms):
        results[platform] = await test_platform(platform, cfg, rng)
        if i < len(platforms) - 1:
            wait = 10
            print(f"\nWaiting {wait}s before next platform...")
            await asyncio.sleep(wait)

    print(f"\n{'=' * 60}")
    print("SUMMARY (order: Windows -> macOS -> Android)")
    for name in TEST_ORDER:
        if name in results:
            print(f"  {name:10} : {'PASS' if results[name] else 'FAIL'}")
    print(f"{'=' * 60}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    asyncio.run(main())
