"""
PlaybackGuardian — Tab-Sentry background loop.

Runs every 10 seconds and ensures:
  1. Main video keeps playing (auto-resumes if paused by ad-click / tab-switch)
  2. Autoplay is HARD-LOCKED to OFF at session start and re-enforced periodically
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional


class PlaybackGuardian:
    """
    Background asyncio task that watches the main video tab.

    Usage::
        guardian = PlaybackGuardian(tab, log=logger.info, check_interval=10.0)
        await guardian.start()          # starts background loop
        # ... rest of session ...
        await guardian.stop()           # cancels loop cleanly
    """

    def __init__(
        self,
        tab,
        *,
        log: Callable[[str], None] | None = None,
        check_interval: float = 10.0,
        autoplay_lock: bool = True,
    ) -> None:
        self._tab = tab
        self._log = log or (lambda msg: None)
        self._interval = check_interval
        self._autoplay_lock = autoplay_lock
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._play_count = 0
        self._autoplay_enforced_count = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Lock autoplay OFF first, then start the background loop."""
        await self._force_autoplay_off()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="playback_guardian")
        self._log("[Guardian] Started | interval=10s autoplay_lock=ON")

    async def stop(self) -> None:
        """Cancel the background loop cleanly."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._log(
            f"[Guardian] Stopped | play_fixes={self._play_count} "
            f"autoplay_enforced={self._autoplay_enforced_count}"
        )

    # ── Internal loop ─────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        check_no = 0
        while self._running:
            await asyncio.sleep(self._interval)
            if not self._running:
                break
            check_no += 1
            try:
                await self._check_and_fix(check_no)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"[Guardian] Check error (ignored): {exc}")

    async def _check_and_fix(self, check_no: int) -> None:
        """Single guardian tick — check paused, re-enforce autoplay every 3 ticks."""
        paused = await self._is_video_paused()
        if paused is True:
            self._log(f"[Guardian] tick={check_no} | Video PAUSED — forcing play()")
            await self._force_play()
            self._play_count += 1
        else:
            self._log(f"[Guardian] tick={check_no} | Playing ✓")

        if self._autoplay_lock and check_no % 3 == 0:
            await self._force_autoplay_off()
            self._autoplay_enforced_count += 1

    # ── JS helpers ────────────────────────────────────────────────────────────

    async def _is_video_paused(self) -> Optional[bool]:
        """Returns True if video is paused, False if playing, None if unknown."""
        try:
            result = await self._tab.evaluate(
                "(() => { var v = document.querySelector('video'); "
                "if (!v) return null; return v.paused; })()",
                return_by_value=True,
            )
            val = result if isinstance(result, bool) else getattr(result, "value", None)
            if val is None:
                return None
            return bool(val)
        except Exception:
            return None

    async def _force_play(self) -> None:
        """Call video.play() to resume playback."""
        try:
            await self._tab.evaluate(
                "(() => { var v = document.querySelector('video'); "
                "if (v && v.paused) { v.play().catch(()=>{}); } })()",
                return_by_value=True,
            )
        except Exception:
            pass

    async def _force_autoplay_off(self) -> None:
        """
        Hard-lock autoplay = OFF.
        Tries JS property first, then CSS selector click if toggle is ON.
        """
        try:
            # JS approach: set YouTube's internal autoplay flag via player API
            await self._tab.evaluate(
                """
                (() => {
                    try {
                        var p = document.querySelector('#movie_player');
                        if (p && p.setAutonavState) { p.setAutonavState(false); return 'api'; }
                    } catch(e) {}
                    return 'no_api';
                })()
                """,
                return_by_value=True,
            )
        except Exception:
            pass

        # CSS toggle approach — click only if autoplay is currently ON
        try:
            result = await self._tab.evaluate(
                """
                (() => {
                    var btn = document.querySelector(
                        'button.ytp-button[data-tooltip-target-id="ytp-autonav-toggle-button"],'
                        '.ytp-autonav-toggle-button'
                    );
                    if (!btn) return 'not_found';
                    var isOn = btn.getAttribute('aria-checked') === 'true'
                              || btn.classList.contains('ytp-autonav-toggle-button-enabled');
                    if (isOn) { btn.click(); return 'toggled_off'; }
                    return 'already_off';
                })()
                """,
                return_by_value=True,
            )
            val = result if isinstance(result, str) else getattr(result, "value", "")
            if val == "toggled_off":
                self._log("[Guardian] Autoplay was ON → forced OFF")
        except Exception:
            pass
