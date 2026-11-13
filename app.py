#!/usr/bin/env python3
"""
Wavelog DX Checker - web app.

Paste (or drop) an ARRL DX Bulletin / DX newsletter into the page and it lists the
DXCC entities mentioned that you haven't confirmed in Wavelog. Optionally watches
your Gmail for new bulletins and emails you what you need.

First run: open the page and follow the setup screen. You'll need the one-time setup
code printed in the container log (`docker logs wavelog-dx-checker`).

Standard library only. Server-level environment variables:

  PORT           listen port (default 8080)
  STATE_DIR      where settings and state are kept (default /data)
  LOOKUP_DELAY   seconds between Wavelog calls (default 0.3)
  CACHE_MINUTES  how long lookups are cached in memory (default 15)
  MAX_CALLS      max callsigns per check (default 400)
  COOKIE_SECURE  1 = mark the login cookie Secure (set this if you serve it over HTTPS)

Everything else (Wavelog, Gmail, ...) is set on the setup/settings page. Those values
can also be passed as environment variables, which then override the page - see
settings.py for the names.
"""

import json
import os
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import dxcheck
import mailwatch
import settings

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 2 * 1024 * 1024

SERVER = {
    "delay": float(os.environ.get("LOOKUP_DELAY", "0.3")),
    "cache_s": float(os.environ.get("CACHE_MINUTES", "15")) * 60,
    "max_calls": int(os.environ.get("MAX_CALLS", "400")),
    "port": int(os.environ.get("PORT", "8080")),
    "cookie_secure": os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes", "on"),
}


class CachedWavelog(dxcheck.Wavelog):
    """Wavelog client with a small in-memory TTL cache, shared across requests."""

    def __init__(self, *a, ttl=900, **kw):
        super().__init__(*a, **kw)
        self.ttl = ttl
        self._cache = {}
        self._lock = threading.Lock()
        self._net_lock = threading.Lock()

    def lookup(self, callsign, band=None, mode=None):
        k = (callsign, band or "", mode or "")
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(k)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        r = super().lookup(callsign, band=band, mode=mode)
        with self._lock:
            self._cache[k] = (now, r)
            if len(self._cache) > 5000:
                for old in sorted(self._cache, key=lambda x: self._cache[x][0])[:1000]:
                    del self._cache[old]
        return r

    def _throttle(self):
        with self._net_lock:
            super()._throttle()


# ---------------------------------------------------------------------------
# Current Wavelog client, rebuilt whenever settings change
# ---------------------------------------------------------------------------

_wl = {"obj": None, "sig": None}
_wl_lock = threading.Lock()


def make_wavelog(e, ttl=None, delay=None):
    return CachedWavelog(e["wavelog_url"], e["wavelog_key"],
                         insecure=bool(e.get("wavelog_insecure")),
                         use_index_php=not e.get("wavelog_no_index_php"),
                         station_ids=e.get("wavelog_station_ids") or None,
                         delay=SERVER["delay"] if delay is None else delay,
                         ttl=SERVER["cache_s"] if ttl is None else ttl)


def get_wl():
    e = settings.effective()
    if not (e.get("wavelog_url") and e.get("wavelog_key")):
        return None
    sig = (e["wavelog_url"], e["wavelog_key"], e.get("wavelog_insecure"),
           e.get("wavelog_no_index_php"), e.get("wavelog_station_ids"))
    with _wl_lock:
        if _wl["sig"] != sig:
            _wl["obj"], _wl["sig"] = make_wavelog(e), sig
        return _wl["obj"]


# One check at a time, so a few browser tabs can't hammer your Wavelog server.
CHECK_LOCK = threading.Semaphore(1)


def parse_calls(body):
    text = dxcheck.strip_html(str(body.get("text", "")))
    calls = dxcheck.extract_calls(text, ignore_case=bool(body.get("ignore_case")))
    skipped = [c for c, v in calls.items() if v["manager"]]
    if not body.get("include_managers"):
        calls = {c: v for c, v in calls.items() if not v["manager"]}
    return calls, skipped


