"""
End-to-end test: ProfileFactory stealth profile + AccountManager Gmail creation.

WARNING: Creates a real Multilogin/MoreLogin profile and attempts a live Gmail signup.
Requires FIVESIM_API_KEY, provider desktop app running, and 5sim balance.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nodriver as uc
from core.ProfileFactory import ProfileFactory, ProfileFactoryError
from providers.BrowserManager import BrowserManager
from services.AccountManager import AccountManager, AccountManagerError

COUNTRY_CODE = "US"
PROFILE_NAME = os.getenv("DEFAULT_PROFILE_NAME", "MMB")


def setup_console_logging() -> None:
    """Mirror AccountManager and ProfileFactory file logs to the console."""
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    for logger_name in ("mmb.account_manager", "mmb.profile_factory"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            logger.addHandler(console)


def log_step(message: str) -> None:
    """Print a clearly visible pipeline step to the console."""
    print(f"\n>>> {message}")


async def main() -> None:
    setup_console_logging()

    provider = os.getenv("BROWSER_PROVIDER", "multilogin").strip().lower()
    log_step(f"Initializing BrowserManager and AccountManager (provider={provider})")
    browser_manager = BrowserManager()
    account_manager = AccountManager()
    print(f"    BrowserManager active provider : {browser_manager.provider_name}")
    print(f"    AccountManager log file        : logs/account_manager.log")

    factory = ProfileFactory()
    profile_id: str | None = None

    try:
        log_step(f"Creating stealth profile | country={COUNTRY_CODE} name={PROFILE_NAME}")
        profile_result = factory.create_stealth_profile(
            country_code=COUNTRY_CODE,
            provider=provider,
            profile_name=PROFILE_NAME,
        )
        profile_id = profile_result["profile_id"]
        identity = profile_result["identity"]

        print(f"    Profile ID  : {profile_id}")
        print(f"    Timezone    : {identity.get('timezone')}")
        print(f"    IP (synced) : {identity.get('ip_address', 'n/a')}")
        print(f"    City        : {identity.get('city', 'n/a')}")

        log_step("Starting Gmail account creation pipeline")
        print("    Expected internal steps:")
        print("      1. Browser warm-up (Amazon / Wikipedia / BBC ...)")
        print("      2. Name + username + password generation")
        print("      3. Gmail signup form fill (human typing)")
        print("      4. 5sim SMS number request")
        print("      5. OTP poll + entry")
        print("      6. Cookie + metadata save")

        result = await account_manager.create_gmail_account(
            profile_id=profile_id,
            country_code=COUNTRY_CODE,
        )

        log_step("SUCCESS — Gmail account created")
        print(f"    Email         : {result.email}")
        print(f"    Password      : {result.password}")
        print(f"    Phone         : {result.phone}")
        print(f"    Profile ID    : {result.profile_id}")
        print(f"    Cookies file  : {result.cookies_path}")
        print(f"    Metadata file : {result.metadata_path}")

        if result.cookies_path.exists() and result.cookies_path.stat().st_size > 0:
            print("    Cookies saved : YES")
        else:
            print("    Cookies saved : NO — file missing or empty")

        if result.metadata_path.exists():
            print("    Metadata saved: YES")
        else:
            print("    Metadata saved: NO")

    except ProfileFactoryError as exc:
        log_step(f"FAILED at profile creation: {exc}")
        sys.exit(1)
    except AccountManagerError as exc:
        log_step(f"FAILED at Gmail creation: {exc}")
        if profile_id:
            print(f"    Profile {profile_id} may still exist in {provider}.")
        sys.exit(1)
    except KeyboardInterrupt:
        log_step("Test interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
