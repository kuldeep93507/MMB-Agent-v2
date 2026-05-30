"""
ShuffleEngine — Video shuffle with per-profile deduplication + rotation strategy.

Modules:
  VideoShuffle    — Pick videos from a target list ensuring no profile watches
                    the same video twice in a cycle.
  RotationStrategy — Distribute total target views evenly across the profile pool.
  ViewAssignment  — Final pairing: profile × video × entry_path for each view.

Usage::
    engine = ShuffleEngine(videos=[...], profiles=[...], rng=random.Random(42))
    assignments = engine.plan_cycle(total_views=20)
    for a in assignments:
        print(a.profile_id, a.video_id, a.entry_path)
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEDUP_PATH = PROJECT_ROOT / "data" / "shuffle_dedup.json"
DEFAULT_LOG_PATH   = PROJECT_ROOT / "logs" / "shuffle_engine.log"

ENTRY_PATHS = ("search", "homepage", "direct")


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class VideoTarget:
    """A single video in the target pool."""
    video_id:         str
    search_keywords:  str
    title_hint:       str = ""
    keyword_variants: list[str] = field(default_factory=list)
    weight:           float = 1.0    # relative probability of selection

    def pick_keywords(self, rng: random.Random) -> str:
        if self.keyword_variants:
            return rng.choice(self.keyword_variants)
        return self.search_keywords


@dataclass
class ViewAssignment:
    """A single planned view: which profile watches which video via which path."""
    profile_id:  str
    platform:    str
    video_id:    str
    keywords:    str
    title_hint:  str
    entry_path:  str           # search | homepage | direct
    cycle_key:   str           # date string — used for dedup reset


# ── Deduplication Store ───────────────────────────────────────────────────────

class DedupStore:
    """
    Tracks which (profile, video) pairs have been used this cycle.
    Persisted to disk — survives crashes.

    Layout:
        {
          "2026-05-30": {
            "profile-id-1": ["videoA", "videoB"],
            ...
          }
        }
    """

    def __init__(self, path: Path = DEFAULT_DEDUP_PATH) -> None:
        self._path = path
        self._data: dict[str, dict[str, list[str]]] = {}
        self._load()

    def cycle_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def has_seen(self, profile_id: str, video_id: str) -> bool:
        key = self.cycle_key()
        return video_id in self._data.get(key, {}).get(profile_id, [])

    def mark_seen(self, profile_id: str, video_id: str) -> None:
        key = self.cycle_key()
        if key not in self._data:
            # Prune old cycle keys (keep only last 3 days)
            self._data = {k: v for k, v in self._data.items() if k >= key[:7]}
            self._data[key] = {}
        self._data[key].setdefault(profile_id, [])
        if video_id not in self._data[key][profile_id]:
            self._data[key][profile_id].append(video_id)
        self._save()

    def seen_by_profile(self, profile_id: str) -> list[str]:
        key = self.cycle_key()
        return list(self._data.get(key, {}).get(profile_id, []))

    def reset_profile(self, profile_id: str) -> None:
        key = self.cycle_key()
        if key in self._data:
            self._data[key].pop(profile_id, None)
        self._save()

    def reset_cycle(self) -> None:
        self._data = {}
        self._save()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        tmp.replace(self._path)


# ── Video Shuffle ─────────────────────────────────────────────────────────────

class VideoShuffle:
    """
    Picks a video for a given profile ensuring no repeats in the same cycle.

    If the profile has already seen all available videos this cycle,
    it resets the dedup for that profile and picks fresh.
    """

    def __init__(
        self,
        videos: list[VideoTarget],
        dedup: DedupStore,
        rng: random.Random,
    ) -> None:
        if not videos:
            raise ValueError("VideoShuffle requires at least one video.")
        self._videos = videos
        self._dedup  = dedup
        self._rng    = rng

    def pick(self, profile_id: str) -> VideoTarget:
        """Pick an unseen video for this profile. Resets dedup if all seen."""
        seen = set(self._dedup.seen_by_profile(profile_id))
        available = [v for v in self._videos if v.video_id not in seen]

        if not available:
            # All videos seen — reset this profile's dedup for fresh cycle
            self._dedup.reset_profile(profile_id)
            available = list(self._videos)

        # Weighted random selection
        weights = [v.weight for v in available]
        [chosen] = self._rng.choices(available, weights=weights, k=1)
        self._dedup.mark_seen(profile_id, chosen.video_id)
        return chosen

    def pick_keywords(self, video: VideoTarget) -> str:
        return video.pick_keywords(self._rng)


# ── Rotation Strategy ─────────────────────────────────────────────────────────

class RotationStrategy:
    """
    Distribute total_views evenly across all eligible profiles.

    Strategies:
      "round_robin"  — cycle through profiles in order
      "weighted"     — prefer less-used profiles
      "random"       — fully random assignment
    """

    def __init__(
        self,
        profiles: list[dict[str, Any]],  # [{profile_id, platform, ...}]
        rng: random.Random,
        strategy: str = "round_robin",
    ) -> None:
        if not profiles:
            raise ValueError("RotationStrategy requires at least one profile.")
        self._profiles = list(profiles)
        self._rng      = rng
        self._strategy = strategy
        self._cursor   = 0

    def assign(self, total_views: int) -> list[dict[str, Any]]:
        """
        Return a list of length=total_views where each element is a profile dict.
        Views are distributed as evenly as possible.
        """
        n = len(self._profiles)
        if self._strategy == "round_robin":
            return self._round_robin(total_views, n)
        elif self._strategy == "weighted":
            return self._weighted(total_views)
        else:
            return self._random_assign(total_views)

    def _round_robin(self, total: int, n: int) -> list[dict[str, Any]]:
        result = []
        for i in range(total):
            result.append(self._profiles[i % n])
        return result

    def _weighted(self, total: int) -> list[dict[str, Any]]:
        """Prefer profiles with fewer total_views."""
        result = []
        for _ in range(total):
            weights = [
                1.0 / (1 + p.get("total_views", 0))
                for p in self._profiles
            ]
            [chosen] = self._rng.choices(self._profiles, weights=weights, k=1)
            chosen["total_views"] = chosen.get("total_views", 0) + 1
            result.append(chosen)
        return result

    def _random_assign(self, total: int) -> list[dict[str, Any]]:
        return [self._rng.choice(self._profiles) for _ in range(total)]


# ── Shuffle Engine (main) ─────────────────────────────────────────────────────

class ShuffleEngine:
    """
    Combines VideoShuffle + RotationStrategy to plan a full view cycle.

    Given:
      - A list of VideoTarget objects (target pool)
      - A list of profile dicts (eligible profiles)
      - total_views to deliver

    Returns:
      - List of ViewAssignment objects ready for the Orchestrator to execute.
    """

    def __init__(
        self,
        videos: list[VideoTarget],
        profiles: list[dict[str, Any]],
        *,
        rng: Optional[random.Random] = None,
        rotation_strategy: str = "round_robin",
        dedup_path: Optional[Path] = None,
        log_path: Optional[Path] = None,
    ) -> None:
        self._rng      = rng or random.Random()
        self._dedup    = DedupStore(dedup_path or DEFAULT_DEDUP_PATH)
        self._shuffle  = VideoShuffle(videos, self._dedup, self._rng)
        self._rotation = RotationStrategy(profiles, self._rng, rotation_strategy)
        self._logger   = self._make_logger(log_path or DEFAULT_LOG_PATH)
        self._profiles = profiles
        self._videos   = videos

    def plan_cycle(self, total_views: int) -> list[ViewAssignment]:
        """
        Plan total_views assignments.

        Returns list of ViewAssignment — each profile gets a unique video
        (dedup-enforced) via a randomly picked entry path.
        """
        if total_views <= 0:
            return []

        assignments: list[ViewAssignment] = []
        profile_slots = self._rotation.assign(total_views)
        cycle_key = self._dedup.cycle_key()

        for profile in profile_slots:
            pid      = profile["profile_id"]
            platform = profile.get("platform", "windows")
            video    = self._shuffle.pick(pid)
            keywords = self._shuffle.pick_keywords(video)
            path     = self._pick_entry_path(platform)

            assignments.append(ViewAssignment(
                profile_id = pid,
                platform   = platform,
                video_id   = video.video_id,
                keywords   = keywords,
                title_hint = video.title_hint,
                entry_path = path,
                cycle_key  = cycle_key,
            ))

        self._logger.info(
            "Cycle planned | views=%d profiles=%d videos=%d",
            total_views, len(self._profiles), len(self._videos),
        )
        self._log_distribution(assignments)
        return assignments

    def _pick_entry_path(self, platform: str) -> str:
        """
        Platform-aware entry path selection.
        Android: only search (notification path is Coming Soon).
        Desktop: search 60%, homepage 30%, direct 10%.
        """
        if platform == "android":
            return "search"
        # Desktop weights
        paths   = ["search", "homepage", "direct"]
        weights = [0.60,     0.30,       0.10]
        [chosen] = self._rng.choices(paths, weights=weights, k=1)
        return chosen

    def _log_distribution(self, assignments: list[ViewAssignment]) -> None:
        """Log per-profile and per-video counts."""
        per_profile: dict[str, int] = {}
        per_video:   dict[str, int] = {}
        for a in assignments:
            per_profile[a.profile_id[:8]] = per_profile.get(a.profile_id[:8], 0) + 1
            per_video[a.video_id]         = per_video.get(a.video_id, 0) + 1
        self._logger.info("  Per-profile: %s", per_profile)
        self._logger.info("  Per-video:   %s", per_video)

    def export_plan(self, assignments: list[ViewAssignment]) -> list[dict[str, Any]]:
        """Convert assignments to JSON-serializable list."""
        return [asdict(a) for a in assignments]

    @staticmethod
    def _make_logger(log_path: Path | str) -> logging.Logger:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("mmb.shuffle_engine")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            h = logging.FileHandler(path, encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(h)
        return logger


# ── Convenience: load from jobs.json ─────────────────────────────────────────

def load_videos_from_jobs(jobs_path: Path) -> list[VideoTarget]:
    """Load VideoTarget list from jobs.json format."""
    with jobs_path.open(encoding="utf-8") as f:
        data = json.load(f)
    targets = []
    for job in data.get("jobs", []):
        variants = job.get("search_keyword_variants", [])
        kw = job.get("search_keywords", "")
        if isinstance(kw, list):
            primary = kw[0] if kw else ""
            if not variants:
                variants = kw
        else:
            primary = kw
        targets.append(VideoTarget(
            video_id        = str(job.get("video_id", "")),
            search_keywords = primary,
            title_hint      = str(job.get("title_hint", "")),
            keyword_variants= variants,
            weight          = float(job.get("weight", 1.0)),
        ))
    return targets
