"""
MMB Batch Runner — run a fixed number of views RIGHT NOW (no 24h spread).

Usage:
    python run_batch.py --views 10
    python run_batch.py --views 10 --platform windows   # windows only
    python run_batch.py --views 10 --platform macos     # mac only
    python run_batch.py --views 10 --jobs data/jobs.json

This script patches jobs.json target_views on the fly and starts the
Orchestrator in batch_now mode so all tasks execute immediately.
Android profiles are automatically excluded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.Orchestrator import (
    DEFAULT_JOBS_PATH,
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
logger = logging.getLogger("mmb.batch")


def _patch_jobs_for_batch(
    jobs_path: Path,
    total_views: int,
    platform_filter: str | None,
) -> dict:
    """
    Load jobs.json, set target_views to total_views, filter out android profiles,
    optionally filter by platform. Returns patched config dict (does NOT write to disk).
    """
    with jobs_path.open(encoding="utf-8") as f:
        cfg = json.load(f)

    # Filter profiles — never include android
    original_profiles = cfg.get("profiles", [])
    filtered = [
        p for p in original_profiles
        if p.get("platform", "").lower() not in ("android",)
    ]
    if platform_filter:
        filtered = [
            p for p in filtered
            if p.get("platform", "").lower() == platform_filter.lower()
        ]
    if not filtered:
        raise ValueError(
            f"No profiles remain after filter (platform={platform_filter!r}). "
            f"Available: {[p.get('platform') for p in original_profiles]}"
        )
    cfg["profiles"] = filtered

    # Set target_views
    for job in cfg.get("jobs", []):
        job["target_views"] = total_views

    # Concurrency: max 3 concurrent (Multilogin safe), stagger handled in Orchestrator
    cfg["max_concurrent_profiles"] = min(3, len(filtered))
    cfg["min_concurrent_profiles"] = 1

    # Daily limit must allow total_views ÷ num_profiles views per profile
    n_profiles = len(filtered)
    views_per_profile = max(1, (total_views + n_profiles - 1) // n_profiles)
    cfg["daily_profile_view_limit"] = max(cfg.get("daily_profile_view_limit", 5), views_per_profile)

    logger.info(
        "Batch config | views=%s profiles=%s (%s) views_per_profile=%s concurrency=%s",
        total_views,
        n_profiles,
        ", ".join(p.get("platform", "?") for p in filtered),
        views_per_profile,
        cfg["max_concurrent_profiles"],
    )
    return cfg


async def run_batch(
    total_views: int,
    platform_filter: str | None = None,
    jobs_path: Path = DEFAULT_JOBS_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
) -> None:
    """Patch config and run Orchestrator in batch_now mode."""

    # Write patched config to a temp file so Orchestrator reads it cleanly
    tmp_jobs = jobs_path.parent / "_batch_tmp_jobs.json"
    try:
        patched = _patch_jobs_for_batch(jobs_path, total_views, platform_filter)
        with tmp_jobs.open("w", encoding="utf-8") as f:
            json.dump(patched, f, indent=2)

        # Use fresh state for each batch (don't re-use previous cycle data)
        tmp_state = state_path.parent / "_batch_tmp_state.json"
        if tmp_state.exists():
            tmp_state.unlink()

        orchestrator = Orchestrator(
            jobs_path=tmp_jobs,
            state_path=tmp_state,
        )

        logger.info("=" * 60)
        logger.info("MMB BATCH START  views=%s  platform=%s", total_views, platform_filter or "all")
        logger.info("=" * 60)

        report = orchestrator.status_report()
        logger.info(
            "Pool: %s profiles | Concurrency: %s",
            report["profile_pool_size"],
            report["concurrency_limit"],
        )

        audit = await orchestrator.run(continuous=False, batch_now=True)

        logger.info("=" * 60)
        logger.info("BATCH COMPLETE")
        logger.info("  Successful views : %s", audit.total_successful_views)
        logger.info("  Failed attempts  : %s", audit.total_failed_attempts)
        logger.info("  Tasks scheduled  : %s", audit.total_tasks_scheduled)
        logger.info("=" * 60)

    finally:
        if tmp_jobs.exists():
            tmp_jobs.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="MMB Batch Runner — run N views immediately")
    parser.add_argument("--views", type=int, default=10, help="Total views to deliver (default: 10)")
    parser.add_argument(
        "--platform",
        choices=["windows", "macos", "mac"],
        default=None,
        help="Restrict to a single platform (default: windows + macos)",
    )
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS_PATH, help="Path to jobs.json")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Path to state file")
    args = parser.parse_args()

    platform = "macos" if args.platform == "mac" else args.platform

    asyncio.run(
        run_batch(
            total_views=args.views,
            platform_filter=platform,
            jobs_path=args.jobs,
            state_path=args.state,
        )
    )


if __name__ == "__main__":
    main()
