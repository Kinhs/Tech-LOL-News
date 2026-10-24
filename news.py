"""Fetch tech RSS/Atom feeds into news.db and append new items to a daily Markdown digest."""
import html
import json
import re
import sqlite3
import sys
import time
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
ATOM = "{http://www.w3.org/2005/Atom}"
USER_AGENT = "Mozilla/5.0 (tech-news-digest)"
ICONS = ROOT / "icons"  # cached source favicons, served by server.py
IMAGE_TYPES = ((b"\x89PNG", "png"), (b"\x00\x00\x01\x00", "ico"), (b"GIF8", "gif"), (b"\xff\xd8\xff", "jpg"))  # no SVG: it can carry scripts


def load_cfg():
    return tomllib.loads((ROOT / "feeds.toml").read_text(encoding="utf-8"))  # explicit: Windows defaults to cp1252


DB_PATH = ROOT / "news.db"
_ready = False


def init_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            url TEXT PRIMARY KEY, title TEXT, summary TEXT, source TEXT, ts REAL,
            tags TEXT, read INTEGER DEFAULT 0, starred INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS lol_items (
            url TEXT PRIMARY KEY, game TEXT, kind TEXT, title TEXT, ts REAL, summary TEXT,
            parsed INTEGER DEFAULT 0, details TEXT, other TEXT);
    """)
    have = {r[1] for r in db.execute("PRAGMA table_info(lol_items)")}
    for col in ("details", "other"):  # databases created before these columns existed
        if col not in have:
            db.execute(f"ALTER TABLE lol_items ADD COLUMN {col} TEXT")
    db.commit()
    db.close()


def connect():
    global _ready
    if not _ready:
        init_db()
        _ready = True
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _timestamp(text, rss):
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text) if rss else datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _clean(text, limit=300):
    """Strip tags/entities from feed HTML and cut to a short plain-text snippet."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _text(el):
    return "".join(el.itertext()) if el is not None else ""


def parse_feed(data):
    """Return [(title, link, timestamp_or_None, summary)] from RSS 2.0 or Atom bytes."""
    root = ET.fromstring(data)
    out = []
    # shortcut: RSS 1.0 (RDF, namespaced <item>) is not handled; add it if a feed needs it.
    for item in root.iter("item"):
        out.append((item.findtext("title", ""), item.findtext("link", ""),
                    _timestamp(item.findtext("pubDate"), rss=True), item.findtext("description")))
    for entry in root.iter(ATOM + "entry"):
        link = next((l.get("href", "") for l in entry.findall(ATOM + "link") if l.get("rel", "alternate") == "alternate"), "")
        date = entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated")
        body = _text(entry.find(ATOM + "summary")) or _text(entry.find(ATOM + "content"))
        out.append((entry.findtext(ATOM + "title", ""), link, _timestamp(date, rss=False), body))
    return [(" ".join(t.split()), l.strip(), ts, _clean(s)) for t, l, ts, s in out if l.strip()]


def compile_topics(topics):
    """[(topic, regex)]: one whole-word regex per topic. A topic with no keywords never matches."""
    return [(name, re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w.lower()) for w in words) + r")(?!\w)"))
            for name, words in topics.items() if words]


def tag(text, default, topics):
    """Every topic with a keyword hit in text; the feed's default topic if none. topics: dict or compile_topics()."""
    compiled = compile_topics(topics) if isinstance(topics, dict) else topics
    text = text.lower()
    return [name for name, rx in compiled if rx.search(text)] or [default]


