import hashlib
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlparse

import feedparser
import requests

MAX_RESULTS = 20          # כמה תוצאות מציגים בסוף
CHECK_TOP = 30            # כמה פידים בודקים (נגישות, פרקים, תאריך)
COUNTRIES = ("IL", "US", "GB")
REQUEST_TIMEOUT = 15
FEED_TIMEOUT = 10
MAX_FEED_BYTES = 3 * 1024 * 1024
WORKERS = 8

NOTES_FILE = "release_notes.md"
TXT_FILE = "results.txt"
ENV_OUTPUT_FILE = "env_output.env"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GitManualSearch/1.0)"}
TRACKING_PARAMS = {"fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid"}


# ---------------------------------------------------------------- נרמול

def normalize_text(text):
    """מנרמל טקסט להשוואה: בלי ניקוד, פיסוק, רווחים כפולים, אותיות קטנות."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\u0591-\u05C7]", "", text)  # ניקוד וטעמי מקרא
    text = re.sub(r"[\"'`„“”‘’׳״\-–—_.,:;!?()\[\]{}|/\\]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_url(url):
    """מפתח להשוואת כתובות: בלי http/https, www, / בסוף ופרמטרי מעקב."""
    url = (url or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return url.lower()
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/")
    query = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    q = urlencode(query)
    return f"{host}{path}" + (f"?{q}" if q else "")


def single_line(text):
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------- מקורות חיפוש

def make_candidate(title, artist, rss, source):
    return {
        "title": single_line(title),
        "artist": single_line(artist),
        "rss": (rss or "").strip(),
        "sources": {source},
        "score": 0.0,
        "check": None,
    }


def itunes_search(term, country):
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": term, "media": "podcast", "entity": "podcast",
                    "limit": 20, "country": country},
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        items = r.json().get("results", [])
    except Exception as e:
        print(f"iTunes ({country}) נכשל: {e}")
        return []
    return [
        make_candidate(i.get("collectionName"), i.get("artistName"), i.get("feedUrl"), "iTunes")
        for i in items if i.get("feedUrl")
    ]


def itunes_lookup(apple_id):
    try:
        r = requests.get(
            "https://itunes.apple.com/lookup",
            params={"id": apple_id, "entity": "podcast"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        items = r.json().get("results", [])
    except Exception as e:
        print(f"iTunes lookup נכשל: {e}")
        return []
    return [
        make_candidate(i.get("collectionName"), i.get("artistName"), i.get("feedUrl"), "iTunes")
        for i in items if i.get("feedUrl")
    ]


def podcastindex_search(term):
    key = os.environ.get("PODCASTINDEX_KEY", "").strip()
    secret = os.environ.get("PODCASTINDEX_SECRET", "").strip()
    if not key or not secret:
        print("Podcast Index מדולג: לא הוגדרו PODCASTINDEX_KEY ו-PODCASTINDEX_SECRET.")
        return []  # אופציונלי - מדלגים אם אין מפתחות
    ts = str(int(time.time()))
    auth = hashlib.sha1((key + secret + ts).encode("utf-8")).hexdigest()
    headers = {
        "User-Agent": "GitManualSearch/1.0",
        "X-Auth-Date": ts,
        "X-Auth-Key": key,
        "Authorization": auth,
    }
    try:
        r = requests.get(
            "https://api.podcastindex.org/api/1.0/search/byterm",
            params={"q": term, "max": 20},
            headers=headers, timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        feeds = r.json().get("feeds", [])
    except Exception as e:
        print(f"Podcast Index נכשל: {e}")
        return []
    return [
        make_candidate(f.get("title"), f.get("author") or f.get("ownerName"), f.get("url"), "Podcast Index")
        for f in feeds if f.get("url")
    ]


def search_all_sources(query):
    terms = [query]
    normalized = normalize_text(query)
    if normalized and normalized != query.lower():
        terms.append(normalized)

    tasks = []
    for term in terms:
        for country in COUNTRIES:
            tasks.append(lambda t=term, c=country: itunes_search(t, c))
        tasks.append(lambda t=term: podcastindex_search(t))

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda fn: fn(), tasks))
    return [c for batch in results for c in batch]


# ---------------------------------------------------------------- כפילויות

def absorb(dst, src):
    dst["sources"] |= src["sources"]
    if src["rss"].startswith("https://") and not dst["rss"].startswith("https://"):
        dst["rss"] = src["rss"]
    if not dst["artist"] and src["artist"]:
        dst["artist"] = src["artist"]


def dedupe(candidates):
    # שלב 1: לפי כתובת RSS מנורמלת
    by_url = {}
    for c in candidates:
        key = normalize_url(c["rss"])
        if not key:
            continue
        if key in by_url:
            absorb(by_url[key], c)
        else:
            by_url[key] = c

    # שלב 2: אותו שם ויוצר עם כתובות שונות (למשל Feedburner מול המקורית)
    by_name = {}
    for c in by_url.values():
        key = (normalize_text(c["title"]), normalize_text(c["artist"]))
        if key[0] and key in by_name:
            absorb(by_name[key], c)
        else:
            by_name[key if key[0] else id(c)] = c
    return list(by_name.values())


# ---------------------------------------------------------------- דירוג ובדיקת פידים

def base_score(query, c):
    q = normalize_text(query)
    t = normalize_text(c["title"])
    a = normalize_text(c["artist"])
    score = SequenceMatcher(None, q, t).ratio()
    if q and q == t:
        score += 0.3
    elif q and q in t:
        score += 0.15
    if q and q in a:
        score += 0.1
    score += min(0.1, 0.05 * (len(c["sources"]) - 1))
    return score


def to_datetime(struct_time):
    try:
        return datetime(*struct_time[:6], tzinfo=timezone.utc)
    except Exception:
        return None


def check_feed(c):
    """בודק נגישות, מספר פרקים ותאריך הפרק האחרון."""
    try:
        start = time.monotonic()
        chunks, size, truncated = [], 0, False
        with requests.get(c["rss"], headers=HEADERS, stream=True, timeout=FEED_TIMEOUT) as r:
            r.raise_for_status()
            for chunk in r.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_FEED_BYTES or time.monotonic() - start > 2 * FEED_TIMEOUT:
                    truncated = True
                    break
        parsed = feedparser.parse(b"".join(chunks))
        if not parsed.entries:
            return {"ok": False}

        dates = []
        for e in parsed.entries:
            st = e.get("published_parsed") or e.get("updated_parsed")
            if st:
                d = to_datetime(st)
                if d:
                    dates.append(d)

        if not c["title"]:
            c["title"] = single_line(parsed.feed.get("title", ""))
        if not c["artist"]:
            c["artist"] = single_line(parsed.feed.get("author", ""))

        return {
            "ok": True,
            "episodes": len(parsed.entries),
            "truncated": truncated,
            "last": max(dates) if dates else None,
        }
    except Exception:
        return {"ok": False}


def apply_activity(c, query):
    info = c["check"] or {"ok": False}
    c["score"] = base_score(query, c)
    if not info["ok"]:
        c["score"] -= 0.2
        return
    last = info.get("last")
    if last:
        days = (datetime.now(timezone.utc) - last).days
        if days <= 180:
            c["score"] += 0.1
        elif days > 730:
            c["score"] -= 0.1


# ---------------------------------------------------------------- פלט

def status_line(c):
    info = c["check"]
    if not info or not info["ok"]:
        return "⚠️ הפיד לא נגיש או ריק"
    plus = "+" if info.get("truncated") else ""
    last = info["last"].strftime("%Y-%m-%d") if info.get("last") else "לא ידוע"
    return f"{info['episodes']}{plus} פרקים · פרק אחרון: {last}"


def write_outputs(query, results, repo):
    now = datetime.now(timezone.utc)
    title = single_line(f"חיפוש: {query}")[:100]
    tag = "search-" + now.strftime("%Y%m%d-%H%M%S")

    # --- גוף ה-Release (Markdown) ---
    md = [f"# חיפוש: {single_line(query)}", ""]
    if results:
        md.append(f"נמצאו **{len(results)}** תוצאות, מסודרות לפי רלוונטיות.")
    else:
        md.append("לא נמצאו תוצאות. נסה שם אחר, או הדבק קישור Apple Podcasts / כתובת RSS.")
    md.append("")
    for i, c in enumerate(results, 1):
        md += [
            "---",
            f"### {i}. {c['title'] or 'ללא שם'}",
            f"- **יוצר:** {c['artist'] or 'לא ידוע'}",
            f"- **מקורות:** {', '.join(sorted(c['sources']))}",
            f"- **סטטוס:** {status_line(c)}",
            "",
            "```",
            c["rss"],
            "```",
            "",
        ]
    if repo:
        md += [
            "---",
            f"להורדת פרקים: [GitManual](https://github.com/{repo}/actions/workflows/gitmanual.yml)"
            " ← Run workflow ← הדבק את כתובת ה-RSS.",
        ]
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    # --- קובץ TXT ---
    lines = [f"חיפוש: {single_line(query)}", f"נוצר: {now.strftime('%Y-%m-%d %H:%M')} UTC", ""]
    if not results:
        lines.append("לא נמצאו תוצאות.")
    for i, c in enumerate(results, 1):
        lines += [
            f"{i}. {c['title'] or 'ללא שם'}",
            f"   יוצר: {c['artist'] or 'לא ידוע'}",
            f"   סטטוס: {status_line(c)}",
            f"   RSS: {c['rss']}",
            "",
        ]
    with open(TXT_FILE, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")

    # --- משתנים ל-workflow ---
    with open(ENV_OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(f"SEARCH_TITLE={title}\n")
        f.write(f"SEARCH_TAG={tag}\n")
        f.write(f"RESULT_COUNT={len(results)}\n")


# ---------------------------------------------------------------- main

def main():
    query = os.environ.get("PODCAST_NAME", "").strip()
    if not query:
        print("לא הוזן שם פודקאסט.")
        sys.exit(1)

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    print(f"מחפש: {query}")

    apple_match = re.search(r"podcasts\.apple\.com/.*?/id(\d+)", query)
    if apple_match:
        print("זוהה קישור Apple Podcasts.")
        candidates = itunes_lookup(apple_match.group(1))
    elif re.match(r"https?://", query):
        print("זוהתה כתובת RSS ישירה.")
        candidates = [make_candidate("", "", query, "קלט ישיר")]
    else:
        candidates = search_all_sources(query)

    print(f"נאספו {len(candidates)} תוצאות גולמיות.")
    candidates = dedupe(candidates)
    print(f"אחרי ניקוי כפילויות: {len(candidates)}.")

    for c in candidates:
        c["score"] = base_score(query, c)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    to_check = candidates[:CHECK_TOP]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        checks = list(pool.map(check_feed, to_check))
    for c, info in zip(to_check, checks):
        c["check"] = info
        apply_activity(c, query)

    to_check.sort(key=lambda c: c["score"], reverse=True)
    results = to_check[:MAX_RESULTS]

    for c in results:
        print(f"- {c['title']} ({c['artist']}) [{c['score']:.2f}] {status_line(c)}")

    write_outputs(query, results, repo)
    print("הקבצים נכתבו.")


if __name__ == "__main__":
    main()
