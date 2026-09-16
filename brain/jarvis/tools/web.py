"""Web tools: search, page fetch, weather. All keyless and free.

DuckDuckGo's HTML endpoint and Open-Meteo need no account, which matters for a
box that is meant to work offline-ish and never phone an API key home.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

import httpx
import lxml.etree
import lxml.html

from jarvis.sysutil import ToolError
from jarvis.tools import ToolContext, tool

# A real desktop UA: DDG serves a stripped, result-less page to obvious bots.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DDG_HTML = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"

_BLOCKED = re.compile(r"anomaly|unusual traffic|bots use duckduckgo|too many requests", re.I)


def _clean(text: str, limit: int = 0) -> str:
    text = " ".join((text or "").split())
    if limit and len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        text = text[: cut if cut > limit // 2 else limit].rstrip(" ,.;:-") + "…"
    return text


def _domain(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _unwrap(href: str) -> str:
    """DDG sometimes hands back /l/?uddg=<real url> instead of the target."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        qs = parse_qs(urlsplit(href).query)
        target = qs.get("uddg", [""])[0]
        if target:
            return target
    return href


def _parse_html_results(body: str) -> list[dict[str, str]]:
    doc = lxml.html.fromstring(body)
    out: list[dict[str, str]] = []
    # Exact class token: plain contains() also matches the <div class="results">
    # wrapper, which would hand back the first result once per nesting level.
    blocks = doc.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' result ')"
        " and not(contains(@class,'result--ad'))]"
    )
    for node in blocks:
        link = node.xpath(".//a[contains(@class,'result__a')]")
        if not link:
            continue
        url = _unwrap(link[0].get("href", ""))
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:
            continue
        snip = node.xpath(".//a[contains(@class,'result__snippet')]")
        out.append(
            {
                "title": _clean(link[0].text_content(), 95),
                "url": url,
                "snippet": _clean(snip[0].text_content(), 150) if snip else "",
            }
        )
    return out


def _parse_lite_results(body: str) -> list[dict[str, str]]:
    doc = lxml.html.fromstring(body)
    links = doc.xpath("//a[contains(@class,'result-link')]")
    snips = doc.xpath("//td[contains(@class,'result-snippet')]")
    out: list[dict[str, str]] = []
    for i, link in enumerate(links):
        url = _unwrap(link.get("href", ""))
        if not url.startswith("http"):
            continue
        snippet = _clean(snips[i].text_content(), 150) if i < len(snips) else ""
        out.append({"title": _clean(link.text_content(), 95), "url": url, "snippet": snippet})
    return out


async def _ddg(client: httpx.AsyncClient, url: str, query: str) -> str | None:
    """Fetch one result page. None means 'soft-blocked, try again'."""
    try:
        resp = await client.post(url, data={"q": query, "kl": "us-en"})
    except httpx.HTTPError as exc:
        raise ToolError(f"could not reach DuckDuckGo: {type(exc).__name__}")
    # 202 is DDG's anomaly challenge, not a hard block - it clears on a retry.
    if resp.status_code in (202, 403, 429):
        return None
    if resp.status_code >= 400:
        raise ToolError(f"search failed with HTTP {resp.status_code}")
    if _BLOCKED.search(resp.text[:4000]):
        return None
    return resp.text


# Alternate the two renderers with a little backoff; the challenge is per-request.
_ATTEMPTS = ((DDG_HTML, 0.0), (DDG_LITE, 0.7), (DDG_HTML, 1.5), (DDG_LITE, 2.0))


async def _search(client: httpx.AsyncClient, query: str) -> list[dict[str, str]]:
    for url, delay in _ATTEMPTS:
        if delay:
            await asyncio.sleep(delay)
        body = await _ddg(client, url, query)
        if body is None:
            continue
        parse = _parse_html_results if url is DDG_HTML else _parse_lite_results
        results = await asyncio.to_thread(parse, body)
        if results:
            return results
    raise ToolError(
        "DuckDuckGo is throttling searches right now. Wait a minute and try again."
    )