def fetch(url, limit=8_000_000, total=60):
    """GET url. The 20 s timeout only covers each wait, so also stop at `limit` bytes or `total` seconds
    (a server that drips one byte at a time would otherwise hold a refresh for ever)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    end, chunks, size = time.monotonic() + total, [], 0
    with urllib.request.urlopen(req, timeout=20) as resp:
        while chunk := resp.read1(65536):  # read1: return what has arrived, so the deadline is checked while data drips in
            size += len(chunk)
            if size > limit:
                raise ValueError(f"response over {limit} bytes: {url}")
            if time.monotonic() > end:
                raise TimeoutError(f"download took over {total} s: {url}")
            chunks.append(chunk)
    return b"".join(chunks)


def _load_feed(feed):
    try:
        return feed, parse_feed(fetch(feed["url"]))
    except Exception as e:  # one broken feed must not stop the rest
        print(f"warn: {feed['name']}: {e}", file=sys.stderr)
        return feed, []


def refresh(db, cfg):
    """Fetch all feeds, store unseen items, prune old ones. Returns the new items."""
    now = time.time()
    cutoff = now - cfg.get("keep_days", 30) * 86400
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(_load_feed, cfg["feeds"]))
    topics = compile_topics(cfg["topics"])
    known = {r[0] for r in db.execute("SELECT url FROM items")}  # no point tagging what is already stored
    new = []
    for feed, entries in results:
        for title, url, ts, summary in entries:
            ts = min(ts or now, now)  # a feed that dates a story in the future must not pin it to the top
            if ts < cutoff or url in known:
                continue
            known.add(url)
            new.append({"title": title, "url": url, "ts": ts, "source": feed["name"], "summary": summary,
                        "tags": tag(f"{title} {summary}", feed["topic"], topics)})
    with db:  # the write lock is held only for these statements, never while fetching or tagging
        db.executemany("INSERT OR IGNORE INTO items (url, title, summary, source, ts, tags) VALUES (?,?,?,?,?,?)",
                       [(i["url"], i["title"], i["summary"], i["source"], i["ts"], json.dumps(i["tags"])) for i in new])
        db.execute("DELETE FROM items WHERE ts < ? AND starred = 0", (cutoff,))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('last_refresh', ?)", (str(now),))
    try:
        fetch_icons(cfg)
    except Exception as e:  # icons are cosmetic
        print(f"warn: icons: {e}", file=sys.stderr)
    return new


def icon_slug(name):
    return re.sub(r"\W+", "-", name.lower()).strip("-")


def icon_files(cfg):
    """{source name: file name} for the favicons cached so far."""
    present = {p.name for p in ICONS.iterdir()} if ICONS.is_dir() else set()
    out = {}
    for feed in cfg["feeds"]:
        slug = icon_slug(feed["name"])
        found = next((f"{slug}.{ext}" for _, ext in IMAGE_TYPES if f"{slug}.{ext}" in present), None)
        if found:
            out[feed["name"]] = found
    return out


def _find_icon(feed):
    """(bytes, ext) of a site's favicon: the `icon` URL from feeds.toml, else the homepage's <link rel=icon>, else /favicon.ico."""
    host = re.sub(r"^(www|feeds?)\.", "", urllib.parse.urlparse(feed["url"]).hostname)
    urls = [feed["icon"]] if feed.get("icon") else []
    for h in [host] + ([host.split(".", 1)[1]] if host.count(".") >= 2 else []):
        base = f"https://{h}/"
        try:
            for tag in re.findall(r"<link\b[^>]*>", fetch(base).decode("utf-8", "ignore"), re.I):
                if re.search(r"rel=[\"'][^\"']*icon", tag, re.I) and (m := re.search(r"href=[\"']([^\"']+)", tag)):
                    urls.append(urllib.parse.urljoin(base, html.unescape(m.group(1))))
        except Exception:
            pass
        urls.append(base + "favicon.ico")
    for url in urls:
        try:
            data = fetch(url, limit=300_000)
        except Exception:
            continue
        ext = next((e for magic, e in IMAGE_TYPES if data.startswith(magic)), None)
        if ext and len(data) < 300_000:
            return data, ext
    return None, None


def fetch_icons(cfg):
    """Cache each source's favicon in icons/ once, so the page never contacts the sites. A miss is retried after a week."""
    ICONS.mkdir(exist_ok=True)
    have = icon_files(cfg)

    def one(feed):
        miss = ICONS / f"{icon_slug(feed['name'])}.none"
        if feed["name"] in have or (miss.exists() and time.time() - miss.stat().st_mtime < 7 * 86400):
            return
        data, ext = _find_icon(feed)
        if data:
            (ICONS / f"{icon_slug(feed['name'])}.{ext}").write_bytes(data)
        else:
            miss.touch()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, cfg["feeds"]))


def render(items, order, run_time):
    """Digest block grouped by each item's first (primary) topic, newest first."""
    lines = [f"## {run_time:%H:%M}", ""]
    for topic in order:
        group = sorted((i for i in items if i["tags"][0] == topic), key=lambda i: i["ts"], reverse=True)
        if not group:
            continue
        lines += [f"### {topic}", ""]
        for i in group:
            title = i["title"].replace("[", r"\[").replace("]", r"\]")
            lines.append(f"- [{title}]({i['url']}) — {i['source']}, {datetime.fromtimestamp(i['ts']):%m-%d %H:%M}")
        lines.append("")
    return "\n".join(lines)


def main():
    cfg = load_cfg()
    db = connect()
    recent_cutoff = time.time() - cfg.get("max_age_days", 2) * 86400
    new = [i for i in refresh(db, cfg) if i["ts"] >= recent_cutoff]
    if not new:
        print("no new items")
        return
    run_time = datetime.now()
    path = ROOT / "digest" / f"{run_time:%Y-%m-%d}.md"
    path.parent.mkdir(exist_ok=True)
    header = "" if path.exists() else f"# Tech news {run_time:%Y-%m-%d}\n\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(header + render(new, list(cfg["topics"]), run_time) + "\n")
    print(f"{len(new)} new items -> {path}")


if __name__ == "__main__":
    main()
