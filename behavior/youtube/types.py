"""Shared types, constants, and dataclasses for YouTube automation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "youtube_universal.log"
SELECTOR_FAILURE_LOG = PROJECT_ROOT / "logs" / "youtube_selector_failures.json"

YOUTUBE_HOME_DESKTOP = "https://www.youtube.com"
YOUTUBE_HOME_MOBILE = "https://m.youtube.com"

TRAFFIC_MIX: tuple[tuple[str, float], ...] = (
    ("search", 0.60),
    ("homepage", 0.20),
    ("suggested", 0.20),
)

PLAYBACK_SPEEDS: tuple[float, ...] = (1.0, 1.0, 1.0, 1.25, 1.25)


class PlatformKind(str, Enum):
    DESKTOP = "desktop"
    MOBILE = "mobile"


class NavigationRoute(str, Enum):
    SEARCH = "search"
    HOMEPAGE = "homepage"
    SUGGESTED = "suggested"
    DIRECT = "direct"


class YouTubeManagerError(Exception):
    """Raised when YouTube automation fails irrecoverably."""


class ElementNotFoundError(YouTubeManagerError):
    """Raised when a DOM target is missing — retry-with-scroll, do not tear down browser."""


@dataclass
class VideoTarget:
    """Describes the video the session should reach organically."""

    video_id: Optional[str] = None
    search_keywords: Optional[str] = None
    title_hint: Optional[str] = None
    channel_name: Optional[str] = None
    direct_url: Optional[str] = None

    def validate(self) -> None:
        if self.direct_url:
            return
        if not self.video_id and not self.search_keywords:
            raise YouTubeManagerError(
                "Provide video_id or search_keywords for organic navigation."
            )


@dataclass
class WatchSessionResult:
    """Summary of a completed watch session."""

    platform: str
    route: str
    video_id: Optional[str]
    planned_watch_seconds: float
    actual_watch_seconds: float
    watch_fraction: float
    liked: bool = False
    subscribed: bool = False
    commented: bool = False
    engagement_events: list[str] = field(default_factory=list)


class EntryPath(str, Enum):
    SEARCH = "search"
    NOTIFICATION = "notification"
    HOMEPAGE = "homepage"


class AdStrategy(str, Enum):
    SKIP_ALL = "skip_all"
    CLICK_END_AD = "click_end_ad"
    WATCH_ALL = "watch_all"


class EngagementIntensity(str, Enum):
    LOW = "low"       # 1-2 random interactions
    MEDIUM = "medium" # 3-4 random interactions
    HIGH = "high"     # 5+ all interactions enabled


@dataclass
class ProfileConfig:
    """
    User-configurable per-profile behaviour.
    Pass this to YouTubeManager to fully control every session.

    Example::
        cfg = ProfileConfig(
            entry_path='notification',
            actions={'like': True, 'subscribe': True, 'comment': True, 'bell': True},
            ad_strategy='skip_all',
            watch_time_pct=0.85,
            engagement_intensity='high',
            own_channel_ids=['UCxxxxxx', 'UCyyyyyy'],
            comment_text='Great video!',
        )
        manager = YouTubeManager(profile_id=..., profile_config=cfg)
    """
    entry_path: str = "search"          # EntryPath value
    actions: dict[str, bool] = field(default_factory=lambda: {
        "like": True,
        "subscribe": True,
        "comment": False,
        "bell": True,
        "dislike": False,
    })
    ad_strategy: str = "skip_all"       # AdStrategy value
    watch_time_pct: float = 0.80        # 0.0 – 1.0
    engagement_intensity: str = "medium"  # EngagementIntensity value
    own_channel_ids: List[str] = field(default_factory=list)
    comment_text: Optional[str] = None

    def __post_init__(self) -> None:
        # Normalise & validate
        self.entry_path = self.entry_path.lower().strip()
        self.ad_strategy = self.ad_strategy.lower().strip()
        self.engagement_intensity = self.engagement_intensity.lower().strip()
        self.watch_time_pct = max(0.10, min(1.0, self.watch_time_pct))
        valid_paths = {e.value for e in EntryPath}
        if self.entry_path not in valid_paths:
            raise ValueError(f"entry_path must be one of {valid_paths}")
        valid_ad = {e.value for e in AdStrategy}
        if self.ad_strategy not in valid_ad:
            raise ValueError(f"ad_strategy must be one of {valid_ad}")
        valid_int = {e.value for e in EngagementIntensity}
        if self.engagement_intensity not in valid_int:
            raise ValueError(f"engagement_intensity must be one of {valid_int}")

    def action_enabled(self, key: str) -> bool:
        return bool(self.actions.get(key, False))

    @property
    def interaction_count(self) -> int:
        """How many extra micro-interactions to do based on intensity."""
        return {"low": 2, "medium": 4, "high": 7}.get(self.engagement_intensity, 4)


@dataclass
class InteractionContext:
    """Runtime dependencies injected into platform strategies."""

    identity: dict[str, Any]
    rng: Any
    logger: Any
    watch_mean: float
    platform: PlatformKind
    behavior_profile: str = "default"
    pause_probability: float = 0.10
    watch_chunk_min: float = 4.0
    watch_chunk_max: float = 18.0

    @property
    def is_mobile(self) -> bool:
        return self.platform == PlatformKind.MOBILE

    @property
    def youtube_home(self) -> str:
        return YOUTUBE_HOME_MOBILE if self.is_mobile else YOUTUBE_HOME_DESKTOP
