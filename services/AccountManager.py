"""
Gmail account creation automation with browser warm-up, human-like interaction,
and 5sim SMS verification integration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import nodriver as uc
import requests
from dotenv import load_dotenv
from nodriver import cdp
from nodriver.core.browser import Browser
from nodriver.core.element import Element
from nodriver.core.tab import Tab
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError, RequestException, Timeout

from providers.BrowserManager import BrowserManager, BrowserProviderError
from services.IdentityManager import IdentityManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "account_manager.log"
PROFILE_DATA_DIR = PROJECT_ROOT / "data" / "profiles"

GMAIL_SIGNUP_URL = (
    "https://accounts.google.com/signup/v2/createaccount"
    "?flowName=GlifWebSignIn&flowEntry=SignUp"
)

WARMUP_SITES: tuple[str, ...] = (
    "https://www.amazon.com",
    "https://www.wikipedia.org",
    "https://www.bbc.com",
    "https://www.nytimes.com",
    "https://www.weather.com",
)

FIVESIM_COUNTRY_MAP: dict[str, str] = {
    "US": "usa",
    "GB": "england",
    "CA": "canada",
    "DE": "germany",
}

FIRST_NAMES: tuple[str, ...] = (
    "James", "Mary", "Robert", "Patricia", "Michael", "Jennifer", "David",
    "Linda", "William", "Elizabeth", "Richard", "Barbara", "Joseph", "Susan",
    "Thomas", "Jessica", "Christopher", "Sarah", "Daniel", "Karen",
)

LAST_NAMES: tuple[str, ...] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
)

GENDER_OPTIONS: tuple[str, ...] = ("Male", "Female", "Rather not say")

FEMALE_FIRST_NAMES: frozenset[str] = frozenset({
    "Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara",
    "Susan", "Jessica", "Sarah", "Karen",
})

MALE_FIRST_NAMES: frozenset[str] = frozenset({
    "James", "Robert", "Michael", "David", "William", "Richard",
    "Joseph", "Thomas", "Christopher", "Daniel",
})

MONTH_NAMES: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

BIRTHDAY_VALIDATION_ERRORS: tuple[str, ...] = (
    "Please fill in a complete birthday",
    "Please select your gender",
)

MONTH_DROPDOWN_CSS: tuple[str, ...] = (
    "#month",
    'div[aria-label="Month"]',
    'div[role="combobox"][aria-label*="Month"]',
    'div[jsname="wQNmvb"]',
)

GENDER_DROPDOWN_CSS: tuple[str, ...] = (
    "#gender",
    'div[aria-label="Gender"]',
    'div[role="combobox"][aria-label*="Gender"]',
)

DAY_INPUT_CSS: tuple[str, ...] = (
    'input[name="day"]',
    "#day",
    'input[aria-label="Day"]',
    'input[aria-label*="Day"]',
)

YEAR_INPUT_CSS: tuple[str, ...] = (
    'input[name="year"]',
    "#year",
    'input[aria-label="Year"]',
    'input[aria-label*="Year"]',
)

PASSWORD_INPUT_CSS: tuple[str, ...] = (
    'input[name="Passwd"]',
    'input[type="password"][name="Passwd"]',
    'input[aria-label="Password"]',
    'input[aria-label*="Password"]',
    'input[autocomplete="new-password"]',
)

PASSWORD_INPUT_XPATH: tuple[str, ...] = (
    '//input[@name="Passwd"]',
    '//input[@type="password" and contains(@aria-label, "Password")]',
    '(//input[@type="password"])[1]',
)

CONFIRM_PASSWORD_CSS: tuple[str, ...] = (
    'input[name="ConfirmPasswd"]',
    'input[type="password"][name="ConfirmPasswd"]',
    'input[aria-label="Confirm"]',
    'input[aria-label*="Confirm"]',
)

CONFIRM_PASSWORD_XPATH: tuple[str, ...] = (
    '//input[@name="ConfirmPasswd"]',
    '//input[@type="password" and contains(@aria-label, "Confirm")]',
    '(//input[@type="password"])[2]',
)

ERROR_NEXT_SCREENSHOT = PROJECT_ROOT / "logs" / "error_next_button.png"
ERROR_PAGE_HTML = PROJECT_ROOT / "logs" / "error_page.html"
ERROR_BIRTHDAY_SCREENSHOT = PROJECT_ROOT / "logs" / "error_birthday_gender.png"
ERROR_PASSWORD_SCREENSHOT = PROJECT_ROOT / "logs" / "error_password.png"

NEXT_BUTTON_CSS_SELECTORS: tuple[str, ...] = (
    "#accountDetailsNext",
    "#birthdaygenderNext",
    "#createpasswordNext",
    "#phoneNumberNext",
    "#verifyPhoneNext",
    "#recoveryEmailNext",
    'button[jsname="LgbsSe"]',
    'div[role="button"][jsname="LgbsSe"]',
    "button.VfPpkd-LgbsSe",
    'div[role="button"].VfPpkd-LgbsSe',
    'button[type="button"]',
    'div[role="button"]',
)

NEXT_BUTTON_XPATH_SELECTORS: tuple[str, ...] = (
    '//button[contains(normalize-space(.), "Next")]',
    '//span[normalize-space(text())="Next"]/ancestor::button[1]',
    '//span[normalize-space(text())="Next"]/ancestor::div[@role="button"][1]',
    '//button[contains(normalize-space(.), "Continue")]',
    '//span[normalize-space(text())="Continue"]/ancestor::button[1]',
    '//div[@role="button"]//span[contains(text(),"Next")]/ancestor::div[@role="button"][1]',
    '//*[@id="accountDetailsNext"]',
    '//*[@id="birthdaygenderNext"]',
)

NEXT_BUTTON_TEXT_LABELS: tuple[str, ...] = ("Next", "Continue", "I agree")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AccountManagerError(Exception):
    """Base exception for account automation failures."""


class FiveSimError(AccountManagerError):
    """Raised when 5sim API operations fail."""


class GmailSignupError(AccountManagerError):
    """Raised when Gmail signup flow fails."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HumanName:
    first: str
    last: str

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}"


