#!/usr/bin/env python3
"""
mailwatch.py - watch a mailbox (e.g. Gmail) for new ARRL DX Bulletins, check them
against Wavelog, and email you the DXCC entities you still need.

Read-only on your mailbox: it opens the folder with EXAMINE (read-only), never
marks, moves or deletes anything, and remembers what it has handled in a small
state file.

Settings come from the setup/settings page (stored by settings.py). The same values
can also be given as environment variables, which take precedence:

  MAIL_USER            your Gmail address
  MAIL_PASSWORD        a Gmail *app password* (Google Account > Security > App passwords)
  MAIL_TO              where to send alerts (default: MAIL_USER)
  MAIL_IMAP_HOST       default imap.gmail.com
  MAIL_SMTP_HOST       default smtp.gmail.com  (port 465, SSL)
  MAIL_FOLDER          default "auto" = Gmail All Mail (finds archived mail too), else INBOX
  MAIL_SUBJECT         comma-separated subject words, default "ARLD, DX Bulletin, DX News"
  MAIL_FROM_FILTER     optional sender to match, e.g. "arrl.org"
  MAIL_CHECK_HOURS     how often to look, default 4
  MAIL_LOOKBACK_DAYS   on the very first run, only consider mail this recent, default 14
  MAIL_MAX_AGE_DAYS    never alert on a bulletin older than this, default 10

Only the newest bulletin is ever alerted on: if several new ones are found (first run,
or the container was off for a while) the older ones are skipped. Stations whose
activity dates in the bulletin have already passed are left out of the alert.
  MAIL_NOTIFY_NONE     1 = also email when a bulletin has nothing new (default off)
  MAIL_BAND / MAIL_MODE  optional: also report band/mode slots in alerts (e.g. 10m / CW)
  STATE_DIR            where state.json / config.json live, default /data

Run standalone for testing:
  python3 mailwatch.py --once            # check now, send alerts for new bulletins
  python3 mailwatch.py --once --dry-run  # check now, print the email instead of sending
  python3 mailwatch.py --once --resend   # re-process the newest bulletin even if seen
"""

import datetime as dt
import email
import email.header
import email.utils
import html
import imaplib
import json
import os
import smtplib
import ssl
import sys
import threading
import time
from email.message import EmailMessage

import dxcheck
import settings


def _env(name, default=None):
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default


DEFAULT_SUBJECTS = "ARLD, DX Bulletin, DX News"


def config():
    """Current mail settings (stored settings, overridden by MAIL_* env vars)."""
    e = settings.effective()
    return {
        "user": e.get("mail_user"),
        "password": e.get("mail_password"),
        "to": e.get("mail_to") or e.get("mail_user"),
        "imap_host": e.get("mail_imap_host") or "imap.gmail.com",
        "smtp_host": e.get("mail_smtp_host") or "smtp.gmail.com",
        "folder": e.get("mail_folder") or "auto",
        "subject": e.get("mail_subject") or DEFAULT_SUBJECTS,
        "from_filter": e.get("mail_from_filter"),
        "check_hours": float(e.get("mail_check_hours") or 4),
        "lookback_days": int(e.get("mail_lookback_days") or 14),
        "max_age_days": int(e.get("mail_max_age_days") or 10),
        "notify_none": bool(e.get("mail_notify_none")),
        "band": e.get("mail_band"),
        "mode": e.get("mail_mode"),
        "state_dir": settings.STATE_DIR,
        "public_url": e.get("public_url"),
    }


def enabled(cfg=None):
    cfg = cfg or config()
    return bool(cfg["user"] and cfg["password"])


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_path(cfg):
    return os.path.join(cfg["state_dir"], "state.json")


def load_state(cfg):
    try:
        with open(_state_path(cfg), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"seen": [], "history": []}


def save_state(cfg, state):
    os.makedirs(cfg["state_dir"], exist_ok=True)
    state["seen"] = state["seen"][-500:]
    state["history"] = state["history"][-50:]
    tmp = _state_path(cfg) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, _state_path(cfg))


# ---------------------------------------------------------------------------
# Mail in
# ---------------------------------------------------------------------------

def _decode_header(v):
    if not v:
        return ""
    return str(email.header.make_header(email.header.decode_header(v)))


def message_text(msg):
    """Best plain-text body of an email (text/plain preferred, else stripped HTML)."""
    plain, htm = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        (plain if ctype == "text/plain" else htm).append(text)
    if plain:
        return "\n".join(plain)
    return dxcheck.strip_html("\n".join(htm))


