"""
MMB Create & Run — 8 fresh Multilogin profiles banana + parallel batch run.

Step 1: 4 Windows + 4 macOS profiles create karo Multilogin mein
Step 2: Sab 8 ek saath parallel YouTube views chalaao

Usage:
    python create_and_run.py --views 8     # 1 view per profile (default)
    python create_and_run.py --views 16    # 2 views per profile
    python create_and_run.py --skip-create # sirf run karo, profiles mat banao
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from core.ProfileFactory import ProfileFactory, ProfileFactoryError
from core.Orchestrator import (
    DEFAULT_STATE_PATH,
    Orchestrator,
    OrchestratorConfig,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("mmb.create_run")

CREATED_PROFILES_PATH = PROJECT_ROOT / "data" / "created_profiles.json"
BATCH_JOBS_PATH       = PROJECT_ROOT / "data" / "_batch_8_jobs.json"
BATCH_STATE_PATH      = PROJECT_ROOT / "data" / "_batch_8_state.json"

# 4 Windows + 4 macOS
PROFILE_PLAN = [
    {"platform": "windows", "name": "MMB-Win-01", "os_type": "windows"},
    {"platform": "windows", "name": "MMB-Win-02", "os_type": "windows"},
    {"platform": "windows", "name": "MMB-Win-03", "os_type": "windows"},
    {"platform": "windows", "name": "MMB-Win-04", "os_type": "windows"},
    {"platform": "macos",   "name": "MMB-Mac-01", "os_type": "macos"},
    {"platform": "macos",   "name": "MMB-Mac-02", "os_type": "macos"},
    {"platform": "macos",   "name": "MMB-Mac-03", "os_type": "macos"},
    {"platform": "macos",   "name": "MMB-Mac-04", "os_type": "macos"},
]


# ── Step 1: Profile Creation ──────────────────────────────────────────────────

def create_profiles() -> list[dict[str, Any]]:
    """
    Create 8 profiles in Multilogin (4 Windows + 4 macOS).
    Returns list of {profile_id, platform} dicts.
    """
    factory = ProfileFactory()
    created: list[dict[str, Any]] = []

    for i, plan in enumerate(PROFILE_PLAN):
        logger.info(
            "[%d/8] Creating %s profile | name=%s",
            i + 1, plan["platform"], plan["name"],
        )
        try:
            result = factory.create_stealth_profile(
                country_code="US",
                provider="multilogin",
                profile_name=plan["name"],
                mobile_first=False,   # Desktop only — no Android
            )

            # Inject os_type so YouTubeManager picks correct strategy
            identity = result.get("identity", {})
            identity["os_type"] = plan["os_type"]
            identity["mobile_first"] = False

            entry = {
                "profile_id": result["profile_id"],
                "platform":   plan["platform"],
                "name":       plan["name"],
                "country_code": "US",
                "provider":   "multilogin",
                "identity":   identity,
            }
            created.append(entry)
            logger.info(
                "  ✓ Created | id=%s platform=%s",
                result["profile_id"], plan["platform"],
            )

            # Small gap between API calls so Multilogin doesn't rate-limit
            if i < len(PROFILE_PLAN) - 1:
                time.sleep(3)

        except ProfileFactoryError as exc:
            logger.error("  ✗ FAILED %s: %s", plan["name"], exc)
            # Continue creating remaining profiles

    # Save created profiles to disk
    CREATED_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CREATED_PROFILES_PATH.open("w", encoding="utf-8") as f:
        json.dump(created, f, indent=2)

    logger.info(
        "Profile creation done | created=%d / 8 | saved to %s",
        len(created), CREATED_PROFILES_PATH,
    )
    return created


# ── Step 2: Build jobs.json for batch ────────────────────────────────────────

def build_jobs_config(profiles: list[dict], views_per_profile: int) -> dict:
    """Build Orchestrator-compatible jobs.json dict."""
    total_views = len(profiles) * views_per_profile

    profiles_list = [
        {
            "profile_id":   p["profile_id"],
            "platform":     p["platform"],
            "country_code": p.get("country_code", "US"),
            "provider":     p.get("provider", "multilogin"),
        }
        for p in profiles
    ]

    return {
        "cycle_hours": 1,
        "country_code": "US",
        "provider": "multilogin",
        "mobile_first": False,
        "timezone": "America/New_York",
        "daily_profile_view_limit": max(views_per_profile, 2),
        "max_concurrent_profiles": len(profiles),   # SARE EK SAATH
        "min_concurrent_profiles": 1,
        "ram_per_profile_mb": 1200,
        "min_inter_arrival_seconds": 30,
        "max_inter_arrival_seconds": 120,
        "auto_create_profiles": False,
        "target_profile_pool_size": len(profiles),
        "perform_engagement": True,
        "like_probability": 0.35,
        "subscribe_probability": 0.08,
        "profiles": profiles_list,
        "jobs": [
            {
                "id": "batch-8-parallel-01",
                "video_id": "KjNyAVwtAUg",
                "search_keywords": "best credit cards 2026",
                "search_keyword_variants": [
                    "best credit cards 2026",
                    "how to earn 1000 dollars monthly from credit cards",
                    "best rewards credit cards usa 2026",
                    "credit card monthly income strategy",
                    "best cash back cards 2026",
                ],
                "title_hint": "Best Credit Card 2026",
                "target_views": total_views,
                "perform_engagement": True,
                "comment_text": "Great strategy for 2026, very useful!",
                "behavior_profile": "serious_learner",
                "referrer_search": True,
            }
        ],
    }


# ── Step 3: Run batch ─────────────────────────────────────────────────────────

async def run_batch(profiles: list[dict], views_per_profile: int) -> None:
    """Run all profiles in parallel immediately."""
    cfg = build_jobs_config(profiles, views_per_profile)

    # Write temp jobs file
    BATCH_JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_JOBS_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    # Clear old state
    if BATCH_STATE_PATH.exists():
        BATCH_STATE_PATH.unlink()

    n = len(profiles)
    total = n * views_per_profile
    logger.info("=" * 60)
    logger.info("MMB PARALLEL BATCH | profiles=%d  total_views=%d", n, total)
    logger.info("Platforms: %s", ", ".join(p["platform"] for p in profiles))
    logger.info("Concurrency: %d (sab ek saath)", cfg["max_concurrent_profiles"])
    logger.info("=" * 60)

    orchestrator = Orchestrator(
        jobs_path=BATCH_JOBS_PATH,
        state_path=BATCH_STATE_PATH,
    )

    audit = await orchestrator.run(continuous=False, batch_now=True)

    logger.info("=" * 60)
    logger.info("BATCH COMPLETE")
    logger.info("  Successful : %s / %s views", audit.total_successful_views, total)
    logger.info("  Failed     : %s", audit.total_failed_attempts)
    logger.info("=" * 60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Create 8 profiles + run parallel batch")
    parser.add_argument(
        "--views", type=int, default=1,
        help="Views per profile (default: 1 = 8 total views)",
    )
    parser.add_argument(
        "--skip-create", action="store_true",
        help="Skip profile creation, use already-created profiles from created_profiles.json",
    )
    args = parser.parse_args()

    if args.skip_create:
        if not CREATED_PROFILES_PATH.exists():
            logger.error("created_profiles.json nahi mila. Pehle bina --skip-create ke chalao.")
            sys.exit(1)
        with CREATED_PROFILES_PATH.open(encoding="utf-8") as f:
            profiles = json.load(f)
        logger.info("Loaded %d saved profiles", len(profiles))
    else:
        profiles = create_profiles()

    if not profiles:
        logger.error("Koi profile create nahi hua. Check Multilogin connection.")
        sys.exit(1)

    asyncio.run(run_batch(profiles, views_per_profile=args.views))


if __name__ == "__main__":
    main()
