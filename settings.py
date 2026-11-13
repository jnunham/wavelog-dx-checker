"""
settings.py - stored configuration and access control for the DX checker.

Where things live
-----------------
Everything is kept in one file, $STATE_DIR/config.json (default /data/config.json),
on the container's data volume:

  * created with mode 0600 (readable only by the unprivileged "app" user the
    container runs as), written atomically
  * the admin password is never stored, only a salted PBKDF2-SHA256 hash
    (600,000 iterations)
  * the Wavelog API key and Gmail app password are stored so the background job can
    use them unattended; they are never sent back to the browser (the settings page
    only shows the last 4 characters)

First run
---------
Until setup is finished, the setup page needs a one-time code that is printed
to the container log (`docker logs wavelog-dx-checker`). That stops anyone else on
your network from claiming a freshly started instance before you do.

Environment variables (WAVELOG_URL, WAVELOG_KEY, MAIL_USER, ...) still work and
override the stored values, for people who prefer compose files or Docker secrets.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

STATE_DIR = os.environ.get("STATE_DIR", "/data")
PATH = os.path.join(STATE_DIR, "config.json")
_lock = threading.RLock()

# Stored key -> (env var, type)
FIELDS = {
    "wavelog_url":        ("WAVELOG_URL", str),
    "wavelog_key":        ("WAVELOG_KEY", str),
    "wavelog_insecure":   ("WAVELOG_INSECURE", bool),
    "wavelog_no_index_php": ("WAVELOG_NO_INDEX_PHP", bool),
    "wavelog_station_ids": ("WAVELOG_STATION_IDS", str),
    "mail_user":          ("MAIL_USER", str),
    "mail_password":      ("MAIL_PASSWORD", str),
    "mail_to":            ("MAIL_TO", str),
    "mail_imap_host":     ("MAIL_IMAP_HOST", str),
    "mail_smtp_host":     ("MAIL_SMTP_HOST", str),
    "mail_folder":        ("MAIL_FOLDER", str),
    "mail_subject":       ("MAIL_SUBJECT", str),
    "mail_from_filter":   ("MAIL_FROM_FILTER", str),
    "mail_check_hours":   ("MAIL_CHECK_HOURS", float),
    "mail_lookback_days": ("MAIL_LOOKBACK_DAYS", int),
    "mail_notify_none":   ("MAIL_NOTIFY_NONE", bool),
    "mail_band":          ("MAIL_BAND", str),
    "mail_mode":          ("MAIL_MODE", str),
    "public_url":         ("PUBLIC_URL", str),
}
SECRETS = {"wavelog_key", "mail_password"}
DEFAULTS = {
    "mail_imap_host": "imap.gmail.com", "mail_smtp_host": "smtp.gmail.com",
    "mail_check_hours": 4.0,
    "mail_lookback_days": 14, "mail_notify_none": False,
    "wavelog_insecure": False, "wavelog_no_index_php": False,
}


def _coerce(typ, v):
    if v is None or v == "":
        return None
    if typ is bool:
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
    try:
        return typ(v.strip() if isinstance(v, str) else v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# File storage
# ---------------------------------------------------------------------------

def _read():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)


def stored():
    with _lock:
        return _read()


def effective():
    """Settings actually in use: environment variables win over the stored file."""
    with _lock:
        data = _read()
    out = dict(DEFAULTS)
    for k, (env, typ) in FIELDS.items():
        v = _coerce(typ, data.get(k))
        if v is not None:
            out[k] = v
        ev = _coerce(typ, os.environ.get(env))
        if ev is not None:
            out[k] = ev
    out.setdefault("mail_to", out.get("mail_user"))
    if not out.get("mail_to"):
        out["mail_to"] = out.get("mail_user")
    return out


def env_locked():
    """Fields set by environment variable (can't be changed from the page)."""
    return sorted(k for k, (env, typ) in FIELDS.items() if _coerce(typ, os.environ.get(env)) is not None)


def update(values):
    """Merge values into the stored config. Empty secrets mean 'keep the current one'."""
    with _lock:
        data = _read()
        for k, v in values.items():
            if k not in FIELDS:
                continue
            if k in SECRETS and (v is None or str(v).strip() == ""):
                continue
            typ = FIELDS[k][1]
            cv = _coerce(typ, v)
            if cv is None:
                data.pop(k, None)
            else:
                data[k] = cv
        _write(data)


def masked():
    """Settings for the settings page, with secrets reduced to a hint."""
    eff = effective()
    out = {}
    for k in FIELDS:
        v = eff.get(k)
        if k in SECRETS:
            out[k] = {"set": bool(v), "hint": ("•••• " + str(v)[-4:]) if v and len(str(v)) > 8 else ("set" if v else "")}
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Admin password + setup state
# ---------------------------------------------------------------------------

ITER = 600_000


def _hash(pw, salt, it=ITER):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, it)


