#!/usr/bin/env python3
"""BlogCast: convert blog posts and PDF documents into a self-hosted podcast feed.

Sources are discovered automatically (WordPress REST API or RSS), backfilled in
full, then updated incrementally. Text is normalised, language-detected and
synthesised with Microsoft Edge neural voices. The resulting episodes are
published through a generated RSS feed and a hardened HTTP server that exposes
only /podcast.xml, /audio/ and /assets/.

Configuration lives in the environment (see .env.example):
    BLOGCAST_LANGUAGES   languages to handle, in order of preference
    BLOGCAST_BLOG_URLS   one or more blog URLs, comma- or newline-separated
    BLOGCAST_PDF_PATH    optional host folder mounted at /external-pdf-drop

Commands:
    python blogcast.py update                            fetch new content
    python blogcast.py list                              show configured sources
    python blogcast.py serve --port 8000                 serve feed and audio
    python blogcast.py voices                            list available voices
    python blogcast.py add <url>                         add a source manually
    python blogcast.py remove <slug|url|name> [--purge]  remove a source
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    from babel import Locale
    from babel.dates import format_date
except ImportError:  # pragma: no cover - environment problem, not a code path
    raise SystemExit(
        "BlogCast requires the 'babel' package for spoken dates.\n"
        "    Install it with:  pip install babel"
    )

# --- Configuration ----------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent / "blogcast_data"
FEEDS_FILE = DATA_DIR / "feeds.json"
AUDIO_DIR = DATA_DIR / "audio"
ASSET_DIR = DATA_DIR / "assets"
CHIME_FILE = ASSET_DIR / "chime.mp3"
FEED_OUT = DATA_DIR / "podcast.xml"

# --- Languages --------------------------------------------------------------
# BLOGCAST_LANGUAGES lists the languages this instance handles, in order of
# preference. The first entry is the fallback whenever detection is
# inconclusive, and is used as the feed's <language>. Each language needs a
# voice: a built-in friendly name (Finnish and English only) or a full edge-tts
# voice ID, set through BLOGCAST_VOICE_<LANG>.

# Curated friendly names for the two languages BlogCast ships with. Any other
# language must be configured with a full voice ID.
BUILTIN_VOICE_NAMES = {
    "fi": {"Noora": "fi-FI-NooraNeural", "Harri": "fi-FI-HarriNeural"},
    "en": {"Aria": "en-US-AriaNeural", "Guy": "en-US-GuyNeural",
           "Sonia": "en-GB-SoniaNeural", "Ryan": "en-GB-RyanNeural"},
}
BUILTIN_DEFAULT_VOICE = {"fi": "Noora", "en": "Aria"}

# Spoken around the body text. Placeholders: {blog} {date} {title}.
BUILTIN_INTRO = {"fi": "{blog}. {date}. {title}.",
                 "en": "{blog}. {date}. {title}."}
BUILTIN_OUTRO = {"fi": "Tähän päättyy {blog} teksti {title}.",
                 "en": "This concludes the {blog} post: {title}."}
GENERIC_INTRO = "{blog}. {date}. {title}."

_VOICE_ID_RE = re.compile(r"^[a-z]{2,3}-[A-Za-z0-9]{2,}-\S+$")
_PLACEHOLDER_RE = re.compile(r"{(\w*)}")


def _invalid_placeholders(template: str, allowed: set) -> set:
    """Return placeholders used in a template that are not allowed."""
    return {name for name in _PLACEHOLDER_RE.findall(template)
            if name not in allowed}


def _resolve_voice_setting(lang: str, value: str):
    """Return (display_name, voice_id) for a configured voice, or None.

    Accepts a built-in friendly name for the language (case-insensitive) or a
    full edge-tts voice ID.
    """
    if not value:
        return None
    value = value.strip()
    for name, voice_id in BUILTIN_VOICE_NAMES.get(lang, {}).items():
        if name.lower() == value.lower():
            return name, voice_id
    if _VOICE_ID_RE.match(value):
        return value, value
    return None


def _load_languages():
    """Build the language list and its voices from the environment.

    A language without a usable voice is dropped with a warning rather than
    failing the run, so one bad entry cannot take the whole feed down.
    """
    requested = []
    for value in re.split(r"[,\s]+", os.getenv("BLOGCAST_LANGUAGES", "fi,en")):
        code = value.strip().lower()
        if code and code not in requested:
            requested.append(code)
    if not requested:
        requested = ["fi"]

    languages, names, ids = [], {}, {}
    for lang in requested:
        configured = (os.getenv(f"BLOGCAST_VOICE_{lang.upper()}")
                      or BUILTIN_DEFAULT_VOICE.get(lang, ""))
        resolved = _resolve_voice_setting(lang, configured)
        if not resolved:
            print(f"⚠️  Language '{lang}' has no usable voice; ignoring it. "
                  f"Set BLOGCAST_VOICE_{lang.upper()} to an edge-tts voice ID "
                  f"such as '{lang}-XX-NameNeural'.")
            continue
        languages.append(lang)
        names[lang], ids[lang] = resolved

    if not languages:
        raise SystemExit(
            "No usable language configured. Set BLOGCAST_LANGUAGES and a "
            "matching BLOGCAST_VOICE_<LANG> in .env."
        )
    return languages, names, ids


LANGUAGES, DEFAULT_VOICES, VOICE_IDS = _load_languages()
FALLBACK_LANG = LANGUAGES[0]


# --- Spoken dates -----------------------------------------------------------
# Dates are spoken using Babel's CLDR data, which supplies both the month names
# and the word order for any language it knows -- including inflected forms
# such as Finnish "kesakuuta" rather than "kesakuu". A language Babel does not
# recognise falls back to a numeric date, which is still understandable aloud.
#
# Two optional overrides exist for unusual cases:
#   BLOGCAST_MONTHS_<LANG>       twelve comma-separated month names
#   BLOGCAST_DATE_FORMAT_<LANG>  word order, using {day}, {month} and {year}

NUMERIC_DATE_FORMAT = "{day}.{month}.{year}"
GENERIC_DATE_FORMAT = "{day} {month} {year}"


def _babel_locale(lang: str):
    """Return the Babel locale for a language code, or None if unknown."""
    try:
        return Locale.parse(lang)
    except Exception:
        return None


def _babel_month_names(locale) -> list | None:
    """Return the twelve CLDR month names used inside a full date."""
    if locale is None:
        return None
    try:
        months = locale.months["format"]["wide"]
        names = [months[i] for i in range(1, 13)]
    except Exception:
        return None
    return names if all(names) else None


def _configured_month_names(lang: str) -> list | None:
    """Return month names from BLOGCAST_MONTHS_<LANG>, or None."""
    configured = os.getenv(f"BLOGCAST_MONTHS_{lang.upper()}", "").strip()
    if not configured:
        return None
    names = [n.strip() for n in configured.split(",")]
    if len(names) == 12 and all(names):
        return names
    print(f"\u26a0\ufe0f  BLOGCAST_MONTHS_{lang.upper()} needs exactly twelve "
          f"comma-separated names; ignoring it.")
    return None


def _configured_date_format(lang: str) -> str | None:
    """Return the word order from BLOGCAST_DATE_FORMAT_<LANG>, or None."""
    configured = os.getenv(f"BLOGCAST_DATE_FORMAT_{lang.upper()}", "").strip()
    if not configured:
        return None
    if _invalid_placeholders(configured, {"day", "month", "year"}):
        raise SystemExit(
            f"BLOGCAST_DATE_FORMAT_{lang.upper()} may only use "
            f"{{day}}, {{month}} and {{year}}."
        )
    return configured


BABEL_LOCALES = {lang: _babel_locale(lang) for lang in LANGUAGES}
CLDR_MONTHS = {lang: _babel_month_names(BABEL_LOCALES[lang])
               for lang in LANGUAGES}
MONTH_OVERRIDES = {lang: _configured_month_names(lang) for lang in LANGUAGES}
DATE_FORMATS = {lang: _configured_date_format(lang) for lang in LANGUAGES}

for _lang in LANGUAGES:
    if BABEL_LOCALES[_lang] is None and not MONTH_OVERRIDES[_lang]:
        print(f"\u2139\ufe0f  No date formatting data for '{_lang}'; dates will be "
              f"spoken as numbers. Set BLOGCAST_MONTHS_{_lang.upper()} to "
              f"change that.")


def format_spoken_date(iso_date: str, lang: str) -> str:
    """Format an ISO date for speech, e.g. "29. kesakuuta 2026".

    Babel provides both names and word order. An explicit date format is
    applied directly; custom month names alone are substituted into Babel's
    output so the language's own punctuation and order are preserved.
    """
    try:
        dt = datetime.fromisoformat(iso_date)
    except Exception:
        return ""

    custom_months = MONTH_OVERRIDES.get(lang)
    template = DATE_FORMATS.get(lang)
    locale = BABEL_LOCALES.get(lang)
    cldr_months = CLDR_MONTHS.get(lang)

    if template:
        names = custom_months or cldr_months
        month = names[dt.month - 1] if names else str(dt.month)
        return template.format(day=dt.day, month=month, year=dt.year)

    if locale is not None:
        try:
            spoken = format_date(dt.date(), format="long", locale=locale)
        except Exception:
            spoken = ""
        if spoken:
            if custom_months and cldr_months:
                # Swap in the custom name so the locale's own word order and
                # punctuation survive.
                spoken = spoken.replace(cldr_months[dt.month - 1],
                                        custom_months[dt.month - 1])
            return spoken

    if custom_months:
        return GENERIC_DATE_FORMAT.format(day=dt.day,
                                          month=custom_months[dt.month - 1],
                                          year=dt.year)
    return NUMERIC_DATE_FORMAT.format(day=dt.day, month=dt.month, year=dt.year)


# --- Spoken intro and outro -------------------------------------------------
# Per-language templates come from BLOGCAST_INTRO_<LANG> / BLOGCAST_OUTRO_<LANG>,
# falling back to the language-neutral BLOGCAST_INTRO / BLOGCAST_OUTRO and then
# to the built-in wording. Setting a variable to an empty value disables that
# part of the episode.


def _load_template(kind: str, lang: str, builtin: dict, generic: str) -> str:
    """Resolve one intro/outro template and validate its placeholders."""
    for name in (f"BLOGCAST_{kind}_{lang.upper()}", f"BLOGCAST_{kind}"):
        value = os.getenv(name)
        if value is None:
            continue
        value = value.strip()
        if not value:
            return ""  # explicitly disabled
        if _invalid_placeholders(value, {"blog", "date", "title"}):
            raise SystemExit(
                f"{name} may only use {{blog}}, {{date}} and {{title}}."
            )
        return value
    return builtin.get(lang, generic)


INTRO_TEMPLATES = {lang: _load_template("INTRO", lang, BUILTIN_INTRO,
                                        GENERIC_INTRO)
                   for lang in LANGUAGES}
OUTRO_TEMPLATES = {lang: _load_template("OUTRO", lang, BUILTIN_OUTRO, "")
                   for lang in LANGUAGES}


def _log_date(iso_date: str) -> str:
    """Format an ISO date compactly for logs, e.g. "11.9.2026".

    Returns "?" when the date is missing or unparseable.
    """
    try:
        dt = datetime.fromisoformat(iso_date)
        return f"{dt.day}.{dt.month}.{dt.year}"
    except Exception:
        return "?"


PUBLIC_BASE_URL = os.getenv(
    "BLOGCAST_BASE_URL",
    "http://localhost:8000"
)

# --- Podcast feed metadata --------------------------------------------------
FEED_TITLE = os.getenv(
    "BLOGCAST_FEED_TITLE",
    "BlogCast"
)

FEED_AUTHOR = os.getenv(
    "BLOGCAST_FEED_AUTHOR",
    "BlogCast User"
)

FEED_DESCRIPTION = os.getenv(
    "BLOGCAST_FEED_DESCRIPTION",
    "Turning blog posts into podcast episodes"
)
# Channel artwork. When blogcast_data/assets/podcast_logo.png exists it is used
# as the feed logo; otherwise the first source's own image is used.
FEED_LOGO_FILE = ASSET_DIR / "podcast_logo.png"
FEED_LOGO_URL_PATH = "/assets/podcast_logo.png"   # path used by the server

# Generic artwork for PDF episodes, which have no per-source logo. Optional:
# without the file, PDF episodes simply carry no itunes:image tag.
PDF_ICON_FILE = ASSET_DIR / "pdf_icon.png"
PDF_ICON_URL_PATH = "/assets/pdf_icon.png"

# --- Optional external PDF folder and configured blog URLs ------------------
# Docker Compose mounts BLOGCAST_PDF_PATH at /external-pdf-drop when PDF
# processing is enabled. If the variable is empty, PDF processing is disabled.
PDF_DROP_SLUG = "pdf"
PDF_DROP_NAME = "PDF"
PDF_DROP_DIR = Path("/external-pdf-drop")
PDF_DROP_CONFIGURED = bool(os.getenv("BLOGCAST_PDF_PATH", "").strip())

# One environment variable may contain multiple blog URLs separated by commas
# or line breaks. Duplicate URLs are ignored while preserving their order.
BLOG_URLS_RAW = os.getenv("BLOGCAST_BLOG_URLS", "")


def configured_blog_urls() -> list[str]:
    """Return unique blog URLs configured through BLOGCAST_BLOG_URLS."""
    urls = []
    seen = set()
    for value in re.split(r"[,\r\n]+", BLOG_URLS_RAW):
        url = value.strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls



def _cache_bust(path: Path) -> str:
    """Return the file mtime for use as a cache-busting URL parameter.

    The timestamp changes whenever the file is overwritten, so podcast apps
    fetch the new artwork instead of a cached copy, with no manual version
    number to maintain."""
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return "0"


def _pdf_icon_url() -> str:
    """Return the generic PDF icon URL, or "" when the asset does not exist.

    Without it, PDF episodes simply carry no itunes:image tag."""
    if PDF_ICON_FILE.exists():
        return f"{PUBLIC_BASE_URL}{PDF_ICON_URL_PATH}?v={_cache_bust(PDF_ICON_FILE)}"
    return ""


# --- Chime ------------------------------------------------------------------
# The chime is optional: episodes are rendered without it when the asset is
# not present in ASSET_DIR.

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REQUEST_TIMEOUT = 30


# --- HTTP and state helpers -------------------------------------------------

def _http_get(url: str, as_json: bool = False):
    import requests
    r = requests.get(url, headers={"User-Agent": USER_AGENT,
                                   "Accept": "application/json, text/xml, */*"},
                     timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return (r.json(), r.headers) if as_json else (r.text, r.headers)


def load_state() -> dict:
    if FEEDS_FILE.exists():
        return json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
    return {"blogs": {}}


def save_state(state: dict) -> None:
    """Write state atomically (temp file + rename).

    State is saved after every episode, so an atomic write prevents a
    concurrent reader from seeing a half-written JSON file.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FEEDS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FEEDS_FILE)


