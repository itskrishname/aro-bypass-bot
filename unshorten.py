"""Full bypass engine: turn a shortened link into its real destination.

Two lanes, one orchestrator:

* HTTP lane  - follows the redirect chain hop by hop with a single cookie jar,
               mining interstitial pages for the embedded target.
* Browser lane - drives headless Chromium: fast-forwards countdowns, clicks
                 "Get Link" / "Continue", records every off-site navigation and
                 popup, and returns the URL a human would land on.

`bypass()` runs the HTTP lane first and escalates to the browser lane when the
result is still a shortener page (that is what makes it a bypass, not a peek).
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
import re
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cf_requests
except Exception:  # optional
    cf_requests = None


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_HOPS = 15
TIMEOUT = 20
MAX_BULK = 25

# Dev switch: LB_ALLOW_LOCAL=1 lets the engine resolve loopback/LAN hosts so the
# bundled test walls (test_wall.py) can be exercised. Never enable in public use.
ALLOW_PRIVATE_HOSTS = os.environ.get("LB_ALLOW_LOCAL", "") == "1"
DEBUG = os.environ.get("LB_DEBUG", "") == "1"


def _debug(*args) -> None:
    if DEBUG:
        print("[lb]", *args, flush=True)

# --------------------------------------------------------------------------- #
# domain knowledge
# --------------------------------------------------------------------------- #

# Roots that are shorteners / ad walls: landing on one of these means "not done".
SHORTENER_ROOTS = {
    "arolinks.com", "link-arul.com", "linkvertise.com", "shrinkme.io",
    "adshrink.com", "shrinkearn.com", "shrinkme.click", "cutt.ly", "cuttlinks.com",
    "bit.ly", "rebrand.ly", "shorturl.at", "soo.gd", "clck.ru", "tinyurl.com",
    "is.gd", "v.gd", "t.co", "shorturl.com", "ow.ly", "buff.ly", "isgd.in",
    "ouo.io", "ouo.press", "ouo.press", "excloo.com", "excloo.com", "shrinkme.io",
    "shrinkearn.com", "adf.ly", "shorte.st", "adfoc.us", "j.gs", "q.gs", "bc.vc",
    "clk.sh", "mgnet.me", "fc.lc", "l.linklyhq.com", "linklyhq.com", "1link.in",
    "shrinkme.io", "clicky.me", "direct-link.net", "za.gl", "shrinkme.io",
    "1tk.us", "shrink.gy", "cursurl.com", "shrinkme.io", "gg.gg", "shorturl.gg",
    "a.gg", "v.ht", "shorturl.com.br", "encurtacash.com.br", "linkto.im",
    "urlshortner.com", "shorturl.net", "u.to", "1url.com", "1link.in",
    "wpshealth.com", "go.linkbaza.com", "shrinkearn.org", "shrinkme.io",
    "ezurl.click", "shrinke.me", "shrinkme.io", "clck.ru", "vk.cc",
    "t.me", "wa.me", "chat.whatsapp.com", "whatsapp.com",
    "discord.gg", "discord.com", "youtu.be", "amzn.to", "ebay.to", "apple.co",
    "lnkd.in", "reut.rs", "dlvr.it", "ift.tt", "shorturl.at",
}
# NOTE: telegram.me / discord invite links are real destinations, so they are
# deliberately NOT shortener roots - a campaign can legitimately end there.

# Never a destination, in any context: ad networks, trackers, CDNs, captchas.
AD_JUNK_ROOTS = {
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "googletagservices.com", "google-analytics.com", "googletagmanager.com",
    "adnxs.com", "rubiconproject.com", "pubmatic.com", "openx.net",
    "casalemedia.com", "taboola.com", "outbrain.com", "revcontent.com", "mgid.com",
    "adsterra.com", "hilltopads.net", "popads.net", "popcash.net", "propellerads.com",
    "exoclick.com", "exosrv.com", "juicyads.com", "trafficjunky.com", "poperblock.com",
    "clickadu.com", "onclickalgo.com", "pushengage.com", "adpush.ai", "a.pub.network",
    "cdnjs.cloudflare.com", "jsdelivr.net", "unpkg.com", "ajax.googleapis.com",
    "fonts.googleapis.com", "fonts.gstatic.com", "use.typekit.net", "fontawesome.com",
    "maxcdn.com", "w3.org", "schema.org", "gravatar.com", "cloudflareinsights.com",
    "cloudflare.com", "challenges.cloudflare.com", "hcaptcha.com", "recaptcha.net",
    "recaptcha.google.com", "gstatic.com", "app-measurement.com", "criteo.com",
    "criteo.net", "scorecardresearch.com", "quantserve.com", "hotjar.com",
    "mixpanel.com", "segment.com", "sentry.io", "jquery.min.js", "bootstrap.min.js",
    "giphy.com", "doubleclick", "adform.net", "smartadserver.com", "bidswitch.net",
    "teads.tv", "media.net", "yieldmo.com", "sharethrough.com", "33across.com",
}

# A real destination can be a social page (a shortener often points at one),
# but these hosts are also where footer widgets point, so they only count when
# the redirect mechanism is unambiguous.
SOCIAL_JUNK_ROOTS = {
    "facebook.com", "fbcdn.net", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "ytimg.com", "ggpht.com", "tiktok.com", "pinterest.com",
    "linkedin.com", "snapchat.com", "reddit.com", "t.me", "telegram.org",
    "wa.me", "whatsapp.com", "web.whatsapp.com", "api.whatsapp.com",
    "static.whatsapp.net", "imgur.com", "i.imgur.com", "vk.com", "ok.ru",
}

# Same-host links that are site chrome, never the next step of a wall.
NAV_PATHS = (
    "/auth", "/login", "/logout", "/register", "/signup", "/signin", "/pages",
    "/contact", "/payout", "/terms", "/privacy", "/dmca", "/about", "/blog",
    "/help", "/faq", "/support", "/dashboard", "/account", "/user", "/admin",
    "/proof", "/advertiser", "/publisher", "/tools", "/api/", "/js/", "/css/",
)

ASSET_EXTENSIONS = (
    ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm", ".json", ".xml", ".txt",
)

# Page text that means the wall is still in front of the link.
INTERSTITIAL_MARKERS = (
    "continue to", "click here to continue", "press continue", "your link is ready",
    "countdown", "ad-free", "skip ads", "please wait", "get link", "generating",
    "please disable adblock", "do not close this page", "your browser is loading",
    "click here to download", "seconds remaining",
)

# Proof a human gate is present - no honest tool can walk through these.
CAPTCHA_MARKERS = (
    "recaptcha", "g-recaptcha", "hcaptcha", "h-captcha", "cf-turnstile",
    "turnstile", "iconcaptcha", "are you a robot", "verify you are human",
    "i'm not a robot", "checking your browser", "just a moment",
    "verifying you are human", "press and hold", "unusual traffic",
)
SAFELINK_MARKERS = (
    "safelink", "wp-safelink", "click to continue", "you are being redirected",
    "link to the destination",
)


def _root(host: str) -> str:
    """Registrable-ish root: strips www and one extra label for known multi-levels."""
    if "://" in host or "/" in host:                 # tolerate a full URL
        host = urlparse(host).netloc
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) or ":" in (host or ""):
        return host                              # IP literal: never strip labels
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-3:]) in {"co.uk", "com.br", "co.in", "com.au", "com.tr", "co.id"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_ad_junk(host: str) -> bool:
    host = (host or "").lower()
    root = _root(host)
    return host in AD_JUNK_ROOTS or root in AD_JUNK_ROOTS or \
        any(root.endswith("." + junk) for junk in AD_JUNK_ROOTS)


def is_social(host: str) -> bool:
    host = (host or "").lower()
    root = _root(host)
    return host in SOCIAL_JUNK_ROOTS or root in SOCIAL_JUNK_ROOTS or \
        any(root.endswith("." + junk) for junk in SOCIAL_JUNK_ROOTS)


def is_junk(host: str) -> bool:
    """Hard block: ad/track/CDN hosts. Soft block is handled by is_social()."""
    return is_ad_junk(host)


def is_shortener(host: str) -> bool:
    root = _root(host)
    return root in SHORTENER_ROOTS or host in SHORTENER_ROOTS


# --------------------------------------------------------------------------- #
# result model
# --------------------------------------------------------------------------- #

@dataclass
class Hop:
    index: int
    url: str
    status: int | None = None
    type: str = "redirect"
    location: str | None = None
    elapsed_ms: int = 0
    note: str | None = None


@dataclass
class Result:
    input_url: str
    ok: bool = False
    bypassed: bool = False                 # True = real destination reached
    final_url: str | None = None           # the destination
    destination: str | None = None        # alias, what the UI shows
    shortener: str | None = None
    method: str = "none"                   # redirect | meta-refresh | javascript | base64 | browser | none
    lane: str = "http"                     # http | browser
    protection: str | None = None          # captcha | safelink | cloudflare | money-page | None
    explicit: bool = False                 # True = the page really released this link
    hops: int = 0
    chain: list[Hop] = field(default_factory=list)
    preview: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    note: str | None = None
    error: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# url helpers
# --------------------------------------------------------------------------- #

def normalize_url(raw: str) -> str:
    url = (raw or "").strip().strip("<>\"'")
    if not url:
        raise ValueError("Empty URL")
    if url.lower().startswith("www."):
        url = "http://" + url
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "http://" + url
    parts = urlparse(url)
    if not parts.netloc or not re.match(r"^[a-z0-9._~%-]+(?::\d+)?$", parts.netloc, re.I):
        raise ValueError(f"Not a valid URL: {raw}")
    if "." not in parts.netloc.split(":")[0]:
        raise ValueError(f"Not a valid domain: {raw}")
    return urlunparse(parts._replace(scheme=parts.scheme.lower()))


def is_public_host(host: str) -> bool:
    if ALLOW_PRIVATE_HOSTS:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


# URLs that are never a destination even when the host looks innocent.
_URL_JUNK_RE = re.compile(
    r"(recaptcha|hcaptcha|turnstile|captcha|challenge-platform|google-analytics|"
    r"googletag|googlesyndication|doubleclick|/gtm/|analytics|adservice|hotjar|"
    r"clarity\.ms|facebook\.net|connect\.facebook)", re.I
)


def looks_like_destination(url: str) -> bool:
    """Cheap sanity filter: is this plausibly a real content page?"""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    host = p.netloc
    if is_ad_junk(host) or _URL_JUNK_RE.search(url):
        return False
    if p.path.lower().endswith(ASSET_EXTENSIONS):
        return False
    return True


# --------------------------------------------------------------------------- #
# candidate mining
# --------------------------------------------------------------------------- #

_KIND_SCORE = {
    "meta-refresh": 100,
    "canonical": 95,
    "javascript": 88,
    "variable": 86,
    "base64": 84,
    "json": 80,
    "get-link": 76,
    "data-attr": 60,
    "plain": 45,
    "iframe": 40,
    "step": 20,
}

_META_REFRESH_RE = re.compile(r"""^\s*\d+(?:\.\d+)?\s*;?\s*url\s*=\s*['"]?([^'">\s]+)""", re.I)
_ONCLICK_OPEN_RE = re.compile(r"""open\(\s*['"]([^'"]+)['"]""", re.I)
_ASSIGN_OPEN_RE = re.compile(r"""(?:window\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""", re.I)
_ASSIGN_RE = re.compile(
    r"""(?:var|let|const)?\s*\w*(?:url|link|dest|target|redirect|href)\w*\s*[:=]\s*['"]([^'"]+)['"]""",
    re.I,
)
_ATOB_RE = re.compile(r"""atob\(\s*['"]([A-Za-z0-9+/=]{16,})['"]""", re.I)
_UNESCAPE_RE = re.compile(r"""(?:unescape|decodeURIComponent)\(\s*['"]([^'"]{12,})['"]""", re.I)
_BARE_URL_RE = re.compile(r"""https?://[^\s"'<>\\)\]]{10,}""", re.I)
_DATA_ATTRS = (
    "data-href", "data-url", "data-dest", "data-destination", "data-redirect",
    "data-link", "data-final", "data-target", "data-go", "data-out", "data-to",
)
_GET_LINK_TEXT = re.compile(
    r"(get\s*link|continue|next|skip|proceed|open|redirect|go\s*to|download|visit|keep\s*going)",
    re.I,
)
_SKIP_HREF_RE = re.compile(r"#(getlink|get_link|next|continue|skip|proceed|glink|link)\b", re.I)
_STEP_HINT_RE = re.compile(
    r"(get.?link|continue|next|step|skip|proceed|redirect|open|verify|/?go\b|out)", re.I
)
_JSON_KEYS = ("url", "link", "destination", "dest", "target", "redirect", "href", "redirect_url", "to")