def mail_status():
    st = mailwatch.STATUS
    cfg = mailwatch.config()
    if not mailwatch.enabled(cfg):
        return {"enabled": False}
    return {"enabled": True, "running": st["running"], "last_check": st["last_check"],
            "last_result": st["last_result"], "last_error": st["last_error"],
            "every_hours": cfg["check_hours"], "to": cfg["to"],
            "history": mailwatch.load_state(cfg).get("history", [])[-5:][::-1]}


def test_wavelog(values):
    """Try one lookup with the given (or stored) Wavelog settings."""
    e = settings.effective()
    for k in ("wavelog_url", "wavelog_key", "wavelog_insecure", "wavelog_no_index_php",
              "wavelog_station_ids"):
        if k in values and not (k in settings.SECRETS and not values[k]):
            e[k] = values[k]
    for k in ("wavelog_insecure", "wavelog_no_index_php"):
        e[k] = settings._coerce(bool, e.get(k)) or False
    if not e.get("wavelog_url") or not e.get("wavelog_key"):
        return False, "Enter your Wavelog URL and API key."
    url = str(e["wavelog_url"]).strip()
    if not url.startswith(("http://", "https://")):
        return False, "The URL should start with http:// or https://"
    e["wavelog_url"] = url
    e["wavelog_key"] = str(e["wavelog_key"]).strip()
    try:
        r = make_wavelog(e, ttl=0, delay=0).lookup("W1AW")
    except RuntimeError as ex:
        msg = str(ex)
        if "HTTP 401" in msg or "HTTP 403" in msg or "key" in msg.lower():
            msg += " - check the API key (v2 keys need the lookup:read scope)."
        elif "HTTP 404" in msg:
            msg += " - check the URL. If your Wavelog URLs don't contain /index.php, tick that option."
        return False, msg
    if not r.get("dxcc_id"):
        return False, "Wavelog answered, but didn't resolve a test callsign. Check the URL and key."
    api = "v2" if e["wavelog_key"].startswith("wl2_") else "v1"
    return True, f"Connected (API {api}). Test lookup W1AW → {mailwatch._title(r['dxcc'])}."


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

PUBLIC_API = {"/api/login", "/api/setup", "/api/setup/test-wavelog", "/api/setup/test-mail"}


