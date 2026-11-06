#!/usr/bin/env python3
"""
Daily Local News Brief — a generic template.

All towns, keywords and sources come from a private config (the BRIEF_CONFIG
GitHub Secret, or a local config.json). Runs on GitHub Actions; emails via Gmail.
Usage:
  python brief.py              # normal run (sends email, saves seen.json)
  python brief.py --dry-run    # writes brief.html locally, no email, no state saved
"""
import hashlib
import html
import io
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:  # PDF scanning is optional
    PdfReader = None

# ---------------------------------------------------------------------------
# SETTINGS — edit these freely
# ---------------------------------------------------------------------------
TZ = ZoneInfo("America/New_York")
SEND_WINDOW_HOURS = (7, 9)      # scheduled runs only send between 7:00 and 9:59 AM ET
STATE_FILE = "seen.json"
NEWS_MAX_AGE_HOURS = 36         # ignore news older than this
NEWS_SEEN_KEEP_DAYS = 30        # how long to remember news headlines
MAX_NEW_PER_PAGE = 15           # safety cap if a website gets restructured
MAX_PDFS_PER_RUN = 10           # how many new PDFs to open and scan for flag terms
MAX_PDF_BYTES = 15 * 1024 * 1024
POLITE_DELAY_SEC = 1.0          # pause between requests to the same small sites
TIMEOUT = 25
UA = "Mozilla/5.0 (compatible; LocalNewsBrief/1.0; personal daily digest)"

# --- Everything location-specific comes from the private config -----------
from common import IN_ACTIONS, load_config, load_state as _load_state, log, save_state as _save_state

CFG = load_config().get("brief", {})
DATA_CENTER_TERMS = CFG.get("flag_terms", [])          # always flagged
LOCAL_CONTEXT_TERMS = CFG.get("local_context_terms", [])  # flagged only on local items
LOCAL_TERMS = CFG.get("local_terms", [])                # what counts as "local"
EXCLUDE_TERMS = CFG.get("exclude_terms", [])            # same-name places elsewhere
HOME_REGION_TERMS = CFG.get("home_region_terms", [])
NEWS_FEEDS = CFG.get("news_feeds", [])
WATCH_PAGES = CFG.get("watch_pages", [])
_H = CFG.get("headings", {})

NEWSLETTER_RE = re.compile(r"newsletter|quarterly|bulletin|"
                           r"(spring|summer|fall|autumn|winter)[\s_-]*20\d\d", re.I)

POLICE_TERMS = [
    r"police", r"arrest", r"charged", r"crash", r"collision", r"\bfire\b", r"blaze",
    r"\bDUI\b", r"shooting", r"stabbing", r"burglar", r"theft", r"robbery", r"missing",
    r"overdose", r"emergency", r"rescue", r"trooper", r"sentenced", r"homicide",
]

SECTIONS = [
    ("spotlight", _H.get("spotlight", "⭐ Spotlight")),
    ("flag", _H.get("flag", "🚩 Flagged")),
    ("meetings", _H.get("meetings", "🏛️ Meetings")),
    ("dispatch", _H.get("dispatch", "🚨 911 Dispatches (last 24 hrs)")),
    ("prep", _H.get("prep", "🧭 Preparedness Watch")),
    ("recalls", _H.get("recalls", "⚠️ Serious Recalls")),
    ("newsletters", _H.get("newsletters", "🗞️ Newsletters")),
    ("police", _H.get("police", "🚓 Police & Emergency")),
    ("local", _H.get("local", "📰 Local News")),
    ("county", _H.get("county", "🗺️ Region")),
]

# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _rx(terms):
    # An empty list must match nothing (not everything).
    return re.compile("|".join(f"(?:{t})" for t in terms) or r"(?!x)x", re.I)


DC_RE, CTX_RE = _rx(DATA_CENTER_TERMS), _rx(LOCAL_CONTEXT_TERMS)
LOCAL_RE, EXCLUDE_RE, POLICE_RE = _rx(LOCAL_TERMS), _rx(EXCLUDE_TERMS), _rx(POLICE_TERMS)
HOME_RE = _rx(HOME_REGION_TERMS)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA
NOW_UTC = datetime.now(timezone.utc)


@dataclass
class Item:
    section: str
    title: str
    url: str
    source: str
    flags: list = field(default_factory=list)
    snippet: str = ""
    when: str = ""


def fetch(url, **kw):
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    time.sleep(POLITE_DELAY_SEC)
    return r


def ukey(url: str) -> str:
    return "u:" + hashlib.sha1(url.strip().encode()).hexdigest()[:16]


