# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Local tech-news reader: RSS feeds plus a League of Legends / Wild Rift patch tracker, served as a small web UI. Python standard library only (needs 3.11+ for `tomllib`); there is nothing to install and no build step.

## Commands

```bash
python3 server.py              # start the UI on http://localhost:8000 and open the browser (--no-open to skip)
python3 news.py                # one-off refresh that also appends new items to digest/YYYY-MM-DD.md
python3 -m unittest -q         # all tests (test_server.py starts the real server on a free port with a temp database)
python3 -m unittest test_lol.LolTest.test_icons   # a single test
```

`run.sh` / `run.bat` are double-click launchers for `server.py`.

## Architecture

- **`news.py`**: feed pipeline and shared helpers. `refresh(db, cfg)` fetches every feed in `feeds.toml` in parallel, tags each item with *every* matching topic (whole-word keyword match on title + summary, falling back to the feed's own `topic`), inserts new rows into SQLite, prunes items older than `keep_days` unless starred, then caches source favicons (`fetch_icons`). `init_db()` owns the schema for **all** tables (`items`, `meta`, `lol_items`) in `news.db` and migrates older databases with `ALTER TABLE`; `connect()` runs it once per process, then only opens a connection. Never hold a write transaction (`with db:`) while fetching or tagging: it blocks star/read clicks. `fetch()` has a size cap and a total deadline; keep using it, not `urlopen` directly.
- **`lol.py`**: LoL / Wild Rift tracker, stored in `lol_items`. `refresh(db)` scrapes Riot's news pages (the article list is JSON inside `<script id="__NEXT_DATA__">`), keeps patch notes and champion articles, then fetches each patch page once and parses the champions / items / runes changed (headings differ by game: LoL names are `h3`, Wild Rift names are `h4` plus a `characterChanges` JSON blade). Riot publishes no buff/nerf label, so each entry gets a guess from the `old ⇒ new` numbers (`_verdict`) which is overridden by the label from riftpatchnotes.com (`parse_labels` / `apply_labels`; `verdict_source` records which one is shown). Icons come from Riot's Data Dragon CDN (`refresh_icons` caches the name→URL index in `meta`), with riftpatchnotes.com images as a fallback (`site_icon`).
- **`server.py`**: `ThreadingHTTPServer` on `127.0.0.1:8000`. Serves `ui.html` (`/`), `lol.html` (`/lol`), the shared `common.css` / `common.js` (only those two names are served from disk), JSON APIs (`/api/items`, `/api/lol`, `/api/refresh`, `/api/mark`) and cached favicons (`/icons/<name>`). A background thread runs `do_refresh()` (news + lol, one shared lock) every `refresh_minutes`; errors there are printed, never fatal. If port 8000 is taken by another program, the launcher exits with a message instead of opening it.
- **Lifecycle**: each open page holds an `EventSource('/api/live')` connection; when the last one drops, a watchdog shuts the server down after `IDLE_GRACE` seconds (60 s grace for the very first page). So the server exits shortly after the tab closes — this is intended.
- **UI**: `ui.html` and `lol.html` are vanilla JS pages that share `common.css` (theme, header, buttons) and `common.js` (`el()`, `loadState()`, and `createPage()`, which polls the API, redraws only when data changed, shows status/errors and drives Refresh). Page-specific code stays in each file; change shared behaviour once in `common.*`. Feed/Riot text is untrusted: build DOM with `textContent` / `el()` only, never `innerHTML`, and only allow `http(s)` links and known icon hosts.
- **Config**: `feeds.toml` holds topics (keywords), topic emojis, feeds, and the retention/refresh settings. Adding a topic or feed needs no code change; the UI reads topics from the API.

## Gotchas

- The server loads Python modules once at start; only the HTML, `common.css` and `common.js` files are re-read per request. After editing `.py` files, stop the running server (close the page) and start it again. An old server still on port 8000 makes a new `server.py` exit silently into "just open the browser".
- Tests mock `news.fetch`. Any test that calls `news.refresh` must also patch `news.fetch_icons` (or `news.ICONS`), otherwise it writes into the real `icons/` folder.
- Open text files with `encoding="utf-8"` (`feeds.toml` contains emoji); Windows defaults to cp1252.
- `news.db`, `icons/` and `digest/` are runtime data and git-ignored.
- Riot and riftpatchnotes.com are scraped, not APIs: a layout change shows up as a printed `warn:` line and empty sections, not a crash. Old Wild Rift patches (7.3 and earlier) use a different layout and parse into some junk entries.