@tool(
    name="web_search",
    description=(
        "Search the web with DuckDuckGo and return the top results (title, site, one-line "
        "summary). Use for current events, facts you are unsure about, prices, opening hours, "
        "documentation, or anything after your training cutoff. To read one of the results in "
        "full, follow up with fetch_url."
    ),
    params={
        "query": {
            "type": "string",
            "description": "What to search for, e.g. 'best mechanical keyboard 2026'",
        },
        "limit": {
            "type": "integer",
            "description": "How many results to return, 1 to 8. Default 5.",
        },
    },
    required=["query"],
    category="web",
)
async def web_search(ctx: ToolContext, query: str, limit: int = 5) -> str:
    query = (query or "").strip()
    if not query:
        raise ToolError("empty search query")
    limit = max(1, min(int(limit or 5), 8))

    async with httpx.AsyncClient(
        headers={**HEADERS, "Referer": "https://duckduckgo.com/"},
        timeout=15.0,
        follow_redirects=True,
    ) as client:
        results = await _search(client, query)

    if not results:
        raise ToolError(f"no results for {query!r}. Try different or fewer words.")

    lines = []
    for i, r in enumerate(results[:limit], 1):
        parts = [r["title"], _domain(r["url"]) or "unknown site"]
        if r["snippet"]:
            parts.append(r["snippet"])
        lines.append(f"{i}. " + " - ".join(parts))
    return "\n".join(lines)


def _extract(html: str, url: str) -> tuple[str, str]:
    """(title, text). trafilatura first; a tag strip is the floor, not the plan."""
    title = ""
    text = ""
    try:
        import trafilatura

        text = (
            trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
            or ""
        )
        meta = trafilatura.extract_metadata(html)
        if meta and meta.title:
            title = meta.title
    except Exception:  # noqa: BLE001 - extraction quality must never fail the fetch
        text = ""

    doc = None
    if not title or not text:
        try:
            doc = lxml.html.fromstring(html)
        except (ValueError, lxml.etree.ParserError):
            doc = None

    if not title and doc is not None:
        node = doc.xpath("//title")
        if node:
            title = node[0].text_content()

    if not text and doc is not None:
        for bad in doc.xpath("//script|//style|//nav|//header|//footer|//noscript|//form"):
            parent = bad.getparent()
            if parent is not None:
                parent.remove(bad)
        blocks = [_clean(p.text_content()) for p in doc.xpath("//p|//li|//h1|//h2|//h3")]
        text = "\n".join(b for b in blocks if len(b) > 40)

    return _clean(title, 120), text.strip()


def _truncate_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind(".\n"))
    if cut < limit // 2:
        cut = window.rfind(" ")
    return (window[: cut + 1] if cut > 0 else window).rstrip() + " […]"


@tool(
    name="fetch_url",
    description=(
        "Download one web page and return its readable text. Use when you have a specific URL - "
        "from web_search, from the user, or a documentation link - and need what it actually "
        "says. Only http and https pages; will not download files."
    ),
    params={
        "url": {"type": "string", "description": "Full URL, e.g. https://example.com/article"},
        "max_chars": {
            "type": "integer",
            "description": "How much text to return. Default 3000.",
        },
    },
    required=["url"],
    ack="Reading the page.",
    category="web",
)
async def fetch_url(ctx: ToolContext, url: str, max_chars: int = 3000) -> str:
    url = (url or "").strip().strip("<>\"'")
    if not url:
        raise ToolError("no URL given")
    if "://" not in url:
        url = "https://" + url
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolError(f"refusing to fetch a {scheme or 'schemeless'} URL; only http and https")
    limit = max(200, min(int(max_chars or 3000), 6000))

    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.TimeoutException:
        raise ToolError(f"{_domain(url)} did not respond within 12 seconds")
    except httpx.HTTPError as exc:
        raise ToolError(f"could not fetch {_domain(url)}: {type(exc).__name__}")

    if resp.status_code >= 400:
        raise ToolError(f"{_domain(url)} returned HTTP {resp.status_code}")

    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and ctype not in ("text/html", "text/plain", "application/xhtml+xml"):
        raise ToolError(f"that URL is {ctype}, not a web page. I can only read HTML or plain text.")

    body = resp.text
    if not body.strip():
        raise ToolError(f"{_domain(url)} returned an empty page")

    if ctype == "text/plain":
        title, text = "", body
    else:
        title, text = await asyncio.to_thread(_extract, body, str(resp.url))

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ToolError(
            f"no readable text on {_domain(url)} - it is probably rendered by JavaScript."
        )

    head = title or _domain(str(resp.url))
    return f"{head}\n\n{_truncate_sentence(text, limit)}"


