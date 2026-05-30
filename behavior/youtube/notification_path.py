"""
Notification Entry Path (Surgical).

Flow:
  YouTube Home → Click Notification Bell → Click latest notification
  → Verify target video_id in URL → return True

If no notification found → caller must fallback to 'search' path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from behavior.youtube.types import VideoTarget


class NotificationPath:
    """
    Navigate to a video via the YouTube notification panel.

    Usage::
        npath = NotificationPath(tab, log=logger.info)
        ok = await npath.execute(target)
        if not ok:
            # fallback to search
    """

    # Notification bell selectors (desktop + mobile)
    _BELL_SELECTORS = [
        # Desktop
        '#bell-button button',
        'button[aria-label*="notification" i]',
        'yt-icon-button#notification-button button',
        '#notifications-button button',
        '.ytd-notification-topbar-button-renderer button',
        'button[aria-label="Notifications"]',
        # Mobile (m.youtube.com)
        'ytm-notification-action-button',
        'button.icon-button[aria-label*="notification" i]',
        '.topbar-menu-button-avatar-button',
        'a[href="/feed/activity"]',
    ]

    # Notification panel / dropdown selectors (desktop + mobile)
    _PANEL_SELECTORS = [
        'ytd-notification-renderer',
        '#notifications ytd-notification-renderer',
        'ytd-multi-page-menu-renderer ytd-notification-renderer',
        # Mobile
        'ytm-notification-renderer',
        '.notification-item',
    ]

    # Latest notification click targets (desktop + mobile)
    _NOTIF_LINK_SELECTORS = [
        'ytd-notification-renderer a#thumbnail',
        'ytd-notification-renderer a[href*="/watch"]',
        'ytd-notification-renderer:first-child a',
        '#notifications ytd-notification-renderer:first-child a',
        # Mobile
        'ytm-notification-renderer a[href*="/watch"]',
        '.notification-item a[href*="/watch"]',
    ]

    def __init__(
        self,
        tab,
        *,
        log: Callable[[str], None] | None = None,
        rng=None,
        target_video_id: Optional[str] = None,
    ) -> None:
        self._tab = tab
        self._log = log or (lambda msg: None)
        self._rng = rng
        self._target_video_id = target_video_id

    async def execute(self, target: VideoTarget) -> bool:
        """
        Full notification path.
        Returns True if successfully landed on target video.
        """
        self._log("[NotifPath] Starting notification entry path...")

        # Step 1: Click notification bell
        bell_clicked = await self._click_bell()
        if not bell_clicked:
            self._log("[NotifPath] Bell not found — fallback required")
            return False

        await asyncio.sleep(1.5)

        # Step 2: Wait for notification panel
        panel_visible = await self._wait_panel(timeout=5.0)
        if not panel_visible:
            self._log("[NotifPath] Notification panel did not open — fallback required")
            # Close panel if partially open
            await self._close_panel()
            return False

        self._log("[NotifPath] Notification panel open ✓")
        await asyncio.sleep(0.8)

        # Step 3: Find & click latest notification (prefer target video)
        clicked_url = await self._click_best_notification(target)
        if not clicked_url:
            self._log("[NotifPath] No clickable notification found — fallback required")
            await self._close_panel()
            return False

        self._log(f"[NotifPath] Notification clicked → {clicked_url}")
        await asyncio.sleep(2.5)

        # Step 4: Verify we landed on the right video
        verified = await self._verify_video(target)
        if verified:
            self._log(f"[NotifPath] ✓ Landed on target video | video_id={target.video_id}")
            return True
        else:
            current_url = str(self._tab.url or "")
            self._log(f"[NotifPath] Wrong video landed | url={current_url} — fallback required")
            return False

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _click_bell(self) -> bool:
        """Click the notification bell icon."""
        for sel in self._BELL_SELECTORS:
            try:
                result = await self._tab.evaluate(
                    f"""
                    (() => {{
                        var el = document.querySelector('{sel}');
                        if (el && el.offsetParent !== null) {{
                            el.click();
                            return true;
                        }}
                        return false;
                    }})()
                    """,
                    return_by_value=True,
                )
                val = result if isinstance(result, bool) else getattr(result, "value", False)
                if val:
                    self._log(f"[NotifPath] Bell clicked | selector={sel!r}")
                    return True
            except Exception:
                continue
        return False

    async def _wait_panel(self, timeout: float = 5.0) -> bool:
        """Wait until notification panel appears."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for sel in self._PANEL_SELECTORS:
                try:
                    result = await self._tab.evaluate(
                        f"(() => {{ var el = document.querySelector('{sel}'); "
                        f"return el !== null && el.offsetParent !== null; }})()",
                        return_by_value=True,
                    )
                    val = result if isinstance(result, bool) else getattr(result, "value", False)
                    if val:
                        return True
                except Exception:
                    pass
            await asyncio.sleep(0.4)
        return False

    async def _click_best_notification(self, target: VideoTarget) -> Optional[str]:
        """
        Click the notification that matches target.video_id if possible,
        otherwise click the first/latest notification.
        """
        # Try to find one matching the target video_id
        if target.video_id:
            try:
                result = await self._tab.evaluate(
                    f"""
                    (() => {{
                        var links = document.querySelectorAll(
                            'ytd-notification-renderer a[href*="/watch"]'
                        );
                        for (var i = 0; i < links.length; i++) {{
                            var href = links[i].getAttribute('href') || '';
                            if (href.includes('{target.video_id}')) {{
                                links[i].click();
                                return href;
                            }}
                        }}
                        return null;
                    }})()
                    """,
                    return_by_value=True,
                )
                val = result if isinstance(result, str) else getattr(result, "value", None)
                if val:
                    return val
            except Exception:
                pass

        # Fallback: click first notification link
        for sel in self._NOTIF_LINK_SELECTORS:
            try:
                result = await self._tab.evaluate(
                    f"""
                    (() => {{
                        var el = document.querySelector('{sel}');
                        if (el) {{
                            var href = el.getAttribute('href') || '';
                            el.click();
                            return href || 'clicked';
                        }}
                        return null;
                    }})()
                    """,
                    return_by_value=True,
                )
                val = result if isinstance(result, str) else getattr(result, "value", None)
                if val:
                    return val
            except Exception:
                continue
        return None

    async def _verify_video(self, target: VideoTarget) -> bool:
        """Check if current URL has the target video_id (JS-based, Android-safe)."""
        await asyncio.sleep(1.0)
        # Use JS window.location.href — tab.url unreliable on Android CDP
        try:
            result = await self._tab.evaluate(
                "(() => window.location.href)()", return_by_value=True
            )
            current_url = result if isinstance(result, str) else getattr(result, "value", "")
        except Exception:
            current_url = str(self._tab.url or "")
        if not current_url or "/watch" not in current_url:
            return False
        if target.video_id and target.video_id not in current_url:
            return False
        return True

    async def _close_panel(self) -> None:
        """Press Escape to close notification panel."""
        try:
            await self._tab.evaluate(
                "document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))",
                return_by_value=True,
            )
        except Exception:
            pass
        await asyncio.sleep(0.5)
