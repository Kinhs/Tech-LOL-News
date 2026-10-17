import json
import sqlite3
import unittest
from unittest import mock

import lol
import news


def page(blades, body=""):
    data = {"props": {"pageProps": {"page": {"blades": blades}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>{body}</html>'


def card(title, path, when, tags=()):
    return {"title": title, "action": {"payload": {"url": path}}, "publishedAt": when,
            "description": {"body": "<p>Hi &amp; bye</p>"}, "tags": [{"machineName": t} for t in tags]}


LIST = page([{"type": "articleCardGrid", "items": [
    card("League of Legends Patch 26.20 Notes", "/en-us/news/league-of-legends-patch-26-20-notes", "2026-10-06T18:00:00.000Z", ["patch_notes"]),
    card("Locke Champion Spotlight", "/en-us/news/locke", "2026-06-09T10:00:00.000Z"),
    card("Introducing the Worlds Merch", "/en-us/news/merch", "2026-10-07T10:00:00.000Z"),
]}])

LOL_PATCH = page([], "<h2>Patch Highlights</h2><h2>Champions</h2>"
                     '<h3 class="change-title">Ambessa</h3><blockquote><p>Too strong.</p></blockquote>'
                     "<h4>Passive</h4><ul><li><strong>Damage</strong>: 5 - 30 \u21d2 <strong>5 - 25</strong></li></ul>"
                     "<h3>K'Sante</h3><ul><li>Mana Cost: 40 \u21d2 45</li><li>Damage: 10 \u21d2 12</li></ul>"
                     "<h2>Items</h2><h3>Runaan&#x27;s Hurricane</h3><h4>Cleanup</h4><h2>ARAM: Mayhem</h2><h3>Not Counted</h3>")

WR_PATCH = page([{"type": "characterChanges", "characters": [
    {"character": {"name": "KAI'SA"}, "summary": {"body": "<p>Weak.</p>"},
     "changes": [{"title": "Q", "description": {"body": "<ul><li>Cooldown: 8s \u2192 6s</li></ul>"}}]},
    {"character": {"name": "VOLIBEAR"}, "changes": []}]}],
                "<h2>Items</h2><h4>Thornmail</h4><p>Base Stats</p><ul><li>Price: 2700 \u2192 2850</li></ul>"
                "<h2>Runes</h2><h4>Aery</h4><h2>BUG FIXES</h2>")


LABELS_PAGE = page([], '<h2>Jump to changes</h2><h3>Buffs (1)</h3><a href="#a">Ambessa</a>')
LABELS = ("<h2>Jump to changes</h2>"
          '<h3 class="x">Buffs<!-- --> (<!-- -->1<!-- -->)</h3><a href="#ambessa" class="c"><img src="/champions/ambessa.png"/>Ambessa</a>'
          '<h3>Nerfs (2)</h3><a href="#k">K&#x27;Sante</a><a href="#r">Runaan&#x27;s Hurricane</a>'
          '<h3>Changes (1)</h3><a href="#n">Nobody</a><h3>Ambessa</h3><a href="#late">Late Body Link</a>')


class LolTest(unittest.TestCase):
    def test_articles_and_classify(self):
        found = lol.articles(LIST, "https://x.test")
        self.assertEqual([lol.classify(a) for a in found], ["patch", "champion", None])
        self.assertEqual(found[0]["url"], "https://x.test/en-us/news/league-of-legends-patch-26-20-notes")
        self.assertEqual(found[0]["summary"], "Hi & bye")

    def test_parse_patch_lol(self):
        p = lol.parse_patch(LOL_PATCH, "lol")
        self.assertEqual([(e["kind"], e["name"], e["verdict"]) for e in p["entries"]],
                         [("champions", "Ambessa", "nerf"), ("champions", "K'Sante", "mixed"), ("items", "Runaan's Hurricane", "adjust")])
        ambessa = p["entries"][0]
        self.assertEqual((ambessa["reason"], ambessa["changes"][0]["group"], ambessa["changes"][0]["stat"]), ("Too strong.", "Passive", "Damage"))
        self.assertEqual(p["other"], ["ARAM: Mayhem"])  # section headings are listed, their h3s are not entries

    def test_parse_patch_wild_rift(self):
        p = lol.parse_patch(WR_PATCH, "wr")
        self.assertEqual([(e["kind"], e["name"], e["verdict"]) for e in p["entries"]],
                         [("items", "Thornmail", "nerf"), ("runes", "Aery", "adjust"), ("champions", "Kai'Sa", "buff"), ("champions", "Volibear", "adjust")])
        self.assertEqual(p["other"], ["Bug Fixes"])

    def test_labels_url(self):
        self.assertEqual(lol.labels_url("lol", "https://x/en-us/news/game-updates/league-of-legends-patch-26-20-notes"),
                         "https://www.riftpatchnotes.com/lol/patch/26-20")
        self.assertEqual(lol.labels_url("wr", "https://x/en-us/news/game-updates/wild-rift-patch-notes-7-3b/"),
                         "https://www.riftpatchnotes.com/patch/wild-rift-patch-notes-7-3b")
        self.assertIsNone(lol.labels_url("lol", "https://x/en-us/news/some-article"))

    def test_parse_and_apply_labels(self):
        labels = lol.parse_labels(page([], LABELS))
        self.assertEqual({k: v["verdict"] for k, v in labels.items()},
                         {"ambessa": "buff", "ksante": "nerf", "runaanshurricane": "nerf", "nobody": "adjust"})  # stops at the body h3
        self.assertEqual(labels["ambessa"]["icon"], "https://www.riftpatchnotes.com/champions/ambessa.png")
        self.assertIsNone(labels["ksante"]["icon"])
        entries = lol.parse_patch(LOL_PATCH, "lol")["entries"]  # Ambessa guessed nerf, K'Sante guessed mixed
        lol.apply_labels(entries, labels)
        self.assertEqual([(e["name"], e["verdict"], e["verdict_source"], e["guess"]) for e in entries],
                         [("Ambessa", "buff", "riftpatchnotes.com", "nerf"), ("K'Sante", "nerf", "riftpatchnotes.com", "mixed"),
                          ("Runaan's Hurricane", "nerf", "riftpatchnotes.com", "adjust")])
        self.assertEqual(entries[0]["site_icon"], "https://www.riftpatchnotes.com/champions/ambessa.png")
        lol.apply_labels(entries, {})  # site has nothing: fall back to the guess
        self.assertEqual((entries[0]["verdict"], entries[0]["verdict_source"], entries[0]["site_icon"]), ("nerf", "inferred", None))

    def test_icons(self):
        icons = lol.build_icons("16.1.1",
                                {"KSante": {"name": "K'Sante", "image": {"full": "KSante.png"}}},
                                {"223075": {"name": "Thornmail"}, "3075": {"name": "Thornmail"}},
                                [{"slots": [{"runes": [{"name": "Electrocute", "icon": "perk-images/E.png"}]}]}])
        self.assertEqual(icons["champions:ksante"], "https://ddragon.leagueoflegends.com/cdn/16.1.1/img/champion/KSante.png")
        self.assertTrue(icons["items:thornmail"].endswith("/img/item/3075.png"))  # lowest id wins over the Arena copy
        self.assertEqual(icons["runes:electrocute"], "https://ddragon.leagueoflegends.com/cdn/img/perk-images/E.png")
        self.assertEqual(lol._icon(icons, "items", "[NEW] K'Sante")[0], "champions")  # tag ignored; kind fixed from the index
        self.assertEqual(lol._icon(icons, "champions", "Thornmail (Boots upgrade)")[0], "items")
        self.assertEqual(lol._icon(icons, "items", "Unknown Thing"), ("items", None))

    def test_verdict_direction(self):
        self.assertEqual(lol._verdict("Damage", "5 - 30", "5 - 25"), "nerf")
        self.assertEqual(lol._verdict("Mana Cost", "40 / 45", "30 / 35"), "buff")  # lower cost is a buff
        self.assertEqual(lol._verdict("Cooldown", "8s", "10s"), "nerf")
        self.assertEqual(lol._verdict("Base Mana", "400", "450"), "buff")  # "mana" alone is not a cost
        self.assertEqual(lol._verdict("Passive", "old text", "new text"), "adjust")

    def test_refresh_stores_once_and_parses_patch(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""CREATE TABLE lol_items (url TEXT PRIMARY KEY, game TEXT, kind TEXT, title TEXT, ts REAL, summary TEXT,
            champions TEXT DEFAULT '[]', items TEXT DEFAULT '[]', parsed INTEGER DEFAULT 0, details TEXT, other TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);""")
        ddragon = {"versions.json": '["1.0.0"]', "champion.json": '{"data": {}}', "item.json": '{"data": {}}', "runesReforged.json": "[]"}
        fake = lambda url: (next((v for k, v in ddragon.items() if url.endswith(k)), None)
                            or (LABELS_PAGE if "riftpatchnotes" in url else LOL_PATCH if "patch-26-20-notes" in url else LIST)).encode()
        with mock.patch.object(news, "fetch", side_effect=fake):
            first = lol.refresh(db)
            second = lol.refresh(db)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)
        patch = lol.rows(db)["lol"]["patches"][0]
        self.assertEqual((patch["parsed"], patch["champions"], patch["items"], patch["other"]),
                         (1, ["Ambessa", "K'Sante"], ["Runaan's Hurricane"], ["ARAM: Mayhem"]))
        self.assertEqual(patch["details"][0]["verdict"], "buff")  # the site's label beats my guess (nerf)
        self.assertEqual(patch["details"][1]["verdict_source"], "inferred")  # K'Sante is not on the site's list
        self.assertEqual(len(lol.rows(db)["lol"]["champions"]), 1)


if __name__ == "__main__":
    unittest.main()
