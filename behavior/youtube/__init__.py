"""Cross-platform YouTube interaction strategies."""

from behavior.youtube.base import YouTubeInteraction
from behavior.youtube.desktop import DesktopInteraction
from behavior.youtube.mobile import MobileInteraction
from behavior.youtube.types import (
    NavigationRoute,
    PlatformKind,
    VideoTarget,
    WatchSessionResult,
    YouTubeManagerError,
)

__all__ = [
    "DesktopInteraction",
    "MobileInteraction",
    "NavigationRoute",
    "PlatformKind",
    "VideoTarget",
    "WatchSessionResult",
    "YouTubeInteraction",
    "YouTubeManagerError",
]
