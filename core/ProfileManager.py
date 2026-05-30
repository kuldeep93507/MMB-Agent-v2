"""
ProfileManager — Professional profile CRUD, grouping, identity editing,
and health monitoring for MMB-Agent-v2.

Features:
  - Full CRUD (add, remove, update, get, list)
  - Group management (assign profiles to named groups)
  - Health states: HEALTHY | BLOCKED | IDLE | ERROR | COOLDOWN
  - Identity editor: syncs OS, UA, resolution, proxy with IdentityManager
  - Auto-persistence to JSON — crash-safe
  - Health statistics per profile (success rate, last error, cooldown timer)

Usage::
    pm = ProfileManager()
    pm.add_profile("abc-uuid", platform="windows", group="finance")
    pm.set_health("abc-uuid", HealthStatus.BLOCKED, reason="Bot detected")
    healthy = pm.get_eligible_profiles(group="finance")
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILES_PATH = PROJECT_ROOT / "data" / "managed_profiles.json"
DEFAULT_LOG_PATH      = PROJECT_ROOT / "logs" / "profile_manager.log"


# ── Health Status ─────────────────────────────────────────────────────────────

class HealthStatus(str, Enum):
    HEALTHY   = "HEALTHY"    # Ready to run
    IDLE      = "IDLE"       # Created but never used
    BLOCKED   = "BLOCKED"    # YouTube / provider ban detected
    ERROR     = "ERROR"      # Repeated failures — needs inspection
    COOLDOWN  = "COOLDOWN"   # Temporary rest period (will auto-recover)


# ── Profile Record ────────────────────────────────────────────────────────────

@dataclass
class ManagedProfile:
    """Single profile with full state tracking."""

    profile_id:   str
    platform:     str                        # windows | macos | android
    provider:     str = "multilogin"
    country_code: str = "US"
    group:        str = "default"            # logical group name
    label:        str = ""                   # human-readable name
    proxy_host:   str = ""
    proxy_port:   int = 0
    proxy_user:   str = ""
    proxy_pass:   str = ""
    proxy_type:   str = "http"

    # Health
    health:          HealthStatus = HealthStatus.IDLE
    health_reason:   str = ""
    cooldown_until:  Optional[str] = None    # ISO datetime

    # Usage stats
    total_views:       int = 0
    successful_views:  int = 0
    failed_views:      int = 0
    last_used_at:      Optional[str] = None
    last_error:        str = ""
    daily_views:       int = 0
    usage_date:        Optional[str] = None

    # Timestamps
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def success_rate(self) -> float:
        if self.total_views == 0:
            return 1.0
        return self.successful_views / self.total_views

    @property
    def is_eligible(self) -> bool:
        """True if profile can be used right now."""
        if self.health == HealthStatus.BLOCKED:
            return False
        if self.health == HealthStatus.ERROR:
            return False
        if self.health == HealthStatus.COOLDOWN:
            if self.cooldown_until:
                until = datetime.fromisoformat(self.cooldown_until)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < until:
                    return False
                # Cooldown expired — auto-recover
        return True

    def reset_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.usage_date != today:
            self.usage_date = today
            self.daily_views = 0


# ── Manager ───────────────────────────────────────────────────────────────────

class ProfileManager:
    """
    Central registry for all managed profiles.

    Thread-safe via an internal lock.  All mutations auto-save to disk.
    """

    def __init__(
        self,
        profiles_path: Optional[Path | str] = None,
        log_path: Optional[Path | str]      = None,
        *,
        auto_save: bool = True,
    ) -> None:
        self._path     = Path(profiles_path or DEFAULT_PROFILES_PATH)
        self._auto     = auto_save
        self._lock     = threading.Lock()
        self._profiles: dict[str, ManagedProfile] = {}
        self._logger   = self._make_logger(log_path or DEFAULT_LOG_PATH)

        self._load()
        self._logger.info(
            "ProfileManager ready | profiles=%d groups=%s",
            len(self._profiles),
            list(self.list_groups()),
        )

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def add_profile(
        self,
        profile_id: str,
        *,
        platform: str = "windows",
        provider: str = "multilogin",
        country_code: str = "US",
        group: str = "default",
        label: str = "",
        proxy: Optional[dict[str, Any]] = None,
    ) -> ManagedProfile:
        """Register a new profile. Idempotent — updates if already exists."""
        with self._lock:
            pid = profile_id.strip()
            if pid in self._profiles:
                # Update mutable fields only
                rec = self._profiles[pid]
                rec.group        = group or rec.group
                rec.label        = label or rec.label
                rec.platform     = platform or rec.platform
                rec.country_code = country_code or rec.country_code
                if proxy:
                    self._apply_proxy(rec, proxy)
                self._save()
                self._logger.info("Profile updated | id=%s group=%s", pid, group)
                return rec

            rec = ManagedProfile(
                profile_id=pid,
                platform=platform.lower().strip(),
                provider=provider.lower().strip(),
                country_code=country_code.upper().strip(),
                group=group,
                label=label or pid[:8],
                health=HealthStatus.IDLE,
            )
            if proxy:
                self._apply_proxy(rec, proxy)

            self._profiles[pid] = rec
            self._save()
            self._logger.info(
                "Profile added | id=%s platform=%s group=%s",
                pid, platform, group,
            )
            return rec

    def remove_profile(self, profile_id: str) -> bool:
        """Remove profile from registry. Returns True if it existed."""
        with self._lock:
            if profile_id not in self._profiles:
                return False
            del self._profiles[profile_id]
            self._save()
            self._logger.info("Profile removed | id=%s", profile_id)
            return True

    def get_profile(self, profile_id: str) -> Optional[ManagedProfile]:
        """Return profile record or None."""
        with self._lock:
            return self._profiles.get(profile_id)

    def list_profiles(
        self,
        *,
        group: Optional[str] = None,
        platform: Optional[str] = None,
        health: Optional[HealthStatus] = None,
    ) -> list[ManagedProfile]:
        """Return filtered profile list."""
        with self._lock:
            result = list(self._profiles.values())
        if group:
            result = [p for p in result if p.group == group]
        if platform:
            result = [p for p in result if p.platform == platform.lower()]
        if health:
            result = [p for p in result if p.health == health]
        return result

    def list_groups(self) -> set[str]:
        with self._lock:
            return {p.group for p in self._profiles.values()}

    def get_eligible_profiles(
        self,
        *,
        group: Optional[str] = None,
        platform: Optional[str] = None,
        daily_limit: int = 5,
    ) -> list[ManagedProfile]:
        """Return profiles that are eligible to run right now."""
        candidates = self.list_profiles(group=group, platform=platform)
        eligible = []
        for p in candidates:
            p.reset_daily_if_needed()
            if not p.is_eligible:
                continue
            if p.daily_views >= daily_limit:
                continue
            eligible.append(p)
        return eligible

    # ── Identity Editor ───────────────────────────────────────────────────────

    def update_identity(
        self,
        profile_id: str,
        *,
        os_type: Optional[str]   = None,
        user_agent: Optional[str] = None,
        resolution: Optional[str] = None,
        proxy: Optional[dict[str, Any]] = None,
        identity_manager=None,   # IdentityManager instance (optional)
    ) -> bool:
        """
        Edit profile identity fields.  If identity_manager is supplied,
        also updates the IdentityManager's stored identity for this profile.
        """
        with self._lock:
            rec = self._profiles.get(profile_id)
            if not rec:
                return False

            if os_type:
                rec.platform = os_type.lower()
            if proxy:
                self._apply_proxy(rec, proxy)

            if identity_manager is not None:
                stored = identity_manager.get_identity(profile_id)
                if stored:
                    if os_type:
                        stored["os_type"] = os_type
                    if user_agent:
                        stored["user_agent"] = user_agent
                    if resolution:
                        stored["screen_resolution"] = resolution
                    identity_manager.store_identity(profile_id, stored)

            self._save()
            self._logger.info(
                "Identity updated | id=%s os=%s ua=%s res=%s",
                profile_id, os_type, bool(user_agent), resolution,
            )
            return True

    # ── Health Management ─────────────────────────────────────────────────────

    def set_health(
        self,
        profile_id: str,
        status: HealthStatus,
        *,
        reason: str = "",
        cooldown_minutes: int = 0,
    ) -> None:
        """Manually set health status. Cooldown auto-recovers after N minutes."""
        with self._lock:
            rec = self._profiles.get(profile_id)
            if not rec:
                return
            rec.health = status
            rec.health_reason = reason
            if status == HealthStatus.COOLDOWN and cooldown_minutes > 0:
                until = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
                rec.cooldown_until = until.isoformat()
            else:
                rec.cooldown_until = None
            self._save()
        self._logger.info(
            "Health updated | id=%s status=%s reason=%s",
            profile_id, status.value, reason,
        )

    def record_success(self, profile_id: str) -> None:
        """Call after a successful view. Updates stats + marks HEALTHY."""
        with self._lock:
            rec = self._profiles.get(profile_id)
            if not rec:
                return
            rec.total_views      += 1
            rec.successful_views += 1
            rec.daily_views      += 1
            rec.last_used_at      = datetime.now(timezone.utc).isoformat()
            rec.health            = HealthStatus.HEALTHY
            rec.health_reason     = ""
            rec.usage_date        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._save()

    def record_failure(
        self,
        profile_id: str,
        error: str,
        *,
        auto_block_threshold: int = 5,
    ) -> None:
        """
        Call after a failed view.  If failed_views >= threshold → ERROR health.
        """
        with self._lock:
            rec = self._profiles.get(profile_id)
            if not rec:
                return
            rec.total_views  += 1
            rec.failed_views += 1
            rec.last_error    = error[:300]
            rec.last_used_at  = datetime.now(timezone.utc).isoformat()

            if rec.failed_views >= auto_block_threshold:
                rec.health        = HealthStatus.ERROR
                rec.health_reason = f"Auto-error after {rec.failed_views} failures"
            self._save()

    def health_summary(self) -> dict[str, Any]:
        """Return counts per health status across all profiles."""
        with self._lock:
            profiles = list(self._profiles.values())
        summary: dict[str, int] = {s.value: 0 for s in HealthStatus}
        for p in profiles:
            # Refresh cooldown status
            if p.health == HealthStatus.COOLDOWN and p.cooldown_until:
                until = datetime.fromisoformat(p.cooldown_until)
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) >= until:
                    p.health = HealthStatus.HEALTHY
                    p.cooldown_until = None
            summary[p.health.value] = summary.get(p.health.value, 0) + 1
        summary["total"] = len(profiles)
        return summary

    # ── Group Operations ──────────────────────────────────────────────────────

    def move_group(self, profile_id: str, new_group: str) -> bool:
        with self._lock:
            rec = self._profiles.get(profile_id)
            if not rec:
                return False
            rec.group = new_group
            self._save()
            return True

    def rename_group(self, old_name: str, new_name: str) -> int:
        """Rename a group across all profiles. Returns number of profiles updated."""
        count = 0
        with self._lock:
            for rec in self._profiles.values():
                if rec.group == old_name:
                    rec.group = new_name
                    count += 1
            if count:
                self._save()
        self._logger.info(
            "Group renamed | %s → %s | profiles=%d", old_name, new_name, count
        )
        return count

    def import_from_jobs_json(self, jobs_path: Path) -> int:
        """
        Import profiles defined in jobs.json into ProfileManager.
        Returns number of new profiles added.
        """
        if not jobs_path.exists():
            return 0
        with jobs_path.open(encoding="utf-8") as f:
            data = json.load(f)
        added = 0
        for entry in data.get("profiles", []):
            pid = str(entry.get("profile_id", "")).strip()
            if not pid:
                continue
            self.add_profile(
                pid,
                platform=str(entry.get("platform", "windows")).lower(),
                provider=str(entry.get("provider", "multilogin")),
                country_code=str(entry.get("country_code", "US")).upper(),
                group=str(entry.get("group", "default")),
            )
            added += 1
        self._logger.info("Imported %d profiles from %s", added, jobs_path)
        return added

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save(self) -> None:
        """Write all profiles to disk (call inside _lock)."""
        if not self._auto:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(p) for p in self._profiles.values()]
        # Convert HealthStatus enum to string for JSON
        for item in payload:
            item["health"] = item["health"] if isinstance(item["health"], str) else item["health"].value
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(self._path)  # atomic replace — crash-safe

    def _load(self) -> None:
        """Load profiles from disk."""
        if not self._path.exists():
            return
        try:
            with self._path.open(encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                pid = str(item.get("profile_id", "")).strip()
                if not pid:
                    continue
                try:
                    health_raw = item.get("health", HealthStatus.IDLE.value)
                    health     = HealthStatus(health_raw)
                except ValueError:
                    health = HealthStatus.IDLE
                rec = ManagedProfile(
                    profile_id      = pid,
                    platform        = str(item.get("platform", "windows")),
                    provider        = str(item.get("provider", "multilogin")),
                    country_code    = str(item.get("country_code", "US")),
                    group           = str(item.get("group", "default")),
                    label           = str(item.get("label", pid[:8])),
                    proxy_host      = str(item.get("proxy_host", "")),
                    proxy_port      = int(item.get("proxy_port", 0)),
                    proxy_user      = str(item.get("proxy_user", "")),
                    proxy_pass      = str(item.get("proxy_pass", "")),
                    proxy_type      = str(item.get("proxy_type", "http")),
                    health          = health,
                    health_reason   = str(item.get("health_reason", "")),
                    cooldown_until  = item.get("cooldown_until"),
                    total_views     = int(item.get("total_views", 0)),
                    successful_views= int(item.get("successful_views", 0)),
                    failed_views    = int(item.get("failed_views", 0)),
                    last_used_at    = item.get("last_used_at"),
                    last_error      = str(item.get("last_error", "")),
                    daily_views     = int(item.get("daily_views", 0)),
                    usage_date      = item.get("usage_date"),
                    created_at      = str(item.get("created_at", datetime.now(timezone.utc).isoformat())),
                )
                self._profiles[pid] = rec
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            self._logger.warning("Failed to load profiles: %s", exc)

    def save(self) -> None:
        """Public save — call explicitly if auto_save=False."""
        with self._lock:
            self._save()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_proxy(rec: ManagedProfile, proxy: dict[str, Any]) -> None:
        rec.proxy_host = str(proxy.get("host", ""))
        rec.proxy_port = int(proxy.get("port", 0))
        rec.proxy_user = str(proxy.get("user", ""))
        rec.proxy_pass = str(proxy.get("password", ""))
        rec.proxy_type = str(proxy.get("type", "http"))

    @staticmethod
    def _make_logger(log_path: Path | str) -> logging.Logger:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("mmb.profile_manager")
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

    # ── Pretty Print ─────────────────────────────────────────────────────────

    def print_table(self) -> None:
        """Print a human-readable profile table to stdout."""
        profiles = self.list_profiles()
        if not profiles:
            print("No profiles registered.")
            return
        header = f"{'ID':36} {'Platform':10} {'Group':12} {'Health':10} {'Views':6} {'Rate':6} {'Last Used':20}"
        print(header)
        print("-" * len(header))
        for p in profiles:
            last = p.last_used_at[:19] if p.last_used_at else "never"
            rate = f"{p.success_rate*100:.0f}%"
            print(
                f"{p.profile_id:36} {p.platform:10} {p.group:12} "
                f"{p.health.value:10} {p.total_views:6} {rate:6} {last:20}"
            )
        summary = self.health_summary()
        print(f"\nTotal: {summary['total']} | "
              f"Healthy: {summary.get('HEALTHY',0)} | "
              f"Blocked: {summary.get('BLOCKED',0)} | "
              f"Error: {summary.get('ERROR',0)} | "
              f"Idle: {summary.get('IDLE',0)}")