def _when(date_header):
    """Parse an email Date header to an aware datetime (epoch if unparseable)."""
    try:
        d = email.utils.parsedate_to_datetime(date_header)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _or(criteria):
    """IMAP OR-chain: [a, b, c] -> OR a OR b c."""
    if len(criteria) == 1:
        return criteria[0]
    return ["OR", *_flat(criteria[0]), *_flat(_or(criteria[1:]))]


def _flat(x):
    return x if isinstance(x, list) else [x]


def _pick_folder(imap, wanted):
    """'auto' -> Gmail's All Mail (found by its \\All flag, so it works in any language),
    falling back to INBOX. Anything else is used as given."""
    if wanted and wanted.lower() != "auto":
        return wanted
    try:
        typ, data = imap.list()
        for line in data or []:
            line = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if "\\All" in line:
                return line.rsplit(' "/" ', 1)[-1].strip().strip('"')
    except Exception:
        pass
    return "INBOX"


def subject_terms(cfg):
    return [t.strip() for t in str(cfg["subject"] or "").split(",") if t.strip()] or ["ARLD"]


def fetch_bulletins(cfg, state, resend=False):
    """Search the mailbox for DX bulletins.

    Returns a dict:
      new      [(message_id, subject, date_str, text)] not handled before, oldest first
               (with resend=True: just the newest match, handled or not)
      matched  number of matching emails found
      seen     [(subject, date)] matches that were already handled
      folder, since, terms   what was searched (for the status message)
      hints    if nothing matched: subjects of recent mail from ARRL, to help fix the filter
    """
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], ssl_context=ssl.create_default_context())
    try:
        imap.login(cfg["user"], cfg["password"])
        folder = _pick_folder(imap, cfg["folder"])
        typ, _ = imap.select(_quote(folder), readonly=True)   # EXAMINE: never changes flags
        if typ != "OK":
            raise RuntimeError(f"Can't open mail folder {folder!r} - check the folder name in Settings.")

        days = cfg["lookback_days"] if not state["seen"] else 45
        since = dt.date.today() - dt.timedelta(days=days)
        terms = subject_terms(cfg)

        # Gmail's IMAP search matches whole words only ("ARLD" won't find "ARLD045"),
        # so ask the server for a broad set of candidates - mail from ARRL, or with any
        # of the words in the subject - and do the real subject matching here.
        server_terms = [["FROM", _quote(cfg["from_filter"] or "arrl.org")]]
        for t in terms + ["DX news", "DX bulletin"]:
            server_terms.append(["SUBJECT", _quote(t)])
        typ, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"), *_flat(_or(server_terms)))
        if typ != "OK":
            raise RuntimeError("Mailbox search failed")
        ids = data[0].split() if data and data[0] else []

        want = [t.lower() for t in terms]
        from_filter = (cfg["from_filter"] or "").lower()
        found, other_arrl = [], []
        for num in ids[-200:]:
            typ, hd = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
            if typ != "OK" or not hd or not isinstance(hd[0], tuple):
                continue
            h = email.message_from_bytes(hd[0][1])
            subj = _decode_header(h.get("Subject"))
            sender = _decode_header(h.get("From")).lower()
            if from_filter and from_filter not in sender:
                continue
            if not any(w in subj.lower() for w in want):
                if "arrl" in sender:
                    other_arrl.append(subj)
                continue
            mid = (h.get("Message-ID") or f"{num.decode()}-{h.get('Date')}").strip()
            found.append((num, mid, subj, h.get("Date", "")))

        found.sort(key=lambda f: _when(f[3]))
        seen_ids = set(state["seen"])
        unseen = [f for f in found if f[1] not in seen_ids]
        skipped, too_old = [], None
        if resend:
            pick = found[-1:]
        else:
            # Only the newest bulletin matters; older unhandled ones are superseded.
            pick = unseen[-1:]
            skipped = [(f[1], f[2]) for f in unseen[:-1]]
            if pick and (dt.datetime.now(dt.timezone.utc) - _when(pick[0][3])).days > cfg["max_age_days"]:
                too_old = (pick[0][2], pick[0][3])
                skipped.append((pick[0][1], pick[0][2]))
                pick = []
        seen = [(subj, date) for num, mid, subj, date in found if mid in seen_ids]
        new = []
        for num, mid, subj, date in pick:
            typ, msgdata = imap.fetch(num, "(BODY.PEEK[])")   # PEEK: doesn't mark as read
            if typ == "OK" and msgdata and isinstance(msgdata[0], tuple):
                new.append((mid, subj, date, email.message_from_bytes(msgdata[0][1])))
        hints = other_arrl[-5:][::-1] if not found else []
        return {"new": [(mid, subj, date, message_text(msg)) for mid, subj, date, msg in new],
                "matched": len(found), "seen": seen, "folder": folder,
                "skipped": skipped, "too_old": too_old,
                "since": since, "terms": terms, "hints": hints}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def test_login(user, password, imap_host="imap.gmail.com"):
    """Try an IMAP login; returns None on success or an error message."""
    try:
        imap = imaplib.IMAP4_SSL(imap_host, ssl_context=ssl.create_default_context(), timeout=20)
    except TypeError:  # very old Python without timeout=
        imap = imaplib.IMAP4_SSL(imap_host, ssl_context=ssl.create_default_context())
    except OSError as e:
        return f"Couldn't reach {imap_host}: {e}"
    try:
        imap.login(user, password)
        return None
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg.upper() or "invalid credentials" in msg.lower():
            return ("Gmail rejected that login. Use an app password (not your normal password), "
                    "and check IMAP is enabled in Gmail settings.")
        return msg
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Mail out
# ---------------------------------------------------------------------------