def ensure_pdf_source_registered(state: dict) -> None:
    """Register the mounted PDF folder automatically when it is configured."""
    if not PDF_DROP_CONFIGURED:
        return

    existing = state["blogs"].get(PDF_DROP_SLUG)
    if existing:
        existing["folder"] = str(PDF_DROP_DIR)
        return

    state["blogs"][PDF_DROP_SLUG] = {
        "name": PDF_DROP_NAME,
        "slug": PDF_DROP_SLUG,
        "kind": "pdf",
        "folder": str(PDF_DROP_DIR),
        "voices": dict(DEFAULT_VOICES),
        "intro": True,
        "outro": True,
        "chime": True,
        "active": True,
        "seen_guids": [],
        "episodes": [],
    }
    print(f"📂 PDF source registered automatically: {PDF_DROP_DIR}")


def slugify(text: str, maxlen: int = 60) -> str:
    text = text.lower()
    for k, v in {"ä": "a", "ö": "o", "å": "a"}.items():
        text = text.replace(k, v)
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:maxlen] or "post"


def episode_relpath(blog_slug: str, date: str, title: str) -> str:
    """Return a deterministic audio path so re-runs can skip existing files."""
    fname = f"{(date or '')[:10]}_{slugify(title)}.mp3".lstrip("_")
    return f"{blog_slug}/{fname}"


# --- Language detection ------------------------------------------------------

# Stopwords are only used when langdetect is unavailable or inconclusive.
# Languages without a table simply skip the heuristic.
STOPWORDS = {
    "fi": {"ja", "on", "ei", "että", "tämä", "mutta", "kuin", "olla", "hän",
           "niin", "joka", "koska", "ovat", "sekä", "myös", "kun", "vain",
           "jos", "sitä", "ne", "se", "mikä", "meidän", "voi", "nyt", "tai"},
    "en": {"the", "and", "of", "to", "in", "is", "that", "it", "for", "was",
           "with", "as", "on", "are", "this", "be", "at", "by", "an", "have",
           "not", "or", "from", "but", "they", "you", "we", "can", "will"},
}


