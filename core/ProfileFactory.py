"""
Profile orchestration — combines identity generation with provider profile creation.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from providers.BrowserManager import (
    BrowserManager,
    BrowserProviderError,
    ProxyConfig,
)
from services.IdentityManager import GeoIPError, IdentityManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "factory.log"


class ProfileFactoryError(Exception):
    """Raised when profile creation orchestration fails."""


class ProfileFactory:
    """
    Orchestrates stealth profile creation across identity and browser providers.

    Workflow for ``create_stealth_profile``:

    1. Resolve proxy and detect exit IP.
    2. Build IP-synced identity via ``IdentityManager.get_precise_identity``.
    3. Create a provider profile via ``BrowserManager`` with fingerprint settings.
    4. Persist and return the profile id plus identity metadata.

    Example::

        factory = ProfileFactory()
        result = factory.create_stealth_profile(
            country_code="GB",
            provider="morelogin",
            proxy={
                "host": "proxy.example.com",
                "port": 8080,
                "user": "user",
                "password": "pass",
                "type": "http",
            },
        )
        print(result["profile_id"], result["identity"]["timezone"])
    """

    SUPPORTED_PROVIDERS = frozenset({"morelogin", "multilogin"})

    def __init__(
        self,
        env_path: Optional[Path | str] = None,
        log_path: Optional[Path | str] = None,
    ) -> None:
        """
        Initialize orchestrator dependencies and audit logging.

        Args:
            env_path: Optional ``.env`` file path.
            log_path: Optional factory audit log path.
        """
        load_dotenv(env_path or DEFAULT_ENV_PATH)

        self._identity_manager = IdentityManager(env_path=env_path)
        self._browser_manager = BrowserManager(env_path=str(env_path or DEFAULT_ENV_PATH))
        self._logger = self._configure_logger(log_path or DEFAULT_LOG_PATH)

    @staticmethod
    def _configure_logger(log_path: Path | str) -> logging.Logger:
        """Configure a file logger for factory audit trails."""
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("mmb.profile_factory")
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

    def create_stealth_profile(
        self,
        country_code: str = "US",
        provider: str = "morelogin",
        proxy: Optional[ProxyConfig] = None,
        profile_name: Optional[str] = None,
        mobile_first: bool = True,
        mobile_platform: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Create a fully configured anti-detection browser profile.

        Args:
            country_code: ISO country code for geo identity (``US``, ``GB``, etc.).
            provider: Target provider — ``morelogin`` or ``multilogin``.
            proxy: Optional proxy dict with ``host``, ``port``, ``user``,
                ``password``, and ``type`` (``http`` / ``socks5``).
            profile_name: Optional custom profile name.
            mobile_first: When True, configure Android/iOS mobile fingerprint.
            mobile_platform: Force ``android`` or ``ios`` (optional).

        Returns:
            Dictionary with ``profile_id``, ``provider``, ``country_code``,
            and ``identity`` keys.

        Raises:
            ProfileFactoryError: On validation or provider failures.
        """
        provider_name = provider.strip().lower()
        country = country_code.strip().upper()

        if provider_name not in self.SUPPORTED_PROVIDERS:
            supported = ", ".join(sorted(self.SUPPORTED_PROVIDERS))
            raise ProfileFactoryError(
                f"Unsupported provider={provider_name!r}. Choose: {supported}"
            )

        self._logger.info(
            "Starting stealth profile creation | provider=%s country=%s",
            provider_name,
            country,
        )

        try:
            resolved_proxy = self._resolve_proxy(provider_name, country, proxy)
            profile_name_resolved = profile_name or os.getenv("DEFAULT_PROFILE_NAME", "MMB")

            if resolved_proxy:
                self._logger.info(
                    "Proxy configured | source=%s host=%s port=%s type=%s",
                    resolved_proxy.get("source", "custom"),
                    resolved_proxy.get("host"),
                    resolved_proxy.get("port"),
                    resolved_proxy.get("type", "http"),
                )
            else:
                self._logger.info("No proxy supplied — profile will use direct connection")

            proxy_ip = self._resolve_proxy_exit_ip(resolved_proxy)
            identity = self._build_synced_identity(proxy_ip, country, resolved_proxy)

            if mobile_first:
                identity = self._identity_manager.apply_mobile_fingerprint(
                    identity,
                    platform=mobile_platform,
                    provider=provider_name,
                )
                self._logger.info(
                    "Mobile fingerprint applied | platform=%s os_type=%s ua=%s...",
                    identity.get("device_platform"),
                    identity.get("os_type"),
                    str(identity.get("user_agent", ""))[:48],
                )

            self._logger.info(
                "Step A: identity synced | ip=%s tz=%s city=%s source=%s screen=%s",
                identity.get("ip_address", "n/a"),
                identity["timezone"],
                identity.get("city", "n/a"),
                identity.get("timezone_source", "n/a"),
                identity["screen_resolution"],
            )

            self._logger.info(
                "Step B: creating profile via %s API | name=%s",
                provider_name,
                profile_name_resolved,
            )
            profile_id = self._browser_manager.create_profile(
                identity=identity,
                provider=provider_name,
                proxy=self._clean_proxy(resolved_proxy),
                profile_name=profile_name_resolved,
            )
            self._logger.info("Profile created | profile_id=%s", profile_id)

            self._logger.info(
                "Step C: identity settings applied at creation | seeds="
                "canvas:%s webgl:%s audio:%s",
                identity["canvas_seed"][:8],
                identity["webgl_seed"][:8],
                identity["audio_seed"][:8],
            )

            identity["profile_id"] = profile_id
            self._identity_manager.store_identity(profile_id, identity)

            result = {
                "profile_id": profile_id,
                "provider": provider_name,
                "country_code": identity.get("country_code", country),
                "identity": identity,
            }

            self._logger.info(
                "Step D: complete | profile_id=%s provider=%s ip=%s tz=%s",
                profile_id,
                provider_name,
                identity.get("ip_address", "n/a"),
                identity["timezone"],
            )
            return result

        except BrowserProviderError as exc:
            self._logger.error("Provider error during profile creation: %s", exc)
            raise ProfileFactoryError(str(exc)) from exc
        except ValueError as exc:
            self._logger.error("Validation error during profile creation: %s", exc)
            raise ProfileFactoryError(str(exc)) from exc
        except Exception as exc:
            self._logger.exception("Unexpected error during profile creation")
            raise ProfileFactoryError(f"Unexpected error: {exc}") from exc

    def _build_synced_identity(
        self,
        proxy_ip: Optional[str],
        country_code: str,
        proxy: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Build identity synchronized to proxy exit IP when available.

        Falls back to city/country hints from proxy validation, then to
        country-level defaults.
        """
        if proxy_ip:
            try:
                return self._identity_manager.get_precise_identity(proxy_ip)
            except GeoIPError as exc:
                self._logger.warning(
                    "GeoIP lookup failed for %s: %s — using location fallback",
                    proxy_ip,
                    exc,
                )

        if proxy and proxy.get("city"):
            return self._identity_manager.generate_identity(
                country_code=country_code,
                city=str(proxy["city"]),
                region=str(proxy.get("region") or ""),
            )

        self._logger.warning(
            "No proxy exit IP — falling back to country-level identity for %s",
            country_code,
        )
        return self._identity_manager.generate_identity(country_code=country_code)

    def _resolve_proxy_exit_ip(
        self, proxy: Optional[dict[str, Any]]
    ) -> Optional[str]:
        """Validate proxy and return the public exit IP seen by targets."""
        if not proxy:
            return None

        clean = self._clean_proxy(proxy)
        if not clean:
            return None

        try:
            validation = self._browser_manager.validate_proxy(clean)
            exit_ip = validation.get("ip")
            if validation.get("city"):
                proxy["city"] = validation["city"]
            if validation.get("region"):
                proxy["region"] = validation["region"]
            if validation.get("country"):
                proxy["country"] = validation["country"]
            self._logger.info(
                "Proxy exit IP detected | ip=%s city=%s country=%s",
                exit_ip,
                validation.get("city"),
                validation.get("country"),
            )
            return exit_ip
        except BrowserProviderError as exc:
            self._logger.warning("Proxy validation failed: %s", exc)
            return None

    @staticmethod
    def _clean_proxy(proxy: Optional[dict[str, Any]]) -> Optional[ProxyConfig]:
        """Strip internal metadata keys before sending proxy to providers."""
        if not proxy or not proxy.get("host"):
            return None
        return ProxyConfig(
            host=str(proxy["host"]),
            port=int(proxy["port"]),
            user=str(proxy.get("user", "")),
            password=str(proxy.get("password", "")),
            type=str(proxy.get("type", "http")),
        )

    def _resolve_proxy(
        self,
        provider: str,
        country_code: str,
        proxy: Optional[ProxyConfig],
    ) -> Optional[dict[str, Any]]:
        """
        Resolve proxy configuration for profile creation.

        Multilogin uses built-in proxy generation by default.
        MoreLogin falls back to ``PROXY_*`` environment variables.
        """
        if proxy is not None:
            return {**proxy, "source": "custom"}

        if provider == "multilogin":
            self._logger.info(
                "Generating Multilogin built-in proxy for country=%s", country_code
            )
            generated = self._browser_manager.generate_multilogin_proxy(country_code)
            return {**generated, "source": "multilogin"}

        env_proxy = self._proxy_from_env()
        if env_proxy:
            return {**env_proxy, "source": "env"}

        return None

    @staticmethod
    def _proxy_from_env() -> Optional[ProxyConfig]:
        """Build proxy config from ``PROXY_*`` environment variables if present."""
        host = os.getenv("PROXY_SERVER", "").strip()
        port_raw = os.getenv("PROXY_PORT", "").strip()
        if not host or not port_raw:
            return None

        user = (
            os.getenv("PROXY_USER")
            or os.getenv("PROXY_PREFIX")
            or ""
        ).strip()
        password = (
            os.getenv("PROXY_PASS")
            or os.getenv("PROXY_PASSWORD")
            or ""
        ).strip()

        return ProxyConfig(
            host=host,
            port=int(port_raw),
            user=user,
            password=password,
            type=os.getenv("PROXY_TYPE", "http").strip() or "http",
        )


if __name__ == "__main__":
    factory = ProfileFactory()

    print("=== US / MoreLogin (dry structure test — requires running provider) ===")
    try:
        us_result = factory.create_stealth_profile(
            country_code="US",
            provider="morelogin",
        )
        print(f"Created: {us_result['profile_id']}")
        print(f"Timezone: {us_result['identity']['timezone']}")
    except ProfileFactoryError as exc:
        print(f"MoreLogin create skipped/failed: {exc}")

    print("\n=== DE / Multilogin ===")
    try:
        de_result = factory.create_stealth_profile(
            country_code="DE",
            provider="multilogin",
        )
        print(f"Created: {de_result['profile_id']}")
        print(f"Language: {de_result['identity']['language']}")
    except ProfileFactoryError as exc:
        print(f"Multilogin create skipped/failed: {exc}")
