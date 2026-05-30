"""
Create dedicated Multilogin profiles for Windows (existing), macOS, and Android.

Saves IDs to data/platform_profiles.json for sequential platform tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.ProfileFactory import ProfileFactory, ProfileFactoryError
from services.IdentityManager import IdentityManager

PROFILES_PATH = PROJECT_ROOT / "data" / "platform_profiles.json"
WINDOWS_PROFILE_ID = "c58a40dc-d6ff-4234-8d26-a592804d32ea"


def _save(data: dict) -> None:
    PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PROFILES_PATH.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    print(f"Saved {PROFILES_PATH}")


def main() -> None:
    factory = ProfileFactory()
    identity_mgr = IdentityManager()
    store: dict = {
        "provider": "multilogin",
        "country_code": "US",
        "windows": {
            "profile_id": WINDOWS_PROFILE_ID,
            "label": "MMB-Windows-Existing",
        },
    }

    print("=== Android profile ===")
    try:
        android = factory.create_stealth_profile(
            country_code="US",
            provider="multilogin",
            profile_name="MMB-Android-Finance",
            mobile_first=True,
            mobile_platform="android",
        )
        store["android"] = {
            "profile_id": android["profile_id"],
            "label": "MMB-Android-Finance",
            "identity": android["identity"],
        }
        print(f"  Created Android: {android['profile_id']}")
    except ProfileFactoryError as exc:
        print(f"  Android create failed: {exc}")
        store["android"] = {"error": str(exc)}

    print("\n=== macOS profile ===")
    try:
        mac_identity = identity_mgr.generate_identity(country_code="US")
        mac_identity["os_type"] = "macos"
        mac_identity["mobile_first"] = False
        mac_identity["user_agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        mac_identity["navigator_platform"] = "MacIntel"
        mac_identity["screen_resolution"] = "1440x900"

        mac_id = factory._browser_manager.create_profile(
            identity=mac_identity,
            provider="multilogin",
            profile_name="MMB-macOS-Finance",
        )
        mac_identity["profile_id"] = mac_id
        identity_mgr.store_identity(mac_id, mac_identity)
        store["macos"] = {
            "profile_id": mac_id,
            "label": "MMB-macOS-Finance",
            "identity": mac_identity,
        }
        print(f"  Created macOS: {mac_id}")
    except Exception as exc:
        print(f"  macOS create failed: {exc}")
        store["macos"] = {"error": str(exc)}

    windows_identity = identity_mgr.generate_identity(country_code="US", profile_id=WINDOWS_PROFILE_ID)
    windows_identity["os_type"] = "windows"
    windows_identity["mobile_first"] = False
    store["windows"]["identity"] = windows_identity

    _save(store)
    print("\nDone.")


if __name__ == "__main__":
    main()
