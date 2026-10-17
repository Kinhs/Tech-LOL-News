import json
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
        def fake(url):
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

    def test_render_groups_by_primary_topic_and_escapes(self):
        items = [{"title": "[x] AI", "url": "u", "ts": 0, "source": "S", "tags": ["AI", "Phones"]}]
        out = news.render(items, list(TOPICS), datetime(2026, 10, 16, 9, 0))
        self.assertIn("### AI", out)
        self.assertNotIn("### Phones", out)
        self.assertIn(r"- [\[x\] AI](u)", out)

    def test_refresh_dedupes_and_keeps_flags(self):
        db = sqlite3.connect(":memory:")
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
