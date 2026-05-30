"""
Browser profile lifecycle management for anti-detection providers.

Supports starting profiles via MoreLogin or Multilogin local/cloud APIs,
then attaching nodriver to the returned Chrome DevTools Protocol (CDP) port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional, TypedDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

import nodriver as uc
import requests
from dotenv import load_dotenv
from nodriver import cdp
from nodriver.core.browser import Browser
from nodriver.core.tab import Tab
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, RequestException, Timeout

# ---------------------------------------------------------------------------
# Adaptive viewport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewportProfile:
    """Resolved OS viewport applied before first navigation."""

    os_label: str
    width: int
    height: int
    mobile: bool
    device_scale_factor: float = 1.0


ANDROID_VIEWPORTS: tuple[tuple[int, int, float], ...] = (
    (390, 844, 3.0),
    (360, 800, 2.0),
    (412, 915, 2.625),
)

WINDOWS_VIEWPORTS: tuple[tuple[int, int], ...] = (
    (1920, 1080),
    (1366, 768),
    (1536, 864),
)

MACOS_VIEWPORTS: tuple[tuple[int, int], ...] = (
    (1440, 900),
    (1680, 1050),
    (1920, 1080),
)


def _is_mobile_identity(identity: dict[str, Any]) -> bool:
    os_type = str(identity.get("os_type") or "").lower()
    if os_type in {"windows", "macos", "mac", "darwin"} and not identity.get("mobile_first"):
        return False
    if identity.get("mobile_first"):
        return True
    if os_type in {"android", "ios"}:
        return True
    device = str(identity.get("device_platform") or "").lower()
    if device in {"android", "ios"}:
        return True
    if identity.get("operator_system_id") in (3, 4):
        return True
    return False


def _parse_screen_resolution(value: str) -> tuple[int, int] | None:
    try:
        width_str, height_str = str(value).lower().split("x", 1)
        return int(width_str), int(height_str)
    except (ValueError, AttributeError):
        return None


def resolve_viewport_profile(
    identity: dict[str, Any],
    rng: random.Random | None = None,
) -> ViewportProfile:
    """Map identity OS traits to a realistic viewport profile."""
    picker = rng or random.Random()

    if _is_mobile_identity(identity):
        width = int(identity.get("screen_width") or 0)
        height = int(identity.get("screen_height") or 0)
        dpr = float(identity.get("pixel_ratio") or 0)

        if width <= 0 or height <= 0:
            width, height, dpr = picker.choice(ANDROID_VIEWPORTS)
        elif dpr <= 0:
            dpr = 2.75

        return ViewportProfile(
            os_label="Android",
            width=width,
            height=height,
            mobile=True,
            device_scale_factor=dpr,
        )

    os_type = str(identity.get("os_type") or "windows").lower()
    parsed = _parse_screen_resolution(str(identity.get("screen_resolution") or ""))

    if os_type in {"macos", "mac", "darwin", "ios"} and not identity.get("mobile_first"):
        if parsed:
            width, height = parsed
        else:
            width, height = picker.choice(MACOS_VIEWPORTS)
        return ViewportProfile("macOS", width, height, mobile=False, device_scale_factor=1.0)

    if parsed:
        width, height = parsed
    else:
        width, height = picker.choice(WINDOWS_VIEWPORTS)
    return ViewportProfile("Windows", width, height, mobile=False, device_scale_factor=1.0)


async def apply_adaptive_viewport(
    tab: Tab,
    identity: dict[str, Any],
    logger: logging.Logger | None = None,
) -> ViewportProfile:
    """
    Strictly enforce OS-appropriate viewport via CDP before any navigation.

    Sets device metrics (rendering viewport) and resizes the browser window
    to match the chosen profile.
    """
    log = logger or logging.getLogger("mmb.browser_manager")
    profile = resolve_viewport_profile(identity)

    try:
        await tab.send(cdp.emulation.clear_device_metrics_override())
    except Exception:
        pass

    await tab.send(
        cdp.emulation.set_device_metrics_override(
            width=profile.width,
            height=profile.height,
            device_scale_factor=profile.device_scale_factor,
            mobile=profile.mobile,
            screen_width=profile.width,
            screen_height=profile.height,
        )
    )

    if identity.get("user_agent"):
        await tab.send(
            cdp.emulation.set_user_agent_override(
                user_agent=str(identity["user_agent"]),
                platform=str(
                    identity.get("navigator_platform")
                    or ("Linux armv8l" if profile.mobile else "Win32")
                ),
            )
        )

    chrome_margin_w = 0 if profile.mobile else 16
    chrome_margin_h = 0 if profile.mobile else 96
    try:
        window_result = await tab.send(cdp.browser.get_window_for_target())
        window_id = None
        if isinstance(window_result, dict):
            window_id = window_result.get("windowId") or window_result.get("window_id")
        elif isinstance(window_result, (list, tuple)) and window_result:
            window_id = window_result[0]
        if window_id is not None:
            await tab.send(
                cdp.browser.set_window_bounds(
                    window_id=window_id,
                    bounds=cdp.browser.Bounds(
                        width=profile.width + chrome_margin_w,
                        height=profile.height + chrome_margin_h,
                        window_state="normal",
                    ),
                )
            )
    except Exception as exc:
        log.debug("Window bounds adjustment skipped: %s", exc)

    log.info(
        "Applied %s resolution: %sx%s",
        profile.os_label,
        profile.width,
        profile.height,
    )
    return profile


# ---------------------------------------------------------------------------
# Profile lock — one active start/connect per profile_id
# ---------------------------------------------------------------------------

_profile_locks: dict[str, asyncio.Lock] = {}
_lock_registry = asyncio.Lock()
CDP_CONNECT_COOLDOWN_SECONDS = 5.0


async def _get_profile_lock(profile_id: str) -> asyncio.Lock:
    async with _lock_registry:
        if profile_id not in _profile_locks:
            _profile_locks[profile_id] = asyncio.Lock()
        return _profile_locks[profile_id]


@asynccontextmanager
async def profile_session_lock(profile_id: str) -> AsyncIterator[None]:
    """Prevent concurrent profile start/connect for the same profile_id."""
    lock = await _get_profile_lock(profile_id.strip())
    await lock.acquire()
    try:
        yield
    finally:
        if lock.locked():
            lock.release()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BrowserProviderError(Exception):
    """Base exception for browser provider operations."""


class ProviderConnectionError(BrowserProviderError):
    """Raised when the provider API cannot be reached."""


class ProfileStartError(BrowserProviderError):
    """Raised when a profile fails to start or returns an invalid response."""


class ProfileCreateError(BrowserProviderError):
    """Raised when a profile fails to be created via the provider API."""


class UnsupportedProviderError(BrowserProviderError):
    """Raised when BROWSER_PROVIDER is not registered."""


class ProxyConfig(TypedDict, total=False):
    """Proxy credentials bound to a profile at creation time."""

    host: str
    port: int
    user: str
    password: str
    type: str


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


class BaseBrowserProvider(ABC):
    """Abstract provider contract. Subclass to add new anti-detect browsers."""

    @abstractmethod
    def start_profile(self, profile_id: str) -> int:
        """
        Start a browser profile and return its CDP debug port.

        Args:
            profile_id: Provider-specific profile identifier.

        Returns:
            Local CDP debug port as an integer.

        Raises:
            ProviderConnectionError: Network or timeout failure.
            ProfileStartError: API responded with an error or missing port.
        """

    @abstractmethod
    def create_profile(
        self,
        identity: dict[str, Any],
        proxy: Optional[ProxyConfig] = None,
        profile_name: Optional[str] = None,
    ) -> str:
        """
        Create a new browser profile with fingerprint settings.

        Args:
            identity: Identity payload from ``IdentityManager``.
            proxy: Optional proxy configuration to bind at creation.
            profile_name: Optional human-readable profile name.

        Returns:
            Provider-specific profile identifier.

        Raises:
            ProviderConnectionError: Provider API unreachable.
            ProfileCreateError: Profile creation failed.
        """


# ---------------------------------------------------------------------------
# MoreLogin
# ---------------------------------------------------------------------------


class MoreLoginProvider(BaseBrowserProvider):
    """
    MoreLogin local desktop API integration.

    Docs: POST http://127.0.0.1:{port}/api/env/start
    """

    def __init__(
        self,
        api_key: Optional[str],
        port: int,
        host: str = "127.0.0.1",
        timeout: int = 120,
    ) -> None:
        self._api_key = api_key.strip() if api_key else None
        self._host = host
        self._port = port
        self._timeout = timeout
        self._base_url = f"http://{host}:{port}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = self._api_key
        return headers

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = requests.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json()
        except RequestsConnectionError as exc:
            raise ProviderConnectionError(
                f"Could not connect to MoreLogin at {self._base_url}. "
                "Ensure the MoreLogin desktop app is running."
            ) from exc
        except Timeout as exc:
            raise ProviderConnectionError(
                f"MoreLogin API timed out after {self._timeout}s."
            ) from exc
        except HTTPError as exc:
            raise ProfileCreateError(
                f"MoreLogin HTTP error {response.status_code}: {response.text}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise ProfileCreateError(f"MoreLogin request failed: {exc}") from exc

    @staticmethod
    def _parse_resolution(screen_resolution: str) -> tuple[int, int]:
        try:
            width_str, height_str = screen_resolution.lower().split("x", 1)
            return int(width_str), int(height_str)
        except (ValueError, AttributeError) as exc:
            raise ProfileCreateError(
                f"Invalid screen_resolution format: {screen_resolution!r}"
            ) from exc

    def create_profile(
        self,
        identity: dict[str, Any],
        proxy: Optional[ProxyConfig] = None,
        profile_name: Optional[str] = None,
    ) -> str:
        """Create a MoreLogin profile via ``/api/env/create/advanced``."""
        name = profile_name or "MMB"
        operator_system_id = int(
            identity.get("operator_system_id")
            or (3 if identity.get("mobile_first") else 1)
        )

        payload: dict[str, Any] = {
            "browserTypeId": 1,
            "operatorSystemId": operator_system_id,
            "envName": name,
            "advancedSetting": {
                "time_zone": {
                    "switcher": 2,
                    "value": identity["timezone"],
                },
                "language": {
                    "switcher": 2,
                    "value": identity["language"],
                },
                "resolution": {
                    "switcher": 2,
                    "id": identity["screen_resolution"],
                },
                "canvas": {"switcher": 1},
                "webgl_image": {"switcher": 1},
                "webgl_metadata": {"switcher": 3},
                "audio_context": {"switcher": 1},
            },
        }

        if identity.get("user_agent"):
            payload["advancedSetting"]["ua"] = {
                "switcher": 2,
                "value": identity["user_agent"],
            }

        body = self._post("/api/env/create/advanced", payload)
        profile_id = self._extract_created_profile_id(body)

        if proxy and proxy.get("host"):
            self._bind_proxy(profile_id, proxy)

        return profile_id

    def _extract_created_profile_id(self, body: dict[str, Any]) -> str:
        code = body.get("code")
        if code is not None and code != 0:
            msg = body.get("msg") or body.get("message") or "Unknown error"
            raise ProfileCreateError(f"MoreLogin API error (code={code}): {msg}")

        data = body.get("data")
        if isinstance(data, list) and data:
            return str(data[0])
        if isinstance(data, dict):
            env_id = data.get("envId") or data.get("id")
            if env_id is not None:
                return str(env_id)

        raise ProfileCreateError(
            f"MoreLogin create response missing profile id: {body}"
        )

    def _bind_proxy(self, profile_id: str, proxy: ProxyConfig) -> None:
        proxy_payload = {
            "envDataList": [
                {
                    "envId": profile_id,
                    "proxyInfo": {
                        "proxyType": proxy.get("type", "http"),
                        "host": proxy["host"],
                        "port": int(proxy["port"]),
                        "proxyUserName": proxy.get("user", ""),
                        "proxyPassword": proxy.get("password", ""),
                    },
                }
            ]
        }
        body = self._post("/api/env/updateProxy/batch", proxy_payload)
        code = body.get("code")
        if code is not None and code != 0:
            msg = body.get("msg") or body.get("message") or "Unknown error"
            raise ProfileCreateError(
                f"MoreLogin proxy bind failed (code={code}): {msg}"
            )

    def start_profile(self, profile_id: str) -> int:
        """Start a MoreLogin profile via the local /api/env/start endpoint."""
        url = f"{self._base_url}/api/env/start"
        headers = self._headers()

        payload = {"envId": profile_id}

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except RequestsConnectionError as exc:
            raise ProviderConnectionError(
                f"Could not connect to MoreLogin at {self._base_url}. "
                "Ensure the MoreLogin desktop app is running."
            ) from exc
        except Timeout as exc:
            raise ProviderConnectionError(
                f"MoreLogin API timed out after {self._timeout}s."
            ) from exc
        except HTTPError as exc:
            raise ProfileStartError(
                f"MoreLogin HTTP error {response.status_code}: {response.text}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise ProfileStartError(
                f"MoreLogin request failed: {exc}"
            ) from exc

        return self._extract_debug_port(body, provider="MoreLogin")

    @staticmethod
    def _extract_debug_port(body: dict[str, Any], provider: str) -> int:
        code = body.get("code")
        if code is not None and code != 0:
            msg = body.get("msg") or body.get("message") or "Unknown error"
            raise ProfileStartError(f"{provider} API error (code={code}): {msg}")

        data = body.get("data") or {}
        raw_port = data.get("debugPort") or data.get("debug_port")
        if raw_port is None:
            raise ProfileStartError(
                f"{provider} response missing debugPort: {body}"
            )

        try:
            return int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ProfileStartError(
                f"{provider} returned invalid debugPort: {raw_port!r}"
            ) from exc

    def stop_profile(self, profile_id: str) -> None:
        """Stop a running MoreLogin profile."""
        url = f"{self._base_url}/api/env/close"
        try:
            requests.post(
                url,
                json={"envId": profile_id.strip()},
                headers=self._headers(),
                timeout=self._timeout,
            )
        except RequestException:
            pass


# ---------------------------------------------------------------------------
# Multilogin
# ---------------------------------------------------------------------------


class MultiloginProvider(BaseBrowserProvider):
    """
    Multilogin X launcher API integration.

    Docs: GET https://launcher.mlx.yt:45001/api/v2/profile/f/{folder_id}/p/{profile_id}/start
    """

    LAUNCHER_BASE = "https://launcher.mlx.yt:45001"

    def __init__(
        self,
        token: str,
        folder_id: str,
        timeout: int = 120,
    ) -> None:
        if not token.strip():
            raise ValueError("MULTILOGIN_TOKEN is required for Multilogin provider.")
        if not folder_id.strip():
            raise ValueError("MULTILOGIN_FOLDER_ID is required for Multilogin provider.")

        self._token = token.strip()
        self._folder_id = folder_id.strip()
        self._timeout = timeout

    def generate_builtin_proxy(self, country_code: str) -> ProxyConfig:
        """
        Generate a Multilogin built-in residential proxy for the given country.

        Docs: POST https://profile-proxy.multilogin.com/v1/proxy/connection_url
        """
        country = country_code.strip().upper()
        payload: dict[str, Any] = {
            "country": country,
            "protocol": os.getenv("MULTILOGIN_PROXY_PROTOCOL", "http"),
            "sessionType": os.getenv("MULTILOGIN_PROXY_SESSION", "sticky"),
            "region": os.getenv("MULTILOGIN_PROXY_REGION", ""),
            "city": os.getenv("MULTILOGIN_PROXY_CITY", ""),
            "count": 1,
        }

        url = "https://profile-proxy.multilogin.com/v1/proxy/connection_url"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except RequestsConnectionError as exc:
            raise ProviderConnectionError(
                "Could not connect to Multilogin proxy API."
            ) from exc
        except Timeout as exc:
            raise ProviderConnectionError(
                f"Multilogin proxy API timed out after {self._timeout}s."
            ) from exc
        except HTTPError as exc:
            raise ProfileCreateError(
                f"Multilogin proxy HTTP error {response.status_code}: {response.text}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise ProfileCreateError(f"Multilogin proxy request failed: {exc}") from exc

        proxy_list = body.get("data")
        if not isinstance(proxy_list, list) or not proxy_list:
            raise ProfileCreateError(
                f"Multilogin proxy response missing connection data: {body}"
            )

        return self._parse_connection_url(str(proxy_list[0]), payload["protocol"])

    def validate_proxy(self, proxy: ProxyConfig) -> dict[str, Any]:
        """
        Validate proxy and return exit IP plus geographic metadata.

        Docs: POST https://launcher.mlx.yt:45001/api/v1/proxy/validate
        """
        payload = {
            "type": proxy.get("type", "http"),
            "host": proxy["host"],
            "port": int(proxy["port"]),
            "username": proxy.get("user", ""),
            "password": proxy.get("password", ""),
        }
        url = f"{self.LAUNCHER_BASE}/api/v1/proxy/validate"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except RequestsConnectionError as exc:
            raise ProviderConnectionError(
                "Could not connect to Multilogin proxy validator."
            ) from exc
        except Timeout as exc:
            raise ProviderConnectionError(
                f"Multilogin proxy validation timed out after {self._timeout}s."
            ) from exc
        except HTTPError as exc:
            raise ProfileCreateError(
                f"Multilogin proxy validate HTTP {response.status_code}: {response.text}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise ProfileCreateError(f"Multilogin proxy validate failed: {exc}") from exc

        data = body.get("data") or {}
        exit_ip = data.get("ip")
        if not exit_ip:
            raise ProfileCreateError(
                f"Multilogin proxy validation missing exit IP: {body}"
            )

        return {
            "ip": str(exit_ip),
            "country": data.get("country"),
            "city": data.get("city"),
            "region": data.get("region"),
        }

    @staticmethod
    def _parse_connection_url(connection_url: str, protocol: str) -> ProxyConfig:
        """Parse ``host:port:username:password`` returned by Multilogin proxy API."""
        parts = connection_url.split(":")
        if len(parts) < 4:
            raise ProfileCreateError(
                f"Invalid Multilogin proxy connection_url: {connection_url!r}"
            )

        host = parts[0]
        port = parts[1]
        username = parts[2]
        password = ":".join(parts[3:])

        proxy_type = protocol if protocol in {"http", "https", "socks5"} else "http"
        if port == "8080" and proxy_type == "http":
            proxy_type = "http"

        return ProxyConfig(
            host=host,
            port=int(port),
            user=username,
            password=password,
            type=proxy_type,
        )

    def create_profile(
        self,
        identity: dict[str, Any],
        proxy: Optional[ProxyConfig] = None,
        profile_name: Optional[str] = None,
    ) -> str:
        """Create a Multilogin profile via ``POST /profile/create``."""
        name = profile_name or "MMB"
        width, height = MoreLoginProvider._parse_resolution(
            identity["screen_resolution"]
        )
        os_type = str(identity.get("os_type") or "windows")
        if identity.get("mobile_first"):
            os_type = "android"
        pixel_ratio = float(identity.get("pixel_ratio") or 1)

        fingerprint: dict[str, Any] = {
            "timezone": {"zone": identity["timezone"]},
            "localization": {
                "locale": identity["language"],
                "languages": identity["language"],
                "accept_languages": f"{identity['language']},en;q=0.9",
            },
            "screen": {
                "width": width,
                "height": height,
                "pixel_ratio": pixel_ratio,
            },
        }

        if identity.get("user_agent"):
            fingerprint["navigator"] = {
                "user_agent": identity["user_agent"],
                "platform": identity.get("navigator_platform", "Linux armv8l"),
                "hardware_concurrency": int(
                    identity.get("hardware_concurrency") or 8
                ),
                "max_touch_points": int(identity.get("max_touch_points") or 5),
            }

        parameters: dict[str, Any] = {
            "flags": {
                "audio_masking": "mask",
                "canvas_noise": "mask",
                "fonts_masking": "mask",
                "geolocation_masking": "mask",
                "geolocation_popup": "prompt",
                "graphics_masking": "mask",
                "graphics_noise": "mask",
                "localization_masking": "custom",
                "media_devices_masking": "mask",
                "navigator_masking": "custom" if identity.get("user_agent") else "mask",
                "ports_masking": "mask",
                "screen_masking": "custom",
                "timezone_masking": "custom",
                "webrtc_masking": "mask",
                "proxy_masking": "custom" if proxy and proxy.get("host") else "disabled",
            },
            "fingerprint": fingerprint,
            "storage": {
                "is_local": True,
                "save_service_worker": True,
            },
        }

        if proxy and proxy.get("host"):
            parameters["proxy"] = {
                "type": proxy.get("type", "http"),
                "host": proxy["host"],
                "port": int(proxy["port"]),
                "username": proxy.get("user", ""),
                "password": proxy.get("password", ""),
            }

        payload = {
            "name": name,
            "folder_id": self._folder_id,
            "browser_type": "mimic",
            "os_type": os_type,
            "parameters": parameters,
        }

        url = "https://api.multilogin.com/profile/create"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except RequestsConnectionError as exc:
            raise ProviderConnectionError(
                "Could not connect to Multilogin API. Check network connectivity."
            ) from exc
        except Timeout as exc:
            raise ProviderConnectionError(
                f"Multilogin API timed out after {self._timeout}s."
            ) from exc
        except HTTPError as exc:
            raise ProfileCreateError(
                f"Multilogin HTTP error {response.status_code}: {response.text}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise ProfileCreateError(f"Multilogin request failed: {exc}") from exc

        return self._extract_created_profile_id(body)

    @staticmethod
    def _extract_created_profile_id(body: dict[str, Any]) -> str:
        status = body.get("status") or {}
        error_code = status.get("error_code")
        if error_code:
            message = status.get("message") or "Unknown error"
            raise ProfileCreateError(
                f"Multilogin API error ({error_code}): {message}"
            )

        data = body.get("data") or {}
        profile_id = data.get("profile_id") or data.get("id")
        if not profile_id:
            ids = data.get("ids")
            if isinstance(ids, list) and ids:
                profile_id = ids[0]
        if profile_id:
            return str(profile_id)

        raise ProfileCreateError(
            f"Multilogin create response missing profile_id: {body}"
        )

    def start_profile(self, profile_id: str) -> int:
        """Start a Multilogin profile and return its automation CDP port.

        Auto-handles PROFILE_ALREADY_RUNNING by stopping first then retrying.
        """
        url = (
            f"{self.LAUNCHER_BASE}/api/v2/profile/f/{self._folder_id}"
            f"/p/{profile_id}/start"
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        params = {
            "automation_type": "puppeteer",
            "headless_mode": "false",
        }

        for attempt in range(2):
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self._timeout,
                )
                response.raise_for_status()
                body = response.json()
                return self._extract_debug_port(body)

            except RequestsConnectionError as exc:
                raise ProviderConnectionError(
                    "Could not connect to Multilogin launcher. "
                    "Ensure Multilogin X agent/desktop app is running."
                ) from exc
            except Timeout as exc:
                raise ProviderConnectionError(
                    f"Multilogin API timed out after {self._timeout}s."
                ) from exc
            except HTTPError as exc:
                # Auto-recover: profile already running → stop it then retry once
                if attempt == 0 and response.status_code in (400, 500):
                    body_text = response.text or ""
                    if "ALREADY_RUNNING" in body_text or "LOCK_PROFILE" in body_text or "browser process is running" in body_text:
                        import logging as _logging
                        _logging.getLogger("mmb.browser").warning(
                            "Profile %s already running — auto-stopping and retrying...", profile_id
                        )
                        self.stop_profile(profile_id)
                        import time as _time
                        _time.sleep(4.0)  # wait for Multilogin to release the lock
                        continue  # retry start
                raise ProfileStartError(
                    f"Multilogin HTTP error {response.status_code}: {response.text}"
                ) from exc
            except (ValueError, RequestException) as exc:
                raise ProfileStartError(
                    f"Multilogin request failed: {exc}"
                ) from exc

        raise ProfileStartError(f"Profile {profile_id} could not be started after auto-stop retry.")

    @staticmethod
    def _extract_debug_port(body: dict[str, Any]) -> int:
        status = body.get("status") or {}
        error_code = status.get("error_code")
        if error_code:
            message = status.get("message") or "Unknown error"
            raise ProfileStartError(
                f"Multilogin API error ({error_code}): {message}"
            )

        data = body.get("data") or {}
        raw_port = data.get("port")
        if raw_port is None:
            raise ProfileStartError(
                f"Multilogin response missing port: {body}"
            )

        try:
            return int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ProfileStartError(
                f"Multilogin returned invalid port: {raw_port!r}"
            ) from exc

    def stop_profile(self, profile_id: str) -> None:
        """Stop a running Multilogin profile."""
        url = f"{self.LAUNCHER_BASE}/api/v1/profile/stop/p/{profile_id.strip()}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        try:
            requests.get(url, headers=headers, timeout=self._timeout, verify=False)
        except RequestException:
            pass


# ---------------------------------------------------------------------------
# BrowserManager
# ---------------------------------------------------------------------------


class BrowserManager:
    """
    High-level facade for starting anti-detection profiles and attaching nodriver.

    Reads configuration from environment variables (via python-dotenv):

    - BROWSER_PROVIDER   : ``morelogin`` | ``multilogin``
    - MORELOGIN_API_KEY  : Optional auth token for MoreLogin local API
    - MORELOGIN_PORT     : MoreLogin local API port (default: 40000)
    - MULTILOGIN_TOKEN   : Bearer token for Multilogin launcher API
    - MULTILOGIN_FOLDER_ID : Folder UUID containing the target profile

    Example::

        manager = BrowserManager()
        browser = await manager.get_browser_instance("your-profile-id")
        page = await browser.get("https://example.com")
    """

    SUPPORTED_PROVIDERS = frozenset({"morelogin", "multilogin"})

    def __init__(self, env_path: Optional[str] = None) -> None:
        """
        Initialize the manager and load environment configuration.

        Args:
            env_path: Optional path to a ``.env`` file. Defaults to project root.
        """
        load_dotenv(env_path or DEFAULT_ENV_PATH)

        self._provider_name = (os.getenv("BROWSER_PROVIDER") or "").strip().lower()
        self._cdp_host = "127.0.0.1"
        self._morelogin_api_key = os.getenv("MORELOGIN_API_KEY")
        self._morelogin_port = os.getenv("MORELOGIN_PORT", "40000")
        self._multilogin_token = os.getenv("MULTILOGIN_TOKEN", "")
        self._multilogin_folder_id = os.getenv("MULTILOGIN_FOLDER_ID", "")
        self._provider = self._build_provider(self._provider_name)
        self._logger = logging.getLogger("mmb.browser_manager")

    def _build_provider(self, provider_name: str) -> BaseBrowserProvider:
        if provider_name not in self.SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(self.SUPPORTED_PROVIDERS))
            raise UnsupportedProviderError(
                f"Unsupported BROWSER_PROVIDER={provider_name!r}. "
                f"Choose one of: {supported}"
            )

        if provider_name == "morelogin":
            port_raw = self._morelogin_port
            try:
                port = int(port_raw)
            except ValueError as exc:
                raise ValueError(
                    f"MORELOGIN_PORT must be an integer, got {port_raw!r}"
                ) from exc

            return MoreLoginProvider(
                api_key=self._morelogin_api_key,
                port=port,
            )

        return MultiloginProvider(
            token=self._multilogin_token,
            folder_id=self._multilogin_folder_id,
        )

    def _resolve_provider(self, provider: Optional[str] = None) -> BaseBrowserProvider:
        name = (provider or self._provider_name).strip().lower()
        if name == self._provider_name:
            return self._provider
        return self._build_provider(name)

    @property
    def provider_name(self) -> str:
        """Active provider identifier from ``BROWSER_PROVIDER``."""
        return self._provider_name

    def start_profile(self, profile_id: str) -> int:
        """
        Start a browser profile through the configured provider.

        Args:
            profile_id: MoreLogin ``envId`` or Multilogin profile UUID.

        Returns:
            CDP debug port exposed on localhost.

        Raises:
            ProviderConnectionError: Provider API unreachable.
            ProfileStartError: Profile failed to start.
        """
        if not profile_id or not profile_id.strip():
            raise ValueError("profile_id must be a non-empty string.")

        return self._provider.start_profile(profile_id.strip())

    def stop_profile(self, profile_id: str) -> None:
        """Stop a running browser profile via the active provider."""
        if not profile_id or not profile_id.strip():
            return
        stop_fn = getattr(self._provider, "stop_profile", None)
        if callable(stop_fn):
            stop_fn(profile_id.strip())

    def create_profile(
        self,
        identity: dict[str, Any],
        provider: Optional[str] = None,
        proxy: Optional[ProxyConfig] = None,
        profile_name: Optional[str] = None,
    ) -> str:
        """
        Create a browser profile through the specified provider.

        Args:
            identity: Identity payload from ``IdentityManager``.
            provider: ``morelogin`` or ``multilogin``. Defaults to env setting.
            proxy: Optional proxy configuration.
            profile_name: Optional profile display name.

        Returns:
            Provider-specific profile identifier.
        """
        return self._resolve_provider(provider).create_profile(
            identity=identity,
            proxy=proxy,
            profile_name=profile_name,
        )

    def generate_multilogin_proxy(self, country_code: str) -> ProxyConfig:
        """Generate a Multilogin built-in proxy for the given country."""
        provider = self._resolve_provider("multilogin")
        if not isinstance(provider, MultiloginProvider):
            raise UnsupportedProviderError("Multilogin provider is not configured.")
        return provider.generate_builtin_proxy(country_code)

    def validate_proxy(self, proxy: ProxyConfig) -> dict[str, Any]:
        """Validate a proxy and return its exit IP and location metadata."""
        provider = self._resolve_provider("multilogin")
        if not isinstance(provider, MultiloginProvider):
            raise UnsupportedProviderError("Multilogin provider is not configured.")
        clean: ProxyConfig = {
            "host": proxy["host"],
            "port": int(proxy["port"]),
            "type": proxy.get("type", "http"),
            "user": proxy.get("user", ""),
            "password": proxy.get("password", ""),
        }
        return provider.validate_proxy(clean)

    async def get_browser_instance(
        self,
        profile_id: str,
        identity: Optional[dict[str, Any]] = None,
        *,
        apply_viewport: bool = False,
    ) -> Browser:
        """
        Start a profile and connect nodriver to its CDP debug port.

        Applies a mandatory post-start cooldown before CDP attach. Viewport
        enforcement is optional here — prefer ``apply_viewport_to_tab`` after
        the session tab is stable (YouTubeManager.open_session).

        Args:
            profile_id: Provider-specific profile identifier.
            identity: Optional IdentityManager payload for viewport mapping.
            apply_viewport: When True, apply viewport immediately after connect.

        Returns:
            Connected nodriver ``Browser`` instance.

        Raises:
            ProviderConnectionError: Provider API unreachable.
            ProfileStartError: Profile failed to start.
        """
        profile_id = profile_id.strip()
        is_android = _is_mobile_identity(identity or {})

        async with profile_session_lock(profile_id):
            # Run blocking HTTP call in thread pool so event loop stays unblocked
            loop = asyncio.get_event_loop()
            debug_port = await loop.run_in_executor(None, self.start_profile, profile_id)
            self._logger.info(
                "Profile started | id=%s port=%s cooldown=%ss android=%s",
                profile_id,
                debug_port,
                CDP_CONNECT_COOLDOWN_SECONDS,
                is_android,
            )
            await asyncio.sleep(CDP_CONNECT_COOLDOWN_SECONDS)

            # Android: mandatory 10s settle — no interactions until TCP stack ready
            if is_android:
                self._logger.info(
                    "Android profile detected — 10s stability settle (no CDP touch)"
                )
                await asyncio.sleep(10.0)

            # Android: retry with 15s gap on ConnectionClosedError
            for _attempt in range(2):
                try:
                    browser = await uc.start(host=self._cdp_host, port=debug_port)
                    break
                except Exception as exc:
                    exc_name = type(exc).__name__
                    if is_android and _attempt == 0 and (
                        "ConnectionClosed" in exc_name or "ConnectionReset" in exc_name
                        or "ConnectionError" in exc_name or "closed" in str(exc).lower()
                    ):
                        self._logger.warning(
                            "Android CDP ConnectionClosed on first attempt — 15s TCP recovery wait | %s",
                            exc,
                        )
                        await asyncio.sleep(15.0)
                        continue
                    raise
            else:
                raise ProfileStartError(
                    f"Android CDP connect failed after retry for profile {profile_id}"
                )

            await asyncio.sleep(1.0)

            if apply_viewport and identity and not is_android:
                # Android: skip viewport at this stage — apply only after settle
                tab = browser.main_tab
                if tab is None:
                    tab = await browser.get("about:blank")
                await apply_adaptive_viewport(tab, identity, self._logger)

            return browser

    async def prepare_session_tab(
        self,
        browser: Browser,
        identity: dict[str, Any],
    ) -> Tab:
        """
        Apply adaptive viewport to the main tab **before** any navigation.

        Stability-first: ensures CDP device metrics match the profile OS/resolution
        while the tab is still on its startup page (e.g. chrome://newtab/).
        """
        tab = browser.main_tab
        if tab is None:
            raise BrowserProviderError("Browser connected but no main tab is available.")

        start_url = ""
        try:
            start_url = tab.url or ""
        except Exception:
            pass

        profile = await self.apply_viewport_to_tab(tab, identity)
        self._logger.info(
            "Viewport applied before navigation | %sx%s %s | start_url=%s",
            profile.width,
            profile.height,
            profile.os_label,
            start_url or "unknown",
        )
        return tab

    async def apply_viewport_to_tab(
        self,
        tab: Tab,
        identity: dict[str, Any],
    ) -> ViewportProfile:
        """Apply adaptive viewport to an existing tab (idempotent helper)."""
        return await apply_adaptive_viewport(tab, identity, self._logger)