def _b64_decode(value: str) -> str | None:
    try:
        pad = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + pad, validate=False).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        return None
    return decoded if "http" in decoded else None


def _clean(value: str) -> str:
    return unquote(value.replace("\\/", "/").replace("\\u002F", "/").replace("&amp;", "&").strip())


# Unambiguous redirect mechanisms - trusted on any page.
ALWAYS_ALLOWED = ("meta-refresh", "canonical", "javascript", "base64")
# Plausible but ambiguous - only trusted when the page is a wall / jump page.
WALL_ONLY = ("variable", "json", "get-link", "data-attr", "plain", "iframe", "step")
HIGH_CONFIDENCE = ALWAYS_ALLOWED
LOW_CONFIDENCE = WALL_ONLY


def is_jump_page(html: str) -> bool:
    """A bare page whose only link is the way out (multi-step walls look like this)."""
    if not html or len(html) > 8000:
        return False
    lowered = html.lower()
    if "<nav" in lowered or "<footer" in lowered:
        return False
    return lowered.count("<a ") <= 3 and lowered.count("<img") <= 2


def find_candidates(html: str, base_url: str, page_hosts: set[str],
                    wall_page: bool = False) -> list[tuple[str, str, int]]:
    """Return [(url, kind, score)] for every plausible destination in the page.

    `wall_page` means the HTML is a shortener/interstitial page. Low confidence
    sources (bare URLs in the footer, "get link" anchors, same-host next-step
    links) are only trusted on such pages - on a normal page they are just
    navigation, and following them walks away from the destination.
    """
    found: dict[str, tuple[str, int]] = {}
    base_host = urlparse(base_url).netloc.lower()
    base_root = _root(base_host)
    soup = BeautifulSoup(html, "lxml")

    def offer(raw: str, kind: str) -> None:
        if not raw:
            return
        candidate = urljoin(base_url, _clean(raw))
        if not looks_like_destination(candidate):
            return
        host = urlparse(candidate).netloc.lower()
        if is_ad_junk(host):
            return
        high = kind in ALWAYS_ALLOWED
        if kind == "step":
            path = urlparse(candidate).path.lower()
            if not wall_page or path in ("", "/") or any(path.startswith(p) for p in NAV_PATHS):
                return
        elif kind in WALL_ONLY:
            if not wall_page:
                return
            if is_social(host) and kind not in ("get-link", "step"):
                return
            if host in page_hosts and kind != "step":
                return
        if kind != "step" and _root(host) == base_root and kind != "meta-refresh":
            return
        score = _KIND_SCORE.get(kind, 10)
        if urlparse(candidate).path.strip("/"):
            score += 8
        prev = found.get(candidate)
        if prev is None or prev[1] < score:
            found[candidate] = (kind, score)

    # 1. meta refresh + canonical
    for tag in soup.find_all(["meta", "link"]):
        key = tag.get("http-equiv") or tag.get("rel") or ""
        if isinstance(key, (list, tuple)):
            key = " ".join(key)
        key = str(key).lower()
        content = tag.get("content") or tag.get("href")
        if not content:
            continue
        if "refresh" in key:
            m = _META_REFRESH_RE.match(content)
            if m:
                offer(m.group(1), "meta-refresh")
        elif key == "canonical":
            offer(content, "canonical")

    # 2. "Get Link" / "Continue" anchors - the button a human would press
    for a in soup.find_all("a", href=True):
        href, text = a["href"], a.get_text(" ", strip=True)
        if _GET_LINK_TEXT.search(text) or _SKIP_HREF_RE.search(href):
            offer(href, "get-link")

    # 3. data-* attributes
    for attr in _DATA_ATTRS:
        for tag in soup.find_all(attrs={attr: True}):
            offer(tag[attr], "data-attr")

    # 4. iframes (some walls wrap the target)
    for frame in soup.find_all(["iframe", "frame"]):
        if frame.get("src"):
            offer(frame["src"], "iframe")

    # 5. inline scripts
    for script in soup.find_all("script"):
        code = script.string or script.get_text() or ""
        if "http" not in code and "atob" not in code:
            continue
        for pattern in (_ONCLICK_OPEN_RE, _ASSIGN_OPEN_RE):
            for m in pattern.finditer(code):
                offer(m.group(1), "javascript")
        for m in _ASSIGN_RE.finditer(code):
            offer(m.group(1), "variable")
        for m in _ATOB_RE.finditer(code):
            decoded = _b64_decode(m.group(1))
            if decoded:
                offer(decoded, "base64")
        for m in _UNESCAPE_RE.finditer(code):
            offer(m.group(1), "javascript")

    # 6. JSON payloads, and bare URLs only in non-HTML (text/JSON) responses
    text = html.strip()
    if text.startswith(("{", "[")):
        try:
            for k, v in _walk_json(json.loads(text)):
                if k.lower() in _JSON_KEYS and isinstance(v, str):
                    offer(v, "json")
        except Exception:
            pass
    if "<a " not in html[:20000].lower() and len(html) < 20000:
        for m in _BARE_URL_RE.finditer(html[:20000]):
            offer(m.group(0), "plain")

    # 7. same-host "next step" pages (multi step walls: /code -> /code/step2)
    if wall_page:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if _GET_LINK_TEXT.search(a.get_text(" ", strip=True)) or _STEP_HINT_RE.search(href):
                offer(href, "step")

    return [(url, kind, score) for url, (kind, score) in
            sorted(found.items(), key=lambda kv: kv[1][1], reverse=True)]


