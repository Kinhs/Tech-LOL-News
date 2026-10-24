import http.client
import http.server
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import news
import server

PNG = b"\x89PNG" + b"0" * 20


class ServerTest(unittest.TestCase):
    """Runs the real Handler on a free port, with a throw-away database and icon folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "icons").mkdir()
        (tmp / "icons" / "good-site.png").write_bytes(PNG)
        self.patches = [mock.patch.object(news, "DB_PATH", tmp / "t.db"), mock.patch.object(news, "_ready", False),
                        mock.patch.object(news, "ICONS", tmp / "icons")]
        for p in self.patches:
            p.start()
        db = news.connect()
        db.execute("INSERT INTO items (url, title, summary, source, ts, tags) VALUES ('https://a/1', 'Hello', 's', 'Good Site', ?, '[\"AI\"]')", (time.time(),))
        db.commit(); db.close()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.host = f"127.0.0.1:{self.port}"
        hosts = mock.patch.object(server, "HOSTS", {self.host})
        hosts.start(); self.patches.append(hosts)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers={"Host": self.host, **(headers or {})})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, r.getheader("Content-Type"), data

    def test_pages_and_shared_assets_are_served_but_nothing_else(self):
        for path in ("/", "/lol", "/common.css", "/common.js"):
            self.assertEqual(self.call("GET", path)[0], 200, path)
        for path in ("/server.py", "/news.db", "/feeds.toml", "/common.py", "/..%2Fserver.py", "/etc/passwd"):
            self.assertEqual(self.call("GET", path)[0], 404, path)

    def test_wrong_host_or_origin_is_refused(self):
        self.assertEqual(self.call("GET", "/api/items", headers={"Host": "evil.example"})[0], 403)  # DNS rebinding
        self.assertEqual(self.call("POST", "/api/mark", body="{}", headers={"Origin": "http://evil.example"})[0], 403)  # cross-site form
        self.assertEqual(self.call("POST", "/api/mark", body="{}", headers={"Origin": f"http://{self.host}"})[0], 400)  # same site is let in

    def test_icons_only_from_the_cache_folder(self):
        status, ctype, data = self.call("GET", "/icons/good-site.png")
        self.assertEqual((status, ctype, data), (200, "image/png", PNG))
        for path in ("/icons/../server.py", "/icons/..%2Fserver.py", "/icons/missing.png", "/icons/good-site.svg", "/icons/good-site.png/x"):
            self.assertEqual(self.call("GET", path)[0], 404, path)

    def test_mark_validates_input_and_updates_the_row(self):
        for bad in ("not json", "[]", "{}", '{"url": 5, "read": true}', '{"url": "https://a/1"}', '{"url": "https://a/1", "read": "yes"}'):
            self.assertEqual(self.call("POST", "/api/mark", body=bad)[0], 400, bad)
        self.assertEqual(self.call("POST", "/api/mark", body='{"url": "https://a/1", "read": true, "starred": true}')[0], 200)
        status, _, data = self.call("GET", "/api/items?days=3")
        item = json.loads(data)["items"][0]
        self.assertEqual((status, item["read"], item["starred"]), (200, True, True))
        self.assertEqual(self.call("POST", "/api/mark", body='{"url": "https://a/1", "starred": false}')[0], 200)
        self.assertFalse(json.loads(self.call("GET", "/api/items?days=3")[2])["items"][0]["starred"])

    def test_negative_content_length_does_not_hang(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(f"POST /api/mark HTTP/1.1\r\nHost: {self.host}\r\nContent-Length: -5\r\n\r\n".encode())
            self.assertIn(b" 400 ", s.recv(200))  # answered at once instead of waiting for data that never comes

    def test_days_parameter_is_checked(self):
        self.assertEqual(self.call("GET", "/api/items?days=abc")[0], 400)
        self.assertEqual(self.call("GET", "/api/items?days=99999")[0], 200)

    def test_failed_refresh_gives_a_500_and_the_server_keeps_working(self):
        with mock.patch.object(server, "do_refresh", side_effect=RuntimeError("boom")):
            status, _, data = self.call("POST", "/api/refresh", body="{}")
        self.assertEqual((status, json.loads(data)["error"]), (500, "boom"))
        self.assertEqual(self.call("GET", "/api/items?days=3")[0], 200)

    def test_is_ours_tells_this_app_from_another_server(self):
        self.assertTrue(server.is_ours(self.port))
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            self.assertFalse(server.is_ours(other.server_address[1]))  # some other program on the port
        finally:
            other.shutdown(); other.server_close()
        self.assertFalse(server.is_ours(1))  # nothing listening


class AutoRefreshTest(unittest.TestCase):
    def test_one_failed_refresh_does_not_end_the_loop(self):
        calls = []

        def due():
            calls.append(1)
            if len(calls) == 1:
                raise KeyError("topic")  # e.g. a feed added to feeds.toml without a topic line

        with mock.patch.object(server, "refresh_if_due", side_effect=due), \
                mock.patch.object(server.time, "sleep", side_effect=[None, None, SystemExit]):
            with self.assertRaises(SystemExit):
                server.auto_refresh()
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