def is_set_up():
    d = stored()
    return bool(d.get("admin_hash")) and bool(effective().get("wavelog_url")) and bool(effective().get("wavelog_key"))


def has_admin():
    return bool(stored().get("admin_hash"))


def set_admin_password(pw):
    if len(pw) < 8:
        raise ValueError("Use at least 8 characters for the admin password.")
    salt = secrets.token_bytes(16)
    with _lock:
        data = _read()
        data["admin_hash"] = {"alg": "pbkdf2_sha256", "iter": ITER,
                              "salt": base64.b64encode(salt).decode(),
                              "hash": base64.b64encode(_hash(pw, salt)).decode()}
        # rotating the session secret signs everyone else out
        data["session_secret"] = secrets.token_hex(32)
        _write(data)


def check_admin_password(pw):
    h = stored().get("admin_hash")
    if not h:
        return False
    salt = base64.b64decode(h["salt"])
    return hmac.compare_digest(_hash(pw or "", salt, h.get("iter", ITER)), base64.b64decode(h["hash"]))


def _session_secret():
    with _lock:
        data = _read()
        if not data.get("session_secret"):
            data["session_secret"] = secrets.token_hex(32)
            _write(data)
        return data["session_secret"].encode()


# ---------------------------------------------------------------------------
# Sessions (signed cookie, no server-side storage)
# ---------------------------------------------------------------------------

COOKIE = "dxn_session"
SESSION_DAYS = 30


def make_session():
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{exp}.{secrets.token_hex(8)}"
    sig = hmac.new(_session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_session(token):
    if not token or token.count(".") != 2:
        return False
    exp, nonce, sig = token.split(".")
    good = hmac.new(_session_secret(), f"{exp}.{nonce}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return int(exp) > time.time()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# First-run setup code
# ---------------------------------------------------------------------------

_setup_code = None


def setup_code():
    """One-time code shown in the container log, needed to complete first-run setup."""
    global _setup_code
    if _setup_code is None:
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        _setup_code = f"{raw[:4]}-{raw[4:]}"
    return _setup_code


def check_setup_code(code):
    norm = lambda s: "".join(ch for ch in (s or "").upper() if ch.isalnum())
    return hmac.compare_digest(norm(code), norm(setup_code()))


# ---------------------------------------------------------------------------
# Brute-force throttle for login / setup code
# ---------------------------------------------------------------------------

_fails = {}


def throttled(ip):
    n, until = _fails.get(ip, (0, 0))
    return until > time.time()


def record_failure(ip):
    n, _ = _fails.get(ip, (0, 0))
    n += 1
    # 5 free tries, then lock out for 1, 2, 4, ... minutes (max 1 hour)
    until = time.time() + min(3600, 60 * 2 ** (n - 5)) if n >= 5 else 0
    _fails[ip] = (n, until)


def record_success(ip):
    _fails.pop(ip, None)


# ---------------------------------------------------------------------------
# Command line: reset a forgotten admin password
#   docker exec -it wavelog-dx-checker python settings.py reset-password
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import getpass
    import sys
    if sys.argv[1:] != ["reset-password"]:
        sys.exit("usage: python settings.py reset-password")
    if not has_admin():
        sys.exit("Setup hasn't been done yet - open the web page to set it up.")
    pw = getpass.getpass("New admin password: ")
    if pw != getpass.getpass("Again: "):
        sys.exit("Passwords don't match.")
    try:
        set_admin_password(pw)
    except ValueError as e:
        sys.exit(str(e))
    print("Password changed. All browsers have been signed out.")