def tkey(title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", "", title.lower())[:80]
    return "t:" + hashlib.sha1(norm.encode()).hexdigest()[:16]


def flag_terms(text: str, is_local: bool) -> list:
    hits = {m.group(0).strip().lower() for m in DC_RE.finditer(text)}
    if is_local or LOCAL_RE.search(text):
        hits |= {m.group(0).strip().lower() for m in CTX_RE.finditer(text)}
    return sorted(hits)


def flag_terms_near_local(text: str, window: int) -> list:
    """For big county-wide documents (e.g. a weekly legal journal): data center terms
    always count; zoning/utility terms count only within `window` characters of a
    local place name, so a rezoning in a far-off township isn't flagged."""
    hits = {m.group(0).strip().lower() for m in DC_RE.finditer(text)}
    local_spans = [m.start() for m in LOCAL_RE.finditer(text)]
    if local_spans:
        for m in CTX_RE.finditer(text):
            if any(abs(m.start() - p) <= window for p in local_spans):
                hits.add(m.group(0).strip().lower())
    return sorted(hits)


def snippet_for(text: str, terms: list, width: int = 220) -> str:
    low = text.lower()
    for t in terms:
        i = low.find(t)
        if i >= 0:
            start = max(0, i - width // 2)
            return "…" + " ".join(text[start:start + width].split()) + "…"
    return ""


def is_excluded_place(text: str) -> bool:
    return bool(EXCLUDE_RE.search(text)) and not HOME_RE.search(text)


def load_state() -> dict:
    s = _load_state(STATE_FILE, {})
    s.setdefault("news_seen", {})
    s.setdefault("links_seen", {})
    s.setdefault("baselined", [])
    return s


def save_state(state: dict):
    cutoff = (NOW_UTC - timedelta(days=NEWS_SEEN_KEEP_DAYS)).date().isoformat()
    state["news_seen"] = {k: v for k, v in state["news_seen"].items() if v >= cutoff}
    _save_state(STATE_FILE, state)


# --- News feeds -------------------------------------------------------------
def collect_news(state, items, report):
    today = NOW_UTC.date().isoformat()
    for src in NEWS_FEEDS:
        try:
            feed = feedparser.parse(fetch(src["url"]).content)
        except Exception as e:
            report.append((src["label"], f"⚠️ failed ({e.__class__.__name__})"))
            continue
        new = 0
        for e in feed.entries:
            title = html.unescape(e.get("title", "")).strip()
            link = e.get("link", "")
            if not title or not link:
                continue
            pub = e.get("published_parsed") or e.get("updated_parsed")
            when = ""
            if pub:
                dt = datetime(*pub[:6], tzinfo=timezone.utc)
                if NOW_UTC - dt > timedelta(hours=NEWS_MAX_AGE_HOURS if src["require"] != "flag" else 24 * 7):
                    continue
                when = dt.astimezone(TZ).strftime("%a %b %-d")
            source = ""
            src_obj = e.get("source")
            if src_obj and src_obj.get("title"):
                source = src_obj["title"]
                title = re.sub(r"\s+-\s+" + re.escape(source) + r"\s*$", "", title)
            if not source:
                source = src["label"].split(":")[0]
            summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")
            text = f"{title} {summary}"
            if is_excluded_place(text):
                continue
            if src["require"] == "local" and not LOCAL_RE.search(text):
                continue
            if src["require"] == "flag" and not DC_RE.search(text):
                continue
            keys = (ukey(link), tkey(title))
            if any(k in state["news_seen"] for k in keys):
                continue
            for k in keys:
                state["news_seen"][k] = today
            flags = flag_terms(text, is_local=False)
            section = "police" if POLICE_RE.search(title) and src["section"] == "local" else src["section"]
            items.append(Item(section=section, title=title, url=link, source=source,
                              flags=flags, snippet=snippet_for(text, flags) if flags else "", when=when))
            new += 1
        report.append((src["label"], f"{new} new"))


# --- Watched official pages -------------------------------------------------
def _host(u):
    return urlparse(u).netloc.lower().removeprefix("www.")


def crawl(src) -> dict:
    """Return {link_url: link_text} for links matching src['links']."""
    start, host = src["url"], _host(src["url"])
    queue, visited, found = [start], set(), {}
    while queue and len(visited) < src.get("max_pages", 1):
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            r = fetch(url)
        except Exception:
            if url == start:
                raise
            continue
        if "html" not in r.headers.get("content-type", "html"):
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"]).split("#")[0]
            if not href.startswith("http"):
                continue
            text = " ".join(a.get_text(" ").split()) or a.get("title", "")
            if len(text) < 12:  # e.g. "Agenda" — use the surrounding row/item for context
                box = a.find_parent(["tr", "li", "article"]) or a.parent
                ctx = " ".join(box.get_text(" ").split()) if box else ""
                if ctx and len(ctx) > len(text):
                    text = ctx[:140]
            if re.search(src["links"], href, re.I):
                found.setdefault(href, text)
            elif (src.get("follow") and _host(href) == host and href not in visited
                  and re.search(src["follow"], href, re.I)
                  and not re.search(r"\.(pdf|jpe?g|png|gif|docx?|xlsx?|zip)(\?|$)", href, re.I)):
                queue.append(href)
    return found


def pdf_text(url: str, max_pages: int = 25) -> str:
    if PdfReader is None:
        return ""
    r = SESSION.get(url, timeout=60, stream=True)
    r.raise_for_status()
    data = r.raw.read(MAX_PDF_BYTES + 1, decode_content=True)
    time.sleep(POLITE_DELAY_SEC)
    if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF"):
        return ""
    reader = PdfReader(io.BytesIO(data))
    return " ".join((p.extract_text() or "") for p in reader.pages[:max_pages])


def collect_pages(state, items, report):
    today = NOW_UTC.date().isoformat()
    pdf_budget = MAX_PDFS_PER_RUN
    for src in WATCH_PAGES:
        try:
            found = crawl(src)
        except Exception as e:
            report.append((src["label"], f"⚠️ failed ({e.__class__.__name__})"))
            continue
        if src["url"] not in state["baselined"]:
            for href in found:
                state["links_seen"][ukey(href)] = today
            state["baselined"].append(src["url"])
            report.append((src["label"], f"baseline saved ({len(found)} existing links)"))
            continue
        new = [(h, t) for h, t in found.items() if ukey(h) not in state["links_seen"]]
        for href, text in new[:MAX_NEW_PER_PAGE]:
            state["links_seen"][ukey(href)] = today
            fname = os.path.basename(urlparse(href).path) or href
            generic = re.fullmatch(r"(?:(?:view|download|open|click here|here|pdf|file|document)\s*)+", text, re.I)
            if generic or not text:
                title = re.sub(r"[_\s]+", " ", os.path.splitext(fname)[0]).strip()
            else:
                title = text if len(text) >= 12 else f"{text} ({fname})".strip()
            if src.get("title_prefix"):
                title = f"{src['title_prefix']}: {title}"
            body = ""
            must_scan = bool(src.get("only_if_flagged"))  # these sources are useless unscanned
            if re.search(r"\.pdf(\?|$)|showpublisheddocument|ShowDocument|ViewFile", href, re.I) and (pdf_budget > 0 or must_scan):
                if not must_scan:
                    pdf_budget -= 1
                try:
                    body = pdf_text(href, src.get("pdf_max_pages", 25))
                except Exception:
                    body = ""
            text_all = f"{title} {body}"
            if src.get("flag_window"):
                flags = flag_terms_near_local(text_all, int(src["flag_window"]))
            else:
                flags = flag_terms(text_all, is_local=src["local_doc"])
            if src.get("only_if_flagged") and not flags:
                continue
            section = src["section"]
            if section == "meetings" and not src.get("title_prefix") and (NEWSLETTER_RE.search(title) or NEWSLETTER_RE.search(href)
                                          or NEWSLETTER_RE.search(body[:400])):
                section = "newsletters"
            items.append(Item(section=section, title=title, url=href, source=src["label"],
                              flags=flags, snippet=snippet_for(text_all, flags) if flags else ""))
        extra = f" (+{len(new) - MAX_NEW_PER_PAGE} more not shown — site may have changed)" if len(new) > MAX_NEW_PER_PAGE else ""
        report.append((src["label"], f"{min(len(new), MAX_NEW_PER_PAGE)} new{extra}"))


# --- 911 dispatches (saved every 15 min by dispatch.py) ---------------------
DISPATCH_FILE = "dispatches.json"
DISPATCH_URL = load_config().get("dispatch", {}).get("public_page", "")


def collect_dispatches(items, report):
    incidents = _load_state(DISPATCH_FILE, {}).get("incidents", {})
    if not incidents:
        report.append(("911 dispatches", "no saved local calls yet"))
        return
    since = datetime.now(TZ) - timedelta(hours=24)
    rows = []
    for c in incidents.values():
        try:
            t = datetime.strptime(c["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        except (KeyError, ValueError):
            continue
        if t >= since:
            rows.append((t, c))
    for t, c in sorted(rows, key=lambda x: x[0], reverse=True):
        items.append(Item(section="dispatch", title=f"{c['type']} — {c['location']}",
                          url=c.get("map") or DISPATCH_URL, source=c.get("category", "911"),
                          when=t.strftime("%a %-I:%M %p")))
    report.append(("911 dispatches", f"{len(rows)} local calls in last 24 hrs"))


# --- Weather: National Weather Service (free, no key) ------------------------
NWS = "https://api.weather.gov"
NWS_HEADERS = {"User-Agent": "(DailyBrief personal digest)", "Accept": "application/geo+json"}


def collect_weather(report) -> dict:
    """Return {"days": [...3 days...], "alerts": [...]} or {} if not configured/failed."""
    w = CFG.get("weather") or {}
    lat, lon = w.get("lat"), w.get("lon")
    if lat is None or lon is None:
        return {}
    pt = f"{float(lat):.4f},{float(lon):.4f}"
    out = {"days": [], "alerts": [], "label": w.get("label", "")}
    try:
        meta = SESSION.get(f"{NWS}/points/{pt}", headers=NWS_HEADERS, timeout=TIMEOUT)
        meta.raise_for_status()
        fc = SESSION.get(meta.json()["properties"]["forecast"], headers=NWS_HEADERS, timeout=TIMEOUT)
        fc.raise_for_status()
        periods = fc.json()["properties"]["periods"]
    except Exception as ex:
        report.append(("Weather (NWS)", f"⚠️ failed ({ex.__class__.__name__})"))
        return {}

    # Pair each daytime period with the night after it; a leading night = "Tonight".
    days, cur = [], None
    for p in periods:
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        info = dict(temp=p.get("temperature"), text=p.get("shortForecast", ""),
                    pop=pop, wind=f"{p.get('windDirection', '')} {p.get('windSpeed', '')}".strip())
        if p.get("isDaytime"):
            cur = {"name": p.get("name", ""), "day": info, "night": None}
            days.append(cur)
        else:
            if cur is None or cur["night"] is not None:
                cur = {"name": p.get("name", ""), "day": None, "night": info}
                days.append(cur)
            else:
                cur["night"] = info
        if len(days) > 3:
            break
    out["days"] = days[:3]

    try:
        al = SESSION.get(f"{NWS}/alerts/active", params={"point": pt}, headers=NWS_HEADERS, timeout=TIMEOUT)
        al.raise_for_status()
        for f in al.json().get("features", []):
            pr = f.get("properties", {})
            out["alerts"].append(dict(event=pr.get("event", ""), headline=pr.get("headline", ""),
                                      severity=pr.get("severity", "")))
    except Exception:
        pass
    report.append(("Weather (NWS)", f"3-day forecast, {len(out['alerts'])} active alerts"))
    return out


def _pop(v):
    return f" · {v}% precip" if v else ""


def weather_html(w) -> str:
    if not w or not w.get("days"):
        return ""
    e = html.escape
    parts = []
    for a in w["alerts"]:
        parts.append(f'<div style="background:#fff4e5;border-left:4px solid #e37400;padding:8px 10px;margin:6px 0;'
                     f'font-size:14px"><b>⚠️ {e(a["event"])}</b><br><span style="font-size:12px;color:#555">'
                     f'{e(a["headline"])}</span></div>')
    cells = []
    for d in w["days"]:
        hi = f'{d["day"]["temp"]}°' if d["day"] else "—"
        lo = f'{d["night"]["temp"]}°' if d["night"] else "—"
        main = d["day"] or d["night"]
        pop = max([x["pop"] or 0 for x in (d["day"], d["night"]) if x] or [0])
        cells.append(f'<td style="vertical-align:top;padding:8px;border:1px solid #e5e5e5;width:33%">'
                     f'<div style="font-weight:bold">{e(d["name"])}</div>'
                     f'<div style="font-size:18px;margin:2px 0">{hi} <span style="color:#888">/ {lo}</span></div>'
                     f'<div style="font-size:13px">{e(main["text"])}</div>'
                     f'<div style="font-size:12px;color:#666">{f"{pop}% precip · " if pop else ""}{e(main["wind"])}</div></td>')
    label = f' — {e(w["label"])}' if w.get("label") else ""
    parts.append(f'<h3 style="border-bottom:2px solid #ddd;padding-bottom:4px;margin-top:10px">🌤️ Weather{label}</h3>'
                 f'<table style="border-collapse:collapse;width:100%"><tr>{"".join(cells)}</tr></table>')
    return "\n".join(parts)


def weather_text(w) -> str:
    if not w or not w.get("days"):
        return ""
    lines = ["WEATHER" + (f" — {w['label']}" if w.get("label") else "")]
    for a in w["alerts"]:
        lines.append(f"  ⚠️ {a['event']}: {a['headline']}")
    for d in w["days"]:
        hi = f"{d['day']['temp']}°" if d["day"] else "—"
        lo = f"{d['night']['temp']}°" if d["night"] else "—"
        main = d["day"] or d["night"]
        pop = max([x["pop"] or 0 for x in (d["day"], d["night"]) if x] or [0])
        lines.append(f"  {d['name']}: {hi}/{lo}, {main['text']}{_pop(pop)}")
    return "\n".join(lines) + "\n"


# --- Preparedness watch ------------------------------------------------------
# Official signals (grid emergencies, solar storms, public-health alerts) are always
# shown; general supply-chain/trend news is scored and capped (prep.max_trend).
PREP = CFG.get("prep") or {}


def _seen(state, *keys) -> bool:
    if any(k in state["news_seen"] for k in keys):
        return True
    today = NOW_UTC.date().isoformat()
    for k in keys:
        state["news_seen"][k] = today
    return False


def _feed_entries(url, max_age_h):
    feed = feedparser.parse(fetch(url).content)
    for e in feed.entries:
        title = html.unescape(e.get("title", "")).strip()
        link = e.get("link", "")
        pub = e.get("published_parsed") or e.get("updated_parsed")
        if not title or not link:
            continue
        when = ""
        if pub:
            dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if NOW_UTC - dt > timedelta(hours=max_age_h):
                continue
            when = dt.astimezone(TZ).strftime("%a %b %-d")
        src = (e.get("source") or {}).get("title", "")
        if src:
            title = re.sub(r"\s+-\s+" + re.escape(src) + r"\s*$", "", title)
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ")
        yield title, link, src, when, summary


def _space_weather(state, items, report):
    url = PREP.get("space_weather_url")
    if not url:
        return
    min_g = int(PREP.get("space_weather_min_g", 3))
    try:
        alerts = fetch(url).json()
    except Exception as ex:
        report.append(("Space weather (NOAA)", f"⚠️ failed ({ex.__class__.__name__})"))
        return
    n = 0
    for a in alerts:
        msg = a.get("message", "")
        m = re.search(r"NOAA Scale:\s*G(\d)", msg)
        if not m or int(m.group(1)) < min_g:
            continue
        try:
            issued = datetime.strptime(a.get("issue_datetime", "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if NOW_UTC - issued > timedelta(hours=36):
            continue
        head = next((ln.strip() for ln in msg.splitlines()
                     if re.match(r"\s*(WATCH|WARNING|ALERT|EXTENDED WARNING):", ln)), f"Geomagnetic storm G{m.group(1)}")
        if _seen(state, ukey(a.get("product_id", "") + a.get("issue_datetime", ""))):
            continue
        items.append(Item(section="prep", title=f"☀️ {head} (G{m.group(1)})",
                          url="https://www.swpc.noaa.gov/", source="NOAA Space Weather",
                          when=issued.astimezone(TZ).strftime("%a %-I:%M %p")))
        n += 1
    report.append(("Space weather (NOAA)", f"{n} storm watches/warnings G{min_g}+"))


def _official_feeds(state, items, report):
    for src in PREP.get("official_feeds", []):
        must = re.compile(src.get("title_must", "."), re.I)
        try:
            n = 0
            for title, link, s_name, when, summary in _feed_entries(src["url"], src.get("max_age_hours", 48)):
                if not must.search(title) or _seen(state, ukey(link), tkey(title)):
                    continue
                items.append(Item(section="prep", title=f"{src.get('icon', '')} {title}".strip(),
                                  url=link, source=s_name or src["label"], when=when))
                n += 1
            report.append((src["label"], f"{n} new"))
        except Exception as ex:
            report.append((src["label"], f"⚠️ failed ({ex.__class__.__name__})"))


def _trend_news(state, items, report):
    feeds = PREP.get("trend_feeds", [])
    if not feeds:
        return
    terms = _rx(PREP.get("trend_terms", []))
    skip = _rx(PREP.get("skip_terms", []))
    trusted = [t.lower() for t in PREP.get("trusted_sources", [])]
    cands, seen_titles = [], set()
    for src in feeds:
        try:
            for title, link, s_name, when, summary in _feed_entries(src["url"], src.get("max_age_hours", 36)):
                text = f"{title} {summary}"
                hits = {m.group(0).lower() for m in terms.finditer(text)}
                if not hits or skip.search(title):
                    continue
                k = tkey(title)
                if k in seen_titles or k in state["news_seen"] or ukey(link) in state["news_seen"]:
                    continue
                seen_titles.add(k)
                score = len(hits) + (2 if any(t in s_name.lower() for t in trusted) else 0)
                cands.append((score, title, link, s_name, when, sorted(hits), src.get("icon", "📦")))
        except Exception as ex:
            report.append((src["label"], f"⚠️ failed ({ex.__class__.__name__})"))
    cands.sort(key=lambda c: c[0], reverse=True)
    cap = int(PREP.get("max_trend", 5))
    for _, title, link, *_ in cands:  # mark all as seen so cut stories don't resurface tomorrow
        _seen(state, ukey(link), tkey(title))
    for score, title, link, s_name, when, hits, icon in cands[:cap]:
        items.append(Item(section="prep", title=f"{icon} {title}", url=link, source=s_name or "News",
                          when=when, snippet="Signals: " + ", ".join(hits[:4])))
    report.append(("Preparedness trend news", f"{min(len(cands), cap)} shown of {len(cands)} matches"))


def collect_prep(state, items, report):
    if not PREP:
        return
    _official_feeds(state, items, report)
    _space_weather(state, items, report)
    _trend_news(state, items, report)


# --- Serious recalls (FDA, USDA-FSIS, CPSC) -----------------------------------
RECALLS = PREP.get("recalls") or {}


def _recall_item(state, key, title, url, source, when, snippet=""):
    if _seen(state, key):
        return None
    return Item(section="recalls", title=title, url=url, source=source, when=when, snippet=snippet)


def _fda_recalls(state, items, days):
    base = RECALLS.get("fda_url", "https://api.fda.gov/food/enforcement.json")
    a = (NOW_UTC - timedelta(days=days)).strftime("%Y%m%d")
    b = NOW_UTC.strftime("%Y%m%d")
    url = f'{base}?search=classification:"Class+I"+AND+report_date:[{a}+TO+{b}]&limit=100'
    r = SESSION.get(url, timeout=TIMEOUT)
    if r.status_code == 404:  # openFDA answers 404 when nothing matches
        return 0
    r.raise_for_status()
    area = _rx(RECALLS.get("area_terms", [r"nationwide", r"\bPA\b", r"Pennsylvania"]))
    n = 0
    for rec in r.json().get("results", []):
        if not area.search(rec.get("distribution_pattern", "")):
            continue
        prod = " ".join(rec.get("product_description", "").split())[:110]
        it = _recall_item(state, ukey("fda" + rec.get("recall_number", prod)),
                          f"{rec.get('recalling_firm', '')}: {prod}",
                          "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
                          "FDA Class I", rec.get("report_date", ""),
                          "Reason: " + " ".join(rec.get("reason_for_recall", "").split())[:180])
        if it:
            items.append(it); n += 1
    return n


def _fsis_recalls(state, items, days):
    base = RECALLS.get("fsis_url", "https://www.fsis.usda.gov/fsis/api/recall/v/1")
    cutoff = (NOW_UTC - timedelta(days=days)).date().isoformat()
    n = 0
    for state_id in RECALLS.get("fsis_state_ids", [63, 557]):  # 63 = Pennsylvania, 557 = Nationwide
        r = SESSION.get(base, params={"field_risk_level_id": 9, "field_states_id": state_id},
                        headers={"Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        for rec in r.json():
            date = (rec.get("field_recall_date") or "")[:10]
            if date < cutoff:
                continue
            it = _recall_item(state, ukey("fsis" + (rec.get("field_recall_number") or rec.get("field_title", ""))),
                              rec.get("field_title", "USDA recall"), "https://www.fsis.usda.gov/recalls",
                              "USDA High Risk", date,
                              "Reason: " + (rec.get("field_recall_reason") or "")[:120])
            if it:
                items.append(it); n += 1
        time.sleep(POLITE_DELAY_SEC)
    return n


def _cpsc_recalls(state, items, days):
    base = RECALLS.get("cpsc_url", "https://www.saferproducts.gov/RestWebServices/Recall")
    start = (NOW_UTC - timedelta(days=days)).date().isoformat()
    r = SESSION.get(base, params={"format": "json", "RecallDateStart": start}, timeout=60)
    r.raise_for_status()
    serious = _rx(RECALLS.get("cpsc_serious_terms", [r"death", r"fatal", r"serious injur", r"fire", r"burn",
                                                       r"carbon monoxide", r"poison", r"electrocut", r"suffocat"]))
    n = 0
    for rec in r.json():
        date = (rec.get("RecallDate") or "")[:10]
        if date < start:
            continue
        hazards = " ".join(h.get("Name", "") for h in rec.get("Hazards", []) or [])
        if not serious.search(hazards):
            continue
        it = _recall_item(state, ukey("cpsc" + str(rec.get("RecallNumber") or rec.get("RecallID"))),
                          rec.get("Title", "CPSC recall"), rec.get("URL") or "https://www.cpsc.gov/Recalls",
                          "CPSC", date, "Hazard: " + " ".join(hazards.split())[:180])
        if it:
            items.append(it); n += 1
    return n


def collect_recalls(state, items, report):
    if not RECALLS:
        return
    days = int(RECALLS.get("lookback_days", 10))
    for label, fn in (("Recalls: FDA food Class I", _fda_recalls),
                      ("Recalls: USDA meat/poultry high risk", _fsis_recalls),
                      ("Recalls: CPSC serious hazards", _cpsc_recalls)):
        try:
            report.append((label, f"{fn(state, items, days)} new"))
        except Exception as ex:
            report.append((label, f"⚠️ failed ({ex.__class__.__name__})"))


# --- Spotlight (a person/topic you follow; shown first) -----------------------
SPOT = CFG.get("spotlight") or {}
YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="


def _youtube_channel_id(state, handle):
    """Resolve a @handle to its channel ID once, then remember it."""
    cache = state.setdefault("yt_ids", {})
    if handle in cache:
        return cache[handle]
    r = fetch(f"https://www.youtube.com/{handle}", headers={"Accept-Language": "en-US"})
    m = (re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', r.text)
         or re.search(r'"(?:externalId|channelId)":"(UC[\w-]{22})"', r.text))
    if not m:
        raise ValueError("channel id not found")
    cache[handle] = m.group(1)
    return m.group(1)


def collect_spotlight(state, items, report):
    if not SPOT:
        return
    name = SPOT.get("name", "Spotlight")
    # New videos
    for handle in SPOT.get("youtube_handles", []):
        try:
            cid = _youtube_channel_id(state, handle)
            n = 0
            for title, link, _, when, _ in _feed_entries(YT_FEED + cid, SPOT.get("video_max_age_hours", 48)):
                if _seen(state, ukey(link)):
                    continue
                items.append(Item(section="spotlight", title=f"▶️ New video: {title}", url=link,
                                  source=f"YouTube {handle}", when=when))
                n += 1
            report.append((f"{name}: YouTube {handle}", f"{n} new"))
        except Exception as ex:
            report.append((f"{name}: YouTube {handle}", f"⚠️ failed ({ex.__class__.__name__})"))
    # News, newest first, capped
    news, keys = [], set()
    for src in SPOT.get("news_feeds", []):
        try:
            for title, link, s_name, when, _ in _feed_entries(src["url"], src.get("max_age_hours", 30)):
                k = tkey(title)
                if k in keys or k in state["news_seen"] or ukey(link) in state["news_seen"]:
                    continue
                keys.add(k)
                news.append((title, link, s_name, when))
        except Exception as ex:
            report.append((src.get("label", name), f"⚠️ failed ({ex.__class__.__name__})"))
    cap = int(SPOT.get("max_news", 5))
    for title, link, s_name, when in news:
        _seen(state, ukey(link), tkey(title))
    for title, link, s_name, when in news[:cap]:
        items.append(Item(section="spotlight", title=title, url=link, source=s_name or "News", when=when))
    report.append((f"{name}: news", f"{min(len(news), cap)} shown of {len(news)}"))


# --- Rendering --------------------------------------------------------------
def group(items):
    out = {k: [] for k, _ in SECTIONS}
    for it in items:
        out["flag" if it.flags else it.section].append(it)
    return out


def render_html(items, report, date_str, weather=None):
    g = group(items)
    e = html.escape
    parts = [f"""<div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#222">
<h2 style="margin-bottom:4px">Local Brief — {e(date_str)}</h2>
<div style="color:#666;font-size:13px;margin-bottom:16px">{e(CFG.get("subtitle", ""))}</div>"""]
    any_items = False
    order = SECTIONS[:1] + [("__weather__", "")] + SECTIONS[1:]
    for key, heading in order:
        if key == "__weather__":
            parts.append(weather_html(weather))
            continue
        rows = g[key]
        if key == "spotlight" and SPOT and not rows:
            parts.append(f'<h3 style="border-bottom:2px solid #ddd;padding-bottom:4px;margin-top:10px">{heading}</h3>'
                         f'<p style="color:#777;font-size:13px">Nothing new since yesterday.</p>')
            continue
        if not rows:
            continue
        any_items = True
        bg = {"flag": "#fff4f2", "prep": "#f3f7ff", "recalls": "#fffaf0", "spotlight": "#fbf5ff"}.get(key, "#fff")
        parts.append(f'<h3 style="border-bottom:2px solid #ddd;padding-bottom:4px;margin-top:22px">{heading}</h3>')
        for it in rows:
            meta = " · ".join(x for x in [it.source, it.when] if x)
            flags = (f'<div style="color:#b3261e;font-size:12px;font-weight:bold">🚩 {e(", ".join(it.flags))}</div>'
                     if it.flags else "")
            snip = f'<div style="font-size:13px;color:#444;margin-top:2px">{e(it.snippet)}</div>' if it.snippet else ""
            parts.append(f"""<div style="background:{bg};padding:8px 10px;margin:6px 0;border-left:3px solid {'#b3261e' if it.flags else '#ccc'}">
{flags}<a href="{e(it.url)}" style="font-size:15px;color:#0b57d0;text-decoration:none">{e(it.title)}</a>
<div style="font-size:12px;color:#777">{e(meta)}</div>{snip}</div>""")
    if not any_items:
        parts.append("<p>Quiet day — nothing new from any source.</p>")
    parts.append('<h4 style="margin-top:28px;color:#666">Sources checked</h4><ul style="font-size:12px;color:#666">')
    parts += [f"<li>{e(lbl)}: {e(status)}</li>" for lbl, status in report]
    parts.append("</ul></div>")
    return "\n".join(parts)


def render_text(items, report, date_str, weather=None):
    g = group(items)
    lines = [f"LOCAL BRIEF — {date_str}", ""]
    order = SECTIONS[:1] + [("__weather__", "")] + SECTIONS[1:]
    for key, heading in order:
        if key == "__weather__":
            lines.append(weather_text(weather))
            continue
        if key == "spotlight" and SPOT and not g[key]:
            lines += [heading.upper(), "  Nothing new since yesterday.", ""]
            continue
        if not g[key]:
            continue
        lines.append(heading.upper())
        for it in g[key]:
            flag = f"[FLAG: {', '.join(it.flags)}] " if it.flags else ""
            lines.append(f"- {flag}{it.title} ({it.source})\n  {it.url}")
            if it.snippet:
                lines.append(f"  {it.snippet}")
        lines.append("")
    if not items:
        lines.append("Quiet day — nothing new.\n")
    lines.append("Sources checked:")
    lines += [f"  {lbl}: {status}" for lbl, status in report]
    return "\n".join(lines)


def send_email(subject, html_body, text_body):
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    to = os.environ.get("BRIEF_TO") or user
    recipients = [a.strip() for a in to.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, user, ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.sendmail(user, recipients, msg.as_string())


def main():
    dry = "--dry-run" in sys.argv
    now_ny = datetime.now(TZ)
    state = load_state()

    if os.environ.get("SCHEDULED") == "true":
        lo, hi = SEND_WINDOW_HOURS
        if not (lo <= now_ny.hour <= hi):
            print(f"Outside send window ({now_ny:%H:%M} ET) — skipping.")
            return
        if state.get("last_sent") == now_ny.date().isoformat():
            print("Already sent today — skipping.")
            return

    items, report = [], []
    collect_spotlight(state, items, report)
    weather = collect_weather(report)
    collect_pages(state, items, report)
    collect_news(state, items, report)
    collect_dispatches(items, report)
    collect_prep(state, items, report)
    collect_recalls(state, items, report)

    date_str = now_ny.strftime("%a, %b %-d, %Y")
    n_flags = sum(1 for i in items if i.flags)
    subject = f"Local Brief — {now_ny:%a %b %-d}" + (f" · 🚩 {n_flags} flagged" if n_flags else "")
    if weather and weather.get("alerts"):
        subject += f" · ⚠️ {weather['alerts'][0]['event']}"
    html_body = render_html(items, report, date_str, weather)
    text_body = render_text(items, report, date_str, weather)

    if dry:
        with open("brief.html", "w") as f:
            f.write(html_body)
        print(text_body)
        print("\n(dry run — wrote brief.html, no email sent, state not saved)")
        return

    send_email(subject, html_body, text_body)
    state["last_sent"] = now_ny.date().isoformat()
    save_state(state)
    log(f"Brief sent ({len(items)} items).")


if __name__ == "__main__":
    main()