# ------------------------------------------------------------------ weather

WMO = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}

WET = set(range(51, 87)) | {95, 96, 99}


def _sky(code: Any) -> str:
    try:
        return WMO.get(int(code), "unsettled")
    except (TypeError, ValueError):
        return "unsettled"


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        raise ToolError(f"weather service unreachable: {type(exc).__name__}")
    except ValueError:
        raise ToolError("weather service returned something that was not JSON")


async def _geocode(client: httpx.AsyncClient, name: str) -> dict:
    data = await _get_json(
        client,
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": name, "count": 1, "language": "en", "format": "json"},
    )
    hits = data.get("results") or []
    if not hits:
        raise ToolError(f"I could not find a place called {name!r}.")
    hit = hits[0]
    label = hit.get("name") or name
    region = hit.get("admin1") or hit.get("country") or ""
    return {
        "city": f"{label}, {region}" if region and region != label else label,
        "lat": float(hit["latitude"]),
        "lon": float(hit["longitude"]),
        "timezone": hit.get("timezone") or "auto",
    }


async def _locate(ctx: ToolContext, client: httpx.AsyncClient) -> dict:
    """IP geolocation, cached into config so this happens exactly once."""
    data = await _get_json(
        client,
        "http://ip-api.com/json/",
        {"fields": "status,message,city,regionName,country,lat,lon,timezone"},
    )
    if data.get("status") != "success":
        raise ToolError(
            "I do not know where you are. Say the city name, or set location.city in the config."
        )
    city = data.get("city") or ""
    region = data.get("regionName") or data.get("country") or ""
    place = {
        "city": f"{city}, {region}".strip(", "),
        "lat": float(data["lat"]),
        "lon": float(data["lon"]),
        "timezone": data.get("timezone") or "auto",
    }
    ctx.cfg.set("location.city", place["city"])
    ctx.cfg.set("location.lat", place["lat"])
    ctx.cfg.set("location.lon", place["lon"])
    if not ctx.cfg.get("location.timezone"):
        ctx.cfg.set("location.timezone", place["timezone"])
    return place


async def _resolve_place(ctx: ToolContext, client: httpx.AsyncClient, location: str | None) -> dict:
    if location and location.strip():
        return await _geocode(client, location.strip())
    lat, lon = ctx.cfg.get("location.lat"), ctx.cfg.get("location.lon")
    if lat is not None and lon is not None:
        return {
            "city": ctx.cfg.get("location.city") or "here",
            "lat": float(lat),
            "lon": float(lon),
            "timezone": ctx.cfg.get("location.timezone") or "auto",
        }
    city = (ctx.cfg.get("location.city") or "").strip()
    if city:
        place = await _geocode(client, city)
        ctx.cfg.set("location.lat", place["lat"])
        ctx.cfg.set("location.lon", place["lon"])
        return place
    return await _locate(ctx, client)