def _walk_json(payload: Any, key: str = ""):
    if isinstance(payload, dict):
        for k, v in payload.items():
            yield from _walk_json(v, str(k))
    elif isinstance(payload, list):
        for v in payload[:50]:
            yield from _walk_json(v, key)
    else:
        yield key, payload


STEP_LINK_WORDS = {"continue", "next", "skip", "proceed", "get link", "open",
                   "go", "redirect", "go to", "keep going", "visit"}


def has_step_link(html: str) -> bool:
    """A lone "Continue"/"Next" link is a wall's next step, not site navigation."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return False
    for a in soup.find_all("a", href=True):
        if a.get_text(" ", strip=True).lower() in STEP_LINK_WORDS:
            return True
    return False


def page_wall_score(html: str, url: str) -> tuple[int, bool]:
    """How much does this page look like a shortener wall? -> (score, is_wall)

    Scored rather than pattern-matched so a normal content page (a tiny landing
    page, a 404) is never mistaken for a wall and escalated pointlessly.
    """
    if not html:
        return 0, False
    low = html[:60000].lower()
    score = 0
    if is_shortener(urlparse(url).netloc):
        score += 3
    if any(m in low for m in CAPTCHA_MARKERS):
        score += 3
    if any(m in low for m in SAFELINK_MARKERS):
        score += 2
    if any(m.lower() in low for m in INTERSTITIAL_MARKERS):
        score += 2
    jump = is_jump_page(html)
    if jump:
        score += 1
    if jump and has_step_link(html):
        return score + 2, True          # bare page + one "Continue" link = wall step
    return score, score >= 3


def detect_protection(html: str, url: str) -> str | None:
    lowered = (html or "")[:60000].lower()
    for marker in CAPTCHA_MARKERS:
        if marker in lowered:
            return "captcha"
    for marker in SAFELINK_MARKERS:
        if marker in lowered:
            return "safelink"
    if is_shortener(urlparse(url).netloc):
        return "shortener-wall"
    return None


def scrape_preview(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    names = (
        "og:title", "og:description", "og:image", "og:site_name",
        "twitter:title", "twitter:description", "twitter:image",
        "description", "keywords", "author", "robots",
    )
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        name = str(tag.get("property") or tag.get("name") or "").strip().lower()
        if name in names and tag.get("content") and name not in meta:
            meta[name] = tag["content"].strip()

    title = meta.get("og:title") or meta.get("twitter:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else urlparse(url).netloc

    image = meta.get("og:image") or meta.get("twitter:image")
    if image:
        image = urljoin(url, image)

    favicon = ""
    for rel in ("icon", "shortcut icon", "apple-touch-icon"):
        link = soup.find("link", rel=rel)
        if link and link.get("href"):
            favicon = urljoin(url, link["href"])
            break
    if not favicon:
        favicon = f"https://www.google.com/s2/favicons?domain={urlparse(url).netloc}&sz=64"

    return {
        "title": re.sub(r"\s+", " ", title)[:300],
        "description": re.sub(r"\s+", " ",
                              meta.get("og:description") or meta.get("twitter:description")
                              or meta.get("description") or "")[:600],
        "image": image or "",
        "favicon": favicon,
        "site_name": meta.get("og:site_name", ""),
        "keywords": meta.get("keywords", ""),
        "robots": meta.get("robots", ""),
        "html_bytes": len(html or ""),
    }


# --------------------------------------------------------------------------- #
# HTTP lane
# --------------------------------------------------------------------------- #

def _headers(referer: str | None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin" if _root(urlparse(referer).netloc) == _root(urlparse(referer).netloc) else "cross-site"
    return headers


class _Session:
    """One cookie jar for the whole chain, with TLS impersonation when possible."""

    def __init__(self, impersonate: bool = True):
        self._impersonate = impersonate and cf_requests is not None
        self._s = (cf_requests.Session(impersonate="chrome") if self._impersonate
                   else requests.Session())

    def get(self, url: str, referer: str | None = None) -> Any:
        return self._s.get(url, headers=_headers(referer), timeout=TIMEOUT,
                           allow_redirects=False)

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass


def http_lane(raw_url: str, safety: bool = True) -> Result:
    started = time.perf_counter()
    result = Result(input_url=raw_url, lane="http")

    try:
        current = normalize_url(raw_url)
    except Exception as exc:
        result.error = str(exc)
        return result

    if safety and not is_public_host(urlparse(current).netloc):
        result.error = "Blocked: target host is not a public internet address"
        return result

    start_host = urlparse(current).netloc.lower()
    result.shortener = _root(start_host)
    page_hosts = {start_host}
    referer: str | None = None
    seen: set[str] = {current}
    session = _Session()

    try:
        for index in range(1, MAX_HOPS + 1):
            hop_started = time.perf_counter()
            try:
                response = session.get(current, referer)
            except Exception as exc:
                result.error = f"Request failed: {type(exc).__name__}"
                result.chain.append(Hop(index=index, url=current, note=str(exc)[:160]))
                break

            status = response.status_code
            hop = Hop(index=index, url=current, status=status,
                      elapsed_ms=int((time.perf_counter() - hop_started) * 1000))

            # --- 3xx: follow Location ------------------------------------ #
            if 300 <= status < 400:
                location = response.headers.get("Location", "").strip()
                if not location:
                    hop.type = "final"
                    result.chain.append(hop)
                    result.final_url = current
                    break
                nxt = urljoin(current, location)
                hop.type = "redirect"
                hop.location = nxt
                result.chain.append(hop)
                page_hosts.add(urlparse(nxt).netloc.lower())
                if nxt in seen:
                    result.warnings.append("Redirect loop - stopped")
                    result.final_url = nxt
                    break
                seen.add(nxt)
                referer, current = current, nxt
                result.hops = index
                if result.method == "none":
                    result.method = "redirect"
                continue

            # --- 2xx/4xx body ------------------------------------------- #
            try:
                body = response.text
            except Exception:
                body = ""

            protection = detect_protection(body, current)
            _score, is_wall = page_wall_score(body, current)
            wall_page = is_wall or protection in ("captcha", "safelink", "cloudflare")
            candidates = find_candidates(body, current, page_hosts, wall_page=wall_page) if body else []
            if not candidates and wall_page:
                hop.note = "wall detected, no embedded target in the HTML"

            if candidates:
                target, kind, _score = candidates[0]
                hop.type = kind
                hop.location = target
                result.chain.append(hop)
                result.hops = index
                if target in seen:
                    result.warnings.append("Redirect loop - stopped")
                    result.final_url = target
                    break
                seen.add(target)
                result.method = kind
                referer, current = current, target
                continue

            hop.type = "final"
            result.chain.append(hop)
            result.hops = index
            result.final_url = current
            result.preview = scrape_preview(body, current) if body else {}
            result.protection = protection
            if wall_page and not candidates:
                # still standing on the wall: force the orchestrator to escalate
                result.protection = result.protection or "shortener-wall"
                hop.note = "wall reached, destination not released yet"
            if result.method == "none":
                result.method = "direct"
            if status >= 400:
                result.warnings.append(f"Final page returned HTTP {status}")
            break
        else:
            result.warnings.append(f"Stopped after {MAX_HOPS} hops")
    finally:
        session.close()

    if result.final_url is None and result.chain:
        last = result.chain[-1]
        result.final_url = last.location or last.url

    result.ok = bool(result.final_url) and not result.error
    result.bypassed = reached_destination(result)
    # never hand a shortener page back as if it were the destination
    result.destination = result.final_url if result.bypassed else None
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return result


def use_browser_hint(html: str) -> bool:
    lowered = (html or "")[:30000].lower()
    return any(m.lower() in lowered for m in INTERSTITIAL_MARKERS + CAPTCHA_MARKERS)


# --------------------------------------------------------------------------- #
# browser lane
# --------------------------------------------------------------------------- #

_BROWSER_INIT = r"""
(() => {
  window.__lbTargets = [];
  const realOpen = window.open;
  window.open = function (u) {
    try { if (u) window.__lbTargets.push(String(u)); } catch (e) {}
    return { closed:false, close(){}, focus(){}, postMessage(){}, opener:null };
  };
  const rec = (u) => { try { if (u) window.__lbTargets.push(String(u)); } catch (e) {} };
  try {
    const desc = Object.getOwnPropertyDescriptor(Location.prototype, 'href');
    if (desc && desc.set) {
      Object.defineProperty(window.location, 'href', {
        set(v) { rec(v); return desc.set.call(window.location, v); }
      });
    }
  } catch (e) {}
  // fast-forward countdowns: keep the callback, shrink the wait
  const st = window.setTimeout, si = window.setInterval;
  const wrap = (fn) => (typeof fn === 'function' ? fn
    : function () { try { eval(fn); } catch (e) {} });
  window.setTimeout = function (fn, ms) { return st.call(window, wrap(fn), Math.min(ms || 0, 25)); };
  window.setInterval = function (fn, ms) { return si.call(window, wrap(fn), Math.min(ms || 0, 100)); };
  // keep the page "focused" so visibility-gated code runs
  try {
    Object.defineProperty(Document.prototype, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(Document.prototype, 'visibilityState', { get: () => 'visible', configurable: true });
    Document.prototype.hasFocus = () => true;
  } catch (e) {}
  window.focus = () => {};
})();
"""

_BTN_TEXT = re.compile(
    r"(get\s*link|continue|next|proceed|open\s*link|go\s*to|redirect|download|visit|"
    r"verify|human|click\s*here|keep\s*going|open\s*website)", re.I
)

# --------------------------------------------------------------------------- #
# multi-step flow pages (the arolinks family)
#
# The shortener walks the visitor through article pages that are tagged
#   "Step 1" / "Step 2" ... and offer a Verify button (a fake progress scanner)
#   followed by a Continue button that goes to /readmore and loads the next
#   article. The real link is only in the href of the final "Get Link" anchor.
# So the lane must keep walking even after it leaves the shortener's domain,
# and must never click the decoys sitting in the article body.
# --------------------------------------------------------------------------- #

_FLOW_DUMP = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const text = (el) => (el.innerText || el.value || '').trim().replace(/\s+/g, ' ');
  const nodes = [...document.querySelectorAll('a[href],button,input[type=submit],input[type=button]')]
    .filter(visible);
  const decoy = (el) =>
    /scanner/i.test(el.getAttribute('onclick') || '') ||
    /skip-link|skip to content/i.test((el.className || '') + ' ' + text(el)) ||
    /^#(content|main|top)$/i.test(el.getAttribute('href') || '');

  const getLinkBtn = nodes.find(el => /get\s*link/i.test(text(el)));
  const getLink = (getLinkBtn && /^https?:/i.test(getLinkBtn.getAttribute('href') || ''))
    ? getLinkBtn.href : null;
  const verifyEl = nodes.find(el => /^verify$/i.test(text(el)))
    || nodes.find(el => /^btn6$/i.test(el.id || ''))
    || nodes.find(el => /ce-btn/i.test((el.className || '').toString()) && /verify/i.test(text(el)));
  // the only Continue that really moves the walk is the anchor into
  // /readmore; the button with the same id just says "Please Wait..." until
  // its countdown is over, and clicking it does nothing.
  const hrefOf = (el) => el.getAttribute('href') || '';
  const realCont = (el) => /\/readmore|step|flow|gotolink/i.test(hrefOf(el)) && !decoy(el);
  const contEl = nodes.find(el => realCont(el))
    || nodes.find(el => /^continueBtn$/i.test(el.id || '') && !/please wait/i.test(text(el)))
    || nodes.find(el => /^btn7$/i.test(el.id || '') && !/please wait/i.test(text(el)))
    || nodes.find(el => /^continue\b/i.test(text(el)) && !/please wait/i.test(text(el)))
    || nodes.find(el => /^next$/i.test(text(el)));
  const waiting = nodes.some(el => /please wait/i.test(text(el)));
  const body = document.body ? document.body.innerText : '';
  const robots = (document.querySelector('meta[name=robots]') || {}).content || '';
  const title = (document.title || '').trim();
  const waitMatch = body.match(/(?:please wait|wait for)\s*(\d+)\s*second/i) ||
                    body.match(/(\d+)\s*seconds?\s*(?:remaining|left)/i);

  return {
    steps: (body.match(/step\s*\d+/gi) || []).slice(0, 3),
    getLink: getLink,
    getLinkBtn: !!getLinkBtn,
    verify: !!verifyEl,
    cont: !!contEl,
    waiting: waiting,
    countdown: waitMatch ? parseInt(waitMatch[1], 10) : 0,
    ready: document.readyState,
    // thin "Landing.." hop pages of a campaign, never a destination
    tracker: /noindex/i.test(robots) || /^landing/i.test(title),
    protection: /recaptcha|hcaptcha|cf-turnstile|are you a robot|verify you are human/i.test(body),
  };
}
"""

_FLOW_CLICK = r"""
(kind) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0) && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const text = (el) => (el.innerText || el.value || '').trim().replace(/\s+/g, ' ');
  const decoy = (el) =>
    /scanner/i.test(el.getAttribute('onclick') || '') ||
    /skip-link|skip to content/i.test((el.className || '') + ' ' + text(el)) ||
    /^#(content|main|top)$/i.test(el.getAttribute('href') || '');
  const nodes = [...document.querySelectorAll('a[href],button,input[type=submit],input[type=button]')]
    .filter(el => visible(el) && !el.disabled && !decoy(el) && !el.dataset.lbFlow);
  // only mark the target here - the real click is dispatched from Python as a
  // trusted mouse event, because these pages ignore synthetic el.click()
  const pick = (el, label) => {
    if (!el) return null;
    document.querySelectorAll('[data-lb-target]').forEach(e => e.removeAttribute('data-lb-target'));
    el.dataset.lbTarget = '1';
    el.dataset.lbFlow = '1';
    el.scrollIntoView({ block: 'center' });
    return label;
  };

  if (kind === 'getlink') {
    return pick(nodes.find(e => /get\s*link/i.test(text(e))), 'Get Link');
  }
  if (kind === 'verify') {
    return pick(nodes.find(e => /^btn6$/i.test(e.id || ''))
      || nodes.find(e => /^verify$/i.test(text(e)))
      || nodes.find(e => /ce-btn/i.test((e.className || '').toString()) && /verify/i.test(text(e))),
      'Verify');
  }
  if (kind === 'continue') {
    // btn7 is the button that arms the walk: its handler shows the real
    // /readmore anchor and clicks it. Clicking that anchor directly does
    // nothing, and its "Please Wait..." state must be left alone.
    const waiting = /please wait/i;
    return pick(nodes.find(e => /^btn7$/i.test(e.id || '') && !waiting.test(text(e)))
      || nodes.find(e => /^continueBtn$/i.test(e.id || '') && !waiting.test(text(e)))
      || nodes.find(e => /^\/?readmore/i.test(e.getAttribute('href') || ''))
      || nodes.find(e => /^continue\b/i.test(text(e)) && !waiting.test(text(e)))
      || nodes.find(e => /^next$/i.test(text(e))),
      'Continue');
  }
  return null;
}
"""

# where the marked element sits, for a trusted mouse click
_FLOW_POINT = r"""
() => {
  const el = document.querySelector('[data-lb-target]');
  if (!el) return null;
  el.scrollIntoView({ block: 'center' });
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}
"""


# The campaign wraps the widget in a full-screen ad overlay. We block ad
# networks, so the overlay never fills and just eats every click - lift it off
# the page before pressing a flow button.
_FLOW_UNBLOCK = r"""
() => {
  let dropped = 0;
  const sels = ['#adOverlay', '[id*="adOverlay" i]', '[class*="ad-overlay" i]',
                '[id*="admodal" i]', '[class*="admodal" i]', '[id*="popup" i]'];
  for (const sel of sels) {
    document.querySelectorAll(sel).forEach((el) => {
      const r = el.getBoundingClientRect();
      if (r.width > 200 && r.height > 200) {
        try { el.remove(); dropped++; } catch (e) {}
      }
    });
  }
  return dropped;
}
"""


# Clicking Continue is not enough: the campaign gates the /readmore anchor
# behind an ad handler that swallows the click. Assigning the very same href
# from inside the page skips the click listeners and keeps the walk moving.
_FLOW_FORCE = r"""
() => {
  const hrefOf = (el) => (el && (el.href || el.getAttribute('href'))) || null;
  const cand = hrefOf(document.getElementById('btn1'))
    || hrefOf(document.getElementById('btn7'))
    || hrefOf(document.getElementById('btn0'))
    || hrefOf(document.getElementById('continueBtn'))
    || hrefOf(document.querySelector('a[href*="/readmore"]'));
  if (!cand || !/\/readmore|\/step|\/gotolink/i.test(cand)) return null;
  window.location.assign(cand);
  return cand;
}
"""


def _flow_click(page, kind: str) -> str | None:
    for _ in range(3):
        try:
            try:
                page.evaluate(_FLOW_UNBLOCK)
            except Exception:
                pass
            label = page.evaluate(_FLOW_CLICK, kind)
            if not label:
                return None
            point = page.evaluate(_FLOW_POINT)
            if point:
                page.mouse.click(point["x"], point["y"], delay=60)
            else:                    # no layout (headless quirk): fall back
                page.evaluate("() => { const e = document.querySelector('[data-lb-target]');"
                              " if (e) e.click(); }")
            return label
        except Exception:
            time.sleep(0.6)          # navigation destroyed the context
    return None


def _flow_dump(page) -> dict[str, Any]:
    for _ in range(3):
        try:
            return page.evaluate(_FLOW_DUMP)
        except Exception:
            time.sleep(0.6)
    return {"steps": [], "getLink": None, "verify": False, "cont": False,
            "countdown": 0, "protection": False}



_ASSET_URL_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|mp4|webm|m4v|woff2?|ttf|eot|avi|mov)(\?|$)", re.I
)