class Handler(BaseHTTPRequestHandler):
    server_version = "WavelogDXChecker/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ------------------------------------------------------------
    def _ip(self):
        return self.client_address[0]

    def _authed(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        token = c[settings.COOKIE].value if settings.COOKIE in c else None
        return settings.has_admin() and settings.valid_session(token)

    def _cookie(self, value, max_age):
        parts = [f"{settings.COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Strict",
                 f"Max-Age={max_age}"]
        if SERVER["cookie_secure"]:
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")

    def _json(self, code, obj, cookie=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self._security_headers()
        if cookie is not None:
            self._cookie(*cookie)
        self.end_headers()
        self.wfile.write(b)

    def _file(self, name):
        with open(os.path.join(HERE, name), "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "frame-ancestors 'none'; form-action 'self'")
        self._security_headers()
        self.end_headers()
        self.wfile.write(b)

    def _redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self):
        # JSON only: together with the SameSite=Strict cookie this blocks cross-site form posts.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise ValueError("Expected JSON.")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("That's too much text (2 MB max).")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Bad request body.")
        if not isinstance(body, dict):
            raise ValueError("Bad request body.")
        return body

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True})

        has_admin = settings.has_admin()
        authed = self._authed()
        if path in ("/", "/index.html"):
            if not has_admin:
                return self._redirect("/setup")
            if not authed:
                return self._redirect("/login")
            if get_wl() is None:
                return self._redirect("/settings")
            return self._file("index.html")
        if path == "/setup":
            if has_admin:
                return self._redirect("/settings" if authed else "/login")
            return self._file("setup.html")
        if path == "/settings":
            if not has_admin:
                return self._redirect("/setup")
            if not authed:
                return self._redirect("/login")
            return self._file("setup.html")
        if path == "/login":
            if not has_admin:
                return self._redirect("/setup")
            if authed:
                return self._redirect("/")
            return self._file("login.html")

        if path == "/api/state":
            return self._json(200, {"setup_done": has_admin, "authed": authed})

        if not authed:
            return self._json(401, {"error": "Please sign in."})

        if path == "/api/config":
            e = settings.effective()
            wl = get_wl()
            return self._json(200, {
                "configured": wl is not None,
                "api": ("v2" if wl.v2 else "v1") if wl else None,
                "host": urlparse(e.get("wavelog_url") or "").netloc or None,
                "max_calls": SERVER["max_calls"],
                "mail": mail_status(),
            })
        if path == "/api/settings":
            return self._json(200, {"values": settings.masked(), "env_locked": settings.env_locked()})
        self._json(404, {"error": "not found"})

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as e:
            return self._json(400, {"error": str(e)})

        if path in PUBLIC_API:
            return self._public_post(path, body)
        if not self._authed():
            return self._json(401, {"error": "Please sign in."})

        if path == "/api/extract":
            calls, skipped = parse_calls(body)
            return self._json(200, {
                "calls": [{"call": c, "context": v["context"]} for c, v in calls.items()],
                "skipped": [] if body.get("include_managers") else skipped,
            })
        if path == "/api/check":
            return self._check(body)
        if path == "/api/mail/run":
            wl = get_wl()
            if not mailwatch.enabled() or wl is None:
                return self._json(400, {"error": "Inbox watching isn't set up."})
            threading.Thread(target=mailwatch.run_once, args=(wl,),
                             kwargs={"web_url": mailwatch.config()["public_url"],
                                     "log": lambda m: print(m, file=sys.stderr, flush=True)},
                             daemon=True).start()
            return self._json(202, {"started": True})
        if path == "/api/settings":
            return self._save_settings(body, initial=False)
        if path == "/api/logout":
            return self._json(200, {"ok": True}, cookie=("", 0))
        self._json(404, {"error": "not found"})

    def _public_post(self, path, body):
        ip = self._ip()
        if path == "/api/login":
            if settings.throttled(ip):
                return self._json(429, {"error": "Too many attempts. Wait a few minutes and try again."})
            if settings.check_admin_password(str(body.get("password", ""))):
                settings.record_success(ip)
                return self._json(200, {"ok": True},
                                  cookie=(settings.make_session(), settings.SESSION_DAYS * 86400))
            settings.record_failure(ip)
            time.sleep(0.5)
            return self._json(403, {"error": "That password isn't right."})

        # Setup endpoints: before setup they need the one-time code; after, a login.
        if settings.has_admin():
            if not self._authed():
                return self._json(401, {"error": "Please sign in."})
        else:
            if settings.throttled(ip):
                return self._json(429, {"error": "Too many attempts. Wait a few minutes and try again."})
            if not settings.check_setup_code(str(body.get("setup_code", ""))):
                settings.record_failure(ip)
                return self._json(403, {"error": "That setup code isn't right. It's in the container "
                                                 "log: docker logs wavelog-dx-checker",
                                        "field": "setup_code"})

        if path == "/api/setup/test-wavelog":
            ok, msg = test_wavelog(body)
            return self._json(200, {"ok": ok, "message": msg})
        if path == "/api/setup/test-mail":
            cur = settings.effective()
            user = (body.get("mail_user") or cur.get("mail_user") or "").strip()
            pw = (body.get("mail_password") or cur.get("mail_password") or "").replace(" ", "")
            if not user or not pw:
                return self._json(200, {"ok": False, "message": "Enter your Gmail address and app password."})
            err = mailwatch.test_login(user, pw, cur.get("mail_imap_host") or "imap.gmail.com")
            return self._json(200, {"ok": not err, "message": err or "Signed in to Gmail."})
        if path == "/api/setup":
            return self._save_settings(body, initial=not settings.has_admin())
        self._json(404, {"error": "not found"})

    def _save_settings(self, body, initial):
        values = {k: body[k] for k in settings.FIELDS if k in body}
        if values.get("mail_password"):
            values["mail_password"] = str(values["mail_password"]).replace(" ", "")
        locked = set(settings.env_locked())
        values = {k: v for k, v in values.items() if k not in locked}

        # Wavelog must work before we save it.
        if initial or any(k.startswith("wavelog_") for k in values):
            ok, msg = test_wavelog(values)
            if not ok:
                return self._json(400, {"error": msg, "field": "wavelog"})

        new_pw = str(body.get("admin_password") or "")
        if initial:
            if len(new_pw) < 8:
                return self._json(400, {"error": "Choose an admin password of at least 8 characters.",
                                        "field": "admin_password"})
        elif new_pw:
            if not settings.check_admin_password(str(body.get("current_password") or "")):
                return self._json(403, {"error": "Your current password isn't right.",
                                        "field": "current_password"})
            if len(new_pw) < 8:
                return self._json(400, {"error": "Use at least 8 characters for the new password.",
                                        "field": "admin_password"})

        settings.update(values)
        cookie = None
        if new_pw:
            settings.set_admin_password(new_pw)       # also signs out other sessions
            cookie = (settings.make_session(), settings.SESSION_DAYS * 86400)
        if initial:
            settings.record_success(self._ip())
            print("Setup complete.", file=sys.stderr, flush=True)
        return self._json(200, {"ok": True}, cookie=cookie)

    def _check(self, body):
        wl = get_wl()
        if wl is None:
            return self._json(503, {"error": "Wavelog isn't set up yet. Open Settings."})
        calls, skipped = parse_calls(body)
        if not calls:
            return self._json(400, {"error": "No callsigns found in that text."})
        if len(calls) > SERVER["max_calls"]:
            return self._json(400, {"error": f"Found {len(calls)} callsigns; the limit is "
                                             f"{SERVER['max_calls']} (MAX_CALLS)."})
        band = (str(body.get("band") or "").strip() or None)
        mode = (str(body.get("mode") or "").strip() or None) if band else None

        # Stream newline-delimited JSON so the page can show progress.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("X-Accel-Buffering", "no")   # don't let nginx buffer the stream
        self._security_headers()
        self.end_headers()
        self.close_connection = True

        def emit(obj):
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()

        try:
            emit({"type": "start", "total": len(calls),
                  "skipped": [] if body.get("include_managers") else skipped})
            with CHECK_LOCK:
                def progress(i, total, call, err=None):
                    emit({"type": "progress", "i": i, "total": total, "call": call,
                          **({"error": err} if err else {})})
                out = dxcheck.check_calls(calls, wl, band=band, mode=mode, on_progress=progress)
            emit({"type": "done", "band": band, "mode": mode, **out})
        except RuntimeError as e:
            emit({"type": "error", "error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser went away


def main():
    try:
        os.makedirs(settings.STATE_DIR, exist_ok=True)
        probe = os.path.join(settings.STATE_DIR, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        sys.exit(f"Can't write to {settings.STATE_DIR} ({e.strerror}). If you mounted a host folder "
                 f"there, run: sudo chown -R 10001 <that folder>  - or use the named volume from "
                 f"docker-compose.yml.")
    if not settings.has_admin():
        code = settings.setup_code()
        bar = "=" * 60
        print(f"\n{bar}\n  FIRST-RUN SETUP\n  Open this app in your browser and enter this setup code:\n\n"
              f"        {code}\n\n  (A new code is made each time the container restarts,\n"
              f"   until setup is finished.)\n{bar}\n", file=sys.stderr, flush=True)
    else:
        e = settings.effective()
        print(f"Wavelog: {e.get('wavelog_url') or 'not set'} | Gmail watch: "
              f"{'on (' + str(e.get('mail_user')) + ')' if mailwatch.enabled() else 'off'}",
              file=sys.stderr)
    mailwatch.start_background(get_wl)
    srv = ThreadingHTTPServer(("0.0.0.0", SERVER["port"]), Handler)
    srv.daemon_threads = True
    print(f"Listening on http://0.0.0.0:{SERVER['port']}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