def _clock(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    hour = dt.hour % 12 or 12
    return f"{hour} {'AM' if dt.hour < 12 else 'PM'}"


def _rain_outlook(data: dict, now_iso: str) -> str:
    """The one hourly detail worth saying out loud: when it starts raining."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    try:
        start = next(i for i, t in enumerate(times) if t >= now_iso)
    except StopIteration:
        return ""
    best_i, best_p = -1, 0
    for i in range(start, min(start + 12, len(probs))):
        p = probs[i] or 0
        if p > best_p:
            best_i, best_p = i, p
    if best_i < 0 or best_p < 45:
        return ""
    what = _sky(codes[best_i]) if best_i < len(codes) else "rain"
    if "snow" not in what and "rain" not in what and "drizzle" not in what and "thunder" not in what:
        what = "rain"
    when = _clock(times[best_i])
    return f"{what.capitalize()} likely around {when}" if when else ""


@tool(
    name="get_weather",
    description=(
        "Current conditions or the forecast for a place. Use for 'what's the weather', "
        "'will it rain', 'how cold is it tomorrow', 'do I need a jacket'. Leave location empty "
        "for where the user is."
    ),
    params={
        "location": {
            "type": "string",
            "description": "City name, e.g. 'Boston' or 'Paris, France'. Omit for the user's own location.",
        },
        "when": {
            "type": "string",
            "enum": ["now", "today", "tomorrow", "week"],
            "description": "Which forecast. Default 'now'.",
        },
    },
    required=[],
    category="web",
)
async def get_weather(ctx: ToolContext, location: str = "", when: str = "now") -> str:
    when = (when or "now").strip().lower()
    if when not in ("now", "today", "tomorrow", "week"):
        when = "now"
    imperial = (ctx.cfg.get("location.units") or "imperial").lower().startswith("i")
    speed_word = "miles an hour" if imperial else "kilometres an hour"

    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=15.0, follow_redirects=True) as client:
        place = await _resolve_place(ctx, client, location)
        data = await _get_json(
            client,
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": place["lat"],
                "longitude": place["lon"],
                "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "hourly": "precipitation_probability,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
                "temperature_unit": "fahrenheit" if imperial else "celsius",
                "wind_speed_unit": "mph" if imperial else "kmh",
                "precipitation_unit": "inch" if imperial else "mm",
                "timezone": place.get("timezone") or "auto",
                "forecast_days": 7,
            },
        )

    daily = data.get("daily") or {}
    days = daily.get("time") or []
    if not days:
        raise ToolError("the weather service returned no forecast for that place")
    city = place["city"]

    def day_line(i: int, lead: str) -> str:
        sky = _sky((daily.get("weather_code") or [None])[i])
        hi = round((daily["temperature_2m_max"])[i])
        lo = round((daily["temperature_2m_min"])[i])
        pop = (daily.get("precipitation_probability_max") or [None] * len(days))[i]
        out = f"{lead} {sky}, high {hi} and low {lo}."
        if pop and pop >= 30:
            out += f" {int(pop)} percent chance of precipitation."
        wind = (daily.get("wind_speed_10m_max") or [0] * len(days))[i] or 0
        if wind >= (20 if imperial else 32):
            out += f" Wind up to {round(wind)} {speed_word}."
        return out

    if when == "now":
        cur = data.get("current") or {}
        temp = round(cur.get("temperature_2m", 0))
        feels = round(cur.get("apparent_temperature", temp))
        sky = _sky(cur.get("weather_code"))
        line = f"It's {temp} degrees and {sky} in {city}"
        if abs(feels - temp) >= 4:
            line += f", feeling like {feels}"
        line += "."
        hi = round(daily["temperature_2m_max"][0])
        lo = round(daily["temperature_2m_min"][0])
        line += f" High of {hi}, low of {lo}."
        extra = _rain_outlook(data, cur.get("time", ""))
        if extra and int(cur.get("weather_code") or 0) not in WET:
            line += f" {extra}."
        elif not extra and int(cur.get("weather_code") or 0) not in WET:
            line += " No rain expected."
        wind = cur.get("wind_speed_10m") or 0
        if wind >= (20 if imperial else 32):
            line += f" It's windy, {round(wind)} {speed_word}."
        return line

    if when == "today":
        return f"{day_line(0, f'Today in {city}:')}"

    if when == "tomorrow":
        if len(days) < 2:
            raise ToolError("no forecast available for tomorrow")
        return f"{day_line(1, f'Tomorrow in {city}:')}"

    highs = [round(t) for t in daily["temperature_2m_max"]]
    lows = [round(t) for t in daily["temperature_2m_min"]]
    codes = daily.get("weather_code") or []
    wet_days = [
        datetime.fromisoformat(days[i]).strftime("%A")
        for i, c in enumerate(codes)
        if int(c or 0) in WET
    ]
    line = f"Over the next week in {city}, highs {min(highs)} to {max(highs)} and lows {min(lows)} to {max(lows)}."
    if wet_days:
        named = ", ".join(wet_days[:3])
        line += f" Wet on {named}." if len(wet_days) <= 3 else f" Wet most days, starting {wet_days[0]}."
    else:
        line += " Dry all week."
    return line