# Internal hops of a multi-step flow, never the destination.
_FLOW_INTERNAL_RE = re.compile(
    r"/(readmore|studyeducations|step-?\d*|continue|gotolink|go|flow|link)/?(\?|$)|"
    r"[?&](st|pgtr|universtityeducations|educationsscholorships|pg|step)=",
    re.I,
)


def _launched_target(page, popups: list[Any]) -> str | None:
    """A URL the page handed over by window.open / a popup (no network guessing)."""
    try:
        for raw in page.evaluate("() => window.__lbTargets || []"):
            absolute = urljoin(page.url or "", str(raw))
            if looks_like_destination(absolute) and not is_ad_junk(urlparse(absolute).netloc):
                return absolute
    except Exception:
        pass
    for popup in popups:
        try:
            candidate = popup.url
        except Exception:
            continue
        if candidate and candidate != "about:blank" and \
                looks_like_destination(candidate) and not is_ad_junk(urlparse(candidate).netloc):
            return candidate
    return None


def _js_or_popup_target(page, current: str, popups: list[Any], ledger: list[str],
                        start_root: str) -> str | None:
    """The destination when the page hands it over via JS, a popup or the network."""
    current_root = _root(urlparse(current).netloc)

    def usable(url: str) -> bool:
        return bool(
            url
            and looks_like_destination(url)
            and _root(urlparse(url).netloc) != start_root
            and _root(urlparse(url).netloc) != current_root   # not the page's own host
            and not is_ad_junk(urlparse(url).netloc)
            and not _FLOW_INTERNAL_RE.search(url)
        )

    try:
        for raw in page.evaluate("() => window.__lbTargets || []"):
            absolute = urljoin(current, str(raw))
            if usable(absolute):
                return absolute
    except Exception:
        pass
    for popup in popups:
        try:
            candidate = popup.url
        except Exception:
            continue
        if candidate and candidate != "about:blank" and usable(candidate):
            return candidate
    for req in reversed(ledger):
        if not usable(req):
            continue
        params = parse_qs(urlparse(req).query)
        for key in ("url", "link", "to", "target", "u", "redirect"):
            if key in params:
                value = urljoin(current, params[key][0])
                if usable(value):
                    return value
    return None


