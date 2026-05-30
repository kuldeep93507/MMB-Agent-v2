"""One-shot live test on a fixed Multilogin profile."""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from behavior.YouTubeManager import VideoTarget, YouTubeManager, YouTubeManagerError

PROFILE_ID = "c58a40dc-d6ff-4234-8d26-a592804d32ea"
KEYWORDS = [
    "best credit cards 2026",
    "how to earn 1000 dollars monthly from credit cards",
    "best rewards credit cards usa 2026",
    "credit card monthly income strategy",
    "best cash back cards 2026",
]


async def main() -> None:
    print(f"Starting live test | profile={PROFILE_ID} provider=multilogin")
    manager = YouTubeManager(
        profile_id=PROFILE_ID,
        country_code="US",
        force_mobile=True,
        behavior_profile="serious_learner",
        referrer_search=True,
    )
    tab = await manager.open_session()
    try:
        target = VideoTarget(
            video_id="KjNyAVwtAUg",
            search_keywords=random.choice(KEYWORDS),
            title_hint="Best Credit Card 2026",
        )
        print(f"Navigating | keywords={target.search_keywords!r}")
        route = await manager.navigate_to_video(tab, target, force_route="search")
        print(f"Route: {route}")

        result = await manager.watch_video(
            tab,
            perform_engagement=True,
            comment_text="This is a very helpful strategy for 2026, thanks for sharing!",
            like_probability=0.35,
            subscribe_probability=0.08,
        )
        print(
            f"Done | platform={result.platform} watched={result.actual_watch_seconds:.0f}s "
            f"fraction={result.watch_fraction:.0%} liked={result.liked} subscribed={result.subscribed}"
        )
    except YouTubeManagerError as exc:
        print(f"FAILED: {exc}")
        raise
    finally:
        await manager.close_session()


if __name__ == "__main__":
    asyncio.run(main())