@dataclass
class FiveSimOrder:
    order_id: int
    phone: str
    country: str
    operator: str
    expires: Optional[str] = None


@dataclass
class GmailAccountResult:
    profile_id: str
    email: str
    password: str
    first_name: str
    last_name: str
    phone: str
    country_code: str
    cookies_path: Path
    metadata_path: Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class NameGenerator:
    """Generate plausible human first/last name pairs."""

    @staticmethod
    def generate() -> HumanName:
        return HumanName(
            first=random.choice(FIRST_NAMES),
            last=random.choice(LAST_NAMES),
        )

    @staticmethod
    def generate_password(length: int = 16) -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%"
        while True:
            password = "".join(secrets.choice(alphabet) for _ in range(length))
            if (
                any(c.islower() for c in password)
                and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password)
            ):
                return password

    @staticmethod
    def infer_gender(first_name: str) -> str:
        """Map a first name to Male or Female for the signup persona."""
        if first_name in FEMALE_FIRST_NAMES:
            return "Female"
        if first_name in MALE_FIRST_NAMES:
            return "Male"
        return random.choice(("Male", "Female"))

    @staticmethod
    def suggest_username(first: str, last: str) -> str:
        suffix = random.randint(100, 9999)
        base = f"{first.lower()}.{last.lower()}{suffix}"
        return re.sub(r"[^a-z0-9.]", "", base)


class FiveSimClient:
    """
    5sim.net SMS verification API client.

    Docs: https://5sim.net/docs
    """

    BASE_URL = "https://5sim.net/v1"

    def __init__(self, api_key: str, timeout: int = 30) -> None:
        if not api_key.strip():
            raise FiveSimError("FIVESIM_API_KEY is not configured.")
        self._api_key = api_key.strip()
        self._timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.BASE_URL}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self._headers,
                timeout=self._timeout,
                **kwargs,
            )
            response.raise_for_status()
            if response.text:
                return response.json()
            return {}
        except RequestsConnectionError as exc:
            raise FiveSimError("Could not connect to 5sim API.") from exc
        except Timeout as exc:
            raise FiveSimError("5sim API request timed out.") from exc
        except HTTPError as exc:
            raise FiveSimError(
                f"5sim HTTP error {response.status_code}: {response.text}"
            ) from exc
        except (ValueError, RequestException) as exc:
            raise FiveSimError(f"5sim request failed: {exc}") from exc

    @staticmethod
    def map_country(country_code: str) -> str:
        code = country_code.strip().upper()
        return FIVESIM_COUNTRY_MAP.get(code, code.lower())

    def buy_number(
        self,
        country_code: str,
        product: str = "google",
        operator: Optional[str] = None,
    ) -> FiveSimOrder:
        """Purchase a virtual number for Google verification."""
        country = self.map_country(country_code)
        op = operator or os.getenv("FIVESIM_OPERATOR", "any")
        data = self._request(
            "GET",
            f"/user/buy/activation/{country}/{op}/{product}",
        )
        order_id = data.get("id")
        phone = data.get("phone")
        if not order_id or not phone:
            raise FiveSimError(f"5sim buy response invalid: {data}")

        return FiveSimOrder(
            order_id=int(order_id),
            phone=str(phone),
            country=country,
            operator=str(data.get("operator", op)),
            expires=data.get("expires"),
        )

    def check_order(self, order_id: int) -> dict[str, Any]:
        """Poll order status and SMS payload."""
        return self._request("GET", f"/user/check/{order_id}")

    def finish_order(self, order_id: int) -> None:
        """Mark order complete after successful verification."""
        self._request("GET", f"/user/finish/{order_id}")

    def cancel_order(self, order_id: int) -> None:
        """Cancel order and request refund when possible."""
        try:
            self._request("GET", f"/user/cancel/{order_id}")
        except FiveSimError:
            pass

    @staticmethod
    def extract_otp(sms_text: str) -> Optional[str]:
        """Extract a 4-8 digit OTP code from SMS body."""
        match = re.search(r"\b(\d{4,8})\b", sms_text)
        return match.group(1) if match else None

    def poll_otp(
        self,
        order_id: int,
        *,
        interval: int = 5,
        timeout: int = 300,
        logger: Optional[logging.Logger] = None,
    ) -> str:
        """
        Poll 5sim every ``interval`` seconds until OTP arrives or timeout.

        Returns:
            Extracted numeric OTP string.
        """
        elapsed = 0
        while elapsed < timeout:
            data = self.check_order(order_id)
            status = str(data.get("status", "")).upper()

            if status in {"TIMEOUT", "CANCELED", "BANNED"}:
                raise FiveSimError(f"5sim order {order_id} ended with status={status}")

            sms_list = data.get("sms") or []
            if sms_list:
                for item in sms_list:
                    text = str(item.get("text") or item.get("code") or "")
                    otp = self.extract_otp(text)
                    if otp:
                        if logger:
                            logger.info("5sim OTP received for order %s", order_id)
                        return otp

            if logger:
                logger.info(
                    "5sim polling | order=%s elapsed=%ss status=%s",
                    order_id,
                    elapsed,
                    status or "waiting",
                )

            import time

            time.sleep(interval)
            elapsed += interval

        raise FiveSimError(
            f"Timed out waiting for OTP on 5sim order {order_id} after {timeout}s"
        )


