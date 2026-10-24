"""League of Legends / Wild Rift: new champions and the champions + items changed in each patch.

shortcut: reads the JSON Riot embeds in its news pages (__NEXT_DATA__) and patch-note headings;
a site redesign breaks it (warning printed, section stays empty). Upgrade: Riot has no public feed.
"""
import html
import json
import re
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import news

SOURCES = {
    "lol": ("https://www.leagueoflegends.com", ["/en-us/news/", "/en-us/news/tags/patch-notes/"]),
    "wr": ("https://wildrift.leagueoflegends.com", ["/en-us/news/", "/en-us/news/game-updates/"]),
}
KEEP_PATCHES, KEEP_CHAMPIONS = 12, 20  # newest per game
DDRAGON = "https://ddragon.leagueoflegends.com"  # Riot's public asset CDN: champion, item and rune icons
LABEL_NAME = "riftpatchnotes.com"
LABEL_SITE = f"https://www.{LABEL_NAME}"  # second opinion: it lists buffs / nerfs / changes per patch
RELABEL_DAYS = 10  # a patch still on guessed labels is re-checked against the site for this long (it can lag Riot)
CHAMPION_TITLE = re.compile(r"champion (overview|spotlight|trailer|roadmap|release|update)|\brework\b", re.I)


def _next_data(page):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    return json.loads(m.group(1))["props"]["pageProps"]["page"]["blades"]


def _name(fragment):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def articles(page, base):
    """[{title, url, ts, summary, tags}] from the article list embedded in a Riot news page."""
    out = []
    for blade in _next_data(page):
        for it in blade.get("items") or []:
            path = ((it.get("action") or {}).get("payload") or {}).get("url")
            when = it.get("publishedAt") or (it.get("analytics") or {}).get("publishDate")
            if not (it.get("title") and path and when):
                continue
            if path.startswith("/"):
                url = base + path
            elif path.startswith(base + "/"):
                url = path
            else:  # a link to another site: never fetch it from here
                continue
            out.append({"title": it["title"], "url": url,
                        "ts": datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp(),
                        "summary": news._clean((it.get("description") or {}).get("body", "")),
                        "tags": [t.get("machineName") for t in it.get("tags") or []]})
    return out


def classify(a):
    if "patch_notes" in a["tags"] or re.search(r"\bpatch\b.*\bnotes\b", a["title"], re.I):
        return "patch"
    return "champion" if CHAMPION_TITLE.search(a["title"]) else None


ARROW = re.compile(r"\s*(?:⇒|→)\s*")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
# stats where a lower number is the buff
LOWER_IS_BETTER = re.compile(r"cooldown|cool down|\bcost\b|price|delay|cast time|wind-?up|recharge|threshold|damage taken|\btaken\b", re.I)
TOKEN = re.compile(r"<h([2-4])[^>]*>(.*?)</h\1>|<(blockquote|li|p)\b[^>]*>(.*?)</\3>", re.S)
SECTIONS = (("champion", "champions"), ("item", "items"), ("rune", "runes"))
SKIP_H2 = {"", "patch highlights", "related articles", "appendix", "gameplay changes"}


def _verdict(stat, before, after):
    """buff / nerf / adjust, guessed from the numbers: more is better unless the stat is a cost or cooldown."""
    b, a = [float(x) for x in NUMBER.findall(before)], [float(x) for x in NUMBER.findall(after)]
    if not b or not a:
        return "adjust"
    if len(a) == len(b):
        d = sum((x > y) - (x < y) for x, y in zip(a, b))
    else:
        d = (sum(a) > sum(b)) - (sum(a) < sum(b))
    if LOWER_IS_BETTER.search(stat):
        d = -d
    return "buff" if d > 0 else "nerf" if d < 0 else "adjust"


def _change(group, text):
    text = _name(text)
    parts = ARROW.split(text, maxsplit=1)
    if len(parts) < 2:
        return {"group": group, "stat": "", "before": "", "after": text, "verdict": "adjust"}
    stat, _, before = re.split(r"(:|：)", parts[0], maxsplit=1) if re.search(r"[:：]", parts[0]) else ("", "", parts[0])
    return {"group": group, "stat": stat.strip(), "before": before.strip(), "after": parts[1],
            "verdict": _verdict(stat, before, parts[1])}


def _finish(entry):
    seen = {c["verdict"] for c in entry["changes"]} - {"adjust"}
    entry["verdict"] = "mixed" if len(seen) == 2 else seen.pop() if seen else "adjust"
    return entry


