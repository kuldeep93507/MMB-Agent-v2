"""
Browser identity and geographic fingerprint synchronization.

Generates consistent, country-aware identity payloads (timezone, language,
screen resolution, and noise seeds) for anti-detection browser profiles.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, RequestException, Timeout

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "identities.json"
GEOIP_API_URL = "http://ip-api.com/json/{ip}"

# ---------------------------------------------------------------------------
# Country-level defaults
# ---------------------------------------------------------------------------

GEO_MAP: dict[str, dict[str, str]] = {
    "US": {"timezone": "America/New_York", "language": "en-US"},
    "GB": {"timezone": "Europe/London", "language": "en-GB"},
    "CA": {"timezone": "America/Toronto", "language": "en-CA"},
    "DE": {"timezone": "Europe/Berlin", "language": "de-DE"},
}

# ---------------------------------------------------------------------------
# Region-level timezone map (ISO country + region code)
# ---------------------------------------------------------------------------

REGION_TIMEZONE_MAP: dict[str, str] = {
    "US:CA": "America/Los_Angeles",
    "US:WA": "America/Los_Angeles",
    "US:OR": "America/Los_Angeles",
    "US:NV": "America/Los_Angeles",
    "US:AZ": "America/Phoenix",
    "US:AK": "America/Anchorage",
    "US:HI": "Pacific/Honolulu",
    "US:CO": "America/Denver",
    "US:MT": "America/Denver",
    "US:NM": "America/Denver",
    "US:UT": "America/Denver",
    "US:WY": "America/Denver",
    "US:TX": "America/Chicago",
    "US:IL": "America/Chicago",
    "US:MN": "America/Chicago",
    "US:WI": "America/Chicago",
    "US:NY": "America/New_York",
    "US:FL": "America/New_York",
    "US:GA": "America/New_York",
    "US:MA": "America/New_York",
    "US:NJ": "America/New_York",
    "US:PA": "America/New_York",
    "CA:BC": "America/Vancouver",
    "CA:AB": "America/Edmonton",
    "CA:ON": "America/Toronto",
    "CA:QC": "America/Toronto",
    "DE:BY": "Europe/Berlin",
    "DE:BE": "Europe/Berlin",
    "GB:ENG": "Europe/London",
}

# ---------------------------------------------------------------------------
# City / region name → IANA timezone (normalized lowercase keys)
# ---------------------------------------------------------------------------

CITY_TIMEZONE_MAP: dict[str, str] = {
    "US:california": "America/Los_Angeles",
    "US:los angeles": "America/Los_Angeles",
    "US:san francisco": "America/Los_Angeles",
    "US:san diego": "America/Los_Angeles",
    "US:seattle": "America/Los_Angeles",
    "US:portland": "America/Los_Angeles",
    "US:las vegas": "America/Los_Angeles",
    "US:phoenix": "America/Phoenix",
    "US:denver": "America/Denver",
    "US:chicago": "America/Chicago",
    "US:dallas": "America/Chicago",
    "US:houston": "America/Chicago",
    "US:austin": "America/Chicago",
    "US:new york": "America/New_York",
    "US:miami": "America/New_York",
    "US:atlanta": "America/New_York",
    "US:boston": "America/New_York",
    "US:philadelphia": "America/New_York",
    "US:washington": "America/New_York",
    "US:detroit": "America/Detroit",
    "CA:toronto": "America/Toronto",
    "CA:vancouver": "America/Vancouver",
    "CA:montreal": "America/Toronto",
    "GB:london": "Europe/London",
    "DE:berlin": "Europe/Berlin",
    "DE:munich": "Europe/Berlin",
}

COMMON_SCREEN_RESOLUTIONS: tuple[str, ...] = (
    "1920x1080",
    "1366x768",
    "1440x900",
    "1536x864",
    "1280x720",
    "2560x1440",
    "1600x900",
    "1680x1050",
)

MOBILE_SCREEN_RESOLUTIONS: tuple[str, ...] = (
    "390x844",
    "412x915",
    "360x800",
    "384x854",
    "393x873",
    "414x896",
    "375x812",
)

MOBILE_ANDROID_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S911B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 7 Pro) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
)

MOBILE_IOS_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) CriOS/124.0.6367.88 Mobile/15E148 Safari/604.1",
)


class GeoIPError(Exception):
    """Raised when GeoIP lookup fails."""


class IdentityManager:
    """
    Manages browser identity fingerprints with geographic consistency.

    Supports country-level defaults, city/region timezone mapping, and
    IP-based precise synchronization via the ip-api.com GeoIP service.
    """

    def __init__(
        self,
        env_path: Optional[Path | str] = None,
        cache_path: Optional[Path | str] = None,
        geoip_timeout: int = 10,
    ) -> None:
        load_dotenv(env_path or DEFAULT_ENV_PATH)

        self._default_country = (
            os.getenv("DEFAULT_COUNTRY_CODE", "US").strip().upper() or "US"
        )
        cache_override = os.getenv("IDENTITY_CACHE_PATH")
        self._cache_path = Path(cache_override or cache_path or DEFAULT_CACHE_PATH)
        self._cache: dict[str, dict[str, Any]] = self._load_cache()
        self._geoip_timeout = geoip_timeout

    @staticmethod
    def get_supported_countries() -> tuple[str, ...]:
        """Return ISO country codes with geo-mapping entries."""
        return tuple(sorted(GEO_MAP.keys()))

    @staticmethod
    def _normalize_location_key(value: str) -> str:
        """Normalize city/region names for map lookups."""
        cleaned = re.sub(r"[^a-z0-9\s]", "", value.lower().strip())
        return re.sub(r"\s+", " ", cleaned)

    @classmethod
    def resolve_timezone(
        cls,
        country_code: str,
        *,
        region_code: Optional[str] = None,
        region_name: Optional[str] = None,
        city: Optional[str] = None,
        geoip_timezone: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        Resolve the best IANA timezone for a geographic location.

        Priority:
        1. GeoIP-provided IANA timezone (most accurate)
        2. City name mapping
        3. Region name mapping
        4. ISO region code mapping
        5. Country-level fallback

        Returns:
            Tuple of ``(timezone, source)`` describing how it was resolved.
        """
        country = country_code.strip().upper()

        if geoip_timezone:
            return geoip_timezone, "geoip"

        if city:
            city_key = f"{country}:{cls._normalize_location_key(city)}"
            if city_key in CITY_TIMEZONE_MAP:
                return CITY_TIMEZONE_MAP[city_key], "city"

        if region_name:
            region_key = f"{country}:{cls._normalize_location_key(region_name)}"
            if region_key in CITY_TIMEZONE_MAP:
                return CITY_TIMEZONE_MAP[region_key], "region_name"

        if region_code:
            iso_key = f"{country}:{region_code.strip().upper()}"
            if iso_key in REGION_TIMEZONE_MAP:
                return REGION_TIMEZONE_MAP[iso_key], "region_code"

        if country in GEO_MAP:
            return GEO_MAP[country]["timezone"], "country"

        return "UTC", "fallback"

    @staticmethod
    def get_geo_profile(country_code: str) -> dict[str, str]:
        """Look up default timezone and language for a country code."""
        code = country_code.strip().upper()
        if code not in GEO_MAP:
            supported = ", ".join(sorted(GEO_MAP))
            raise ValueError(
                f"Unsupported country_code={code!r}. Supported: {supported}"
            )
        return dict(GEO_MAP[code])

    @staticmethod
    def get_language_for_country(country_code: str) -> str:
        """Return BCP-47 language tag for a country, with sensible fallback."""
        code = country_code.strip().upper()
        if code in GEO_MAP:
            return GEO_MAP[code]["language"]
        return "en-US"

    def lookup_geoip(self, ip_address: str) -> dict[str, Any]:
        """
        Query ip-api.com for geographic metadata of an IP address.

        Args:
            ip_address: Public IPv4/IPv6 address.

        Returns:
            Dict with ``country_code``, ``country``, ``region_code``,
            ``region_name``, ``city``, and ``timezone`` keys.

        Raises:
            GeoIPError: On network failure or invalid response.
        """
        ip = ip_address.strip()
        if not ip:
            raise GeoIPError("IP address must not be empty.")

        fields = (
            "status,message,country,countryCode,region,regionName,city,timezone,query"
        )
        url = GEOIP_API_URL.format(ip=ip)

        try:
            response = requests.get(
                url,
                params={"fields": fields},
                timeout=self._geoip_timeout,
            )
            response.raise_for_status()
            data = response.json()
        except (RequestsConnectionError, Timeout) as exc:
            raise GeoIPError(f"GeoIP lookup failed for {ip}: connection error") from exc
        except HTTPError as exc:
            raise GeoIPError(
                f"GeoIP HTTP error for {ip}: {response.status_code}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise GeoIPError(f"GeoIP lookup failed for {ip}: {exc}") from exc

        if data.get("status") != "success":
            message = data.get("message", "unknown error")
            raise GeoIPError(f"GeoIP lookup failed for {ip}: {message}")

        return {
            "ip_address": data.get("query", ip),
            "country_code": (data.get("countryCode") or self._default_country).upper(),
            "country": data.get("country"),
            "region_code": data.get("region"),
            "region_name": data.get("regionName"),
            "city": data.get("city"),
            "timezone": data.get("timezone"),
        }

    def get_precise_identity(
        self,
        ip_address: str,
        profile_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Build an identity synchronized to a specific IP's geographic location.

        Uses GeoIP lookup first, then city/region maps, then country fallback
        to eliminate timezone gaps (e.g. California IP + New York timezone).

        Args:
            ip_address: Public exit IP of the proxy or connection.
            profile_id: Optional stable profile id for deterministic seeds.

        Returns:
            Full identity dictionary including ``ip_address``, ``city``,
            ``region``, and ``timezone_source`` metadata.
        """
        try:
            geo = self.lookup_geoip(ip_address)
        except GeoIPError:
            country = self._default_country
            geo = {
                "ip_address": ip_address,
                "country_code": country,
                "country": None,
                "region_code": None,
                "region_name": None,
                "city": None,
                "timezone": None,
            }

        country_code = geo["country_code"]
        timezone, tz_source = self.resolve_timezone(
            country_code,
            region_code=geo.get("region_code"),
            region_name=geo.get("region_name"),
            city=geo.get("city"),
            geoip_timezone=geo.get("timezone"),
        )

        language = self.get_language_for_country(country_code)
        geo_profile = {"timezone": timezone, "language": language}

        cache_key = None
        if profile_id:
            cache_key = self._cache_key(profile_id, country_code)
            cached = self._cache.get(cache_key)
            if cached and cached.get("ip_address") == ip_address:
                return dict(cached)

        identity = self._build_identity(
            country_code,
            geo_profile,
            profile_id=profile_id,
            ip_address=geo.get("ip_address", ip_address),
            city=geo.get("city"),
            region=geo.get("region_name") or geo.get("region_code"),
            timezone_source=tz_source,
        )

        if profile_id and cache_key:
            self._cache[cache_key] = identity
            self._save_cache()

        return dict(identity)

    def generate_identity(
        self,
        country_code: Optional[str] = None,
        profile_id: Optional[str] = None,
        *,
        city: Optional[str] = None,
        region: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Generate a browser identity aligned to a geographic region.

        When ``city`` or ``region`` is supplied, timezone is resolved at
        city/region granularity before falling back to country defaults.
        """
        code = (country_code or self._default_country).strip().upper()
        timezone, tz_source = self.resolve_timezone(
            code,
            region_name=region,
            city=city,
        )
        language = self.get_language_for_country(code)
        geo = {"timezone": timezone, "language": language}

        if profile_id:
            cache_key = self._cache_key(profile_id, code)
            cached = self._cache.get(cache_key)
            if cached:
                return dict(cached)

            identity = self._build_identity(
                code,
                geo,
                profile_id=profile_id,
                city=city,
                region=region,
                timezone_source=tz_source,
            )
            self._cache[cache_key] = identity
            self._save_cache()
            return dict(identity)

        return self._build_identity(
            code,
            geo,
            profile_id=None,
            city=city,
            region=region,
            timezone_source=tz_source,
        )

    def store_identity(self, profile_id: str, identity: dict[str, Any]) -> None:
        """Persist an identity bound to a provider profile id."""
        country_code = str(identity.get("country_code", self._default_country)).upper()
        cache_key = self._cache_key(profile_id, country_code)
        self._cache[cache_key] = {**identity, "profile_id": profile_id}
        self._save_cache()

    def _build_identity(
        self,
        country_code: str,
        geo: dict[str, str],
        profile_id: Optional[str],
        *,
        ip_address: Optional[str] = None,
        city: Optional[str] = None,
        region: Optional[str] = None,
        timezone_source: Optional[str] = None,
    ) -> dict[str, Any]:
        """Assemble a full identity dict using deterministic or random entropy."""
        seed_basis = profile_id or ip_address or country_code

        if profile_id:
            rng = self._deterministic_rng(profile_id, country_code)
            canvas_seed = self._deterministic_seed(profile_id, "canvas")
            webgl_seed = self._deterministic_seed(profile_id, "webgl")
            audio_seed = self._deterministic_seed(profile_id, "audio")
            screen_resolution = rng.choice(COMMON_SCREEN_RESOLUTIONS)
        else:
            rng = random.Random(
                int(
                    hashlib.sha256(str(seed_basis).encode()).hexdigest()[:16],
                    16,
                )
            )
            canvas_seed = secrets.token_hex(16)
            webgl_seed = secrets.token_hex(16)
            audio_seed = secrets.token_hex(16)
            screen_resolution = rng.choice(COMMON_SCREEN_RESOLUTIONS)

        identity: dict[str, Any] = {
            "country_code": country_code,
            "timezone": geo["timezone"],
            "language": geo["language"],
            "screen_resolution": screen_resolution,
            "canvas_seed": canvas_seed,
            "webgl_seed": webgl_seed,
            "audio_seed": audio_seed,
        }

        if ip_address:
            identity["ip_address"] = ip_address
        if city:
            identity["city"] = city
        if region:
            identity["region"] = region
        if timezone_source:
            identity["timezone_source"] = timezone_source
        if profile_id:
            identity["profile_id"] = profile_id

        return identity

    def apply_mobile_fingerprint(
        self,
        identity: dict[str, Any],
        platform: Optional[str] = None,
        *,
        provider: str = "multilogin",
    ) -> dict[str, Any]:
        """
        Enrich a desktop identity with mobile device traits (UA, screen, OS).

        Multilogin supports ``android`` os_type; MoreLogin supports Android (3)
        and iOS (4) via ``operator_system_id``.
        """
        mobile = dict(identity)
        chosen = (platform or os.getenv("MOBILE_PLATFORM", "")).strip().lower()
        if chosen not in {"android", "ios"}:
            chosen = random.choice(("android", "ios"))

        if provider == "multilogin" and chosen == "ios":
            chosen = "android"

        resolution = random.choice(MOBILE_SCREEN_RESOLUTIONS)
        width_str, height_str = resolution.split("x", 1)
        pixel_ratio = random.choice((2.0, 2.625, 3.0))

        mobile["device_platform"] = chosen
        mobile["mobile_first"] = True
        mobile["screen_resolution"] = resolution
        mobile["screen_width"] = int(width_str)
        mobile["screen_height"] = int(height_str)
        mobile["pixel_ratio"] = pixel_ratio
        mobile["operator_system_id"] = 3 if chosen == "android" else 4
        mobile["os_type"] = "android" if chosen == "android" else "ios"
        mobile["user_agent"] = random.choice(
            MOBILE_ANDROID_USER_AGENTS
            if chosen == "android"
            else MOBILE_IOS_USER_AGENTS
        )
        mobile["navigator_platform"] = (
            "Linux armv8l" if chosen == "android" else "iPhone"
        )
        mobile["max_touch_points"] = random.randint(5, 10)
        mobile["hardware_concurrency"] = random.choice((4, 6, 8))
        return mobile

    @staticmethod
    def _cache_key(profile_id: str, country_code: str) -> str:
        return f"{profile_id.strip()}::{country_code.strip().upper()}"

    @staticmethod
    def _deterministic_rng(profile_id: str, country_code: str) -> random.Random:
        digest = hashlib.sha256(
            f"{profile_id.strip()}:{country_code.strip().upper()}".encode()
        ).hexdigest()
        return random.Random(int(digest[:16], 16))

    @staticmethod
    def _deterministic_seed(profile_id: str, salt: str) -> str:
        digest = hashlib.sha256(f"{profile_id.strip()}:{salt}".encode()).hexdigest()
        return digest[:32]

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        if not self._cache_path.exists():
            return {}
        try:
            with self._cache_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_path.open("w", encoding="utf-8") as handle:
            json.dump(self._cache, handle, indent=2, sort_keys=True)


if __name__ == "__main__":
    manager = IdentityManager()

    print("=== City-level timezone resolution ===")
    for label, kwargs in (
        ("California", {"country_code": "US", "city": "Los Angeles"}),
        ("New York", {"country_code": "US", "city": "New York"}),
        ("Chicago", {"country_code": "US", "city": "Chicago"}),
    ):
        identity = manager.generate_identity(**kwargs)
        print(f"{label:12} -> {identity['timezone']} ({identity.get('timezone_source')})")

    print("\n=== Precise identity from IP (Google DNS) ===")
    try:
        precise = manager.get_precise_identity("8.8.8.8")
        print(
            f"IP 8.8.8.8 -> tz={precise['timezone']} "
            f"city={precise.get('city')} source={precise.get('timezone_source')}"
        )
    except GeoIPError as exc:
        print(f"GeoIP test skipped: {exc}")