# ---------------------------------------------------------------------------
# AccountManager
# ---------------------------------------------------------------------------


class AccountManager:
    """
    Automates high-trust Gmail account creation inside anti-detect profiles.

    Integrates ``BrowserManager`` (profile launch), ``IdentityManager`` (geo
    context), ``nodriver`` (stealth browser control), and ``FiveSimClient``
    (SMS OTP).

    Example::

        manager = AccountManager()
        result = await manager.create_gmail_account(
            profile_id="your-profile-id",
            country_code="US",
        )
        print(result.email, result.cookies_path)
    """

    def __init__(
        self,
        env_path: Optional[Path | str] = None,
        log_path: Optional[Path | str] = None,
    ) -> None:
        load_dotenv(env_path or DEFAULT_ENV_PATH)

        self._browser_manager = BrowserManager(
            env_path=str(env_path or DEFAULT_ENV_PATH)
        )
        self._identity_manager = IdentityManager(env_path=env_path)
        self._fivesim: Optional[FiveSimClient] = None
        self._fivesim_api_key = os.getenv("FIVESIM_API_KEY", "")
        self._logger = self._configure_logger(log_path or DEFAULT_LOG_PATH)

    @property
    def fivesim(self) -> FiveSimClient:
        """Lazy-loaded 5sim client."""
        if self._fivesim is None:
            self._fivesim = FiveSimClient(self._fivesim_api_key)
        return self._fivesim

    @staticmethod
    def _configure_logger(log_path: Path | str) -> logging.Logger:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        logger = logging.getLogger("mmb.account_manager")
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

    async def _human_delay(self, minimum: float = 2.0, maximum: float = 5.0) -> None:
        """Random pause to mimic human hesitation."""
        delay = random.uniform(minimum, maximum)
        self._logger.debug("Human delay %.2fs", delay)
        await asyncio.sleep(delay)

    async def _human_type(self, element: Element, text: str) -> None:
        """Type text character-by-character with random inter-key delays."""
        await element.focus()
        for char in text:
            await element._tab.send(cdp.input_.dispatch_key_event("char", text=char))
            await asyncio.sleep(random.uniform(0.05, 0.2))

    async def _is_element_clickable(self, element: Element) -> bool:
        """Return True when the element is visible, enabled, and hit-testable."""
        try:
            result = await element.apply(
                """
                function(el) {
                    if (!el) return false;
                    if (el.disabled) return false;
                    if (el.getAttribute('aria-disabled') === 'true') return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    if (parseFloat(style.opacity || '1') < 0.1) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return false;
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const top = document.elementFromPoint(cx, cy);
                    if (!top) return false;
                    return el === top || el.contains(top) || top.contains(el);
                }
                """,
                return_by_value=True,
            )
            return bool(result)
        except Exception:
            return False

    async def _wait_for_clickable(
        self,
        element: Element,
        timeout: float = 10.0,
    ) -> bool:
        """Poll until the element is present in the DOM and actually clickable."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                await element.update()
            except Exception:
                pass
            if await self._is_element_clickable(element):
                return True
            await asyncio.sleep(0.25)
        return False

    async def _human_click_element(self, element: Element) -> None:
        """Move the mouse to the element and click with human-like timing."""
        if not await self._wait_for_clickable(element, timeout=12.0):
            raise GmailSignupError("Element found but never became clickable.")

        await self._human_delay(0.3, 0.9)

        try:
            position = await element.get_position()
            if position and position.center:
                x, y = position.center
                x += random.uniform(-3.0, 3.0)
                y += random.uniform(-3.0, 3.0)
                await element.tab.mouse_move(
                    x, y, steps=random.randint(8, 18)
                )
                await asyncio.sleep(random.uniform(0.06, 0.18))
                await element.tab.mouse_click(x, y)
            else:
                await element.mouse_move()
                await asyncio.sleep(random.uniform(0.06, 0.18))
                await element.mouse_click()
        except Exception:
            await element.click()

        await self._human_delay(0.6, 1.4)

    async def _click_element(self, element: Element) -> None:
        await self._human_click_element(element)

    async def _click_next(self, tab: Tab) -> None:
        """Click a Google Next/Continue button with fallbacks and human-like input."""
        self._logger.info("Attempting to click Next/Continue button")
        await asyncio.sleep(random.uniform(0.4, 1.0))

        tried: list[str] = []
        last_error: Exception | None = None

        css_and_xpath = [
            ("css", selector) for selector in NEXT_BUTTON_CSS_SELECTORS
        ] + [("xpath", xpath) for xpath in NEXT_BUTTON_XPATH_SELECTORS]
        text_strategies = [("text", label) for label in NEXT_BUTTON_TEXT_LABELS]
        strategies = css_and_xpath + text_strategies

        for kind, value in strategies:
            strategy_id = f"{kind}:{value}"
            tried.append(strategy_id)
            try:
                element: Element | None = None
                if kind == "css":
                    element = await tab.select(value, timeout=2)
                elif kind == "xpath":
                    items = await tab.xpath(value, timeout=2)
                    element = next((item for item in (items or []) if item), None)
                elif kind == "text":
                    element = await tab.find(value, best_match=True, timeout=3)

                if not element:
                    continue

                if not await self._wait_for_clickable(element, timeout=8.0):
                    self._logger.debug(
                        "Found %s but element is not yet clickable", strategy_id
                    )
                    continue

                await self._human_click_element(element)
                self._logger.info("Clicked Next via %s", strategy_id)
                return
            except Exception as exc:
                last_error = exc
                self._logger.debug("Next strategy %s failed: %s", strategy_id, exc)
                continue

        await self._capture_next_button_debug(tab)
        detail = f" Last error: {last_error}" if last_error else ""
        raise GmailSignupError(
            "Could not locate or click Next/Continue button. "
            f"Tried {len(tried)} strategies.{detail}"
        )

    async def _is_on_birthday_step(self, tab: Tab) -> bool:
        """Return True when the signup flow has advanced to birthday/gender."""
        css_hints = (
            'select[name="month"]',
            "#month",
            'input[name="day"]',
            "#day",
            'input[name="year"]',
            "#year",
            "#gender",
            'select[id="gender"]',
        )
        for selector in css_hints:
            try:
                if await tab.query_selector(selector):
                    return True
            except Exception:
                continue

        for label in ("Month", "Gender", "Basic information", "birthday"):
            try:
                if await tab.find(label, best_match=True, timeout=2):
                    return True
            except Exception:
                continue
        return False

    async def _click_next_after_name(self, tab: Tab) -> None:
        """Click Next on the name step and verify we reached birthday/gender."""
        await self._click_next(tab)
        await self._human_delay(2.0, 4.0)

        if await self._is_on_birthday_step(tab):
            self._logger.info("Advanced to birthday/gender step")
            return

        self._logger.warning(
            "Still on name step after Next click; retrying with fallbacks"
        )
        await self._click_next(tab)
        await self._human_delay(2.0, 4.0)

        if await self._is_on_birthday_step(tab):
            self._logger.info("Advanced to birthday/gender step on retry")
            return

        await self._capture_next_button_debug(tab)
        raise GmailSignupError(
            "Failed to advance past the name step after clicking Next."
        )

    async def _fill_input_by_label(
        self,
        tab: Tab,
        labels: tuple[str, ...],
        value: str,
    ) -> None:
        """Fill an input field discovered by nearby label text or name attribute."""
        for label in labels:
            try:
                element = await tab.find(label, best_match=True, timeout=6)
                if element:
                    await self._human_type(element, value)
                    return
            except Exception:
                pass

        name_map = {
            "first name": "firstName",
            "last name": "lastName",
            "username": "Username",
            "password": "Passwd",
            "confirm": "ConfirmPasswd",
        }
        for label in labels:
            key = label.lower()
            for fragment, attr in name_map.items():
                if fragment in key:
                    try:
                        element = await tab.select(f'input[name="{attr}"]', timeout=4)
                        if element:
                            await self._human_type(element, value)
                            return
                    except Exception:
                        pass

        raise GmailSignupError(f"Could not fill field for labels={labels}")

    async def _capture_debug_artifacts(
        self,
        tab: Tab,
        screenshot_path: Path,
        log_label: str,
    ) -> None:
        """Save screenshot and HTML for debugging a failed form step."""
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await tab.save_screenshot(str(screenshot_path))
            self._logger.error("Saved debug screenshot to %s (%s)", screenshot_path, log_label)
        except Exception as exc:
            self._logger.error("Failed to save debug screenshot (%s): %s", log_label, exc)
        try:
            html = await tab.get_content()
            ERROR_PAGE_HTML.write_text(html, encoding="utf-8")
            self._logger.error("Saved debug HTML to %s (%s)", ERROR_PAGE_HTML, log_label)
        except Exception as exc:
            self._logger.error("Failed to save debug HTML (%s): %s", log_label, exc)

    async def _capture_next_button_debug(self, tab: Tab) -> None:
        await self._capture_debug_artifacts(tab, ERROR_NEXT_SCREENSHOT, "next_button")

    async def _is_text_visible_on_page(self, tab: Tab, text: str) -> bool:
        """Return True when visible page text matches a validation/error snippet."""
        try:
            element = await tab.find(text, best_match=False, timeout=2)
            if not element:
                return False
            visible = await element.apply(
                """
                function(el) {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    if (parseFloat(style.opacity || '1') < 0.1) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }
                """,
                return_by_value=True,
            )
            return bool(visible)
        except Exception:
            return False

    async def _has_birthday_validation_errors(self, tab: Tab) -> bool:
        for message in BIRTHDAY_VALIDATION_ERRORS:
            if await self._is_text_visible_on_page(tab, message):
                return True
        return False

    async def _wait_birthday_errors_cleared(
        self,
        tab: Tab,
        timeout: float = 12.0,
    ) -> bool:
        """Wait until birthday/gender validation banners disappear."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if not await self._has_birthday_validation_errors(tab):
                return True
            await asyncio.sleep(0.35)
        return False

    async def _find_element_by_strategies(
        self,
        tab: Tab,
        css_selectors: tuple[str, ...],
        xpath_selectors: tuple[str, ...] = (),
        text_labels: tuple[str, ...] = (),
        timeout: float = 2.0,
    ) -> Element | None:
        for selector in css_selectors:
            try:
                element = await tab.select(selector, timeout=timeout)
                if element:
                    return element
            except Exception:
                continue

        for xpath in xpath_selectors:
            for element in await self._safe_xpath(tab, xpath, timeout=timeout):
                return element

        for label in text_labels:
            try:
                element = await tab.find(label, best_match=True, timeout=timeout)
                if element:
                    return element
            except Exception:
                continue

        return None

    async def _safe_xpath(
        self,
        tab: Tab,
        xpath: str,
        timeout: float = 2.0,
    ) -> list[Element]:
        try:
            items = await tab.xpath(xpath, timeout=timeout)
            return [item for item in (items or []) if item]
        except Exception as exc:
            self._logger.debug("XPath lookup skipped (%s): %s", xpath[:60], exc)
            return []

    async def _resolve_dropdown_trigger(
        self,
        tab: Tab,
        placeholder_text: str,
        css_selectors: tuple[str, ...],
    ) -> Element | None:
        """Locate a Google Material dropdown trigger for Month or Gender."""
        element = await self._find_element_by_strategies(tab, css_selectors)
        if element:
            return element

        id_map = {"Month": "#month", "Gender": "#gender"}
        id_selector = id_map.get(placeholder_text)
        if id_selector:
            try:
                element = await tab.select(id_selector, timeout=4)
                if element:
                    return element
            except Exception:
                pass

        try:
            label_el = await tab.find(placeholder_text, best_match=True, timeout=4)
        except Exception:
            return None

        if not label_el:
            return None

        combobox = await label_el.apply(
            """
            function(el) {
                let node = el;
                for (let i = 0; i < 12 && node; i++) {
                    const role = node.getAttribute('role') || '';
                    const haspopup = node.getAttribute('aria-haspopup') || '';
                    if (role === 'combobox' || role === 'listbox' || haspopup === 'listbox') {
                        return true;
                    }
                    node = node.parentElement;
                }
                return false;
            }
            """,
            return_by_value=True,
        )
        if combobox:
            return label_el

        return label_el

    async def _wait_for_listbox_options(self, tab: Tab, timeout: float = 8.0) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for selector in ('li[role="option"]', 'div[role="option"]', 'ul[role="listbox"]'):
                try:
                    options = await tab.select_all(selector, timeout=1)
                    if options:
                        return True
                except Exception:
                    continue
            await asyncio.sleep(0.25)
        return False

    async def _click_dropdown_option(self, tab: Tab, option_text: str) -> bool:
        """Click a visible listbox option by its label text."""
        for selector in ('li[role="option"]', 'div[role="option"]'):
            try:
                options = await tab.select_all(selector, timeout=2)
                for option in options or []:
                    label = await option.apply(
                        "(e) => (e.innerText || e.textContent || '').trim()",
                        return_by_value=True,
                    )
                    if label and (
                        label == option_text
                        or option_text.lower() in label.lower()
                    ):
                        if await self._wait_for_clickable(option, timeout=3.0):
                            await self._human_click_element(option)
                            return True
            except Exception:
                continue

        xpath_options = (
            f'//li[@role="option"]//span[normalize-space(text())="{option_text}"]',
            f'//li[@role="option"][contains(normalize-space(.), "{option_text}")]',
            f'//div[@role="option"][contains(normalize-space(.), "{option_text}")]',
        )
        for xpath in xpath_options:
            for item in await self._safe_xpath(tab, xpath, timeout=2):
                if await self._wait_for_clickable(item, timeout=3.0):
                    await self._human_click_element(item)
                    return True

        try:
            option = await tab.find(option_text, best_match=True, timeout=3)
            if option and await self._wait_for_clickable(option, timeout=3.0):
                await self._human_click_element(option)
                return True
        except Exception:
            pass

        return False

    async def _select_google_dropdown(
        self,
        tab: Tab,
        placeholder_text: str,
        option_text: str,
        css_selectors: tuple[str, ...],
    ) -> None:
        """Open a Material dropdown, wait for options, and click the target value."""
        trigger = await self._resolve_dropdown_trigger(tab, placeholder_text, css_selectors)
        if not trigger:
            raise GmailSignupError(
                f"Could not find {placeholder_text} dropdown trigger."
            )

        await self._human_click_element(trigger)
        await asyncio.sleep(random.uniform(0.5, 1.0))

        if not await self._wait_for_listbox_options(tab, timeout=8.0):
            await self._human_click_element(trigger)
            await asyncio.sleep(random.uniform(0.4, 0.8))
            if not await self._wait_for_listbox_options(tab, timeout=6.0):
                raise GmailSignupError(
                    f"{placeholder_text} dropdown options did not appear."
                )

        if not await self._click_dropdown_option(tab, option_text):
            raise GmailSignupError(
                f"Could not select {placeholder_text} option '{option_text}'."
            )

        await asyncio.sleep(random.uniform(0.4, 0.9))
        self._logger.info("Selected %s -> %s", placeholder_text, option_text)

    async def _fill_numeric_input(
        self,
        tab: Tab,
        css_selectors: tuple[str, ...],
        value: str,
        field_name: str,
    ) -> None:
        element = await self._find_element_by_strategies(tab, css_selectors)
        if not element:
            raise GmailSignupError(f"Could not find {field_name} input field.")

        await self._human_click_element(element)
        try:
            await element.clear_input()
        except Exception:
            pass
        await self._human_type(element, value)
        self._logger.info("Filled %s with %s", field_name, value)

    async def _fill_birthday_and_gender(self, tab: Tab, gender: str) -> None:
        """
        Fill Google's Material birthday/gender form via real dropdown clicks.

        Validates that error banners are gone before clicking Next.
        """
        month_num = random.randint(1, 12)
        day = random.randint(1, 28)
        year = random.randint(1975, 2002)
        month_name = MONTH_NAMES[month_num - 1]
        persona_gender = gender if gender in ("Male", "Female") else random.choice(
            ("Male", "Female")
        )

        self._logger.info(
            "Filling birthday/gender | %s %s/%s gender=%s",
            month_name,
            day,
            year,
            persona_gender,
        )

        await self._select_google_dropdown(
            tab, "Month", month_name, MONTH_DROPDOWN_CSS
        )
        await self._human_delay(0.4, 1.0)

        await self._fill_numeric_input(tab, DAY_INPUT_CSS, str(day), "Day")
        await self._human_delay(0.3, 0.8)
        await self._fill_numeric_input(tab, YEAR_INPUT_CSS, str(year), "Year")
        await self._human_delay(0.5, 1.2)

        await self._select_google_dropdown(
            tab, "Gender", persona_gender, GENDER_DROPDOWN_CSS
        )
        await self._human_delay(0.8, 1.5)

        if not await self._wait_birthday_errors_cleared(tab, timeout=10.0):
            self._logger.warning(
                "Birthday validation errors still visible; retrying dropdowns"
            )
            await self._select_google_dropdown(
                tab, "Month", month_name, MONTH_DROPDOWN_CSS
            )
            await self._fill_numeric_input(tab, DAY_INPUT_CSS, str(day), "Day")
            await self._fill_numeric_input(tab, YEAR_INPUT_CSS, str(year), "Year")
            await self._select_google_dropdown(
                tab, "Gender", persona_gender, GENDER_DROPDOWN_CSS
            )
            await self._human_delay(0.8, 1.5)

        if not await self._wait_birthday_errors_cleared(tab, timeout=8.0):
            await self._capture_debug_artifacts(
                tab, ERROR_BIRTHDAY_SCREENSHOT, "birthday_gender"
            )
            raise GmailSignupError(
                "Birthday/gender validation errors remain after filling the form."
            )

        self._logger.info("Birthday/gender validation passed")
        await self._click_next_after_birthday(tab)

    async def _is_on_username_step(self, tab: Tab) -> bool:
        hints = (
            'input[name="Username"]',
            'input[aria-label*="Gmail"]',
            'input[aria-label*="username"]',
        )
        for selector in hints:
            try:
                if await tab.query_selector(selector):
                    return True
            except Exception:
                continue
        for label in ("Create a Gmail address", "Username", "Gmail address"):
            try:
                if await tab.find(label, best_match=True, timeout=2):
                    return True
            except Exception:
                continue
        return not await self._is_on_birthday_step(tab)

    async def _click_next_after_birthday(self, tab: Tab) -> None:
        """Click Next on birthday/gender only after validation errors are cleared."""
        if await self._has_birthday_validation_errors(tab):
            raise GmailSignupError(
                "Cannot click Next on birthday step while validation errors are visible."
            )

        await self._click_next(tab)
        await self._human_delay(2.0, 4.0)

        if await self._is_on_username_step(tab):
            self._logger.info("Advanced to username/Gmail address step")
            return

        if await self._has_birthday_validation_errors(tab):
            await self._capture_debug_artifacts(
                tab, ERROR_BIRTHDAY_SCREENSHOT, "birthday_gender_after_next"
            )
            raise GmailSignupError(
                "Birthday/gender step rejected after clicking Next."
            )

        self._logger.warning("Username step not detected; retrying Next once")
        await self._click_next(tab)
        await self._human_delay(2.0, 4.0)

        if not await self._is_on_username_step(tab):
            await self._capture_debug_artifacts(
                tab, ERROR_BIRTHDAY_SCREENSHOT, "birthday_gender_stuck"
            )
            raise GmailSignupError(
                "Failed to advance past birthday/gender after clicking Next."
            )

        self._logger.info("Advanced to username/Gmail address step on retry")

    async def _fill_input_by_selectors(
        self,
        tab: Tab,
        value: str,
        css_selectors: tuple[str, ...],
        xpath_selectors: tuple[str, ...] = (),
        field_name: str = "field",
    ) -> None:
        element = await self._find_element_by_strategies(
            tab, css_selectors, xpath_selectors
        )
        if not element:
            raise GmailSignupError(f"Could not find {field_name} input.")

        await self._human_click_element(element)
        try:
            await element.clear_input()
        except Exception:
            pass
        await self._human_type(element, value)

    async def _is_on_password_step(self, tab: Tab) -> bool:
        for selector in PASSWORD_INPUT_CSS:
            try:
                if await tab.query_selector(selector):
                    return True
            except Exception:
                continue
        for xpath in PASSWORD_INPUT_XPATH:
            try:
                items = await tab.xpath(xpath, timeout=1)
                if items and items[0]:
                    return True
            except Exception:
                continue
        try:
            if await tab.find("Password", best_match=True, timeout=2):
                return True
        except Exception:
            pass
        return False

    async def _fill_password_fields(self, tab: Tab, password: str) -> None:
        """Fill password and confirm fields using multiple selector fallbacks."""
        if not await self._is_on_password_step(tab):
            await self._human_delay(1.0, 2.0)

        self._logger.info("Filling password fields")
        try:
            await self._fill_input_by_selectors(
                tab,
                password,
                PASSWORD_INPUT_CSS,
                PASSWORD_INPUT_XPATH,
                field_name="Password",
            )
        except GmailSignupError:
            await self._fill_input_by_label(tab, ("Password", "password"), password)

        await self._human_delay(0.5, 1.2)

        try:
            await self._fill_input_by_selectors(
                tab,
                password,
                CONFIRM_PASSWORD_CSS,
                CONFIRM_PASSWORD_XPATH,
                field_name="Confirm password",
            )
        except GmailSignupError:
            await self._fill_input_by_label(
                tab,
                ("Confirm", "confirm password"),
                password,
            )

        await self._human_delay(0.8, 1.5)

        if not await self._is_on_password_step(tab):
            return

        confirm_visible = await self._find_element_by_strategies(
            tab, CONFIRM_PASSWORD_CSS, CONFIRM_PASSWORD_XPATH
        )
        password_visible = await self._find_element_by_strategies(
            tab, PASSWORD_INPUT_CSS, PASSWORD_INPUT_XPATH
        )
        if password_visible and not confirm_visible:
            self._logger.debug("Confirm field not found; single password field page")

    async def _click_next_after_password(self, tab: Tab) -> None:
        await self._click_next(tab)
        await self._human_delay(2.0, 4.0)

        phone_hints = ('input[type="tel"]', 'input[name="phoneNumberId"]')
        for selector in phone_hints:
            try:
                if await tab.query_selector(selector):
                    self._logger.info("Advanced to phone verification step")
                    return
            except Exception:
                continue

        if await self._is_on_password_step(tab):
            await self._capture_debug_artifacts(
                tab, ERROR_PASSWORD_SCREENSHOT, "password_stuck"
            )
            raise GmailSignupError(
                "Still on password step after clicking Next."
            )

    async def warmup_browser(self, tab: Tab) -> None:
        """
        Visit 2-3 high-authority sites with human-like browsing behavior.

        Performs random scrolling, pauses, and optional link clicks before
        navigating to Gmail signup.
        """
        sites = random.sample(WARMUP_SITES, k=random.randint(2, 3))
        self._logger.info("Warm-up started | sites=%s", sites)

        for url in sites:
            self._logger.info("Warm-up visiting %s", url)
            await tab.get(url)
            await self._human_delay(2, 5)

            scrolls = random.randint(2, 4)
            for _ in range(scrolls):
                if random.choice((True, False)):
                    await tab.scroll_down(random.randint(8, 20))
                else:
                    await tab.scroll_up(random.randint(5, 15))
                await self._human_delay(1, 3)

            if random.random() < 0.7:
                try:
                    links = await tab.select_all('a[href^="http"]')
                    candidates = links[: min(15, len(links))]
                    if candidates:
                        link = random.choice(candidates)
                        self._logger.info("Warm-up clicking random link on %s", url)
                        await self._click_element(link)
                        await self._human_delay(2, 4)
                except Exception as exc:
                    self._logger.debug("Warm-up link click skipped: %s", exc)

        self._logger.info("Warm-up complete")

    async def handle_sms_verification(
        self,
        tab: Tab,
        country_code: str,
    ) -> tuple[str, str]:
        """
        Buy a 5sim number, enter it into Gmail, poll OTP, and submit it.

        Returns:
            Tuple of ``(phone_number, otp_code)``.
        """
        self._logger.info(
            "SMS verification started | country=%s product=google", country_code
        )

        order = self.fivesim.buy_number(country_code, product="google")
        self._logger.info(
            "5sim number acquired | order=%s phone=%s", order.order_id, order.phone
        )

        phone_digits = re.sub(r"\D", "", order.phone)
        if phone_digits.startswith("1") and len(phone_digits) > 10:
            phone_digits = phone_digits[1:]

        try:
            phone_input = await tab.select('input[type="tel"]', timeout=15)
        except Exception as exc:
            self.fivesim.cancel_order(order.order_id)
            raise GmailSignupError("Phone number input not found on Gmail page.") from exc

        await self._human_type(phone_input, phone_digits)
        await self._human_delay(1, 2)
        await self._click_next(tab)
        await self._human_delay(2, 4)

        otp = await asyncio.to_thread(
            self.fivesim.poll_otp,
            order.order_id,
            interval=5,
            timeout=int(os.getenv("FIVESIM_OTP_TIMEOUT", "300")),
            logger=self._logger,
        )

        try:
            code_input = await tab.select('input[type="tel"]', timeout=15)
            await self._human_type(code_input, otp)
            await self._human_delay(1, 2)
            await self._click_next(tab)
        except Exception as exc:
            self.fivesim.cancel_order(order.order_id)
            raise GmailSignupError("OTP input field not found on Gmail page.") from exc

        self.fivesim.finish_order(order.order_id)
        self._logger.info("SMS verification complete | phone=%s", order.phone)
        return order.phone, otp

    async def save_session_cookies(
        self,
        browser: Browser,
        profile_id: str,
        account: dict[str, Any],
    ) -> tuple[Path, Path]:
        """
        Persist session cookies and account metadata under ``data/profiles/``.
        """
        profile_dir = PROFILE_DATA_DIR / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)

        cookies_path = profile_dir / "cookies.json"
        metadata_path = profile_dir / "account.json"

        cookies = await browser.cookies.get_all(requests_cookie_format=True)
        cookie_data = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": bool(c.secure),
                "expires": c.expires,
                "rest": getattr(c, "_rest", {}) or {},
            }
            for c in cookies
        ]

        with cookies_path.open("w", encoding="utf-8") as handle:
            json.dump(cookie_data, handle, indent=2)

        metadata = {
            **account,
            "profile_id": profile_id,
            "saved_at": datetime.utcnow().isoformat() + "Z",
            "cookies_file": str(cookies_path),
        }
        with metadata_path.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2)

        self._logger.info(
            "Session saved | profile=%s cookies=%s", profile_id, cookies_path
        )
        return cookies_path, metadata_path

    async def create_gmail_account(
        self,
        profile_id: str,
        country_code: str = "US",
    ) -> GmailAccountResult:
        """
        Full Gmail creation pipeline for an existing anti-detect profile.

        Args:
            profile_id: MoreLogin/Multilogin profile identifier.
            country_code: ISO country for SMS number purchase.

        Returns:
            ``GmailAccountResult`` with credentials and saved session paths.
        """
        self._logger.info(
            "Gmail creation started | profile=%s country=%s",
            profile_id,
            country_code,
        )

        identity = self._identity_manager.generate_identity(country_code=country_code)
        self._logger.info(
            "Identity context | tz=%s lang=%s", identity["timezone"], identity["language"]
        )

        browser: Optional[Browser] = None
        try:
            browser = await self._browser_manager.get_browser_instance(profile_id)
            tab = browser.main_tab

            await self.warmup_browser(tab)
            await self._human_delay(2, 4)

            self._logger.info("Navigating to Gmail signup")
            await tab.get(GMAIL_SIGNUP_URL)
            await self._human_delay(3, 6)

            name = NameGenerator.generate()
            password = NameGenerator.generate_password()
            username = NameGenerator.suggest_username(name.first, name.last)
            persona_gender = NameGenerator.infer_gender(name.first)

            self._logger.info("Entering name | %s", name.full)
            await self._fill_input_by_label(tab, ("First name", "first name"), name.first)
            await self._human_delay(0.5, 1.5)
            await self._fill_input_by_label(tab, ("Last name", "last name"), name.last)
            await self._human_delay(1, 2)
            await self._click_next_after_name(tab)

            await self._fill_birthday_and_gender(tab, persona_gender)

            self._logger.info("Creating Gmail address | username=%s", username)
            try:
                await self._fill_input_by_label(
                    tab,
                    ("Username", "Gmail address", "Create a Gmail address"),
                    username,
                )
            except GmailSignupError:
                create_option = await tab.find("Create your own Gmail address", timeout=5)
                if create_option:
                    await self._click_element(create_option)
                    await self._human_delay(1, 2)
                    await self._fill_input_by_label(tab, ("Username",), username)

            await self._human_delay(1, 2)
            await self._click_next(tab)
            await self._human_delay(2, 4)

            if not await self._is_on_password_step(tab):
                await self._human_delay(1.0, 2.0)

            self._logger.info("Setting account password")
            await self._fill_password_fields(tab, password)
            await self._click_next_after_password(tab)
            await self._human_delay(2, 5)

            phone, _otp = await self.handle_sms_verification(tab, country_code)
            await self._human_delay(3, 6)

            for skip_label in ("Skip", "Not now", "Next"):
                try:
                    skip = await tab.find(skip_label, best_match=True, timeout=4)
                    if skip:
                        await self._click_element(skip)
                        await self._human_delay(1, 2)
                except Exception:
                    pass

            for agree_label in ("I agree", "Agree"):
                try:
                    agree = await tab.find(agree_label, best_match=True, timeout=5)
                    if agree:
                        await self._click_element(agree)
                        break
                except Exception:
                    pass

            await self._human_delay(4, 8)
            email = f"{username}@gmail.com"

            account_meta = {
                "email": email,
                "password": password,
                "first_name": name.first,
                "last_name": name.last,
                "phone": phone,
                "country_code": country_code.upper(),
                "timezone": identity["timezone"],
                "language": identity["language"],
            }

            cookies_path, metadata_path = await self.save_session_cookies(
                browser,
                profile_id,
                account_meta,
            )

            result = GmailAccountResult(
                profile_id=profile_id,
                email=email,
                password=password,
                first_name=name.first,
                last_name=name.last,
                phone=phone,
                country_code=country_code.upper(),
                cookies_path=cookies_path,
                metadata_path=metadata_path,
            )

            self._logger.info("Gmail creation complete | email=%s", email)
            return result

        except (BrowserProviderError, FiveSimError, GmailSignupError) as exc:
            self._logger.error("Gmail creation failed: %s", exc)
            raise AccountManagerError(str(exc)) from exc
        except Exception as exc:
            self._logger.exception("Unexpected Gmail creation failure")
            raise AccountManagerError(f"Unexpected error: {exc}") from exc
        finally:
            if browser is not None:
                browser.stop()


if __name__ == "__main__":
    print("AccountManager module loaded.")
    print("Usage:")
    print("  manager = AccountManager()")
    print("  result = await manager.create_gmail_account(profile_id='...', country_code='US')")