def _make_route_filter():
    """Drop images/fonts/media and ad networks so the flow loads in seconds.

    Captcha endpoints are deliberately kept: the page still has to be able to
    render its challenge for us to report the gate honestly.
    """

    def handler(route, request) -> None:
        url = request.url
        if _ASSET_URL_RE.search(url):
            return route.abort()
        if not re.search(r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare", url, re.I):
            if is_ad_junk(urlparse(url).netloc) or _URL_JUNK_RE.search(url):
                return route.abort()
        return route.continue_()

    return handler


def fetch_preview(url: str) -> dict[str, Any]:
    """Cheap metadata fetch for a page we did not render (the destination)."""
    session = _Session()
    try:
        response = session.get(url)
        if response.status_code < 400:
            return scrape_preview(response.text, response.final_url if hasattr(response, "final_url") else url)
    except Exception:
        pass
    finally:
        session.close()
    return {}


def browser_lane(raw_url: str, budget_ms: int = 75000, safety: bool = True) -> Result:
    """Drive a real browser and report the URL a human would land on."""
    started = time.perf_counter()
    result = Result(input_url=raw_url, lane="browser", method="browser")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        result.error = f"Playwright not available: {exc}"
        return result

    try:
        url = normalize_url(raw_url)
    except Exception as exc:
        result.error = str(exc)
        return result

    if safety and not is_public_host(urlparse(url).netloc):
        result.error = "Blocked: target host is not a public internet address"
        return result

    start_root = _root(urlparse(url).netloc)
    result.shortener = start_root
    found: str | None = None
    popups: list[Any] = []
    ledger: list[str] = []
    deadline = time.perf_counter() + budget_ms / 1000

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT, viewport={"width": 1366, "height": 900},
                locale="en-US", timezone_id="Asia/Kolkata", java_script_enabled=True,
            )
            context.add_init_script(_BROWSER_INIT)
            context.route("**/*", _make_route_filter())
            page = context.new_page()
            page.on("popup", lambda pg: popups.append(pg))
            page.on("request", lambda rq: ledger.append(rq.url))

            _emit("browser", "Chromium launched; opening shortener page")
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            _emit("browser", "Page loaded; inspecting buttons and redirects")
            step = 0
            last_url = ""
            previous_url = ""
            signature, stalled = "", 0
            stall_since = time.perf_counter()
            reloaded_for = ""
            clicked_flow = False
            getlink_clicked = False
            explicit = False            # destination came from a real release

            def record(page_url: str, note: str | None = None) -> None:
                result.chain.append(Hop(index=len(result.chain) + 1, url=page_url,
                                        type="flow-step", note=note))

            while time.perf_counter() < deadline and step < 120:
                step += 1
                if page.url != last_url:                # only wait when it navigated
                    last_url = page.url
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                try:                      # some walls only reveal the button on scroll
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass

                current = page.url
                off_wall = _root(urlparse(current).netloc) != start_root
                info = _flow_dump(page)
                is_flow = bool(info.get("steps")) or info.get("verify") or \
                    info.get("cont") or bool(info.get("countdown")) or \
                    info.get("getLinkBtn")
                settled = current == previous_url and info.get("ready") != "loading"
                previous_url = current
                _debug(f"t+{time.perf_counter() - started:5.1f}s step {step:3} "
                       f"{current[:70]} flow={is_flow} settled={settled} "
                       f"steps={info.get('steps')} verify={info.get('verify')} "
                       f"cont={info.get('cont')} cd={info.get('countdown')} "
                       f"wait={info.get('waiting')} "
                       f"getlink={bool(info.get('getLink'))}")

                # 1. the answer: the href under the "Get Link" button
                get_link = info.get("getLink")
                if get_link and looks_like_destination(get_link) and not is_ad_junk(urlparse(get_link).netloc):
                    found = get_link
                    explicit = True
                    record(current, "Get Link")
                    break

                # 1a. the Get Link was a button: take only what IT released
                if getlink_clicked:
                    launched = _launched_target(page, popups)
                    if launched:
                        found = launched
                        explicit = True
                        record(current, "Get Link (button)")
                        break

                # 1b. plain redirector: a destination released by JS / popup /
                # network counts. Never on a flow page - there the off-site
                # pages are just the next step, and the answer is the Get Link.
                if not is_flow and not clicked_flow:
                    launched = _js_or_popup_target(page, current, popups, ledger, start_root)
                    if launched:
                        found = launched
                        explicit = True
                        record(current, "released by the page")
                        break

                # 2. never fake a captcha
                if info.get("protection"):
                    result.protection = "captcha"
                    record(current, "captcha gate")
                    break

                # 3. not a flow page and we are off the shortener: that IS the
                # destination - but only for plain redirectors. Once we have
                # pressed a flow button, or we are on a campaign "Landing.."
                # hop, an off-site page is just the next step of the walk and the
                # real link only comes from the Get Link button.
                if off_wall and not is_flow and settled and not clicked_flow \
                        and not getlink_clicked and not info.get("tracker") \
                        and looks_like_destination(current):
                    found = current
                    explicit = True
                    record(current, "left the shortener")
                    break

                # 4. multi-step flow page: Verify, then Continue, then Get Link
                if is_flow:
                    mark = f"{current}|{info.get('verify')}|{info.get('cont')}|{info.get('countdown')}"
                    if mark == signature:
                        stalled += 1
                    else:
                        stalled, stall_since, reloaded_for = 0, time.perf_counter(), current
                    signature = mark
                    if stalled > 6 and current == reloaded_for:
                        # the page may simply not have finished rendering: reload it
                        reloaded_for = ""
                        try:
                            page.reload(wait_until="domcontentloaded", timeout=25000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1200)
                        stalled = 0
                        continue
                    if stalled > 8 and (time.perf_counter() - stall_since) > 25:
                        # on a flow page only the Get Link counts, so never
                        # fall back to a guessed network target here
                        if getlink_clicked:
                            launched = _launched_target(page, popups)
                            if launched:
                                found = launched
                                explicit = True
                                record(current, "Get Link (button)")
                                break
                        result.warnings.append("Flow page stopped changing - gave up")
                        record(current, "stalled")
                        break
                    if info.get("getLinkBtn"):
                        if _flow_click(page, "getlink"):
                            clicked_flow, getlink_clicked = True, True
                            record(current, "Get Link")
                            page.wait_for_timeout(1500)
                            continue
                    if info.get("verify"):
                        # re-clickable: the flow shows Verify again on the same
                        # URL after every Continue, and the per-element marker in
                        # the click helper stops us double-clicking one instance
                        if _flow_click(page, "verify"):
                            clicked_flow = True
                            record(current, "Verify")
                            page.wait_for_timeout(1200)
                            continue
                    if info.get("cont"):
                        before = current
                        if _flow_click(page, "continue"):
                            clicked_flow = True
                            record(current, "Continue")
                            page.wait_for_timeout(2500)
                            if page.url == before:
                                forced = None
                                try:
                                    forced = page.evaluate(_FLOW_FORCE)
                                except Exception:
                                    pass
                                if forced:
                                    record(current, "Continue (forced)")
                                    try:
                                        page.wait_for_load_state(
                                            "domcontentloaded", timeout=20000)
                                    except Exception:
                                        pass
                            continue
                    if info.get("countdown") or info.get("waiting"):
                        # a locked step is counting down - let it finish
                        page.wait_for_timeout(1500)
                        continue
                    page.wait_for_timeout(700)
                    continue

                # 5. plain wall: press whatever releases the link
                if not settled:
                    page.wait_for_timeout(800)
                    continue
                try:
                    clicked_now = page.evaluate(                        """(rx) => {
                            let n = 0;
                            const nodes = [...document.querySelectorAll(
                              'a,button,input[type=button],input[type=submit],[role=button]')];
                            for (const el of nodes) {
                              if (el.disabled || el.classList.contains('disabled')) continue;
                              const style = getComputedStyle(el);
                              if (style.display === 'none' || style.visibility === 'hidden') continue;
                              if (el.offsetParent === null) continue;
                              if (/scanner|skip/i.test((el.getAttribute('onclick') || '') + el.id)) continue;
                              const text = (el.innerText || el.value || el.textContent || '').trim();
                              const id = (el.id || '') + '|' + (el.className || '');
                              if (new RegExp(rx, 'i').test(text) || /getlink|get_link|continue|next/i.test(id)) {
                                  if (el.dataset.lbClicked) continue;
                                  el.dataset.lbClicked = '1';
                                  el.click(); n++;
                                  if (n >= 2) break;
                              }
                            }
                            return n;
                        }""",
                        _BTN_TEXT.pattern,
                    )
                except Exception:
                    clicked_now = 0
                page.wait_for_timeout(1200 if clicked_now else 800)

            # a Get Link button only ever hands over by opening a target, so
            # trust a popup only after we pressed it - an ad popup would
            # otherwise look like a destination
            if getlink_clicked:
                for popup in popups:
                    if found:
                        break
                    try:
                        candidate = popup.url
                    except Exception:
                        continue
                    if candidate and _root(urlparse(candidate).netloc) != start_root \
                            and looks_like_destination(candidate) \
                            and not is_ad_junk(urlparse(candidate).netloc):
                        found = candidate
                        explicit = True
                        result.chain.append(
                            Hop(index=len(result.chain) + 1, url=candidate, type="browser",
                                location=page.url, note="Get Link (popup)"))

            _emit("browser", "Browser steps completed; checking destination")
            if not result.protection:
                detected = detect_protection(page.content(), page.url)
                if detected and not found:
                    result.protection = detected
            if not result.chain:
                result.chain = [Hop(index=1, url=url, type="browser", location=page.url,
                                    note=f"{step} browser steps, {len(ledger)} requests")]
            result.hops = max(len(result.chain), 1)
            try:
                result.preview = scrape_preview(page.content(), page.url)
            except Exception:
                result.preview = {}
            if found:
                result.preview = fetch_preview(found) or result.preview
            browser.close()
    except Exception as exc:
        result.error = f"Browser lane failed: {type(exc).__name__}: {exc}"[:200]

    _emit("browser", "Browser finished: " + ("destination found" if found else (result.error or result.protection or "no destination")))
    result.final_url = found
    result.ok = bool(found)
    result.explicit = bool(found) and explicit
    result.bypassed = bool(found)
    result.destination = found
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    if not found:
        result.note = ("No off-site navigation was exposed by the page. It is most "
                       "likely gated by a captcha or requires a human click.")
    return result


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #

def reached_destination(result: Result) -> bool:
    """True when we are off the shortener, not looking at another wall."""
    if result.error or not result.ok or not result.final_url:
        return False
    host = urlparse(result.final_url).netloc
    if is_ad_junk(host):
        return False
    if result.protection in ("captcha", "safelink", "shortener-wall"):
        return False
    if getattr(result, "explicit", False):
        return True          # the page handed this link over: trust it as-is
    if is_shortener(host) or is_shortener(result.input_url):
        return not is_shortener(host) and result.protection is None
    return True


import threading
_EVENT_LOCAL = threading.local()

def _emit(stage, message):
    callback = getattr(_EVENT_LOCAL, "callback", None)
    if callback:
        try:
            callback(stage, message)
        except Exception:
            pass


def bypass(raw_url: str, mode: str = "auto", use_browser: bool | None = None,
          safety: bool = True) -> Result:
    """mode: auto | http | browser"""
    started = time.perf_counter()
    if use_browser is not None:
        mode = "browser" if use_browser else "http"

    if mode == "browser":
        _emit("browser", "Launching Chromium browser")
        out = browser_lane(raw_url, safety=safety)
        out.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return out

    _emit("http", "Checking redirects and HTML candidates")
    http_res = http_lane(raw_url, safety=safety)
    _emit("http", "HTTP result: " + (http_res.protection or http_res.error or ("destination found" if reached_destination(http_res) else "wall remains")))

    if mode == "auto" and not reached_destination(http_res):
        _emit("browser", "HTTP unresolved; starting Chromium fallback")
        browser_res = browser_lane(raw_url, safety=safety)
        http_res.bypassed = browser_res.bypassed
        http_res.lane = "http+browser"
        http_res.elapsed_ms = int((time.perf_counter() - started) * 1000)
        if browser_res.ok:
            http_res.final_url = browser_res.final_url
            http_res.destination = browser_res.destination
            http_res.explicit = browser_res.explicit
            http_res.preview = browser_res.preview or http_res.preview
            http_res.method = "browser"
            http_res.protection = None
            http_res.note = "Escalated to the browser lane to clear the wall."
            merged = list(http_res.chain)
            for hop in browser_res.chain:              # keep every flow step visible
                hop.index = len(merged) + 1
                merged.append(hop)
            http_res.chain = merged
            http_res.hops = len(merged)
        else:
            http_res.note = browser_res.note or http_res.note
            _emit("browser", "Browser unresolved: " + (browser_res.error or browser_res.protection or browser_res.note or "unknown"))
            if browser_res.error:
                http_res.warnings.append(browser_res.error)
            if http_res.protection in (None, "shortener-wall") and not http_res.error:
                http_res.protection = http_res.protection or "unresolved"

    if mode == "auto" and http_res.lane == "http+browser" and not browser_res.ok:
        http_res.protection = browser_res.protection or http_res.protection
    final_host = urlparse(http_res.final_url).netloc if http_res.final_url else ""
    input_host = urlparse(raw_url).netloc
    left_the_wall = bool(final_host) and _root(final_host) != _root(input_host)
    http_res.bypassed = (
        not http_res.error
        and reached_destination(http_res)
        and (left_the_wall or not is_shortener(input_host))
    )
    http_res.destination = http_res.final_url if http_res.bypassed else None
    if not http_res.bypassed and not http_res.note:
        http_res.note = ("Stopped on the shortener: the destination is only released "
                         "after a captcha, which this tool will not fake.")
    _emit("complete", "Destination verified" if http_res.bypassed else "No verified destination")
    return http_res


def resolve(raw_url: str, use_browser: bool = False, safety: bool = True) -> Result:
    """Backwards-compatible entry point (bypasses by default)."""
    return bypass(raw_url, mode="auto", use_browser=use_browser, safety=safety)


def resolve_many(urls: list[str], mode: str = "auto") -> list[Result]:
    return [bypass(u, mode=mode) for u in urls]