COLORS = {"new": ("#d6336c", "#fdecf2"), "need_qsl": ("#c77800", "#fff4dd"),
          "band": ("#1c7ed6", "#e7f1fc"), "mode": ("#1c7ed6", "#e7f1fc"),
          "have": ("#6b6f76", "#f0efeb")}


def _title(s):
    return " ".join(w.capitalize() if w.lower() not in ("of", "and", "the", "de", "da") else w.lower()
                    for w in s.lower().split()).replace("& ", "& ")


def build_alert(subject, result, band=None, mode=None, web_url=None):
    needed = [e for e in result["entities"] if e["needed"]]
    n_ended = sum(1 for e in result["entities"] if e.get("ended") and e["kind"] != "have")
    n_new = sum(1 for e in needed if e["kind"] == "new")
    n_qsl = sum(1 for e in needed if e["kind"] == "need_qsl")
    n_slot = sum(1 for e in needed if e["kind"] in ("band", "mode"))
    tag = (subject.split()[0] if subject else "bulletin")

    parts = []
    if n_new:
        parts.append(f"{n_new} new one{'s' if n_new != 1 else ''}")
    if n_qsl:
        parts.append(f"{n_qsl} to QSL")
    if n_slot:
        parts.append(f"{n_slot} {band}{(' ' + mode.upper()) if mode else ''} slot{'s' if n_slot != 1 else ''}")
    headline = ", ".join(parts) if parts else "nothing new"
    mail_subject = f"DX Needed: {headline} in {tag}"

    # plain text
    lines = [f"{subject}", f"{len(result['entities'])} DXCC entities mentioned, {len(needed)} needed.", ""]
    for e in needed:
        lines.append(f"{e['status']:<20} {_title(e['dxcc'])}  ({e['callsigns']})"
                     + (f"  [{e['when']}]" if e.get("when") else ""))
        for para in e["context"].split("\n\n"):
            lines.append(f"{'':<20} {para}")
        lines.append("")
    if not needed:
        lines.append("You already have every entity in this bulletin confirmed.")
    if n_ended:
        lines.append(f"Not listed: {n_ended} needed entit{'y' if n_ended == 1 else 'ies'} whose activity has already ended.")
    if result["unresolved"]:
        lines.append(f"Couldn't resolve: {', '.join(result['unresolved'])}")
    if web_url:
        lines.append(f"\nOpen the checker: {web_url}")
    text = "\n".join(lines)

    # html
    rows = []
    for e in needed:
        fg, bg = COLORS.get(e["kind"], COLORS["have"])
        worked = set(e["already_worked_calls"].split())
        calls = " ".join(
            f'<span style="font-family:Menlo,Consolas,monospace;font-size:13px;background:#f3f1ec;'
            f'border:1px solid #e3e0d8;border-radius:4px;padding:1px 5px">{html.escape(c)}'
            f'{" ✓" if c in worked else ""}</span>' for c in e["callsigns"].split())
        rows.append(
            f'<tr><td style="padding:12px 12px 12px 0;vertical-align:top;white-space:nowrap">'
            f'<span style="font:700 11px sans-serif;letter-spacing:.04em;text-transform:uppercase;'
            f'color:{fg};background:{bg};padding:3px 7px;border-radius:4px">{html.escape(e["status"])}</span></td>'
            f'<td style="padding:12px 0;border-bottom:1px solid #eee">'
            f'<div style="font-weight:600;font-size:15px">{html.escape(_title(e["dxcc"]))}'
            f'<span style="color:#9aa0a8;font-weight:400;font-size:12px"> #{html.escape(e["dxcc_id"])}</span>'
            + (f' <span style="font-weight:600;font-size:12px;color:#2b8a3e;background:#e9f7ec;border-radius:4px;'
               f'padding:1px 6px;margin-left:6px">{html.escape(e["when"])}</span>' if e.get("when") else "")
            + '</div>'
            f'<div style="margin:5px 0">{calls}</div>'
            f'<div style="color:#6b6f76;font-size:13px">{html.escape(e["context"]).replace(chr(10) + chr(10), "<br><br>")}</div></td></tr>')
    body = (f'<table style="border-collapse:collapse;width:100%">{"".join(rows)}</table>' if rows else
            '<p style="color:#2b8a3e;font-weight:600">You already have every entity in this bulletin confirmed.</p>')
    foot = ""
    if n_ended:
        foot += (f'<p style="color:#6b6f76;font-size:12px">Not listed: {n_ended} needed '
                 f'entit{"y" if n_ended == 1 else "ies"} whose activity has already ended.</p>')
    if result["unresolved"]:
        foot += f'<p style="color:#6b6f76;font-size:12px">Couldn\'t resolve: {html.escape(", ".join(result["unresolved"]))}</p>'
    if web_url:
        foot += f'<p style="font-size:13px"><a href="{html.escape(web_url)}" style="color:#e8590c">Open the DX checker</a></p>'
    htm = (f'<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#1d1f22;max-width:680px">'
           f'<h2 style="margin:0 0 4px;font-size:19px">{html.escape(headline[0].upper() + headline[1:])}</h2>'
           f'<div style="color:#6b6f76;font-size:13px;margin-bottom:12px">{html.escape(subject)} · '
           f'{len(result["entities"])} entities mentioned</div>{body}{foot}'
           f'<p style="color:#9aa0a8;font-size:11px;margin-top:18px">Only DXCC entities you haven\'t confirmed are listed; '
           f'new callsigns in entities you already have are ignored. ✓ = you\'ve worked that call but the entity '
           f'isn\'t confirmed yet.</p></div>')
    return mail_subject, text, htm, len(needed)


