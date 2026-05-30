"""
System-wide orchestrator for distributing YouTube views across profiles and videos.

Manages job queues, 24-hour organic scheduling, RAM-aware concurrency, profile
rotation, and the full BrowserManager → YouTubeManager pipeline.

Batch / immediate mode:
    Pass ``batch_now=True`` to ``run()`` to schedule all tasks for *right now*
    (ignores inter-arrival spread — useful for quick test runs).

Multilogin safety:
    ``_PROFILE_START_SEMAPHORE`` limits simultaneous profile-start HTTP calls
    to 2 so the Multilogin launcher never gets overloaded.  A 12-second stagger
    delay is inserted between each profile start as well.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import random
import sys
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from behavior.YouTubeManager import VideoTarget, YouTubeManager, YouTubeManagerError
from core.ProfileFactory import ProfileFactory, ProfileFactoryError
from core.ProfileManager import HealthStatus, ManagedProfile, ProfileManager
from core.ShuffleEngine import ShuffleEngine, VideoTarget as ShuffleVideoTarget, load_videos_from_jobs
from providers.BrowserManager import BrowserManager
from services.IdentityManager import IdentityManager

# Multilogin safety: max 2 simultaneous profile-start API calls
_PROFILE_START_SEMAPHORE: Optional[asyncio.Semaphore] = None
_PROFILE_START_STAGGER_SECONDS = 12  # wait between each start to avoid 429 / crash

DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_JOBS_PATH = PROJECT_ROOT / "data" / "jobs.json"
DEFAULT_STATE_PATH = PROJECT_ROOT / "data" / "orchestrator_state.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "orchestrator.log"

# Relative traffic weight per hour (0–23). Peak evening, trough late night.
HOURLY_TRAFFIC_WEIGHTS: tuple[float, ...] = (
    0.12, 0.08, 0.06, 0.05, 0.05, 0.06,
    0.10, 0.18, 0.35, 0.50, 0.55, 0.60,
    0.70, 0.75, 0.72, 0.68, 0.65, 0.70,
    0.85, 0.95, 1.00, 0.90, 0.60, 0.30,
)


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class OrchestratorError(Exception):
    """Raised when orchestrator configuration or execution fails."""


@dataclass
class JobDefinition:
    """Static job configuration loaded from jobs.json."""

    id: str
    video_id: str
    search_keywords: str
    title_hint: Optional[str] = None
    target_views: int = 1
    perform_engagement: Optional[bool] = None
    comment_text: Optional[str] = None
    behavior_profile: Optional[str] = None
    referrer_search: bool = False
    search_keyword_variants: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobDefinition:
        job_id = str(data.get("id") or data.get("job_id") or uuid.uuid4().hex[:12])
        video_id = str(data.get("video_id", "")).strip()

        variants_raw = data.get("search_keyword_variants")
        variants: list[str] = []
        if isinstance(variants_raw, list):
            variants = [str(v).strip() for v in variants_raw if str(v).strip()]
        elif isinstance(data.get("search_keywords"), list):
            variants = [str(v).strip() for v in data["search_keywords"] if str(v).strip()]

        keywords_raw = data.get("search_keywords") or data.get("keywords") or ""
        if isinstance(keywords_raw, list):
            keywords = keywords_raw[0].strip() if keywords_raw else ""
            if not variants:
                variants = [str(v).strip() for v in keywords_raw if str(v).strip()]
        else:
            keywords = str(keywords_raw).strip()
            if not variants and keywords:
                variants = [part.strip() for part in keywords.split(",") if part.strip()]

        if not video_id and not keywords and not variants:
            raise OrchestratorError(f"Job {job_id} requires video_id or search_keywords.")

        if not keywords and variants:
            keywords = variants[0]

        return cls(
            id=job_id,
            video_id=video_id,
            search_keywords=keywords,
            title_hint=data.get("title_hint"),
            target_views=max(1, int(data.get("target_views", 1))),
            perform_engagement=data.get("perform_engagement"),
            comment_text=data.get("comment_text"),
            behavior_profile=data.get("behavior_profile"),
            referrer_search=bool(data.get("referrer_search", False)),
            search_keyword_variants=variants,
        )

    def pick_search_keywords(self, rng: random.Random) -> str:
        if self.search_keyword_variants:
            return rng.choice(self.search_keyword_variants)
        return self.search_keywords

    def to_video_target(self, rng: Optional[random.Random] = None) -> VideoTarget:
        keywords = self.pick_search_keywords(rng) if rng else self.search_keywords
        return VideoTarget(
            video_id=self.video_id or None,
            search_keywords=keywords or None,
            title_hint=self.title_hint,
        )


@dataclass
class ViewTask:
    """A single scheduled view execution unit."""

    task_id: str
    job_id: str
    scheduled_at: str
    status: JobStatus = JobStatus.PENDING
    profile_id: Optional[str] = None
    attempts: int = 0
    last_error: Optional[str] = None
    completed_at: Optional[str] = None
    route: Optional[str] = None
    watch_seconds: Optional[float] = None


@dataclass
class JobRuntime:
    """Mutable job progress tracked across cycles."""

    definition: JobDefinition
    status: JobStatus = JobStatus.PENDING
    completed_views: int = 0
    failed_views: int = 0
    tasks: list[ViewTask] = field(default_factory=list)

    @property
    def remaining_views(self) -> int:
        return max(0, self.definition.target_views - self.completed_views)


@dataclass
class ProfileRecord:
    """Profile pool entry with daily usage tracking."""

    profile_id: str
    country_code: str
    provider: str
    platform: Optional[str] = None
    daily_views: int = 0
    total_views: int = 0
    last_used_at: Optional[str] = None
    usage_date: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class OrchestratorAudit:
    """Rolling audit counters persisted with state."""

    total_successful_views: int = 0
    total_failed_attempts: int = 0
    total_tasks_scheduled: int = 0
    cycles_completed: int = 0
    last_cycle_started_at: Optional[str] = None
    last_cycle_completed_at: Optional[str] = None


@dataclass
class OrchestratorConfig:
    """Top-level orchestrator settings from jobs.json."""

    cycle_hours: int = 24
    country_code: str = "US"
    provider: str = "morelogin"
    mobile_first: bool = True
    timezone: str = "America/New_York"
    daily_profile_view_limit: int = 5
    max_concurrent_profiles: int = 8
    min_concurrent_profiles: int = 1
    ram_per_profile_mb: int = 1200
    min_inter_arrival_seconds: int = 180
    max_inter_arrival_seconds: int = 7200
    auto_create_profiles: bool = True
    target_profile_pool_size: int = 10
    perform_engagement: bool = False
    like_probability: float = 0.45
    subscribe_probability: float = 0.12
    profiles: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[JobDefinition] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> OrchestratorConfig:
        if not path.exists():
            raise OrchestratorError(f"Jobs configuration not found: {path}")

        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)

        jobs = [JobDefinition.from_dict(item) for item in raw.get("jobs", [])]
        if not jobs:
            raise OrchestratorError("jobs.json must contain at least one job.")

        return cls(
            cycle_hours=int(raw.get("cycle_hours", 24)),
            country_code=str(raw.get("country_code", "US")).upper(),
            provider=str(raw.get("provider", os.getenv("BROWSER_PROVIDER", "morelogin"))).lower(),
            mobile_first=bool(raw.get("mobile_first", True)),
            timezone=str(raw.get("timezone", "America/New_York")),
            daily_profile_view_limit=max(1, int(raw.get("daily_profile_view_limit", 5))),
            max_concurrent_profiles=max(1, int(raw.get("max_concurrent_profiles", 8))),
            min_concurrent_profiles=max(1, int(raw.get("min_concurrent_profiles", 1))),
            ram_per_profile_mb=max(512, int(raw.get("ram_per_profile_mb", 1200))),
            min_inter_arrival_seconds=max(30, int(raw.get("min_inter_arrival_seconds", 180))),
            max_inter_arrival_seconds=max(60, int(raw.get("max_inter_arrival_seconds", 7200))),
            auto_create_profiles=bool(raw.get("auto_create_profiles", True)),
            target_profile_pool_size=max(1, int(raw.get("target_profile_pool_size", 10))),
            perform_engagement=bool(raw.get("perform_engagement", False)),
            like_probability=float(raw.get("like_probability", 0.45)),
            subscribe_probability=float(raw.get("subscribe_probability", 0.12)),
            profiles=list(raw.get("profiles", [])),
            jobs=jobs,
        )


class ViewScheduler:
    """Spread views over a 24-hour cycle with time-of-day weighting."""

    def __init__(
        self,
        cycle_hours: int,
        tz_name: str,
        min_gap_seconds: int,
        max_gap_seconds: int,
        rng: random.Random,
    ) -> None:
        self._cycle_hours = cycle_hours
        self._tz = ZoneInfo(tz_name)
        self._min_gap = min_gap_seconds
        self._max_gap = max_gap_seconds
        self._rng = rng
        weights = list(HOURLY_TRAFFIC_WEIGHTS)
        self._hour_weights = weights
        self._weight_total = sum(weights)

    def schedule_views(
        self,
        job_id: str,
        count: int,
        cycle_start: datetime,
    ) -> list[ViewTask]:
        """Generate ``count`` view tasks spread across the cycle window."""
        if count <= 0:
            return []

        if cycle_start.tzinfo is None:
            cycle_start = cycle_start.replace(tzinfo=self._tz)
        else:
            cycle_start = cycle_start.astimezone(self._tz)

        cycle_end = cycle_start + timedelta(hours=self._cycle_hours)
        raw_times: list[datetime] = []

        for _ in range(count):
            hour = self._weighted_hour()
            minute = self._rng.randint(0, 59)
            second = self._rng.randint(0, 59)
            candidate = cycle_start.replace(
                hour=hour, minute=minute, second=second, microsecond=0
            )
            if candidate < cycle_start:
                candidate += timedelta(days=1)
            if candidate >= cycle_end:
                offset = self._rng.uniform(0, self._cycle_hours * 3600)
                candidate = cycle_start + timedelta(seconds=offset)
            raw_times.append(candidate)

        raw_times.sort()
        spaced = self._enforce_inter_arrival(raw_times, cycle_start, cycle_end)

        tasks: list[ViewTask] = []
        for moment in spaced:
            tasks.append(
                ViewTask(
                    task_id=uuid.uuid4().hex[:16],
                    job_id=job_id,
                    scheduled_at=moment.isoformat(),
                    status=JobStatus.PENDING,
                )
            )
        return tasks

    def _weighted_hour(self) -> int:
        pick = self._rng.uniform(0, self._weight_total)
        cumulative = 0.0
        for hour, weight in enumerate(self._hour_weights):
            cumulative += weight
            if pick <= cumulative:
                return hour
        return 20

    def _enforce_inter_arrival(
        self,
        times: list[datetime],
        cycle_start: datetime,
        cycle_end: datetime,
    ) -> list[datetime]:
        if not times:
            return []

        result: list[datetime] = []
        prev = cycle_start - timedelta(seconds=self._min_gap)

        for moment in times:
            min_allowed = prev + timedelta(
                seconds=self._rng.uniform(self._min_gap, self._min_gap * 1.8)
            )
            adjusted = max(moment, min_allowed)
            if adjusted >= cycle_end:
                adjusted = cycle_end - timedelta(
                    seconds=self._rng.uniform(60, min(900, self._max_gap))
                )
            if adjusted < cycle_start:
                adjusted = cycle_start + timedelta(
                    seconds=self._rng.uniform(30, 600)
                )
            result.append(adjusted)
            prev = adjusted

        return result

    def time_of_day_multiplier(self, moment: Optional[datetime] = None) -> float:
        """Return traffic weight for the current local hour (used for dynamic waits)."""
        now = moment or datetime.now(self._tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=self._tz)
        else:
            now = now.astimezone(self._tz)
        return self._hour_weights[now.hour]


class ProfilePool:
    """Round-robin profile assignment with daily usage caps."""

    def __init__(
        self,
        config: OrchestratorConfig,
        factory: ProfileFactory,
        logger: logging.Logger,
        records: Optional[list[ProfileRecord]] = None,
    ) -> None:
        self._config = config
        self._factory = factory
        self._logger = logger
        self._records: list[ProfileRecord] = list(records or [])
        self._rotation: deque[int] = deque(range(len(self._records))) if self._records else deque()
        self._lock = asyncio.Lock()
        self._tz = ZoneInfo(config.timezone)

        for entry in config.profiles:
            profile_id = str(entry.get("profile_id", "")).strip()
            if not profile_id:
                continue
            if any(r.profile_id == profile_id for r in self._records):
                continue
            self._records.append(
                ProfileRecord(
                    profile_id=profile_id,
                    country_code=str(entry.get("country_code", config.country_code)).upper(),
                    provider=str(entry.get("provider", config.provider)).lower(),
                    platform=str(entry.get("platform", "")).strip().lower() or None,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        self._rebuild_rotation()

    @property
    def size(self) -> int:
        return len(self._records)

    def to_serializable(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self._records]

    def load_records(self, records: list[dict[str, Any]]) -> None:
        self._records = [
            ProfileRecord(
                profile_id=str(item["profile_id"]),
                country_code=str(item.get("country_code", self._config.country_code)).upper(),
                provider=str(item.get("provider", self._config.provider)).lower(),
                platform=str(item.get("platform", "")).strip().lower() or None,
                daily_views=int(item.get("daily_views", 0)),
                total_views=int(item.get("total_views", 0)),
                last_used_at=item.get("last_used_at"),
                usage_date=item.get("usage_date"),
                created_at=item.get("created_at"),
            )
            for item in records
            if item.get("profile_id")
        ]
        self._rebuild_rotation()

    def _rebuild_rotation(self) -> None:
        self._rotation = deque(range(len(self._records)))

    def _today_key(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    def _reset_daily_if_needed(self, record: ProfileRecord) -> None:
        today = self._today_key()
        if record.usage_date != today:
            record.usage_date = today
            record.daily_views = 0

    def _eligible_indices(self) -> list[int]:
        eligible: list[int] = []
        for index, record in enumerate(self._records):
            self._reset_daily_if_needed(record)
            if record.daily_views < self._config.daily_profile_view_limit:
                eligible.append(index)
        return eligible

    async def acquire_profile(self) -> ProfileRecord:
        """Round-robin assign an under-limit profile, creating one if needed."""
        async with self._lock:
            eligible = self._eligible_indices()

            if not eligible and self._config.auto_create_profiles:
                if len(self._records) < self._config.target_profile_pool_size:
                    record = await asyncio.to_thread(self._create_profile)
                    self._records.append(record)
                    self._rotation.append(len(self._records) - 1)
                    eligible = [len(self._records) - 1]
                    self._logger.info(
                        "Created profile | id=%s pool_size=%s",
                        record.profile_id,
                        len(self._records),
                    )

            if not eligible:
                raise OrchestratorError(
                    "No eligible profiles available (daily limits reached). "
                    "Wait for reset or increase pool size."
                )

            chosen_index: Optional[int] = None
            for _ in range(len(self._rotation)):
                index = self._rotation[0]
                self._rotation.rotate(-1)
                if index in eligible:
                    chosen_index = index
                    break

            if chosen_index is None:
                chosen_index = self._rng_choice(eligible)

            record = self._records[chosen_index]
            record.daily_views += 1
            record.total_views += 1
            record.last_used_at = datetime.now(timezone.utc).isoformat()
            record.usage_date = self._today_key()
            return record

    def release_profile(self, profile_id: str) -> None:
        """No-op placeholder — usage counted at acquire; session must be closed separately."""
        for record in self._records:
            if record.profile_id == profile_id:
                return

    def _rng_choice(self, items: list[int]) -> int:
        return random.Random().choice(items)

    def _create_profile(self) -> ProfileRecord:
        name = f"MMB-orch-{uuid.uuid4().hex[:8]}"
        result = self._factory.create_stealth_profile(
            country_code=self._config.country_code,
            provider=self._config.provider,
            profile_name=name,
            mobile_first=self._config.mobile_first,
        )
        return ProfileRecord(
            profile_id=str(result["profile_id"]),
            country_code=str(result.get("country_code", self._config.country_code)).upper(),
            provider=str(result.get("provider", self._config.provider)).lower(),
            created_at=datetime.now(timezone.utc).isoformat(),
            usage_date=self._today_key(),
            daily_views=0,
        )


class JobQueue:
    """Loads jobs, tracks runtime state, and persists to disk."""

    def __init__(
        self,
        config: OrchestratorConfig,
        state_path: Path,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._state_path = state_path
        self._logger = logger
        self._jobs: dict[str, JobRuntime] = {}
        self._audit = OrchestratorAudit()
        self._cycle_started_at: Optional[str] = None

        for job_def in config.jobs:
            self._jobs[job_def.id] = JobRuntime(definition=job_def)

        self._load_state()

    @property
    def audit(self) -> OrchestratorAudit:
        return self._audit

    @property
    def jobs(self) -> dict[str, JobRuntime]:
        return self._jobs

    def all_tasks(self) -> list[ViewTask]:
        tasks: list[ViewTask] = []
        for runtime in self._jobs.values():
            tasks.extend(runtime.tasks)
        return tasks

    def pending_tasks(self) -> list[ViewTask]:
        return [
            task
            for task in self.all_tasks()
            if task.status in {JobStatus.PENDING, JobStatus.RUNNING}
        ]

    def due_tasks(self, now: Optional[datetime] = None) -> list[ViewTask]:
        moment = now or datetime.now(timezone.utc)
        due: list[ViewTask] = []
        for task in self.pending_tasks():
            if task.status != JobStatus.PENDING:
                continue
            scheduled = datetime.fromisoformat(task.scheduled_at)
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
            if scheduled <= moment.astimezone(scheduled.tzinfo):
                due.append(task)
        due.sort(key=lambda t: t.scheduled_at)
        return due

    def next_scheduled_at(self) -> Optional[datetime]:
        pending = [
            t for t in self.all_tasks()
            if t.status == JobStatus.PENDING
        ]
        if not pending:
            return None
        times = []
        for task in pending:
            dt = datetime.fromisoformat(task.scheduled_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            times.append(dt)
        return min(times)

    def schedule_cycle(
        self,
        scheduler: ViewScheduler,
        cycle_start: Optional[datetime] = None,
    ) -> int:
        """Schedule all remaining views for every job in the current cycle."""
        start = cycle_start or datetime.now(ZoneInfo(self._config.timezone))
        self._cycle_started_at = start.isoformat()
        self._audit.last_cycle_started_at = self._cycle_started_at

        scheduled_count = 0
        for runtime in self._jobs.values():
            remaining = runtime.definition.target_views - runtime.completed_views
            if remaining <= 0:
                runtime.status = JobStatus.COMPLETED
                continue

            runtime.status = JobStatus.RUNNING
            new_tasks = scheduler.schedule_views(runtime.definition.id, remaining, start)
            runtime.tasks = [
                t for t in runtime.tasks
                if t.status not in {JobStatus.PENDING, JobStatus.RUNNING}
            ]
            runtime.tasks.extend(new_tasks)
            scheduled_count += len(new_tasks)

        self._audit.total_tasks_scheduled += scheduled_count
        self._logger.info(
            "Cycle scheduled | tasks=%s cycle_start=%s",
            scheduled_count,
            self._cycle_started_at,
        )
        self.save_state()
        return scheduled_count

    def mark_task_running(self, task: ViewTask, profile_id: str) -> None:
        task.status = JobStatus.RUNNING
        task.profile_id = profile_id
        task.attempts += 1
        self.save_state()

    def mark_task_completed(
        self,
        task: ViewTask,
        *,
        route: Optional[str],
        watch_seconds: Optional[float],
    ) -> None:
        task.status = JobStatus.COMPLETED
        task.completed_at = datetime.now(timezone.utc).isoformat()
        task.route = route
        task.watch_seconds = watch_seconds
        task.last_error = None

        runtime = self._jobs[task.job_id]
        runtime.completed_views += 1
        if runtime.completed_views >= runtime.definition.target_views:
            runtime.status = JobStatus.COMPLETED

        self._audit.total_successful_views += 1
        self.save_state()

    def mark_task_failed(self, task: ViewTask, error: str) -> None:
        task.last_error = error[:500]
        max_attempts = 3

        if task.attempts >= max_attempts:
            task.status = JobStatus.FAILED
            runtime = self._jobs[task.job_id]
            runtime.failed_views += 1
            self._audit.total_failed_attempts += 1
            if runtime.failed_views >= runtime.definition.target_views:
                runtime.status = JobStatus.FAILED
        else:
            task.status = JobStatus.PENDING
            retry_delay = timedelta(minutes=5 * task.attempts)
            retry_at = datetime.now(timezone.utc) + retry_delay
            task.scheduled_at = retry_at.isoformat()

        self.save_state()

    def cycle_complete(self) -> bool:
        return all(
            runtime.status in {JobStatus.COMPLETED, JobStatus.FAILED}
            for runtime in self._jobs.values()
        )

    def reset_for_new_cycle(self) -> None:
        for runtime in self._jobs.values():
            runtime.completed_views = 0
            runtime.failed_views = 0
            runtime.status = JobStatus.PENDING
            runtime.tasks = []
        self._audit.cycles_completed += 1
        self._audit.last_cycle_completed_at = datetime.now(timezone.utc).isoformat()
        self.save_state()

    def save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

        def serialize_task(task: ViewTask) -> dict[str, Any]:
            data = asdict(task)
            data["status"] = task.status.value
            return data

        payload = {
            "audit": asdict(self._audit),
            "cycle_started_at": self._cycle_started_at,
            "jobs": {
                job_id: {
                    "status": runtime.status.value,
                    "completed_views": runtime.completed_views,
                    "failed_views": runtime.failed_views,
                    "tasks": [serialize_task(task) for task in runtime.tasks],
                }
                for job_id, runtime in self._jobs.items()
            },
        }
        with self._state_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            with self._state_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            self._logger.warning("Could not load orchestrator state: %s", exc)
            return

        audit_raw = data.get("audit", {})
        self._audit = OrchestratorAudit(
            total_successful_views=int(audit_raw.get("total_successful_views", 0)),
            total_failed_attempts=int(audit_raw.get("total_failed_attempts", 0)),
            total_tasks_scheduled=int(audit_raw.get("total_tasks_scheduled", 0)),
            cycles_completed=int(audit_raw.get("cycles_completed", 0)),
            last_cycle_started_at=audit_raw.get("last_cycle_started_at"),
            last_cycle_completed_at=audit_raw.get("last_cycle_completed_at"),
        )
        self._cycle_started_at = data.get("cycle_started_at")

        jobs_raw = data.get("jobs", {})
        for job_id, runtime in self._jobs.items():
            saved = jobs_raw.get(job_id)
            if not saved:
                continue
            try:
                runtime.status = JobStatus(saved.get("status", JobStatus.PENDING.value))
            except ValueError:
                runtime.status = JobStatus.PENDING
            runtime.completed_views = int(saved.get("completed_views", 0))
            runtime.failed_views = int(saved.get("failed_views", 0))
            runtime.tasks = [
                ViewTask(
                    task_id=str(t["task_id"]),
                    job_id=str(t["job_id"]),
                    scheduled_at=str(t["scheduled_at"]),
                    status=JobStatus(t.get("status", JobStatus.PENDING.value)),
                    profile_id=t.get("profile_id"),
                    attempts=int(t.get("attempts", 0)),
                    last_error=t.get("last_error"),
                    completed_at=t.get("completed_at"),
                    route=t.get("route"),
                    watch_seconds=t.get("watch_seconds"),
                )
                for t in saved.get("tasks", [])
            ]


class Orchestrator:
    """
    General of the MMB view-distribution system.

    Example::

        orchestrator = Orchestrator()
        await orchestrator.run()
    """

    def __init__(
        self,
        *,
        jobs_path: Optional[Path | str] = None,
        state_path: Optional[Path | str] = None,
        log_path: Optional[Path | str] = None,
        env_path: Optional[Path | str] = None,
    ) -> None:
        load_dotenv(env_path or DEFAULT_ENV_PATH)

        self._jobs_path = Path(jobs_path or DEFAULT_JOBS_PATH)
        self._state_path = Path(state_path or DEFAULT_STATE_PATH)
        self._logger = self._configure_logger(log_path or DEFAULT_LOG_PATH)

        self._config = OrchestratorConfig.from_file(self._jobs_path)
        self._browser_manager = BrowserManager(env_path=str(env_path or DEFAULT_ENV_PATH))
        self._identity_manager = IdentityManager(env_path=env_path)
        self._profile_factory = ProfileFactory(env_path=env_path)

        self._queue = JobQueue(self._config, self._state_path, self._logger)
        self._scheduler = ViewScheduler(
            cycle_hours=self._config.cycle_hours,
            tz_name=self._config.timezone,
            min_gap_seconds=self._config.min_inter_arrival_seconds,
            max_gap_seconds=self._config.max_inter_arrival_seconds,
            rng=random.Random(),
        )

        profile_state_path = self._state_path.parent / "orchestrator_profiles.json"
        saved_profiles = self._load_profile_records(profile_state_path)
        self._profile_pool = ProfilePool(
            self._config,
            self._profile_factory,
            self._logger,
            records=saved_profiles,
        )
        self._profile_state_path = profile_state_path

        # ── Management Layer ──────────────────────────────────────────────────
        self._profile_manager = ProfileManager(
            profiles_path=self._state_path.parent / "managed_profiles.json",
        )
        # Import any profiles from jobs.json that aren't in ProfileManager yet
        self._profile_manager.import_from_jobs_json(self._jobs_path)

        # ShuffleEngine — video rotation + deduplication
        _videos = load_videos_from_jobs(self._jobs_path)
        _pm_profiles = [
            {
                "profile_id":  r.profile_id,
                "platform":    r.platform or "windows",
                "total_views": r.total_views,
            }
            for r in saved_profiles
        ] if saved_profiles else [
            {"profile_id": p.get("profile_id",""), "platform": p.get("platform","windows"), "total_views": 0}
            for p in self._config.profiles
        ]
        self._shuffle_engine = ShuffleEngine(
            videos=_videos,
            profiles=_pm_profiles,
            rng=random.Random(),
            rotation_strategy="round_robin",
        )
        # ─────────────────────────────────────────────────────────────────────

        self._semaphore: Optional[asyncio.Semaphore] = None
        self._running = False

        self._logger.info(
            "Orchestrator initialized | jobs=%s profiles=%s provider=%s managed=%s",
            len(self._config.jobs),
            self._profile_pool.size,
            self._config.provider,
            len(self._profile_manager.list_profiles()),
        )

    @staticmethod
    def _configure_logger(log_path: Path | str) -> logging.Logger:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("mmb.orchestrator")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if not logger.handlers:
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            logger.addHandler(handler)

        return logger

    @staticmethod
    def _load_profile_records(path: Path) -> list[ProfileRecord]:
        if not path.exists():
            return []
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                return [
                    ProfileRecord(
                        profile_id=str(item["profile_id"]),
                        country_code=str(item.get("country_code", "US")).upper(),
                        provider=str(item.get("provider", "morelogin")).lower(),
                        daily_views=int(item.get("daily_views", 0)),
                        total_views=int(item.get("total_views", 0)),
                        last_used_at=item.get("last_used_at"),
                        usage_date=item.get("usage_date"),
                        created_at=item.get("created_at"),
                    )
                    for item in data
                    if item.get("profile_id")
                ]
        except (json.JSONDecodeError, OSError, KeyError):
            pass
        return []

    def _save_profile_records(self) -> None:
        self._profile_state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._profile_state_path.open("w", encoding="utf-8") as handle:
            json.dump(self._profile_pool.to_serializable(), handle, indent=2)

    def compute_concurrency_limit(self) -> int:
        """
        Derive max concurrent browser sessions from available RAM.

        Uses ``psutil`` when installed; otherwise falls back to config/env caps.
        """
        configured_max = self._config.max_concurrent_profiles
        configured_min = self._config.min_concurrent_profiles
        env_cap = os.getenv("ORCHESTRATOR_MAX_CONCURRENT")
        if env_cap:
            configured_max = max(1, int(env_cap))

        ram_based: Optional[int] = None
        try:
            import psutil

            available_mb = psutil.virtual_memory().available / (1024 * 1024)
            ram_based = int(available_mb // self._config.ram_per_profile_mb)
            self._logger.debug(
                "RAM check | available_mb=%.0f limit=%s",
                available_mb,
                ram_based,
            )
        except ImportError:
            pass

        if ram_based is not None:
            return max(configured_min, min(configured_max, ram_based))
        return max(configured_min, min(configured_max, configured_max))

    async def run(self, *, continuous: bool = True, batch_now: bool = False) -> OrchestratorAudit:
        """
        Main orchestration loop.

        Schedules views across a 24-hour cycle, executes due tasks with
        RAM-aware concurrency, and optionally repeats after cycle completion.

        Args:
            continuous: Re-schedule and loop after each cycle completes.
            batch_now: Schedule ALL tasks for *right now* (ignores time spread).
                       Use for quick test runs — do NOT use in production.
        """
        self._running = True
        self._logger.info(
            "Orchestrator run started | continuous=%s batch_now=%s",
            continuous, batch_now,
        )

        if not self._queue.pending_tasks():
            if batch_now:
                # Immediate mode: schedule all tasks at current time
                now = datetime.now(timezone.utc)
                self._logger.info("Batch-now mode: all tasks scheduled immediately")
                self._queue.schedule_cycle(self._scheduler, cycle_start=now)
                # Override all scheduled times to now
                for runtime in self._queue.jobs.values():
                    for task in runtime.tasks:
                        task.scheduled_at = now.isoformat()
            else:
                self._queue.schedule_cycle(self._scheduler)

        while self._running:
            if self._queue.cycle_complete():
                self._logger.info(
                    "Cycle complete | success=%s failed=%s",
                    self._queue.audit.total_successful_views,
                    self._queue.audit.total_failed_attempts,
                )
                if not continuous:
                    break
                self._queue.reset_for_new_cycle()
                self._queue.schedule_cycle(self._scheduler)
                await asyncio.sleep(10)
                continue

            concurrency = self.compute_concurrency_limit()
            self._semaphore = asyncio.Semaphore(concurrency)

            due = self._queue.due_tasks()
            if not due:
                wait_seconds = self._seconds_until_next_task()
                multiplier = self._scheduler.time_of_day_multiplier()
                adjusted_wait = wait_seconds / max(multiplier, 0.15)
                sleep_for = min(max(5.0, adjusted_wait), 300.0)
                self._logger.info(
                    "No due tasks | sleeping %.0fs (tod_multiplier=%.2f)",
                    sleep_for,
                    multiplier,
                )
                await asyncio.sleep(sleep_for)
                continue

            batch = due[:concurrency]
            self._logger.info(
                "Executing batch | due=%s concurrency=%s success=%s failed=%s",
                len(batch),
                concurrency,
                self._queue.audit.total_successful_views,
                self._queue.audit.total_failed_attempts,
            )

            await asyncio.gather(
                *[
                    self._execute_task(task, stagger_index=i)
                    for i, task in enumerate(batch)
                ],
                return_exceptions=True,
            )
            self._save_profile_records()

            gap = self._inter_arrival_pause()
            await asyncio.sleep(gap)

        self._save_profile_records()
        self._logger.info("Orchestrator run finished")
        return self._queue.audit

    def stop(self) -> None:
        """Signal the run loop to exit after the current batch."""
        self._running = False

    def _seconds_until_next_task(self) -> float:
        nxt = self._queue.next_scheduled_at()
        if nxt is None:
            return 60.0
        now = datetime.now(timezone.utc)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        delta = (nxt - now).total_seconds()
        return max(1.0, delta)

    def _inter_arrival_pause(self) -> float:
        """Short breathing room between batches — scaled by time-of-day."""
        multiplier = self._scheduler.time_of_day_multiplier()
        base = self._scheduler._rng.uniform(
            self._config.min_inter_arrival_seconds * 0.05,
            self._config.min_inter_arrival_seconds * 0.25,
        )
        return base / max(multiplier, 0.2)

    async def _execute_task(self, task: ViewTask, *, stagger_index: int = 0) -> None:
        assert self._semaphore is not None

        # Multilogin safety: stagger each profile start by N seconds
        # so they don't all hit the launcher simultaneously.
        if stagger_index > 0:
            await asyncio.sleep(stagger_index * _PROFILE_START_STAGGER_SECONDS)

        async with self._semaphore:
            profile: Optional[ProfileRecord] = None
            manager: Optional[YouTubeManager] = None

            try:
                profile = await self._profile_pool.acquire_profile()
                self._queue.mark_task_running(task, profile.profile_id)

                job = self._queue.jobs[task.job_id].definition
                self._logger.info(
                    "Pipeline start | task=%s job=%s profile=%s video=%s",
                    task.task_id,
                    task.job_id,
                    profile.profile_id,
                    job.video_id,
                )

                manager = YouTubeManager(
                    profile_id=profile.profile_id,
                    country_code=profile.country_code,
                    force_mobile=self._config.mobile_first if not profile.platform else None,
                    profile_platform=profile.platform,
                    browser_manager=self._browser_manager,
                    identity_manager=self._identity_manager,
                    behavior_profile=job.behavior_profile,
                    referrer_search=job.referrer_search,
                )

                tab = await manager.open_session()
                try:
                    target = job.to_video_target(self._scheduler._rng)
                    route = await manager.navigate_to_video(tab, target)

                    perform = (
                        job.perform_engagement
                        if job.perform_engagement is not None
                        else self._config.perform_engagement
                    )
                    result = await manager.watch_video(
                        tab,
                        perform_engagement=perform,
                        comment_text=job.comment_text,
                        like_probability=self._config.like_probability,
                        subscribe_probability=self._config.subscribe_probability,
                    )

                    self._queue.mark_task_completed(
                        task,
                        route=route,
                        watch_seconds=result.actual_watch_seconds,
                    )
                    # ProfileManager: record success
                    self._profile_manager.record_success(profile.profile_id)
                    self._logger.info(
                        "Pipeline success | task=%s route=%s watched=%.0fs platform=%s",
                        task.task_id,
                        route,
                        result.actual_watch_seconds,
                        result.platform,
                    )
                finally:
                    await manager.close_session()
                    manager = None
                    gc.collect()

            except (YouTubeManagerError, ProfileFactoryError, OrchestratorError) as exc:
                self._logger.error(
                    "Pipeline failed | task=%s profile=%s error=%s",
                    task.task_id,
                    profile.profile_id if profile else "n/a",
                    exc,
                )
                self._queue.mark_task_failed(task, str(exc))
                # ProfileManager: record failure
                if profile:
                    self._profile_manager.record_failure(profile.profile_id, str(exc))
            except Exception as exc:
                self._logger.exception(
                    "Unexpected pipeline error | task=%s", task.task_id
                )
                self._queue.mark_task_failed(task, str(exc))
                if profile:
                    self._profile_manager.record_failure(profile.profile_id, str(exc))
            finally:
                if manager is not None:
                    try:
                        await manager.close_session()
                    except Exception:
                        pass
                    gc.collect()
                if profile is not None:
                    self._profile_pool.release_profile(profile.profile_id)
                self._save_profile_records()

    def status_report(self) -> dict[str, Any]:
        """Return a snapshot for monitoring dashboards."""
        return {
            "audit": asdict(self._queue.audit),
            "concurrency_limit": self.compute_concurrency_limit(),
            "profile_pool_size": self._profile_pool.size,
            "profile_health": self._profile_manager.health_summary(),
            "shuffle_dedup_cycle": self._shuffle_engine._dedup.cycle_key(),
            "jobs": {
                job_id: {
                    "status": runtime.status.value,
                    "target_views": runtime.definition.target_views,
                    "completed_views": runtime.completed_views,
                    "failed_views": runtime.failed_views,
                    "pending_tasks": sum(
                        1 for t in runtime.tasks if t.status == JobStatus.PENDING
                    ),
                }
                for job_id, runtime in self._queue.jobs.items()
            },
        }


async def _main() -> None:
    orchestrator = Orchestrator()
    report = orchestrator.status_report()
    print(f"Jobs loaded: {len(report['jobs'])}")
    print(f"Concurrency limit: {report['concurrency_limit']}")
    print(f"Profile pool: {report['profile_pool_size']}")
    await orchestrator.run(continuous=False)


if __name__ == "__main__":
    asyncio.run(_main())
