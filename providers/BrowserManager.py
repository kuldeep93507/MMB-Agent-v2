"""
Browser profile lifecycle management for anti-detection providers.

Supports starting profiles via MoreLogin or Multilogin local/cloud APIs,
then attaching nodriver to the returned Chrome DevTools Protocol (CDP) port.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, TypedDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

import nodriver as uc
import requests
from dotenv import load_dotenv
from nodriver.core.browser import Browser
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, RequestException, Timeout

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

        payload: dict[str, Any] = {
            "browserTypeId": 1,
            "operatorSystemId": 1,
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
                "navigator_masking": "mask",
                "ports_masking": "mask",
                "screen_masking": "custom",
                "timezone_masking": "custom",
                "webrtc_masking": "mask",
                "proxy_masking": "custom" if proxy and proxy.get("host") else "disabled",
            },
            "fingerprint": {
                "timezone": {"zone": identity["timezone"]},
                "localization": {
                    "locale": identity["language"],
                    "languages": identity["language"],
                    "accept_languages": f"{identity['language']},en;q=0.9",
                },
                "screen": {
                    "width": width,
                    "height": height,
                    "pixel_ratio": 1,
                },
            },
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
            "os_type": "windows",
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
        """Start a Multilogin profile and return its automation CDP port."""
        url = (
            f"{self.LAUNCHER_BASE}/api/v2/profile/f/{self._folder_id}"
            f"/p/{profile_id}/start"
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        params = {
            # puppeteer returns a CDP-compatible port suitable for nodriver
            "automation_type": "puppeteer",
            "headless_mode": "false",
        }

        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
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
            raise ProfileStartError(
                f"Multilogin HTTP error {response.status_code}: {response.text}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise ProfileStartError(
                f"Multilogin request failed: {exc}"
            ) from exc

        return self._extract_debug_port(body)

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

    async def get_browser_instance(self, profile_id: str) -> Browser:
        """
        Start a profile and connect nodriver to its CDP debug port.

        Args:
            profile_id: Provider-specific profile identifier.

        Returns:
            Connected nodriver ``Browser`` instance.

        Raises:
            ProviderConnectionError: Provider API unreachable.
            ProfileStartError: Profile failed to start.
        """
        debug_port = self.start_profile(profile_id)
        return await uc.start(host=self._cdp_host, port=debug_port)
