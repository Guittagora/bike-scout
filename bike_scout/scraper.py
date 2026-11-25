"""HTTP scraping and parsing of Buycycle listings.

Buycycle is a Next.js app: product pages ship their data as a React Server
Components (RSC) payload. Rather than parsing rendered HTML, the scraper
requests that payload (``RSC: 1`` header) and decodes the embedded product
JSON object directly, which is far more stable than CSS selectors.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import Listing, parse_price

log = logging.getLogger(__name__)

BASE_URL = "https://buycycle.com"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}
DESCRIPTION_MAX_CHARS = 250
SOLD_STATUSES = {"sold", "venduta", "reserved_sold"}
_RSC_URL_RE = re.compile(r'"(/[^"]*?_rsc=[^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class BlockedError(RuntimeError):
    """The site answered with an anti-bot (Cloudflare) challenge."""


def _is_challenge(text: str) -> bool:
    lowered = text[:5000].lower()
    return "just a moment" in lowered or "cf-chl" in lowered or "challenge-platform" in lowered


def _ensure_ok(response: requests.Response) -> None:
    """Raise BlockedError for anti-bot refusals, HTTPError for other failures."""
    if response.status_code == 403:
        raise BlockedError(f"HTTP 403 for {response.url} - the session cookie is probably expired")
    response.raise_for_status()


def extract_product_json(payload: str, anchor: str = '"product_id":') -> dict[str, Any] | None:
    """Return the JSON object that contains ``anchor`` inside an RSC payload.

    Walks backwards from the anchor trying each ``{`` as a candidate start and
    lets :class:`json.JSONDecoder` do the decoding, so braces inside strings
    and nested objects preceding the anchor are handled correctly.
    """
    anchor_idx = payload.find(anchor)
    if anchor_idx == -1:
        return None
    decoder = json.JSONDecoder()
    start = payload.rfind("{", 0, anchor_idx)
    while start != -1:
        try:
            obj, end = decoder.raw_decode(payload, start)
        except json.JSONDecodeError:
            obj, end = None, start
        if isinstance(obj, dict) and end > anchor_idx and "product_id" in obj:
            return obj
        start = payload.rfind("{", 0, start)
    return None


def _clean_description(raw: str) -> str:
    text = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", raw or ""))).strip()
    if len(text) > DESCRIPTION_MAX_CHARS:
        text = text[:DESCRIPTION_MAX_CHARS].rstrip() + "…"
    return text


def _to_iso_date(value: Any) -> str | None:
    """Accept epoch seconds/milliseconds or ISO strings; return YYYY-MM-DD."""
    if value in (None, ""):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return None
    if ts > 1e11:  # milliseconds
        ts /= 1000
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def parse_product(data: dict[str, Any], url: str) -> Listing:
    """Map Buycycle's product JSON onto a :class:`Listing`."""
    brand = str(data.get("brand_name") or "").strip()
    name = str(data.get("name") or "").strip()

    size = groupset = ""
    for attr in data.get("attributes") or []:
        if attr.get("key") == "frame_size_string":
            size = str(attr.get("value") or "")
        elif attr.get("key") == "component_name":
            groupset = str(attr.get("value") or "")

    description = (((data.get("information") or {}).get("data") or {}).get("description") or {}).get(
        "translated_value", ""
    )

    images = data.get("images") or []
    image_url = (images[0].get("url_350") or images[0].get("url")) if images else None

    extra = data.get("additional_data") or {}
    price = parse_price(data.get("price"))
    if price is None:
        price = parse_price(data.get("price_formatted"))

    published = None
    for key in ("published_at", "created_at", "updated_at", "dataUpdatedAt"):
        published = _to_iso_date(data.get(key))
        if published:
            break

    return Listing(
        url=url,
        title=f"{brand} {name}".strip() or "Unknown",
        brand=brand,
        price=price,
        price_label=str(data.get("price_formatted") or (f"€ {price:,.0f}" if price is not None else "")),
        market_price=parse_price(extra.get("common_price")),
        size=size,
        groupset=groupset,
        country=str(data.get("country") or ""),
        favorites=int(data.get("favorites_count") or 0),
        hot_deal=bool(extra.get("hot_deal")),
        sold=str(data.get("status") or "").lower() in SOLD_STATUSES,
        image_url=image_url,
        description=_clean_description(description),
        published=published,
    )


class BuycycleScraper:
    def __init__(
        self,
        cookie: str = "",
        session: requests.Session | None = None,
        timeout: float = 15,
        jitter: tuple[float, float] = (2.0, 5.0),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        if cookie:
            self.session.headers["Cookie"] = cookie
        self.timeout = timeout
        self.jitter = jitter
        self._sleep = sleep

    # -- search -----------------------------------------------------------
    def get_listing_urls(self, search_url: str) -> list[str]:
        """Return the product URLs linked from a search/catalogue page."""
        log.info("Scanning search page %s", search_url)
        response = self.session.get(search_url, timeout=self.timeout)
        _ensure_ok(response)
        text = response.content.decode("utf-8", errors="ignore")
        if _is_challenge(text):
            raise BlockedError("Search page returned a Cloudflare challenge; refresh the cookie")

        soup = BeautifulSoup(text, "html.parser")
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/product/" not in href or "_rsc" in href:
                continue
            full = urljoin(BASE_URL, href).split("?")[0].split("#")[0]
            if full not in seen:
                seen.add(full)
                urls.append(full)
        return urls

    # -- availability ------------------------------------------------------
    def check_available(self, url: str) -> bool | None:
        """Cheap availability probe based on HTTP status.

        Returns ``True`` (still online), ``False`` (gone: 404/410 or redirected
        away from the product page) or ``None`` when the result is inconclusive
        (network error, rate-limit, challenge...). Callers must not treat
        ``None`` as "sold".
        """
        try:
            response = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            if response.status_code == 405:
                response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("Availability check failed for %s: %s", url, exc)
            return None

        if response.status_code in (404, 410):
            return False
        if response.status_code != 200:
            return None
        final_path = urlparse(getattr(response, "url", "") or url).path
        return "/product/" in final_path

    # -- detail ------------------------------------------------------------
    def find_rsc_url(self, url: str) -> str:
        """Find the RSC hydration URL referenced by a product page (fallback: the page itself)."""
        response = self.session.get(url, timeout=self.timeout)
        _ensure_ok(response)
        match = _RSC_URL_RE.search(response.content.decode("utf-8", errors="ignore"))
        return urljoin(BASE_URL, match.group(1)) if match else url

    def fetch_listing(self, url: str) -> Listing | None:
        """Download and parse one listing. Returns ``None`` if no product data was found.

        Raises :class:`BlockedError` on anti-bot challenges and
        :class:`requests.RequestException` on HTTP/network errors.
        """
        rsc_url = self.find_rsc_url(url)
        self._sleep(random.uniform(*self.jitter))
        response = self.session.get(rsc_url, headers={"RSC": "1", "Accept": "*/*"}, timeout=self.timeout)
        _ensure_ok(response)
        payload = response.content.decode("utf-8", errors="ignore")
        if _is_challenge(payload):
            raise BlockedError("Product page returned a Cloudflare challenge; refresh the cookie")
        data = extract_product_json(payload)
        if data is None:
            log.warning("No product JSON found in %s", rsc_url)
            return None
        return parse_product(data, url)
