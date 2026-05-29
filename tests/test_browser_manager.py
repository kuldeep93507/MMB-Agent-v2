import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nodriver as uc
from providers.BrowserManager import BrowserManager

STEALTH_URL = "https://bot.sannysoft.com"
SCREENSHOT_PATH = ROOT / "logs" / "browser_manager_check.png"

PROFILE_IDS = {
    "morelogin": "2052791530343174144",
    "multilogin": "7d830996-aa0f-453a-b2ee-531092a0737f",
}


async def main() -> None:
    manager = BrowserManager()
    profile_id = PROFILE_IDS.get(manager.provider_name)

    if not profile_id:
        raise ValueError(
            f"No profile ID configured for provider {manager.provider_name!r}. "
            f"Supported: {', '.join(PROFILE_IDS)}"
        )

    print(f"Provider : {manager.provider_name}")
    print(f"Profile  : {profile_id}")

    browser = await manager.get_browser_instance(profile_id)
    try:
        page = await browser.get(STEALTH_URL)
        await page.sleep(3)
        SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        await page.save_screenshot(SCREENSHOT_PATH, format="png", full_page=True)
        print(f"Connected via {manager.provider_name}")
        print(f"Screenshot saved to {SCREENSHOT_PATH}")
    finally:
        browser.stop()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