def parse_patch(page, game="lol"):
    """{entries, other}: every champion / item / rune changed (reason, each old => new change, and a guessed
    buff/nerf verdict), plus the names of the other sections (game modes, bugfixes, skins...)."""
    level_of_entry = "3" if game == "lol" else "4"  # LoL names are h3 (h4 = ability); Wild Rift names are h4
    entries, other, section, cur, group = [], [], None, None, ""
    for m in TOKEN.finditer(page):
        level, head, tag, body = m.groups()
        if level:
            name = _name(head)
            if level == "2":
                section, cur, group = next((k for w, k in SECTIONS if w in name.lower()), None), None, ""
                label = name.title() if name.isupper() and " " in name else name
                if not section and name.lower() not in SKIP_H2 and len(name) <= 40 and label not in other:
                    other.append(label)
            elif section and level == level_of_entry:
                cur, group = {"kind": section, "name": name, "reason": "", "changes": []}, ""
                entries.append(cur)
            elif cur and level == "4":
                group = name
        elif cur:
            text = _name(body)
            if tag == "blockquote":
                cur["reason"] = text
            elif tag == "p" and text:
                if len(text) <= 40 and not text.endswith("."):
                    group = text
                elif not cur["reason"]:
                    cur["reason"] = text
            elif tag == "li" and text:
                cur["changes"].append(_change(group, text))
    for blade in _next_data(page):
        if blade.get("type") != "characterChanges":
            continue
        for c in blade.get("characters") or []:
            changes = [_change(ch.get("title", ""), li) for ch in c.get("changes") or []
                       for li in re.findall(r"<li[^>]*>(.*?)</li>", (ch.get("description") or {}).get("body", ""), re.S)]
            entries.append({"kind": "champions", "name": c["character"]["name"].title(),
                            "reason": _name((c.get("summary") or {}).get("body", "")), "changes": changes})
    seen_names = set()
    unique = [e for e in entries if (e["kind"], e["name"]) not in seen_names and not seen_names.add((e["kind"], e["name"]))]
    return {"entries": [_finish(e) for e in unique], "other": other}


def labels_url(game, riot_url):
    """The riftpatchnotes page for a Riot patch-note URL, or None if the URL has no patch number."""
    if game == "lol":
        m = re.search(r"patch-(\d+-\d+)", riot_url)
        return f"{LABEL_SITE}/lol/patch/{m.group(1)}" if m else None
    return f"{LABEL_SITE}/patch/{riot_url.rstrip('/').rsplit('/', 1)[-1]}"


def _key(name):
    return re.sub(r"[^a-z0-9]", "", html.unescape(name).lower())


def parse_labels(page):
    """{name key: {verdict, icon}} from the 'Jump to changes' lists (Buffs (n) / Nerfs (n) / Changes (n)).
    icon is the site's own image of that champion / item / rune, or None."""
    start = page.find("Jump to changes")
    out, current = {}, None
    for m in re.finditer(r'<h3[^>]*>(.*?)</h3>|<a href="#[^"]+"[^>]*>(<img[^>]*>)?([^<]+)</a>', page[start:] if start >= 0 else ""):
        head, img, name = m.groups()
        if head is not None:
            word = re.sub(r"<[^>]+>", "", head).split()[0].lower() if head.strip() else ""
            current = {"buffs": "buff", "nerfs": "nerf", "changes": "adjust", "adjustments": "adjust"}.get(word, "stop")
            if current == "stop":  # the list block is over; what follows is the page body
                break
        elif current:
            src = re.search(r'src="(/[^"]+)"', img or "")
            icon = LABEL_SITE + src.group(1) if src and "placeholder" not in src.group(1) else None
            out.setdefault(_key(name), {"verdict": current, "icon": icon})
    return out


def apply_labels(entries, labels):
    """Prefer the site's label; keep my number-based guess as `guess`. verdict_source says which one is shown.
    site_icon is only a fallback for when Data Dragon has no icon."""
    for e in entries:
        e.setdefault("guess", e["verdict"])
        site = labels.get(_key(e["name"]))
        e["site_icon"] = site["icon"] if site else None
        e["verdict"], e["verdict_source"] = (site["verdict"], LABEL_NAME) if site else (e["guess"], "inferred")
    return entries


def build_icons(version, champions, items, runes):
    """{"kind:name key": icon url}. Items sharing a name (Arena copies) keep the lowest id, the normal item."""
    icons = {}
    for c in champions.values():
        icons[f"champions:{_key(c['name'])}"] = f"{DDRAGON}/cdn/{version}/img/champion/{c['image']['full']}"
    for item_id in sorted(items, key=int):
        icons.setdefault(f"items:{_key(items[item_id]['name'])}", f"{DDRAGON}/cdn/{version}/img/item/{item_id}.png")
    for tree in runes:
        for slot in tree["slots"]:
            for r in slot["runes"]:
                icons[f"runes:{_key(r['name'])}"] = f"{DDRAGON}/cdn/img/{r['icon']}"
    return icons


def _icon(icons, kind, name):
    """(kind, icon url) for a name; tags like "[NEW]" are ignored. Some old Wild Rift notes list items under the
    champions heading, so when the name is not of `kind`, the kind that does know it wins."""
    key = _key(re.sub(r"\[[^\]]*\]|\([^)]*\)", "", name))
    for k in (kind, "champions", "items", "runes"):
        if (url := icons.get(f"{k}:{key}")):
            return k, url
    return kind, None