def detect_language(text: str) -> str:
    """Return the configured language that best matches the text.

    Only languages in LANGUAGES are ever returned, so a detector hit on a
    language this instance has no voice for falls through to FALLBACK_LANG.
    """
    if len(LANGUAGES) == 1:
        return LANGUAGES[0]

    sample = text[:2000]
    try:
        from langdetect import detect_langs
        for guess in detect_langs(sample):
            code = str(guess.lang).split("-")[0].lower()
            if code in LANGUAGES:
                return code
    except Exception:
        pass

    words = re.findall(r"[^\W\d_]+", sample.lower(), flags=re.UNICODE)
    if not words:
        return FALLBACK_LANG
    scores = {lang: sum(1 for w in words if w in STOPWORDS[lang])
              for lang in LANGUAGES if lang in STOPWORDS}
    if "fi" in scores and re.search(r"[äöå]", sample):
        scores["fi"] += 3
    if scores:
        best = max(scores.values())
        winners = [lang for lang, score in scores.items() if score == best]
        if best > 0 and len(winners) == 1:
            return winners[0]
    return FALLBACK_LANG


def resolve_voice(lang: str, prefs: dict) -> tuple[str, str]:
    """Return (display_name, voice_id) for a language, honouring source prefs."""
    for candidate in (lang, FALLBACK_LANG):
        resolved = _resolve_voice_setting(candidate, prefs.get(candidate, ""))
        if resolved:
            return resolved
        if candidate in VOICE_IDS:
            return DEFAULT_VOICES[candidate], VOICE_IDS[candidate]
    return DEFAULT_VOICES[FALLBACK_LANG], VOICE_IDS[FALLBACK_LANG]


# --- Text normalisation for TTS ---------------------------------------------

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF\uFE0F]",
    flags=re.UNICODE)

_DECO = r"\*\-\=\_\~\#\•\·\—\–\+\.\|\^"


