"""Self-healing semantic element resolver (aria-label, text, CSS, XPath)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

from nodriver.core.element import Element
from nodriver.core.tab import Tab

from behavior.youtube.selectors import DESKTOP_SELECTORS, MOBILE_SELECTORS, SemanticSpec
from behavior.youtube.types import PlatformKind, SELECTOR_FAILURE_LOG


class SemanticResolver:
    """Resolve UI elements using semantic strategies that survive DOM refactors."""

    def __init__(
        self,
        platform: PlatformKind,
        logger: Any,
        on_failure: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._platform = platform
        self._logger = logger
        self._on_failure = on_failure
        self._map = (
            MOBILE_SELECTORS if platform == PlatformKind.MOBILE else DESKTOP_SELECTORS
        )

    @property
    def selector_map(self) -> dict[str, SemanticSpec]:
        return self._map

    async def find(
        self,
        tab: Tab,
        key: str,
        timeout: float = 4.0,
    ) -> Element | None:
        spec = self._map.get(key)
        if not spec:
            self._logger.error("Unknown selector key: %s", key)
            return None

        strategies = self._build_strategies(spec)
        per_try = max(0.8, timeout / max(1, len(strategies)))

        for kind, value in strategies:
            try:
                element = await self._try_strategy(tab, kind, value, per_try)
                if element:
                    return element
            except Exception as exc:
                self._record_failure(key, f"{kind}:{value}")
                self._logger.debug(
                    "Selector miss [%s/%s] %s:%s | %s",
                    self._platform.value,
                    key,
                    kind,
                    value[:80],
                    exc,
                )

        self._logger.warning(
            "All selectors exhausted | platform=%s key=%s",
            self._platform.value,
            key,
        )
        return None

    async def find_all_links(self, tab: Tab, key: str) -> list[Element]:
        spec = self._map.get(key) or self._map.get("video_link", {})
        seen: set[int] = set()
        elements: list[Element] = []

        from behavior.youtube.human_engine import _js_find_selector
        for selector in spec.get("css", ()):
            try:
                matched = await _js_find_selector(tab, (selector,))
                if matched:
                    found = await tab.select_all(selector)
                    for item in found or []:
                        if id(item) not in seen:
                            seen.add(id(item))
                            elements.append(item)
            except Exception:
                self._record_failure(key, f"css:{selector}")

        for xpath in spec.get("xpath", ()):
            try:
                found = await tab.xpath(xpath)
                for item in found or []:
                    if id(item) not in seen:
                        seen.add(id(item))
                        elements.append(item)
            except Exception:
                self._record_failure(key, f"xpath:{xpath}")

        return elements

    def _build_strategies(self, spec: SemanticSpec) -> list[tuple[str, str]]:
        strategies: list[tuple[str, str]] = []
        for label in spec.get("aria_labels", ()):
            strategies.append(("aria", label))
        for text in spec.get("text", ()):
            strategies.append(("text", text))
        for selector in spec.get("css", ()):
            strategies.append(("css", selector))
        for xpath in spec.get("xpath", ()):
            strategies.append(("xpath", xpath))
        return strategies

    async def _try_strategy(
        self,
        tab: Tab,
        kind: str,
        value: str,
        timeout: float,
    ) -> Element | None:
        from behavior.youtube.human_engine import _js_find_selector, _js_find_text, _js_find_xpath
        if kind == "css":
            # JS check first — never hangs; tab.select only when element confirmed present
            matched = await _js_find_selector(tab, (value,))
            if not matched:
                return None
            return await tab.select(value)
        if kind == "xpath":
            # JS check first — tab.xpath() hangs if no match
            exists = await _js_find_xpath(tab, value)
            if not exists:
                return None
            try:
                items = await tab.xpath(value)
                return next((item for item in (items or []) if item), None)
            except Exception:
                return None
        if kind == "text":
            # JS check first — tab.find() hangs if text not in DOM
            exists = await _js_find_text(tab, value)
            if not exists:
                return None
            try:
                return await tab.find(value, best_match=True)
            except Exception:
                return None
        if kind == "aria":
            css = f'[aria-label*="{value}"]'
            matched = await _js_find_selector(tab, (css,))
            if matched:
                try:
                    return await tab.select(css)
                except Exception:
                    pass
            return None
        return None

    def _record_failure(self, key: str, selector: str) -> None:
        if self._on_failure:
            self._on_failure(key, selector)


def load_selector_failures() -> dict[str, list[str]]:
    if not SELECTOR_FAILURE_LOG.exists():
        return {}
    try:
        with SELECTOR_FAILURE_LOG.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {k: list(v) if isinstance(v, list) else [] for k, v in data.items()}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def persist_selector_failure(
    store: dict[str, list[str]],
    key: str,
    selector: str,
) -> None:
    failures = store.setdefault(key, [])
    stamp = f"{datetime.utcnow().isoformat()}Z | {selector}"
    if stamp not in failures:
        failures.append(stamp)
        failures[:] = failures[-50:]
        SELECTOR_FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SELECTOR_FAILURE_LOG.open("w", encoding="utf-8") as handle:
            json.dump(store, handle, indent=2)
