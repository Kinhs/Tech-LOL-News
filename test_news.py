import json
from contextlib import closing
import sqlite3
import unittest
from datetime import datetime
from unittest import mock

import news

TOPICS = {"AI": ["ai", "openai"], "Phones": ["iphone"], "Tech": []}

RSS = b"""<rss><channel>
<item><title>New iPhone</title><link>https://a/1</link><pubDate>Thu, 15 Oct 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Fast &amp;amp; &lt;b&gt;thin&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>No link</title></item>
</channel></rss>"""

ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>OpenAI ships</title><link rel="alternate" href="https://b/2"/><updated>2026-10-15T10:00:00Z</updated>
<summary>Short</summary></entry>
</feed>"""


class NewsTest(unittest.TestCase):
    def test_parse_rss_and_atom(self):
        self.assertEqual(news.parse_feed(RSS), [("New iPhone", "https://a/1", 1792058400.0, "Fast & thin")])
        self.assertEqual(news.parse_feed(ATOM), [("OpenAI ships", "https://b/2", 1792058400.0, "Short")])

    def test_clean_truncates(self):
        out = news._clean("word " * 200)
        self.assertLessEqual(len(out), 301)
        self.assertTrue(out.endswith("…"))

    def test_tag_multi_and_whole_words(self):
        self.assertEqual(news.tag("OpenAI iPhone app", "Tech", TOPICS), ["AI", "Phones"])
        self.assertEqual(news.tag("He said hello", "Tech", TOPICS), ["Tech"])  # "ai" inside "said" must not match

    def test_icons_cached_once_and_only_real_images(self):
        import tempfile
        from pathlib import Path
        feeds = [{"name": "Good Site", "url": "https://feeds.good.test/rss"}, {"name": "Bad Site", "url": "https://bad.test/rss", "icon": "https://bad.test/i.svg"}]
        png = b"\x89PNG" + b"0" * 20
        def fake(url, **kw):
            if url.endswith("/") and "good" in url:
                return b'<html><link rel="shortcut icon" href="/static/f.png"></html>'
            if url == "https://good.test/static/f.png":
                return png
            if url.endswith(".svg"):
                return b"<svg onload='alert(1)'/>"  # not an accepted image type
            raise OSError("nope")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(news, "ICONS", Path(tmp) / "icons"), mock.patch.object(news, "fetch", side_effect=fake) as f:
            news.fetch_icons({"feeds": feeds})
            self.assertEqual(news.icon_files({"feeds": feeds}), {"Good Site": "good-site.png"})  # svg rejected, miss recorded
            self.assertTrue((news.ICONS / "bad-site.none").exists())
            calls = f.call_count
            news.fetch_icons({"feeds": feeds})
            self.assertEqual(f.call_count, calls)  # cached hit and fresh miss are not fetched again

    def test_icon_files_without_cache_folder(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(news, "ICONS", Path(tmp) / "missing"):
            self.assertEqual(news.icon_files({"feeds": [{"name": "X", "url": "https://x.test"}]}), {})

    def test_fetch_stops_at_size_limit_and_deadline(self):
        class Slow:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read1(self, n): return b"x" * n  # never ends
        with mock.patch.object(news.urllib.request, "urlopen", return_value=Slow()):
            with self.assertRaisesRegex(ValueError, "over 100000 bytes"):
                news.fetch("https://x.test", limit=100_000)
            ticks = iter(range(0, 1000, 30))  # each check of the clock moves 30 s forward
            with mock.patch.object(news.time, "monotonic", side_effect=lambda: next(ticks)):
                with self.assertRaisesRegex(TimeoutError, "took over 60 s"):
                    news.fetch("https://x.test", limit=10**12)

    def test_fetch_deadline_works_on_a_real_server_that_drips_bytes(self):
        import socket, threading, time
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        self.addCleanup(srv.close)

        def drip():
            c, _ = srv.accept()
            c.recv(1000); c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            try:
                while True:
                    c.sendall(b"x"); time.sleep(0.3)
            except OSError:
                pass
        threading.Thread(target=drip, daemon=True).start()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            news.fetch(f"http://127.0.0.1:{srv.getsockname()[1]}/", total=1.5)
        self.assertLess(time.monotonic() - start, 5)  # one byte per 0.3 s never fills a chunk, yet it still stops

    def test_connect_creates_schema_once_and_migrates_old_databases(self):
        import tempfile, sqlite3 as sql
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sql.connect(path)  # a database from before lol_items had details/other
            old.execute("CREATE TABLE lol_items (url TEXT PRIMARY KEY, game TEXT, kind TEXT, title TEXT, ts REAL, summary TEXT, champions TEXT, items TEXT, parsed INTEGER)")
            old.commit(); old.close()
            with mock.patch.object(news, "DB_PATH", path), mock.patch.object(news, "_ready", False), \
                    mock.patch.object(news, "init_db", wraps=news.init_db) as init:
                db = news.connect(); news.connect().close()
                self.assertEqual(init.call_count, 1)  # the schema is not rebuilt on every request
                cols = {r[1] for r in db.execute("PRAGMA table_info(lol_items)")}
                self.assertTrue({"details", "other"} <= cols)
                self.assertEqual(db.execute("SELECT count(*) FROM items").fetchone()[0], 0)
                db.close()

    def test_refresh_tags_outside_the_write_transaction_and_skips_known_urls(self):
        db = self.enterContext(closing(sqlite3.connect(":memory:", check_same_thread=False)))
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE items (url TEXT PRIMARY KEY, title TEXT, summary TEXT, source TEXT, ts REAL,
            tags TEXT, read INTEGER DEFAULT 0, starred INTEGER DEFAULT 0); CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);""")
        db.execute("INSERT INTO items (url, title, summary, source, ts, tags) VALUES ('https://b/2', 't', '', 'F', 1e12, '[]')")
        db.commit()
        cfg = {"topics": TOPICS, "feeds": [{"name": "F", "url": "x", "topic": "Tech"}], "keep_days": 100000}
        seen = []
        real = news.tag
        def spy(*a, **k):
            seen.append(db.in_transaction)
            return real(*a, **k)
        with mock.patch.object(news, "fetch", return_value=ATOM), mock.patch.object(news, "fetch_icons"), mock.patch.object(news, "tag", side_effect=spy):
            self.assertEqual(news.refresh(db, cfg), [])  # the only entry is already stored
        self.assertEqual(seen, [])  # so it was not tagged again

    def test_future_dated_story_is_clamped_to_now(self):
        db = self.enterContext(closing(sqlite3.connect(":memory:")))
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE items (url TEXT PRIMARY KEY, title TEXT, summary TEXT, source TEXT, ts REAL,
            tags TEXT, read INTEGER DEFAULT 0, starred INTEGER DEFAULT 0); CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);""")
        future = ATOM.replace(b"2026-10-15T10:00:00Z", b"2099-01-01T00:00:00Z")
        cfg = {"topics": TOPICS, "feeds": [{"name": "F", "url": "x", "topic": "Tech"}], "keep_days": 100000}
        with mock.patch.object(news, "fetch", return_value=future), mock.patch.object(news, "fetch_icons"):
            news.refresh(db, cfg)
        import time
        self.assertLessEqual(db.execute("SELECT ts FROM items").fetchone()[0], time.time())

    def test_render_groups_by_primary_topic_and_escapes(self):
        items = [{"title": "[x] AI", "url": "u", "ts": 0, "source": "S", "tags": ["AI", "Phones"]}]
        out = news.render(items, list(TOPICS), datetime(2026, 10, 16, 9, 0))
        self.assertIn("### AI", out)
        self.assertNotIn("### Phones", out)
        self.assertIn(r"- [\[x\] AI](u)", out)

    def test_refresh_dedupes_and_keeps_flags(self):
        db = self.enterContext(closing(sqlite3.connect(":memory:")))
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE items (url TEXT PRIMARY KEY, title TEXT, summary TEXT, source TEXT, ts REAL,
            tags TEXT, read INTEGER DEFAULT 0, starred INTEGER DEFAULT 0); CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);""")
        cfg = {"topics": TOPICS, "feeds": [{"name": "F", "url": "x", "topic": "Tech"}], "keep_days": 100000}
        with mock.patch.object(news, "fetch", return_value=ATOM), mock.patch.object(news, "fetch_icons"):
            first = news.refresh(db, cfg)
            db.execute("UPDATE items SET starred = 1")
            second = news.refresh(db, cfg)
        self.assertEqual([i["url"] for i in first], ["https://b/2"])
        self.assertEqual(second, [])
        row = db.execute("SELECT * FROM items").fetchone()
        self.assertEqual((json.loads(row["tags"]), row["starred"]), (["AI"], 1))


if __name__ == "__main__":
    unittest.main()
