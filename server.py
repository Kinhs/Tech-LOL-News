"""Local news UI: python3 server.py. Closing the page stops the server."""
import json
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import lol
import news

HOST, PORT = "127.0.0.1", 8000
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}  # reject other Host/Origin: blocks DNS rebinding and cross-site POSTs
IDLE_GRACE = 4    # seconds with no page connected -> stop (a reload reconnects in time)
FIRST_PAGE_WAIT = 60  # seconds to wait for the first page after start

cfg = news.load_cfg()
refresh_lock = threading.Lock()
# Each open page holds a /api/live connection; when the last one drops, idle_since starts the countdown.
state = {"live": 0, "idle_since": time.time() + FIRST_PAGE_WAIT}
state_lock = threading.Lock()


def do_refresh():
    """Run one refresh; None if another is already running."""
    if not refresh_lock.acquire(blocking=False):
        return None
    try:
        db = news.connect()
        try:
            n = len(news.refresh(db, cfg))
            n += lol.refresh(db)
            return n
        finally:
            db.close()
    finally:
        refresh_lock.release()


def auto_refresh():
    every = cfg.get("refresh_minutes", 30) * 60
    while True:
        db = news.connect()
        row = db.execute("SELECT value FROM meta WHERE key = 'last_refresh'").fetchone()
        db.close()
        if time.time() - float(row["value"] if row else 0) >= every:
            do_refresh()
        time.sleep(60)


def watchdog(server):
    while True:
        time.sleep(1)
        idle = state["idle_since"]
        if idle and time.time() - idle > IDLE_GRACE:
            server.shutdown()
            return


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json", cache="no-store"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        origin = self.headers.get("Origin")
        if self.headers.get("Host") in HOSTS and (not origin or origin in {f"http://{h}" for h in HOSTS}):
            return True
        self.send(403, {"error": "forbidden"})
        return False

    def live(self):
        """Hold the connection open; a closed/crashed page makes a write fail and drops the count."""
        with state_lock:
            state["live"] += 1
            state["idle_since"] = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(1)
        except OSError:
            pass
        finally:
            with state_lock:
                state["live"] -= 1
                if state["live"] == 0:
                    state["idle_since"] = time.time()

    def do_GET(self):
        if not self.allowed():
            return
        url = urlparse(self.path)
        if url.path == "/":
            self.send(200, (news.ROOT / "ui.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path.startswith("/icons/"):
            name = url.path[len("/icons/"):]
            if not re.fullmatch(r"[a-z0-9-]+\.(png|ico|gif|jpg)", name) or not (news.ICONS / name).is_file():
                return self.send(404, {"error": "not found"})
            ctype = {"png": "image/png", "ico": "image/x-icon", "gif": "image/gif", "jpg": "image/jpeg"}[name.rsplit(".", 1)[1]]
            self.send(200, (news.ICONS / name).read_bytes(), ctype, cache="max-age=86400")
        elif url.path == "/lol":
            self.send(200, (news.ROOT / "lol.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/lol":
            db = news.connect()
            try:
                self.send(200, {**lol.rows(db), "refreshing": refresh_lock.locked()})
            finally:
                db.close()
        elif url.path == "/api/live":
            self.live()
        elif url.path == "/api/items":
            try:
                days = min(max(int(parse_qs(url.query).get("days", ["3"])[0]), 1), 365)
            except ValueError:
                return self.send(400, {"error": "bad days"})
            db = news.connect()
            try:
                rows = db.execute("SELECT * FROM items WHERE ts >= ? ORDER BY ts DESC LIMIT 3000",
                                  (time.time() - days * 86400,)).fetchall()
                last = db.execute("SELECT value FROM meta WHERE key = 'last_refresh'").fetchone()
            finally:
                db.close()
            items = [{**dict(r), "tags": json.loads(r["tags"]), "read": bool(r["read"]), "starred": bool(r["starred"])} for r in rows]
            self.send(200, {"items": items, "topics": list(cfg["topics"]), "topic_icons": cfg.get("topic_icons", {}),
                            "icons": {k: "/icons/" + v for k, v in news.icon_files(cfg).items()},
                            "sources": sorted({i["source"] for i in items}),
                            "last_refresh": float(last["value"]) if last else 0,
                            "refreshing": refresh_lock.locked()})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "bad json"})
        path = urlparse(self.path).path
        if path == "/api/refresh":
            n = do_refresh()
            self.send(200, {"busy": True} if n is None else {"new": n})
        elif path == "/api/mark":
            url = body.get("url")
            flags = {k: int(body[k]) for k in ("read", "starred") if isinstance(body.get(k), bool)}
            if not isinstance(url, str) or not flags:
                return self.send(400, {"error": "need url and read/starred bool"})
            db = news.connect()
            try:
                with db:
                    db.execute(f"UPDATE items SET {', '.join(f'{k} = ?' for k in flags)} WHERE url = ?", (*flags.values(), url))
            finally:
                db.close()
            self.send(200, {})
        else:
            self.send(404, {"error": "not found"})


def main():
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:  # already running (e.g. launcher clicked twice): just show the page
        webbrowser.open(f"http://localhost:{PORT}")
        return
    threading.Thread(target=auto_refresh, daemon=True).start()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    url = f"http://localhost:{PORT}"
    print(f"News UI at {url}  (close the page or press Ctrl+C to stop)")
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print("stopped")


if __name__ == "__main__":
    main()
