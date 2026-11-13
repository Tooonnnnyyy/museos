"""Read-only web tools for research tasks: search and fetch.

Both are bounded and guarded:
- ``web_search`` uses the key-less DuckDuckGo HTML endpoint (falling back to the
  Instant Answer API), returning title/url/snippet pairs.
- ``web_fetch`` fetches one URL with a byte cap and a timeout, rejects anything
  but http/https, and refuses hosts that resolve to private, loopback, link-local
  or reserved addresses (SSRF guard). The body is reduced to plain text with the
  stdlib HTML parser.

Neither tool writes anything and neither needs a key, so a tool call costs $0 and
only consumes a budget step; every call is still policy-checked and audited by the
engine before it happens.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import httpx

UA = "SwarmAgentOS/0.1 (local research tool)"


class ToolError(Exception):
    pass


# ------------------------------------------------------------------ fetch
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    # Block boundaries are already "\n" markers appended by the parser; inline runs join directly.
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def _host_allowed(host: str | None) -> bool:
    """Refuse hosts that resolve to non-public addresses (SSRF guard)."""
    if not host:
        return False
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(host, 443, proto=6)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            return False
    return True


async def web_fetch(url: str, max_bytes: int = 150_000, timeout: float = 10.0) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ToolError(f"blocked scheme: {parts.scheme}")
    if not await _host_allowed(parts.hostname):
        raise ToolError("blocked address: not a public host")
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True) as c:
            async with c.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ToolError(f"http {resp.status_code}")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= max_bytes:
                        break
    except httpx.HTTPError as e:
        raise ToolError(f"fetch failed: {type(e).__name__}") from e
    return html_to_text(body.decode("utf-8", "replace"))[:max_bytes]


# ------------------------------------------------------------------ search
def _clean_url(u: str) -> str:
    """Strip the DuckDuckGo redirector wrapper so fetches hit the real site."""
    u = u.strip()
    m = re.search(r"[?&]uddg=([^&]+)", u)
    if m:
        u = unquote(m.group(1))
    return u


def _parse_ddg_html(html: str) -> list[dict[str, str]]:
    import html as html_mod

    results: list[dict[str, str]] = []
    for m in re.finditer(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        title = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
        results.append({"title": title.strip(), "url": _clean_url(m.group(1)), "snippet": ""})
    for i, m in enumerate(re.finditer(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', html, re.S)):
        if i < len(results):
            results[i]["snippet"] = html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    return [r for r in results if r.get("title") and r.get("url") and r["url"].startswith(("http://", "https://"))]


def _parse_ddg_instant(data: dict) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if data.get("AbstractText") and data.get("AbstractURL"):
        out.append({"title": (data.get("Heading") or "Abstract").strip(), "url": data["AbstractURL"],
                    "snippet": data["AbstractText"][:500]})

    def walk(topic: dict) -> None:
        if "Topics" in topic:
            for t in topic["Topics"]:
                walk(t)
            return
        if topic.get("Text") and topic.get("FirstURL"):
            out.append({"title": topic["Text"][:200], "url": topic["FirstURL"], "snippet": topic.get("Text", "")[:500]})

    for t in data.get("RelatedTopics", []) or []:
        walk(t)
    return out


async def web_search(query: str, max_results: int = 5, timeout: float = 8.0) -> list[dict[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}, follow_redirects=True) as c:
            r = await c.get("https://html.duckduckgo.com/html/", params={"q": query})
            hits = _parse_ddg_html(r.text)
    except httpx.HTTPError:
        hits = []
    if not hits:  # fallback: the official, key-less Instant Answer API
        try:
            async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA}) as c:
                r = await c.get("https://api.duckduckgo.com/",
                                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
                hits = _parse_ddg_instant(r.json())
        except (httpx.HTTPError, ValueError):
            hits = []
    return hits[:max_results]