def refresh_icons(db):
    """Download the icon index when Riot's game version changes (about every two weeks); else do nothing."""
    version = json.loads(news.fetch(f"{DDRAGON}/api/versions.json"))[0]
    have = db.execute("SELECT value FROM meta WHERE key = 'ddragon_version'").fetchone()
    if have and have["value"] == version:
        return
    load = lambda name: json.loads(news.fetch(f"{DDRAGON}/cdn/{version}/data/en_US/{name}.json"))
    icons = build_icons(version, load("champion")["data"], load("item")["data"], load("runesReforged"))
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('icons', ?)", (json.dumps(icons),))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('ddragon_version', ?)", (version,))


def _warn(what, e):
    print(f"warn: {what}: {e}", file=sys.stderr)


def prune(db):
    """Keep only the newest KEEP_PATCHES patches and KEEP_CHAMPIONS champion articles per game."""
    for kind, keep in (("patch", KEEP_PATCHES), ("champion", KEEP_CHAMPIONS)):
        db.execute("""DELETE FROM lol_items WHERE kind = ? AND url NOT IN (
                        SELECT url FROM lol_items k WHERE k.kind = lol_items.kind AND k.game = lol_items.game
                        ORDER BY k.ts DESC LIMIT ?)""", (kind, keep))


def refresh(db):
    """Store new patch / champion articles, then parse each patch note once. Returns how many rows were added."""
    chosen = {}
    for game, (base, paths) in SOURCES.items():
        found = {}
        for path in paths:
            try:
                for a in articles(news.fetch(base + path).decode(), base):
                    if (kind := classify(a)):
                        found[a["url"]] = {**a, "game": game, "kind": kind}
            except Exception as e:  # a changed page must not stop the rest
                _warn(base + path, e)
        for kind, keep in (("patch", KEEP_PATCHES), ("champion", KEEP_CHAMPIONS)):
            newest = sorted((a for a in found.values() if a["kind"] == kind), key=lambda a: a["ts"], reverse=True)
            chosen.update({a["url"]: a for a in newest[:keep]})
    with db:
        added = sum(db.execute("INSERT OR IGNORE INTO lol_items (url, game, kind, title, ts, summary) VALUES (?,?,?,?,?,?)",
                               (a["url"], a["game"], a["kind"], a["title"], a["ts"], a["summary"])).rowcount
                    for a in chosen.values())
        prune(db)

    def load(row):
        url, game, details = row
        try:
            entries, other = (json.loads(details), None) if details else (None, None)
            if entries is None:
                parsed = parse_patch(news.fetch(url).decode(), game)
                entries, other = parsed["entries"], parsed["other"]
            labels = {}
            if (target := labels_url(game, url)):
                try:
                    labels = parse_labels(news.fetch(target).decode())
                except urllib.error.HTTPError as e:  # 404: the site has no page for this patch (yet)
                    if e.code != 404:
                        raise
            return url, apply_labels(entries, labels), other
        except Exception as e:
            _warn(url, e)
            return url, None, None
    try:
        refresh_icons(db)
    except Exception as e:  # icons are cosmetic
        _warn("icons", e)
    recent = time.time() - RELABEL_DAYS * 86400
    todo = [(r["url"], r["game"], r["details"] if r["parsed"] else None) for r in db.execute(
        """SELECT url, game, details, parsed FROM lol_items WHERE kind = 'patch' AND
           (parsed = 0 OR details IS NULL OR details NOT LIKE '%"site_icon"%'
            OR (details LIKE '%"inferred"%' AND ts > ?))""", (recent,))]
    with ThreadPoolExecutor(6) as pool:  # network first, no lock held
        results = list(pool.map(load, todo))
    with db:
        for url, entries, other in results:
            if entries is None:
                continue
            db.execute("UPDATE lol_items SET details = ?, other = COALESCE(?, other), parsed = 1 WHERE url = ?",
                       (json.dumps(entries), json.dumps(other) if other is not None else None, url))
    return added


def rows(db):
    """Stored rows grouped for the page: {game: {patches: [...], champions: [...]}}, newest first."""
    out = {g: {"patches": [], "champions": []} for g in SOURCES}
    row = db.execute("SELECT value FROM meta WHERE key = 'icons'").fetchone()
    icons = json.loads(row["value"]) if row else {}
    for r in db.execute("SELECT url, game, kind, title, ts, summary, parsed, details, other FROM lol_items ORDER BY ts DESC"):
        d = {**dict(r), "details": json.loads(r["details"] or "[]"), "other": json.loads(r["other"] or "[]"),
             "labels_url": labels_url(r["game"], r["url"]) if r["kind"] == "patch" else None}
        for e in d["details"]:
            e["kind"], e["icon"] = _icon(icons, e["kind"], e["name"])
            e["icon"] = e["icon"] or e.get("site_icon")  # fallback for Wild Rift-only items and runes
        out[r["game"]]["patches" if r["kind"] == "patch" else "champions"].append(d)
    return out