def normalize_for_tts(text: str) -> str:
    t = html.unescape(text)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\bwww\.\S+", " ", t)
    t = re.sub(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", r"\2", t)
    t = _EMOJI.sub(" ", t)
    t = re.sub(rf"(?m)^[\s{_DECO}]{{3,}}$", " . ", t)
    t = re.sub(r"([^\w\s])\1{2,}", r"\1", t)
    t = re.sub(rf"[{_DECO}]{{2,}}", " ", t)
    t = re.sub(r"(?<=\s)[\*\#\_\~\=\|\•\·\+\<\>\^`]+(?=\s)", " ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s*\n\s*\n\s*", "\n\n", t)
    t = re.sub(r" *\. *(?: *\. *)+", ". ", t)
    return t.strip()


# --- Source discovery -------------------------------------------------------

def site_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _extract_image_from_html(html_text: str, base: str) -> str:
    """Return the best image in a page: og:image, apple-touch-icon, then icon."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    soup = BeautifulSoup(html_text or "", "html.parser")

    og = soup.find("meta", attrs={"property": "og:image"}) \
        or soup.find("meta", attrs={"name": "og:image"})
    if og and og.get("content"):
        return urljoin(base, og["content"])

    for link in soup.find_all("link", rel=True):
        rels = " ".join(link.get("rel", [])).lower()
        if ("apple-touch-icon" in rels or "icon" in rels) and link.get("href"):
            return urljoin(base, link["href"])
    return ""


def discover_blog_image(root: str, wp_info: dict | None) -> str:
    """Return the source's artwork URL, falling back to /favicon.ico."""
    if wp_info:
        for key in ("site_icon_url", "site_logo_url", "site_logo"):
            val = wp_info.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val
    try:
        html_text, _ = _http_get(root)
        img = _extract_image_from_html(html_text, root)
        if img:
            return img
    except Exception:
        pass
    return f"{root}/favicon.ico"


def discover_source(url: str) -> dict:
    root = site_root(url)
    wp_api = f"{root}/wp-json/wp/v2"
    feed_url = f"{root}/feed/"
    name = urlparse(url).netloc
    is_wp = False
    wp_info = None
    try:
        info, _ = _http_get(f"{root}/wp-json", as_json=True)
        wp_info = info
        name = info.get("name") or name
        is_wp = "/wp-json" in json.dumps(info.get("routes", {})) or \
                any("/wp/v2/posts" in r for r in info.get("routes", {}))
    except Exception:
        pass
    image_url = discover_blog_image(root, wp_info)
    return {"name": name, "site_root": root, "feed_url": feed_url,
            "wp_api": wp_api if is_wp else None, "is_wordpress": bool(is_wp),
            "image_url": image_url}


# --- Backfill (WordPress REST API) ------------------------------------------

def _fetch_wp_content_type(wp_api: str, content_type: str,
                           after: str | None = None) -> list[dict]:
    """Fetch every item of one WordPress content type, paging the whole archive."""
    posts, page = [], 1
    while True:
        url = (f"{wp_api}/{content_type}?per_page=100&page={page}"
               f"&orderby=date&order=asc&_fields=id,date_gmt,link,title,content,guid")
        if after:
            url += f"&after={after}"
        try:
            data, headers = _http_get(url, as_json=True)
        except Exception as e:
            if "400" in str(e) or "invalid_page_number" in str(e):
                break
            raise
        if not data:
            break
        for p in data:
            posts.append({
                "guid": (p.get("guid", {}).get("rendered")
                         or p.get("link") or str(p.get("id"))),
                "url": p.get("link"),
                "title": html.unescape(re.sub(r"<[^>]+>", "",
                          p.get("title", {}).get("rendered", "")).strip()),
                "html": p.get("content", {}).get("rendered", ""),
                "date": p.get("date_gmt")})
        total_pages = int(headers.get("X-WP-TotalPages", page))
        if page >= total_pages:
            break
        page += 1
        time.sleep(0.3)
    return posts


def fetch_all_wp_posts(wp_api: str, after: str | None = None) -> list[dict]:
    """Fetch all WordPress content, preferring posts and falling back to pages.

    Some sites build their entire content as pages, so an empty "posts"
    collection triggers a retry against "pages".
    """
    posts = _fetch_wp_content_type(wp_api, "posts", after=after)
    if posts:
        return posts

    print("    ℹ️  No posts found; trying WordPress pages instead...")
    return _fetch_wp_content_type(wp_api, "pages", after=after)


# --- Incremental updates (RSS) ----------------------------------------------

def fetch_rss_items(feed_url: str) -> list[dict]:
    import xml.etree.ElementTree as ET
    xml_text, _ = _http_get(feed_url)
    root = ET.fromstring(xml_text)
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    items = []
    for it in root.iterfind(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        pub = it.findtext("pubDate")
        content = it.findtext("content:encoded", default="", namespaces=ns) \
            or it.findtext("description") or ""
        items.append({"guid": guid, "url": link, "title": html.unescape(title),
                      "html": content, "date": _rfc822_to_iso(pub)})
    return items


def _rfc822_to_iso(pub: str | None) -> str:
    if not pub:
        return datetime.now(timezone.utc).isoformat()
    try:
        return parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _new_only_cutoff() -> str:
    """Return a UTC cutoff accepted by the WordPress REST API."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def _filter_new_only_posts(blog: dict, posts: list[dict]) -> list[dict]:
    """Keep only items published after a source's new-only cutoff."""
    cutoff_value = blog.get("added_after")
    if not cutoff_value:
        return posts
    try:
        cutoff = datetime.fromisoformat(cutoff_value.replace("Z", "+00:00"))
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"    ⚠️  Invalid new-only cutoff for {blog.get('name', 'source')}; "
              "skipping this update.")
        return []

    baseline = set(blog.get("new_only_baseline_guids", []))
    new_posts = []
    for post in posts:
        if post.get("guid") in baseline:
            continue
        try:
            published = datetime.fromisoformat(
                post.get("date", "").replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if published > cutoff:
            new_posts.append(post)
    return new_posts


# --- PDF folder source ------------------------------------------------------
# PDFs dropped into the mounted folder become episodes. Every update rescans the
# whole folder; process_posts() keeps the run idempotent.

# Some PDF tools write a generic placeholder into the Title metadata. Those are
# treated as missing titles and the filename is used instead.
_PDF_PLACEHOLDER_TITLES = {
    "untitled", "untitled document", "untitled1", "document", "document1",
    "new document", "microsoft word - document1",
}

# --- PDF hyphenation repair -------------------------------------------------
# pypdf returns lines verbatim, so a word hyphenated across a line break keeps
# its hyphen and newline (e.g. "Ta-\nvoitteena"), which TTS reads as a pause
# mid-word. Blog HTML never has this pattern, so the fix is PDF-only.

# A soft hyphen is never a readable character; always drop it.
_PDF_SOFT_HYPHEN = "\u00ad"

# Hyphen + newline followed by a lowercase letter is almost always one split
# word: drop the hyphen and join ("Ta-\nvoitteena" -> "Tavoitteena").
_PDF_HYPHEN_LOWER_RE = re.compile(
    r"(?<=[A-Za-zÄÖÅäöå])-\s*\n\s*(?=[a-zäöå])"
)

# Followed by an uppercase letter it is more likely a real compound that merely
# broke at the line end: keep the hyphen, drop the break ("Etela-Suomi").
_PDF_HYPHEN_UPPER_RE = re.compile(
    r"(?<=[A-Za-zÄÖÅäöå])-\s*\n\s*(?=[A-ZÄÖÅ])"
)


def dehyphenate_pdf_text(text: str) -> str:
    """Repair hyphenation artefacts from PDF text extraction.

    Heuristic, not grammatical analysis: it handles the vast majority of cases,
    but a line ending in a dash followed by a lowercase word could in principle
    be joined incorrectly. Applied to PDF text only, never to blog HTML.
    """
    if not text:
        return text
    text = text.replace(_PDF_SOFT_HYPHEN, "")
    text = _PDF_HYPHEN_LOWER_RE.sub("", text)
    text = _PDF_HYPHEN_UPPER_RE.sub("-", text)
    return text


# --- PDF citation removal ---------------------------------------------------
# Academic texts carry many parenthetical citations, e.g. "(Perrone et al.,
# 2015)". These sound odd in speech and break sentence rhythm, so they are
# stripped on the PDF path.
#
# Heuristic: a parenthesised span is a citation when it contains a four-digit
# year (1500-2099). That covers the common citation styles, while ordinary
# parentheses without a year are left untouched.
_PDF_CITATION_YEAR_RE = re.compile(r"\s?\(([^()]*\b(?:1[5-9]|20)\d{2}\b[^()]*)\)")


def strip_pdf_citations(text: str) -> str:
    """Strip academic citations from PDF text (see heuristic above).

    PDF-only, because ordinary blog prose rarely uses this convention. In the
    rare case a sentence opens with a citation, removal leaves it starting
    lowercase, which does not affect speech synthesis.
    """
    if not text:
        return text
    prev = None
    while prev != text:
        prev = text
        text = _PDF_CITATION_YEAR_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text.strip()


# --- PDF page number / header / footer removal ------------------------------
# pypdf returns the entire page including headers and footers, so page numbers
# end up as the first or last lines of a page and would otherwise be read aloud
# mid-sentence at every page break.
#
# Heuristic: a line counts as a page number only when the whole trimmed line is
# a number or a common page pattern ("7", "Page 7 of 99", "Sivu 7", "- 7 -").
# Only the first and last few lines of each page are checked (max_scan), so a
# standalone number inside a table or sentence is never affected.
_PDF_PAGE_NUM_LINE_RE = re.compile(
    r"""^(?:
        \d{1,4}
        |page\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?
        |sivu\s*\d{1,4}(?:\s*/\s*\d{1,4})?
        |[-\u2013\u2014]\s*\d{1,4}\s*[-\u2013\u2014]
        |\d{1,4}\s*/\s*\d{1,4}
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def _strip_pdf_page_number_lines(raw_page_text: str, max_scan: int = 5) -> str:
    """Strip page-number lines and surrounding blanks from a single page.

    Applied per page before pages are joined, because the position of a line on
    its own page is the only reliable signal.
    """
    if not raw_page_text:
        return raw_page_text
    lines = raw_page_text.split("\n")

    start = 0
    while start < len(lines) and start < max_scan:
        candidate = lines[start].strip()
        if candidate == "" or _PDF_PAGE_NUM_LINE_RE.match(candidate):
            start += 1
        else:
            break

    end = len(lines)
    scanned = 0
    while end > start and scanned < max_scan:
        candidate = lines[end - 1].strip()
        if candidate == "" or _PDF_PAGE_NUM_LINE_RE.match(candidate):
            end -= 1
            scanned += 1
        else:
            break

    return "\n".join(lines[start:end])


def extract_pdf_text(pdf_path: Path) -> tuple[str, str]:
    """Return (title, body) for a PDF.

    The title comes from PDF metadata and is empty when it is missing or a
    generic placeholder; callers then fall back to the filename. Page numbers
    are stripped per page before joining, after which the body is de-hyphenated
    and citations removed, so TTS never reads anything outside the actual text.
    """
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))

    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            candidate = reader.metadata.title.strip()
            if candidate and candidate.lower() not in _PDF_PLACEHOLDER_TITLES:
                title = candidate
    except Exception:
        pass

    pages_text = []
    for page in reader.pages:
        try:
            t = (page.extract_text() or "").strip()
        except Exception:
            t = ""
        if t:
            t = _strip_pdf_page_number_lines(t)
        if t:
            pages_text.append(t)
    body = dehyphenate_pdf_text("\n\n".join(pages_text))
    body = strip_pdf_citations(body)
    return title, body


def _pdf_body_to_html(body: str) -> str:
    """Wrap PDF paragraphs in <p> tags before the shared HTML text extractor.

    process_posts() routes every source through extract_body_text(), whose
    _html_to_plain_text() collapses all whitespace inside untagged text nodes.
    Passing raw PDF text would therefore flatten the whole document into one
    pause-free block; wrapping paragraphs makes them block-level elements that
    keep their breaks.

    Paragraph detection: two or more consecutive newlines start a new
    paragraph, while a single newline (ordinary PDF line wrapping) is treated
    as a space so sentences are not broken up.
    """
    if not body:
        return ""

    # Numbered sub-headings ("2.1 Title", "4.3.2 More freedom") are common in
    # academic PDFs and are usually followed by a single newline rather than a
    # blank line, so without this they would merge into the next paragraph.
    # Requiring an uppercase first word after the numbering is what separates a
    # real heading from an ordinary sentence starting with a number.
    body = re.sub(
        r"(?m)^(\d{1,2}(?:\.\d{1,3}){0,3}\.?\s+[A-ZÄÖÅ].{0,90})[ \t]*\n(?!\n)",
        r"\1\n\n",
        body,
    )

    raw_paragraphs = re.split(r"\n\s*\n", body)
    parts = []
    for raw_para in raw_paragraphs:
        collapsed = re.sub(r"\s+", " ", raw_para).strip()
        if collapsed:
            parts.append(f"<p>{html.escape(collapsed)}</p>")
    return "".join(parts)


def _pdf_title_from_filename(pdf_path: Path) -> str:
    """Build a readable title from a filename when PDF metadata has none."""
    stem = re.sub(r"[_\-]+", " ", pdf_path.stem).strip()
    return stem or "PDF-file"


# Minimum file age before a PDF is processed. When the folder is an external
# sync target, a file may still be uploading; reading it early would permanently
# synthesise a truncated episode, since episodes are keyed by filename and would
# not be regenerated later (except with --force).
PDF_MIN_FILE_AGE_SECONDS = 120

# How long the PDF folder may stay missing before the source is disabled
# automatically (see cmd_update). The grace period avoids dropping the source
# because of a temporary mount issue, e.g. a sync that has not finished after a
# reboot.
PDF_FOLDER_MISSING_GRACE_HOURS = 24


def fetch_pdf_folder_items(folder: Path) -> list[dict]:
    """Return every PDF in the folder in the same shape as RSS/WordPress items.

    The full folder is always returned; process_posts() handles idempotency.
    "url" is left empty because a local PDF has no public link. Files younger
    than PDF_MIN_FILE_AGE_SECONDS are skipped until the next run, and the
    extension check is case-insensitive so ".PDF" is not silently ignored on
    case-sensitive filesystems.

    A scan summary is always printed so mount problems are visible in the
    Docker log without shelling into the container.
    """
    items = []
    if not folder.exists():
        print(f"    ⚠️  PDF folder not found: {folder} "
              f"-- check the volume mount in docker-compose.yml.")
        return items

    all_pdf_paths = sorted(
        p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
    )
    print(f"    📂 PDF folder {folder}: found {len(all_pdf_paths)} PDF files.")

    for pdf_path in all_pdf_paths:
        try:
            age_seconds = time.time() - pdf_path.stat().st_mtime
        except OSError:
            continue
        if age_seconds < PDF_MIN_FILE_AGE_SECONDS:
            print(f"    ⏳ Skipping for now; the file may still be transferring: "
                  f"{pdf_path.name} -- it will be retried during the next update.")
            continue

        rel = pdf_path.relative_to(folder).as_posix()
        try:
            meta_title, body = extract_pdf_text(pdf_path)
        except Exception as e:
            print(f"    ⚠️  Failed to read PDF ({pdf_path.name}): {e}")
            continue

        title = meta_title if meta_title else _pdf_title_from_filename(pdf_path)
        mtime = datetime.fromtimestamp(pdf_path.stat().st_mtime, tz=timezone.utc)

        items.append({
            "guid": f"pdf:{rel}",
            "url": "",                    # no link: local file
            "title": title,
            "date": mtime.isoformat(),
            # _pdf_body_to_html (not plain html.escape) keeps paragraph
            # boundaries as <p> tags; see that function for details.
            "html": _pdf_body_to_html(body),
        })
    return items


# --- Text cleanup and spoken form -------------------------------------------

# Block-level elements produce a line break when text is extracted. All other
# elements (<a>, <em>, <strong>, <span>) are inline and must not be separated,
# otherwise TTS would insert a short pause around e.g. a link's text.
_BLOCK_LEVEL_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "header", "footer", "tr", "table",
}


def _html_to_plain_text(soup) -> str:
    """Extract text, breaking lines only on block-level elements.

    Inline elements merge seamlessly into the surrounding text. Newlines that
    exist in the HTML source purely for readability are collapsed inside each
    text node, matching how a browser would render them.
    """
    parts: list[str] = []
    for node in soup.descendants:
        if isinstance(node, str):
            # Collapse runs of whitespace into a single space, as a browser
            # would.
            parts.append(re.sub(r"\s+", " ", str(node)))
        elif getattr(node, "name", None) in _BLOCK_LEVEL_TAGS:
            parts.append("\n")
    return "".join(parts)


def extract_body_text(raw_html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for tag in soup(["script", "style", "figure", "figcaption", "aside",
                     "form", "nav"]):
        tag.decompose()
    return normalize_for_tts(_html_to_plain_text(soup))


def build_spoken_text(title: str, body: str, lang: str, blog_name: str,
                      date: str = "", add_intro: bool = True,
                      add_outro: bool = True) -> str:
    title = normalize_for_tts(title) or "Untitled post"
    body = body or ""

    if add_intro and INTRO_TEMPLATES.get(lang, INTRO_TEMPLATES[FALLBACK_LANG]):
        tmpl = INTRO_TEMPLATES.get(lang, INTRO_TEMPLATES[FALLBACK_LANG])
        pvm = format_spoken_date(date, lang)
        alku = tmpl.format(blog=blog_name, date=pvm, title=title)
        alku = re.sub(r"\.\s*\.\s*", ". ", alku).strip()
        parts = [alku, "", body]
    else:
        parts = [f"{title}.", "", body]

    if add_outro:
        tmpl = OUTRO_TEMPLATES.get(lang, OUTRO_TEMPLATES[FALLBACK_LANG])
        if tmpl:
            parts += [tmpl.format(blog=blog_name, title=title)]

    spoken = "\n\n".join(p for p in parts if p.strip())
    return re.sub(r"\n{3,}", "\n\n", spoken).strip()


# --- TTS and chime ----------------------------------------------------------

async def _tts_save(text: str, out_path: Path, voice_id: str,
                    rate: str = "+0%", pitch: str = "+0Hz") -> None:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=voice_id, rate=rate, pitch=pitch)
    await communicate.save(str(out_path))


def render_episode(text: str, out_path: Path, voice_id: str,
                   add_chime: bool) -> None:
    """Synthesise an episode and append the chime when add_chime is set.

    If the optional chime asset is missing, render the speech without it.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if add_chime and not CHIME_FILE.exists():
        print(f"    ℹ️  Chime file not found; rendering without it: {CHIME_FILE}")
        add_chime = False
    if not add_chime:
        asyncio.run(_tts_save(text, out_path, voice_id))
        return

    from pydub import AudioSegment
    tmp = out_path.with_suffix(".speech.mp3")
    asyncio.run(_tts_save(text, tmp, voice_id))
    speech = AudioSegment.from_file(tmp)
    chime = AudioSegment.from_file(CHIME_FILE)
    final = speech + AudioSegment.silent(duration=350) + chime
    final.export(out_path, format="mp3", bitrate="128k")
    try:
        tmp.unlink()
    except OSError:
        pass


def mp3_duration_seconds(path: Path, char_count: int) -> int:
    try:
        from mutagen.mp3 import MP3
        return int(MP3(str(path)).info.length)
    except Exception:
        return max(1, char_count // 15)


# --- Podcast feed -----------------------------------------------------------

def build_podcast_feed(state: dict) -> str:
    episodes = []
    for blog in state["blogs"].values():
        for ep in blog.get("episodes", []):
            episodes.append((blog, ep))
    episodes.sort(key=lambda be: be[1].get("date", ""), reverse=True)

    now = format_datetime(datetime.now(timezone.utc))
    if FEED_LOGO_FILE.exists():
        channel_img = f"{PUBLIC_BASE_URL}{FEED_LOGO_URL_PATH}?v={_cache_bust(FEED_LOGO_FILE)}"
    else:
        channel_img = next((b.get("image_url") for b in state["blogs"].values()
                            if b.get("image_url")), "")

    items = []
    for blog, ep in episodes:
        blog_name = blog["name"]
        try:
            pub = format_datetime(datetime.fromisoformat(ep.get("date")))
        except Exception:
            pub = now
        url = f"{PUBLIC_BASE_URL}/audio/{ep['relpath']}"
        lang = ep.get("lang", FALLBACK_LANG)
        ep_title = f"{blog_name} - {ep['title']}"
        img = blog.get("image_url", "")
        if not img and blog.get("kind") == "pdf":
            img = _pdf_icon_url()
        img_tag = f'\n      <itunes:image href="{_xe(img)}"/>' if img else ""

        # Show notes: a clickable link to the original post. The same HTML is
        # placed in both <description> and <content:encoded> because clients
        # differ in which one they display.
        post_url = ep.get("url", "")
        link_label = _xe(ep_title)
        link_html = f'<a href="{_xe(post_url)}">{link_label}</a>' if post_url else link_label
        shownotes_html = f"<p>{link_html}</p>"

        items.append(f"""    <item>
      <title>{_xe(ep_title)}</title>
      <description><![CDATA[{shownotes_html}]]></description>
      <content:encoded><![CDATA[{shownotes_html}]]></content:encoded>
      <link>{_xe(post_url)}</link>
      <guid isPermaLink="false">{_xe(ep['guid'])}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url="{_xe(url)}" length="{ep.get('bytes', 0)}" type="audio/mpeg"/>
      <itunes:author>{_xe(blog_name)}</itunes:author>
      <itunes:subtitle>{_xe(lang)}</itunes:subtitle>
      <itunes:duration>{ep.get('duration', 0)}</itunes:duration>{img_tag}
    </item>""")

    channel_img_xml = ""
    if channel_img:
        channel_img_xml = (
            f'\n    <itunes:image href="{_xe(channel_img)}"/>'
            f'\n    <image><url>{_xe(channel_img)}</url>'
            f'<title>{_xe(FEED_TITLE)}</title>'
            f'<link>{PUBLIC_BASE_URL}</link></image>')

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{_xe(FEED_TITLE)}</title>
    <link>{PUBLIC_BASE_URL}</link>
    <language>{FALLBACK_LANG}</language>
    <description>{_xe(FEED_DESCRIPTION)}</description>
    <itunes:author>{_xe(FEED_AUTHOR)}</itunes:author>
    <itunes:category text="Personal Journals"/>{channel_img_xml}
    <lastBuildDate>{now}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>
"""


def _xe(s: str) -> str:
    return html.escape(s or "", quote=True)


def write_podcast_feed(state: dict) -> None:
    """Write podcast.xml atomically (temp file + rename).

    The feed is rewritten after every episode, so an atomic write prevents a
    client from reading a half-written XML document.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FEED_OUT.with_suffix(".xml.tmp")
    tmp.write_text(build_podcast_feed(state), encoding="utf-8")
    tmp.replace(FEED_OUT)


# --- Episode processing -----------------------------------------------------

def process_posts(blog: dict, posts: list[dict], force: bool = False,
                  on_progress=None) -> dict:
    """Render new posts to MP3 and return a summary.

    Skip logic: when the audio file already exists and force is False, the text
    is not fetched, cleaned or synthesised again; a missing state entry is
    backfilled instead.

    on_progress: optional callback invoked as soon as each episode is ready, so
    callers can persist state and the feed incrementally. Episodes then appear
    in the podcast app as they finish rather than after the whole run.
    """
    seen = set(blog.setdefault("seen_guids", []))
    blog.setdefault("episodes", [])
    known_relpaths = {e["relpath"] for e in blog["episodes"]}
    prefs = blog.get("voices", {})
    add_intro = blog.get("intro", True)
    add_outro = blog.get("outro", True)
    add_chime = blog.get("chime", True)
    blog_slug = blog["slug"]

    stats = {"new": 0, "skipped_seen": 0, "skipped_exists": 0}

    for post in posts:
        title = post["title"] or "Untitled post"
        relpath = episode_relpath(blog_slug, post.get("date", ""), title)
        out_path = AUDIO_DIR / relpath
        file_exists = out_path.exists()

        if not force and post["guid"] in seen and relpath in known_relpaths and file_exists:
            stats["skipped_seen"] += 1
            continue

        if file_exists and not force:
            stats["skipped_exists"] += 1
            print(f"    ⏭️  Skipping; audio already exists: {relpath}")
            if relpath not in known_relpaths:
                blog["episodes"].append({
                    "guid": post["guid"], "title": title,
                    "url": post.get("url", ""),
                    "date": post.get("date") or datetime.now(timezone.utc).isoformat(),
                    "relpath": relpath, "bytes": out_path.stat().st_size,
                    "lang": detect_language(title),
                    "voice": "?",
                    "duration": mp3_duration_seconds(out_path, len(title))})
                known_relpaths.add(relpath)
                seen.add(post["guid"])
                blog["seen_guids"] = sorted(seen)
                if on_progress:
                    on_progress()
                continue
            seen.add(post["guid"])
            continue

        body = extract_body_text(post["html"])
        if not body.strip():
            # No usable text was retrieved (empty feed item, extraction
            # failure, paywall...). Skip without rendering audio and without
            # marking the post as seen, so it is retried on the next update
            # once the source returns real content.
            print(f"    ⚠️  No text content; skipping: "
                  f"{blog['name']} - {title[:46]}")
            continue

        lang = detect_language(f"{title}. {body}")
        voice_name, voice_id = resolve_voice(lang, prefs)
        text = build_spoken_text(title, body, lang, blog["name"],
                                 date=post.get("date", ""),
                                 add_intro=add_intro, add_outro=add_outro)

        tag = "🔁 regenerated" if (out_path.exists() and force) else "🎙️ new"
        log_date = _log_date(post.get("date", ""))
        print(f"    {tag} {log_date} {blog['name']} - {title[:46]}")
        render_episode(text, out_path, voice_id, add_chime)

        size = out_path.stat().st_size if out_path.exists() else 0
        blog["episodes"] = [e for e in blog["episodes"] if e["relpath"] != relpath]
        blog["episodes"].append({
            "guid": post["guid"], "title": title, "url": post.get("url", ""),
            "date": post.get("date") or datetime.now(timezone.utc).isoformat(),
            "relpath": relpath, "bytes": size, "lang": lang, "voice": voice_name,
            "duration": mp3_duration_seconds(out_path, len(text))})
        known_relpaths.add(relpath)
        seen.add(post["guid"])
        stats["new"] += 1
        if on_progress:
            on_progress()

    blog["seen_guids"] = sorted(seen)
    return stats


# --- Commands ---------------------------------------------------------------

def _feed_writer(state: dict):
    """Return a callback that saves state and rewrites the feed.

    Passed to process_posts() as on_progress so episodes reach the feed as they
    are completed rather than at the end of a long run.
    """
    def _write():
        save_state(state)
        write_podcast_feed(state)
    return _write


def find_blog(state: dict, ident: str):
    blogs = state.get("blogs", {})
    if ident in blogs:
        return ident, blogs[ident]
    tark = ident.lower().rstrip("/")
    domain = urlparse(ident).netloc.lower() if "//" in ident else tark
    for slug, blog in blogs.items():
        kentat = [slug.lower(), blog.get("name", "").lower(),
                  blog.get("site_root", "").lower(), blog.get("feed_url", "").lower()]
        if any(domain and domain in k for k in kentat) or \
           any(tark and tark in k for k in kentat):
            return slug, blog
    return None, None


def cmd_remove(args):
    state = load_state()
    slug, blog = find_blog(state, args.blog)
    if not blog:
        print(f"❌ Blog not found: {args.blog}")
        for s, b in state.get("blogs", {}).items():
            print(f"     {s}   ({b.get('name','')})")
        return
    n = len(blog.get("episodes", []))
    if args.purge:
        import shutil
        blog_audio = AUDIO_DIR / blog["slug"]
        poistettu = len(list(blog_audio.glob("*.mp3"))) if blog_audio.exists() else 0
        shutil.rmtree(blog_audio, ignore_errors=True)
        del state["blogs"][slug]
        save_state(state)
        write_podcast_feed(state)
        print(f"🗑️  Removed completely: {blog['name']}")
        print(f"    Removed {poistettu} audio files and {n} episodes from the feed.")
        return
    blog["active"] = False
    save_state(state)
    write_podcast_feed(state)
    print(f"✅ Unsubscribed: {blog['name']}")
    print(f"   New posts will no longer be fetched. {n} existing episodes remain in the feed.")
    print(f"   (Also remove existing episodes: python blogcast.py remove {slug} --purge)")


def _voice_overrides(args) -> dict:
    """Merge --voice LANG=VOICE overrides onto the configured defaults."""
    voices = dict(DEFAULT_VOICES)
    for item in getattr(args, "voice", []) or []:
        lang, sep, value = item.partition("=")
        lang, value = lang.strip().lower(), value.strip()
        if not sep or not value:
            raise SystemExit(f"Invalid --voice value: {item!r} (expected LANG=VOICE)")
        if lang not in LANGUAGES:
            raise SystemExit(f"Language '{lang}' is not in BLOGCAST_LANGUAGES.")
        if not _resolve_voice_setting(lang, value):
            raise SystemExit(f"Unknown voice {value!r} for language '{lang}'.")
        voices[lang] = value
    return voices


def cmd_add(args):
    state = load_state()
    print(f"🔎 Discovering source: {args.url}")
    src = discover_source(args.url)
    slug = slugify(src["name"])
    print(f"   Name: {src['name']}")
    print(f"   WordPress REST: {'yes' if src['is_wordpress'] else 'no (RSS)'}")

    blog = state["blogs"].get(slug, {})
    blog.update({"name": src["name"], "slug": slug,
                 "site_root": src["site_root"], "feed_url": src["feed_url"],
                 "wp_api": src["wp_api"], "image_url": src.get("image_url", ""),
                 "fetch_mode": "new_only" if args.new_only else "all",
                 "voices": _voice_overrides(args),
                 "intro": not args.no_intro,
                 "outro": not args.no_outro, "chime": not args.no_chime,
                 "active": True})
    if args.new_only:
        blog["added_after"] = _new_only_cutoff()
        blog["new_only_baseline_guids"] = []
        blog.pop("backfill_pending", None)
    else:
        blog.pop("added_after", None)
        blog.pop("new_only_baseline_guids", None)
    print(f"   Image: {src.get('image_url') or '(not found)'}")
    blog.setdefault("seen_guids", [])
    blog.setdefault("episodes", [])
    state["blogs"][slug] = blog

    if src["wp_api"]:
        if args.new_only:
            print("⏬ Fetching posts published after this source was added...")
        else:
            print("⏬ Downloading all existing posts through the WordPress REST API...")
        posts = fetch_all_wp_posts(
            src["wp_api"], after=blog.get("added_after") if args.new_only else None)
    else:
        print("⏬ No REST API available; downloading the latest RSS items...")
        posts = fetch_rss_items(src["feed_url"])
        if args.new_only:
            blog["new_only_baseline_guids"] = sorted(
                {post["guid"] for post in posts if post.get("guid")})
            blog["added_after"] = _new_only_cutoff()
            posts = []
            print("    ℹ️  Existing RSS items recorded as the starting point; "
                  "they will not become episodes.")
    print(f"   Found {len(posts)} posts.")

    # backfill_pending marks that this list has not been processed to the end.
    # If process_posts is interrupted (e.g. the container restarts mid-run) the
    # flag survives in the last saved state, and the next update resumes the
    # backfill automatically.
    if src["wp_api"] and not args.new_only:
        blog["backfill_pending"] = True
        save_state(state)

    # on_progress writes the feed as soon as each episode is ready, so the
    # first episodes are playable while a long backfill is still running.
    stats = process_posts(blog, posts, force=args.force,
                          on_progress=_feed_writer(state))

    # The whole list was processed without interruption: backfill is complete.
    if src["wp_api"] and not args.new_only:
        blog["backfill_pending"] = False

    save_state(state)
    write_podcast_feed(state)
    print(f"✅ Added. New: {stats['new']}, "
          f"skipped (audio exists): {stats['skipped_exists']}, "
          f"skipped (already processed): {stats['skipped_seen']}.")
    print(f"   Podcast feed: {FEED_OUT}")


def _register_blog_from_url(state: dict, url: str):
    """Register a blog from a URL with default settings (no CLI flags).

    Used to sync BLOGCAST_BLOG_URLS. Returns the slug on success, or None if
    discovery failed (e.g. a transient network error); the URL stays in the
    variable and is retried on the next update.
    """
    try:
        src = discover_source(url)
    except Exception as e:
        print(f"    ⚠️  Failed to discover blog ({url}): {e}")
        return None

    slug = slugify(src["name"])
    blog = state["blogs"].get(slug, {})
    blog.update({"name": src["name"], "slug": slug,
                 "site_root": src["site_root"], "feed_url": src["feed_url"],
                 "wp_api": src["wp_api"], "image_url": src.get("image_url", ""),
                 "fetch_mode": "all",
                 "voices": dict(DEFAULT_VOICES),
                 "intro": True, "outro": True, "chime": True, "active": True})
    blog.setdefault("seen_guids", [])
    blog.setdefault("episodes", [])
    if src["wp_api"]:
        # Same backfill_pending mechanism as cmd_add: cmd_update continues the
        # full archive backfill from here.
        blog["backfill_pending"] = True
    state["blogs"][slug] = blog
    print(f"➕ Added blog from BLOGCAST_BLOG_URLS: {src['name']} ({url})")
    return slug


def _sync_configured_blog_urls(state: dict) -> None:
    """Register blog URLs supplied through BLOGCAST_BLOG_URLS."""
    added_any = False
    for url in configured_blog_urls():
        _existing_slug, existing = find_blog(state, url)
        if existing:
            continue
        if _register_blog_from_url(state, url):
            added_any = True

    if added_any:
        save_state(state)


def _missing_audio_guids(blog: dict) -> set:
    """Return guids whose audio file is missing from disk despite the state."""
    return {e["guid"] for e in blog.get("episodes", [])
            if not (AUDIO_DIR / e["relpath"]).exists()}


def cmd_update(args):
    state = load_state()

    ensure_pdf_source_registered(state)
    _sync_configured_blog_urls(state)
    save_state(state)

    if not state["blogs"]:
        print("No content sources configured. Set BLOGCAST_BLOG_URLS in .env.")
        return
    yhteensa = 0
    for slug, blog in state["blogs"].items():
        if not blog.get("active", True):
            print(f"⏸️  {blog['name']} (unsubscribed; skipped)")
            continue
        if blog.get("kind") == "pdf" and not blog.get("pdf_source_available", True):
            print(f"⏸️  {blog['name']} (PDF source not configured — skipped)")
            continue
        print(f"🔄 {blog['name']}")

        did_full_wp_fetch = False

        if blog.get("kind") == "pdf":
            folder_path = Path(blog["folder"])

            if not folder_path.exists():
                # The folder is missing (volume unmounted, or deleted by
                # hand). This is not treated as "remove the source" right away,
                # because it may be a temporary mount problem. Instead, track
                # how long it has been gone and only after
                # PDF_FOLDER_MISSING_GRACE_HOURS disable the source, like
                # 'remove' without --purge: existing episodes stay in the feed,
                # but no new content is fetched.
                now = datetime.now(timezone.utc)
                missing_since_str = blog.get("folder_missing_since")
                try:
                    missing_since = (datetime.fromisoformat(missing_since_str)
                                     if missing_since_str else now)
                except Exception:
                    missing_since = now
                if not missing_since_str:
                    blog["folder_missing_since"] = now.isoformat()

                elapsed_hours = (now - missing_since).total_seconds() / 3600
                if elapsed_hours >= PDF_FOLDER_MISSING_GRACE_HOURS:
                    if blog.get("active", True):
                        blog["active"] = False
                        print(f"   🧹 PDF folder has been unavailable for more than "
                              f"{PDF_FOLDER_MISSING_GRACE_HOURS} hours "
                              f"({folder_path}); source '{blog['name']}' "
                              f"was disabled automatically. Existing "
                              f"episodes remain in the feed. Check "
                              f"BLOGCAST_PDF_PATH in .env and the volume mount "
                              f"in docker-compose.yml before re-enabling it.")
                else:
                    remaining = PDF_FOLDER_MISSING_GRACE_HOURS - elapsed_hours
                    print(f"   ⚠️  PDF folder is unavailable: {folder_path}. Check "
                          f"that BLOGCAST_PDF_PATH is set correctly in .env and "
                          f"that the volume mount is uncommented in "
                          f"docker-compose.yml "
                          f"(- \"${{BLOGCAST_PDF_PATH}}:/external-pdf-drop:ro\"). "
                          f"If this remains unavailable for another "
                          f"{remaining:.1f} hours, the source will be disabled "
                          f"automatically.")
                posts = []
            else:
                if blog.get("folder_missing_since"):
                    print(f"   ✅ PDF folder is available again: {folder_path}; "
                          f"resetting the missing-folder timer.")
                    blog["folder_missing_since"] = None
                # Rescan the whole folder every time. It is cheap (local
                # filesystem, no network), so missing audio files are repaired
                # automatically without extra fallback logic.
                posts = fetch_pdf_folder_items(folder_path)

        elif blog.get("wp_api"):
            # Prefer the WordPress REST API over RSS when available. Full-history
            # sources fetch the archive; new-only sources query after their cutoff.
            #
            # Many WordPress sites configure their feed to show a summary
            # rather than the full text, so content:encoded holds only the
            # first paragraph. The REST API's content.rendered always returns
            # the full text and is therefore the more reliable source.
            if blog.get("backfill_pending", None) is not False:
                if blog.get("fetch_mode") == "new_only":
                    print("   ⏳ Resuming new-only import after its cutoff...")
                else:
                    print("   ⏳ Backfill is incomplete or its status is unknown; "
                          "fetching the full archive to continue...")
            try:
                after = (blog.get("added_after")
                         if blog.get("fetch_mode") == "new_only" else None)
                posts = fetch_all_wp_posts(blog["wp_api"], after=after)
                did_full_wp_fetch = after is None
            except Exception as e:
                print(f"   ⚠️  REST request failed ({e}); "
                      f"trying the RSS feed as a fallback...")
                try:
                    posts = fetch_rss_items(blog["feed_url"])
                except Exception as e2:
                    print(f"   ⚠️  RSS fallback also failed ({e2}); "
                          f"retrying during the next update.")
                    posts = []

        else:
            # No WordPress REST API (e.g. another platform): RSS is the only
            # available source.
            try:
                posts = fetch_rss_items(blog["feed_url"])
            except Exception as e:
                print(f"   ⚠️  RSS request failed ({e}).")
                posts = []

        if blog.get("fetch_mode") == "new_only":
            posts = _filter_new_only_posts(blog, posts)

        stats = process_posts(blog, posts, force=getattr(args, "force", False),
                              on_progress=_feed_writer(state))

        # The archive was processed without interruption, so the backfill is
        # complete. Save immediately so a later interruption on another source
        # does not lose this.
        if did_full_wp_fetch:
            blog["backfill_pending"] = False
            save_state(state)

        if stats["new"] or stats["skipped_exists"]:
            print(f"   New: {stats['new']}, skipped (audio exists): "
                  f"{stats['skipped_exists']}.")
        else:
            print("   No new episodes.")
        yhteensa += stats["new"]
    save_state(state)
    write_podcast_feed(state)
    print(f"✅ Complete. Generated {yhteensa} new episodes.")


def cmd_list(args):
    state = load_state()
    if not state["blogs"]:
        print("No configured blogs.")
        return
    for slug, blog in state["blogs"].items():
        eps = blog.get("episodes", [])
        per_lang = {lang: sum(1 for e in eps if e.get("lang") == lang)
                    for lang in LANGUAGES}
        v = blog.get("voices", {})
        tila = "🟢 active" if blog.get("active", True) else "⏸️  archived (no updates)"
        print(f"• {blog['name']}  ({slug})  — {tila}")
        if blog.get("kind") == "pdf":
            print(f"    Type: PDF folder")
            print(f"    Folder: {Path(blog['folder'])}")
        else:
            print(f"    Feed: {blog['feed_url']}")
            fetch_mode = ("new items only" if blog.get("fetch_mode") == "new_only"
                          else "full history")
            print(f"    Fetch: {fetch_mode}")
        voices = "  ".join(f"{lang}={v.get(lang, DEFAULT_VOICES[lang])}"
                           for lang in LANGUAGES)
        counts = ", ".join(f"{lang}: {n}" for lang, n in per_lang.items())
        print(f"    Voices: {voices}")
        print(f"    Outro: {'on' if blog.get('outro', True) else 'off'}  "
              f"Chime: {'on' if blog.get('chime', True) else 'off'}")
        print(f"    Episodes: {len(eps)}  ({counts})")


def cmd_serve(args):
    import functools
    import http.server
    import socketserver
    from urllib.parse import unquote, urlparse as _urlparse

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Security: only the podcast feed, audio and assets are served publicly.
    # Everything else, including directory listings and feeds.json, returns 404
    # so the public URL never exposes the blogcast_data layout.
    allowed_exact = {"/podcast.xml"}
    allowed_prefixes = ("/audio/", "/assets/")

    class HardenedHandler(http.server.SimpleHTTPRequestHandler):
        # HTTP/1.1 with keep-alive. Python's http.server defaults to HTTP/1.0,
        # which closes the TCP connection after every request. Podcast apps
        # buffer and seek with many small range requests, which then forced a
        # new TCP/TLS connection for each chunk. Connection reuse cuts both log
        # noise and load on the NAS and reverse proxy considerably.
        protocol_version = "HTTP/1.1"

        # Set per request by send_head() and read by copyfile(): which byte to
        # start from and how many bytes to send. None means no range request.
        _range_start = None
        _range_length = None

        def list_directory(self, path):
            # Block directory listings regardless of path.
            self.send_error(404, "Not Found")
            return None

        def _path_allowed(self) -> bool:
            request_path = unquote(_urlparse(self.path).path)
            # Block dotfiles and path traversal attempts.
            if "/." in request_path or ".." in request_path:
                return False
            if request_path in allowed_exact:
                return True
            if not any(request_path.startswith(prefix) for prefix in allowed_prefixes):
                return False
            # Allow only paths that resolve to an existing file, which also
            # blocks e.g. "/audio/" or "/audio/subfolder/".
            target = Path(self.translate_path(request_path))
            return target.is_file()

        def _parse_range(self, range_header: str, file_size: int):
            """Parse a simple "bytes=start-end" Range header (RFC 7233).

            Returns (start, length), or None when the header is missing,
            malformed, or requests multiple ranges (unsupported: the caller
            then falls back to sending the whole file, which is always valid).
            Supported forms: bytes=500-999, bytes=500-, bytes=-500.
            """
            if not range_header or not range_header.startswith("bytes="):
                return None
            spec = range_header[len("bytes="):].strip()
            if "," in spec:
                return None  # multiple ranges unsupported: send whole file
            if "-" not in spec:
                return None
            start_s, _, end_s = spec.partition("-")
            try:
                if start_s == "":
                    # "bytes=-500" -> the last 500 bytes
                    suffix_len = int(end_s)
                    if suffix_len <= 0:
                        return None
                    start = max(0, file_size - suffix_len)
                    length = file_size - start
                else:
                    start = int(start_s)
                    if start >= file_size or start < 0:
                        return None
                    end = int(end_s) if end_s != "" else file_size - 1
                    end = min(end, file_size - 1)
                    if end < start:
                        return None
                    length = end - start + 1
            except ValueError:
                return None
            return start, length

        def end_headers(self):
            # Advertise range support on every response (200, 206, HEAD, 404).
            # Without it AntennaPod and other players do not know they may use
            # partial requests, so they download the whole file and abort when
            # the buffer fills, over and over. With it they switch to efficient
            # 206 responses, reducing both wasted downloads and "client
            # disconnected during transfer" log noise.
            #
            # The header is added here because send_header() buffers headers
            # and end_headers() writes them before the body, so it lands in the
            # header block instead of leaking into the file content.
            try:
                self.send_header("Accept-Ranges", "bytes")
            except Exception:
                pass
            super().end_headers()

        def send_head(self):
            # Called by both do_GET and do_HEAD.
            if not self._path_allowed():
                self.send_error(404, "Not Found")
                return None

            self._range_start = None
            self._range_length = None

            path = self.translate_path(unquote(_urlparse(self.path).path))
            file_size = None
            try:
                file_size = Path(path).stat().st_size
            except OSError:
                pass

            range_header = self.headers.get("Range")
            if file_size is not None and range_header:
                parsed = self._parse_range(range_header, file_size)
                if parsed is None:
                    # Invalid or unsatisfiable range -> 416 per the RFC.
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return None
                start, length = parsed
                try:
                    f = open(path, "rb")
                except OSError:
                    self.send_error(404, "File not found")
                    return None
                self._range_start = start
                self._range_length = length
                ctype = self.guess_type(path)
                self.send_response(206)
                self.send_header("Content-type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range",
                                 f"bytes {start}-{start + length - 1}/{file_size}")
                # Accept-Ranges is added centrally in end_headers(), so it is
                # not repeated here.
                self.end_headers()
                return f

            # No range request (or size unknown): fall back to the parent
            # handler's full-file response.
            #
            # Note: super().send_head() already calls end_headers() internally.
            # Adding a header afterwards would write it into the start of the
            # file body and corrupt the MP3, so nothing is appended in this
            # branch. Range support still works, because players discover it by
            # simply issuing a range request (see the branch above).
            return super().send_head()

        def copyfile(self, source, outputfile):
            # Podcast apps routinely abort a transfer (seeking, backgrounding);
            # that is normal and does not deserve a traceback in the log.
            try:
                if self._range_start is not None:
                    # Range request: seek and send only the requested bytes,
                    # which is the whole point of range support.
                    source.seek(self._range_start)
                    remaining = self._range_length
                    chunk_size = 64 * 1024
                    while remaining > 0:
                        chunk = source.read(min(chunk_size, remaining))
                        if not chunk:
                            break
                        outputfile.write(chunk)
                        remaining -= len(chunk)
                    return None
                return super().copyfile(source, outputfile)
            except (BrokenPipeError, ConnectionResetError):
                self.log_message("client disconnected during transfer")
                return None

    class ReusableThreadingServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    handler = functools.partial(HardenedHandler, directory=str(DATA_DIR))
    with ReusableThreadingServer(("0.0.0.0", args.port), handler) as httpd:
        print(f"📡 Serving {DATA_DIR} (only /podcast.xml, /audio/, and /assets/)", flush=True)
        print(f"   Feed on this machine: http://localhost:{args.port}/podcast.xml", flush=True)
        print(f"   Feed on your local network: http://<host-LAN-IP>:{args.port}/podcast.xml", flush=True)
        print("   Stop: Ctrl+C", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)


def cmd_voices(args):
    print("Configured languages (first one is the fallback):\n")
    for lang in LANGUAGES:
        print(f"  [{lang}] {DEFAULT_VOICES[lang]} = {VOICE_IDS[lang]}")

    print("\nBuilt-in voice names:\n")
    for lang, names in BUILTIN_VOICE_NAMES.items():
        for name, voice_id in names.items():
            active = "  <- in use" if VOICE_IDS.get(lang) == voice_id else ""
            print(f"  [{lang}] {name:6} = {voice_id}{active}")

    print("\nAny other language needs a full edge-tts voice ID in "
          "BLOGCAST_VOICE_<LANG>, e.g. de-DE-KatjaNeural.")


def main():
    ap = argparse.ArgumentParser(description="Convert blogs and PDFs into a self-hosted podcast feed.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="Add a blog (full history by default)")
    p.add_argument("url")
    p.add_argument("--new-only", action="store_true",
                   help="Skip existing posts and process items published after adding")
    p.add_argument("--voice", action="append", metavar="LANG=VOICE", default=[],
                   help="Override the voice for one language, e.g. "
                        "--voice fi=Harri --voice de=de-DE-KatjaNeural "
                        "(repeatable)")
    p.add_argument("--no-intro", action="store_true", help="Do not add an introduction")
    p.add_argument("--no-outro", action="store_true", help="Do not add an outro")
    p.add_argument("--no-chime", action="store_true", help="Do not append the chime")
    p.add_argument("--force", action="store_true",
                   help="Regenerate audio even when the file already exists")
    p.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", help="Unsubscribe from a blog while keeping existing episodes")
    r.add_argument("blog", help="Blog slug, name, or URL")
    r.add_argument("--purge", action="store_true",
                   help="Also remove existing episodes and audio files")
    r.set_defaults(func=cmd_remove)

    u = sub.add_parser("update", help="Fetch new posts and PDFs")
    u.add_argument("--force", action="store_true",
                   help="Regenerate audio even when the file already exists")
    u.set_defaults(func=cmd_update)

    sub.add_parser("list", help="List configured sources").set_defaults(func=cmd_list)
    sub.add_parser("voices", help="List available voices").set_defaults(func=cmd_voices)

    s = sub.add_parser("serve", help="Serve the podcast feed and audio files")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