def send_mail(cfg, subject, text, htm):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = cfg["to"]
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="dx-needed.local")
    msg.set_content(text)
    msg.add_alternative(htm, subtype="html")
    with smtplib.SMTP_SSL(cfg["smtp_host"], 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg["user"], cfg["password"])
        s.send_message(msg)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

STATUS = {"enabled": False, "last_check": None, "last_result": None, "last_error": None, "running": False}
_run_lock = threading.Lock()


def _short_date(d):
    try:
        return email.utils.parsedate_to_datetime(d).strftime("%b %-d")
    except Exception:
        return d or "?"


def run_once(wl, cfg=None, dry_run=False, resend=False, web_url=None, log=print):
    """Check the mailbox once. Returns a human-readable summary (also kept in STATUS)."""
    cfg = cfg or config()
    if not _run_lock.acquire(blocking=False):
        return "A check is already running."
    STATUS["running"] = True
    try:
        state = load_state(cfg)
        found = fetch_bulletins(cfg, state, resend=resend)
        where = (f"Searched {found['folder']} since {found['since'].strftime('%b %-d')} for subjects "
                 f"containing {' / '.join(found['terms'])}")
        notes = []
        for mid, subj in found["skipped"]:
            if not dry_run and mid not in state["seen"]:
                state["seen"].append(mid)
        n_older = len(found["skipped"]) - (1 if found["too_old"] else 0)
        if n_older:
            notes.append(f"skipped {n_older} older bulletin{'s' if n_older != 1 else ''}")
        if found["too_old"]:
            subj, date = found["too_old"]
            msg = (f"Newest bulletin “{subj}” ({_short_date(date)}) is more than "
                   f"{cfg['max_age_days']} days old, so no alert was sent.")
        elif not found["new"]:
            if found["matched"]:
                subj, date = found["seen"][-1]
                msg = (f"No new bulletins. Latest: “{subj}” ({_short_date(date)}), already handled. "
                       f"Use “Re-check latest” to run it again.")
            else:
                msg = f"{where}: nothing found."
                if found["hints"]:
                    msg += (" Recent ARRL mail: " + "; ".join(f"“{h}”" for h in found["hints"][:3]) +
                            ". If one of these is the bulletin, add a word from its subject in Settings.")
                else:
                    msg += " No mail from ARRL in that time either."
        else:
            msgs = []
            for mid, subj, date, text in found["new"]:
                calls = dxcheck.extract_calls(text)
                calls = {c: v for c, v in calls.items() if not v["manager"]}
                if not calls:
                    log(f"[mail] {subj}: no callsigns found, skipping")
                    msgs.append(f"“{subj}”: no DX stations found in it, so no alert was sent")
                    # Deliberately not marked as handled: if this was a parsing problem,
                    # the next check (after an update) picks the bulletin up again.
                    continue
                ref = dxcheck.bulletin_date(text) or _when(date).date()
                result = dxcheck.check_calls(calls, wl, band=cfg["band"], mode=cfg["mode"], ref_date=ref)
                m_subj, m_text, m_html, n_needed = build_alert(subj, result, cfg["band"], cfg["mode"], web_url)
                if n_needed or cfg["notify_none"] or resend:
                    if dry_run:
                        log(f"--- would send to {cfg['to']}: {m_subj}\n{m_text}")
                        outcome = "alert not sent (dry run)"
                    else:
                        send_mail(cfg, m_subj, m_text, m_html)
                        log(f"[mail] sent: {m_subj}")
                        outcome = f"alert emailed to {cfg['to']}"
                else:
                    log(f"[mail] {subj}: nothing needed, no email sent")
                    outcome = "nothing new, no email sent"
                msgs.append(f"“{subj}”: {n_needed} needed, {outcome}")
                if not dry_run:
                    if mid not in state["seen"]:
                        state["seen"].append(mid)
                    state["history"].append({"subject": subj, "date": date, "needed": n_needed,
                                             "alert": m_subj,
                                             "checked": dt.datetime.now().isoformat(timespec="seconds")})
            msg = "; ".join(msgs)
        if notes:
            msg += " (" + "; ".join(notes) + ")"
        if not dry_run:
            save_state(cfg, state)
        STATUS.update(last_check=time.time(), last_result=msg, last_error=None)
        return msg
    except (imaplib.IMAP4.error, smtplib.SMTPException, OSError, RuntimeError) as e:
        err = str(e) or e.__class__.__name__
        if "AUTHENTICATIONFAILED" in err.upper() or "535" in err:
            err += " - check the Gmail address and app password in Settings"
        STATUS.update(last_check=time.time(), last_error=err)
        log(f"[mail] error: {err}")
        return f"Error: {err}"
    finally:
        STATUS["running"] = False
        _run_lock.release()


