from pathlib import Path

import nodriver as uc

STEALTH_URL = "https://bot.sannysoft.com"
ROOT = Path(__file__).resolve().parent.parent
SCREENSHOT_PATH = ROOT / "logs" / "stealth_check.png"


async def main() -> None:
    browser = await uc.start()
    try:
        page = await browser.get(STEALTH_URL)
        await page.sleep(3)
        SCREENSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        await page.save_screenshot(SCREENSHOT_PATH, format="png", full_page=True)
        print("Browser started successfully and navigated to stealth check page")
    finally:
        browser.stop()


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