def start_background(get_wl):
    """Run the inbox watcher forever in a daemon thread.

    get_wl() returns the current Wavelog client (or None if not configured). Settings
    are re-read every cycle, so changes from the settings page apply without a restart.
    """
    def loop():
        next_due = time.time() + 15
        while True:
            time.sleep(30 if next_due - time.time() > 30 else max(1, next_due - time.time()))
            cfg = config()
            STATUS["enabled"] = enabled(cfg)
            wl = get_wl()
            if not STATUS["enabled"] or wl is None:
                next_due = time.time() + 60
                continue
            if time.time() >= next_due:
                run_once(wl, cfg, web_url=cfg["public_url"],
                         log=lambda s: print(s, file=sys.stderr, flush=True))
                next_due = time.time() + max(0.25, cfg["check_hours"]) * 3600

    STATUS["enabled"] = enabled()
    threading.Thread(target=loop, name="mailwatch", daemon=True).start()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Check Gmail for new ARRL DX bulletins and email what you need.")
    ap.add_argument("--once", action="store_true", help="check now and exit (default: keep running)")
    ap.add_argument("--dry-run", action="store_true", help="print the alert instead of sending it")
    ap.add_argument("--resend", action="store_true", help="re-process the newest matching bulletin")
    args = ap.parse_args()

    e = settings.effective()
    if not (e.get("wavelog_url") and e.get("wavelog_key")):
        sys.exit("Wavelog isn't set up yet - finish setup in the web page first.")
    cfg = config()
    if not enabled(cfg):
        sys.exit("Gmail isn't set up yet - add it on the settings page.")
    wl = dxcheck.Wavelog(e["wavelog_url"], e["wavelog_key"], insecure=bool(e.get("wavelog_insecure")),
                         use_index_php=not e.get("wavelog_no_index_php"),
                         station_ids=e.get("wavelog_station_ids"))
    if args.once:
        print(run_once(wl, cfg, dry_run=args.dry_run, resend=args.resend, web_url=cfg["public_url"]))
    else:
        while True:
            print(run_once(wl, cfg, web_url=cfg["public_url"]), flush=True)
            time.sleep(cfg["check_hours"] * 3600)


if __name__ == "__main__":
    main()
