#!/usr/bin/env python3
"""
dx_needed.py - Check the DX stations in an ARRL DX Bulletin (or any DX newsletter)
against your Wavelog logbook and list the DXCC entities you still need.

Usage:
    python3 dx_needed.py bulletin.txt
    python3 dx_needed.py bulletin.txt --band 20m          # also check one band slot
    pbpaste | python3 dx_needed.py -                     # read from stdin
    python3 dx_needed.py bulletin.html --csv needed.csv  # HTML works too

Configuration (flags override environment variables):
    WAVELOG_URL   e.g. https://log.example.com   (your Wavelog base URL)
    WAVELOG_KEY   API key. A key starting with "wl2_" uses API v2 (Wavelog 3.1+);
                  any other key uses the v1 /api/private_lookup endpoint (1.8.6+).

Only the Python 3 standard library is used.
"""

import argparse
import csv
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Callsign extraction
# ---------------------------------------------------------------------------

# Optional prefix portion (e.g. "VP8/"), then the base call:
#   prefix  = 1-2 letters | digit+1-2 letters | letter+digit
#   numeral = 1-4 digits
#   suffix  = 1-4 letters
# then an optional "/P", "/MM", "/3" etc.
CALL_RE = re.compile(
    r"(?<![A-Za-z0-9/])"
    r"((?:[A-Z0-9]{1,4}/)?"
    r"(?:[A-Z]{1,2}|[0-9][A-Z]{1,2}|[A-Z][0-9])"
    r"[0-9]{1,4}[A-Z]{1,4}"
    r"(?:/[A-Z0-9]{1,4})?)"
    r"(?![A-Za-z0-9/])"
)

# Tokens that look like callsigns but aren't.
NOT_CALLS = {
    "JT65A", "JT65B", "JT9A", "FT991A", "IC7300", "IC705", "K3S", "K4D",
    "TS590S", "TS890S", "FTDX10", "Q65A", "MSK144",
}

# Text right before a call that means "this is a QSL manager / log address",
# not the DX station itself.
MANAGER_BEFORE_RE = re.compile(
    r"(QSL'?S?|QSL\s+(?:VIA|TO|DIRECT\s+TO|MANAGER(?:\s+IS)?)|MANAGER(?:\s+IS)?|"
    r"VIA|DIRECT\s+TO|BUREAU\s+TO|LOGS?\s+(?:TO|VIA)|OQRS\s+(?:VIA|ON))"
    r"[\s:,(]*(?:(?:[A-Z0-9/]+|LOTW|CLUB\s*LOG|EQSL)\s*(?:,|OR|AND|/)\s*)*$",
    re.IGNORECASE,
)


# A home call followed by "... QRV as <DX call>" / "operating as" / "signing" etc.
HOME_AFTER_RE = re.compile(
    r"^[^.\n]{0,40}?\b(?:QRV|ACTIVE|OPERATING|OPERATE|ON\s+THE\s+AIR)\s+(?:AS|USING)\b"
    r"|^[,\s]*(?:IS|WILL\s+BE|ARE)?\s*(?:SIGNING|USING\s+THE\s+CALL(?:SIGN)?)\b",
    re.IGNORECASE,
)
# Credits: "provided by K1XYZ", "thanks to N1ABC", "courtesy of ...", and the
# bulletin header "QST de W1AW" (the sender, not a DX station)
CREDIT_BEFORE_RE = re.compile(
    r"(PROVIDED\s+BY|THANKS\s+TO|COURTESY\s+OF|INFORMATION\s+FROM|REPORTED\s+BY|ACCORDING\s+TO|QST\s+DE|\bDE)"
    r"[\s:,]*(?:(?:[A-Z0-9/]+|THE\s+[\w\s]+?)\s*(?:,|AND|OR)\s*)*$",
    re.IGNORECASE,
)


def strip_html(text):
    if "<" not in text or ">" not in text:
        return text
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


PARA_BREAK_RE = re.compile(r"\n[ \t]*\n")


def paragraph_at(text, start, end, limit=1200):
    """The whole paragraph (blank-line separated) around a match, whitespace collapsed.

    Bulletins wrap each entity's write-up over several lines, so the line alone cuts
    sentences off. Very long paragraphs are trimmed around the match.
    """
    breaks = [m.end() for m in PARA_BREAK_RE.finditer(text, 0, start)]
    p_start = breaks[-1] if breaks else 0
    nxt = PARA_BREAK_RE.search(text, end)
    p_end = nxt.start() if nxt else len(text)
    para = re.sub(r"\s+", " ", text[p_start:p_end]).strip()
    if len(para) > limit:
        call = text[start:end]
        i = para.find(call)
        lo = max(0, i - limit // 3)
        para = ("… " if lo else "") + para[lo:lo + limit].rstrip() + " …"
    return para


def extract_calls(text, ignore_case=False):
    """Return {callsign: {"context": line, "manager": bool}} in order of first appearance.

    "manager" is True for calls that look like QSL managers, operators' home calls
    ("F6ABC will be QRV as 5R8XX") or credit lines, i.e. not the DX station itself.
    """
    found = {}
    if ignore_case:
        text_for_match = text.upper()
    else:
        text_for_match = text
    for m in CALL_RE.finditer(text_for_match):
        call = m.group(1).upper()
        base = call.split("/")
        # pick the longest piece as the "base" for sanity checks
        core = max(base, key=len)
        if call in NOT_CALLS or core in NOT_CALLS:
            continue
        if not re.search(r"[A-Z]", core) or not re.search(r"[0-9]", core):
            continue
        before = text_for_match[max(0, m.start() - 60):m.start()]
        after = text_for_match[m.end():m.end() + 60]
        is_manager = bool(MANAGER_BEFORE_RE.search(before)
                          or CREDIT_BEFORE_RE.search(before)
                          or HOME_AFTER_RE.search(after))
        line = paragraph_at(text, m.start(), m.end())
        if call not in found:
            found[call] = {"context": line, "manager": is_manager}
        elif not is_manager:
            # seen at least once as a non-manager mention -> treat as DX station
            found[call]["manager"] = False
    return found


# ---------------------------------------------------------------------------
# Wavelog API
# ---------------------------------------------------------------------------

def truthy(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "y")


class Wavelog:
    def __init__(self, base_url, key, insecure=False, use_index_php=True,
                 timeout=20, delay=0.3, station_ids=None):
        self.base = base_url.rstrip("/")
        if use_index_php and not self.base.endswith("/index.php"):
            self.base += "/index.php"
        self.key = key
        self.v2 = key.startswith("wl2_")
        self.timeout = timeout
        self.delay = delay
        self.station_ids = station_ids
        self.ctx = ssl._create_unverified_context() if insecure else None
        self._last = 0.0

    def _throttle(self):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _request(self, req):
        self._throttle()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {e.code} from Wavelog: {body}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach Wavelog at {self.base}: {e.reason}") from None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"Wavelog returned non-JSON: {body[:200]!r}") from None

    def lookup(self, callsign, band=None, mode=None):
        """Return a normalised dict: dxcc, dxcc_id, dxcc_confirmed, call_worked, ..."""
        if self.v2:
            params = {"callsign": callsign, "detail": "full"}
            if band:
                params["band"] = band
            if mode:
                params["mode"] = mode
            if self.station_ids:
                params["station_ids"] = self.station_ids
            url = f"{self.base}/api/v2/lookup?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
                "User-Agent": "dx_needed.py",
            })
            resp = self._request(req)
            data = resp.get("data", resp) if isinstance(resp, dict) else {}
        else:
            payload = {"key": self.key, "callsign": callsign}
            if band:
                payload["band"] = band
            if mode:
                payload["mode"] = mode
            req = urllib.request.Request(
                f"{self.base}/api/private_lookup",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json",
                         "User-Agent": "dx_needed.py"},
                method="POST",
            )
            data = self._request(req)
            if isinstance(data, dict) and data.get("status") == "failed":
                raise RuntimeError(f"Wavelog: {data.get('reason', data)}")

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected response for {callsign}: {data!r}")
        dxcc_id = str(data.get("dxcc_id") or "").strip()
        return {
            "dxcc": (data.get("dxcc") or "").strip(),
            "dxcc_id": dxcc_id if dxcc_id not in ("", "0") else "",
            "cont": data.get("cont") or "",
            "dxcc_confirmed": truthy(data.get("dxcc_confirmed")),
            "dxcc_confirmed_on_band": truthy(data.get("dxcc_confirmed_on_band")),
            "dxcc_confirmed_on_band_mode": truthy(data.get("dxcc_confirmed_on_band_mode")),
            "call_worked": truthy(data.get("call_worked", data.get("workedBefore"))),
        }


# ---------------------------------------------------------------------------
# Activity dates ("QRV from November 14 to 28", "until February", ...)
# ---------------------------------------------------------------------------

import calendar
import datetime as _dt

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
_MON = r"(January|February|March|April|May|June|July|August|September|October|November|December|" \
       r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)(?![a-z])\.?"
# "November 14", "Nov. 14, 2026", "November 14 to 28", "November 14-28", "November 15 and 16"
_DATE_RE = re.compile(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?"
                      r"(?:\s*(?:-|–|to|through|and)\s*(\d{1,2})(?:st|nd|rd|th)?\b(?!\s*(?:m\b|meters|UTC|Z\b)))?"
                      r"(?:,?\s+(20\d\d))?", re.IGNORECASE)
# "until February", "until mid-December", "through January 2027", "until the end of March"
_UNTIL_MONTH_RE = re.compile(r"\b(?:until|till|through|thru|to)\s+(?:the\s+end\s+of\s+|mid-?|late\s+|early\s+)?"
                             + _MON + r"(?!\.?\s*\d{1,2}\b(?!\d))(?:\s+(20\d\d))?", re.IGNORECASE)
# reference date in the bulletin itself: "ARLD045 DX news for November 12, 2026"
_REF_RE = re.compile(r"\bfor\s+" + _MON + r"\s+(\d{1,2}),\s*(20\d\d)", re.IGNORECASE)


def bulletin_date(text):
    """Date the bulletin was issued, if it says ("... DX news for November 12, 2026")."""
    m = _REF_RE.search(text[:2000])
    if m:
        try:
            return _dt.date(int(m.group(3)), _MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)))
        except ValueError:
            pass
    return None


def _resolve(month, day, year, ref):
    """Turn a month/day (maybe without year) into a date near the bulletin date."""
    if year:
        try:
            return _dt.date(int(year), month, day)
        except ValueError:
            return None
    for y in (ref.year, ref.year + 1, ref.year - 1):
        try:
            d = _dt.date(y, month, day)
        except ValueError:
            continue
        # bulletins look ahead: a date more than ~2 months before the bulletin is next year
        if -62 <= (d - ref).days <= 300:
            return d
    return None


_UNTIL_BEFORE_RE = re.compile(r"\b(?:until|till|through|thru|by|ending)\s+$", re.IGNORECASE)


def activity_window(context, ref):
    """(start, end) dates mentioned in a station's write-up, or (None, None).

    start is None when only an end is given ("until November 30").
    """
    starts, ends = [], []
    for m in _DATE_RE.finditer(context):
        mon = _MONTHS[m.group(1).lower().rstrip(".")]
        d1 = _resolve(mon, int(m.group(2)), m.group(4), ref)
        d2 = _resolve(mon, int(m.group(3)), m.group(4), ref) if m.group(3) else None
        only_end = bool(_UNTIL_BEFORE_RE.search(context[max(0, m.start() - 12):m.start()]))
        if d1:
            (ends if only_end else starts).append(d1)
            ends.append(d1)
        if d2 and d1 and d2 >= d1:
            ends.append(d2)
    for m in _UNTIL_MONTH_RE.finditer(context):
        mon = _MONTHS[m.group(1).lower().rstrip(".")]
        first = _resolve(mon, 1, m.group(2), ref)
        if first:
            ends.append(first.replace(day=calendar.monthrange(first.year, first.month)[1]))
    if not ends:
        return None, None
    return (min(starts) if starts else None), max(ends)


def window_label(start, end, today):
    """Short human label: "Nov 14–28", "until Feb 28", "ended Nov 7", "on Nov 15"."""
    if not end:
        return ""
    yr = lambda d: f", {d.year}" if d.year != today.year else ""
    md = lambda d: d.strftime("%b %-d")
    if end < today:
        return f"ended {md(end)}{yr(end)}"
    if start and start > today:
        if start == end:
            return f"on {md(start)}{yr(start)}"
        if (start.year, start.month) == (end.year, end.month):
            return f"{md(start)}–{end.day}{yr(end)}"
        if start.year == end.year:
            return f"{md(start)}–{md(end)}{yr(end)}"
        return f"{md(start)}{yr(start)}–{md(end)}{yr(end)}"
    return f"until {md(end)}{yr(end)}"


def check_calls(calls, wl, band=None, mode=None, on_progress=None, ref_date=None, today=None):
    """Look up each call in Wavelog and group by DXCC entity.

    calls: {callsign: {"context": str, ...}} as returned by extract_calls().
    on_progress(i, total, call, err=None) is called before each lookup / on errors.
    Raises RuntimeError if the very first lookup fails (bad URL/key), otherwise
    records failed calls as unresolved and keeps going.

    ref_date: when the bulletin was issued (to place dates like "November 14" in the
    right year); today: for deciding what has already ended. Both default to today.

    Returns {"entities": [...], "unresolved": [...]} with entities sorted needed-first.
    Each entity has "start"/"end" (ISO dates or None), "when" (e.g. "Nov 14–28") and
    "ended" (True when every listed station's activity has finished).
    """
    today = today or _dt.date.today()
    ref_date = ref_date or today
    entities = {}
    unresolved = []
    total = len(calls)
    for i, (call, meta) in enumerate(calls.items(), 1):
        if on_progress:
            on_progress(i, total, call)
        try:
            r = wl.lookup(call, band=band, mode=mode)
        except RuntimeError as e:
            if i == 1:
                raise
            if on_progress:
                on_progress(i, total, call, err=str(e))
            unresolved.append(call)
            continue
        if not r["dxcc_id"]:
            unresolved.append(call)
            continue
        ent = entities.setdefault(r["dxcc_id"], {
            "dxcc_id": r["dxcc_id"], "dxcc": r["dxcc"], "cont": r["cont"],
            "confirmed": r["dxcc_confirmed"],
            "confirmed_band": r["dxcc_confirmed_on_band"],
            "confirmed_band_mode": r["dxcc_confirmed_on_band_mode"],
            "calls": [], "worked_calls": [], "context": meta["context"], "windows": [],
        })
        ent["calls"].append(call)
        ent["windows"].append(activity_window(meta["context"], ref_date))
        if r["call_worked"]:
            ent["worked_calls"].append(call)

    def status(e):
        if not e["confirmed"]:
            return ("need_qsl", "NEED QSL") if e["worked_calls"] else ("new", "NEW ONE")
        if band and not e["confirmed_band"]:
            return "band", f"BAND SLOT ({band})"
        if band and mode and not e["confirmed_band_mode"]:
            return "mode", f"MODE SLOT ({band} {mode.upper()})"
        return "have", "have it"

    order = {"new": 0, "need_qsl": 1, "band": 2, "mode": 3, "have": 4}
    rows = []
    for e in entities.values():
        kind, label = status(e)
        starts = [w[0] for w in e["windows"]]
        ends = [w[1] for w in e["windows"]]
        # If any station has no dates we can't tell it's over, so keep the entity.
        end = max(ends) if ends and all(ends) else None
        start = min(starts) if starts and all(starts) else None
        ended = bool(end and end < today)
        rows.append({
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "when": window_label(start, end, today),
            "ended": ended,
            "kind": kind, "status": label, "needed": kind != "have" and not ended,
            "dxcc": e["dxcc"], "dxcc_id": e["dxcc_id"], "continent": e["cont"],
            "callsigns": " ".join(e["calls"]),
            "already_worked_calls": " ".join(e["worked_calls"]),
            "context": e["context"],
        })
    rows.sort(key=lambda r: (r["ended"], order[r["kind"]], r["dxcc"]))
    return {"entities": rows, "unresolved": unresolved}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def read_input(paths):
    chunks = []
    for p in paths:
        if p == "-":
            chunks.append(sys.stdin.read())
        else:
            if p.lower().endswith(".pdf"):
                sys.exit(f"{p}: PDFs aren't supported directly - save the bulletin as text "
                         f"(or copy/paste it) and try again.")
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                chunks.append(f.read())
    return strip_html("\n".join(chunks))


def main():
    ap = argparse.ArgumentParser(
        description="List DXCC entities mentioned in a DX newsletter that you haven't "
                    "confirmed in Wavelog.")
    ap.add_argument("files", nargs="+", help="newsletter file(s) (.txt or .html), or - for stdin")
    ap.add_argument("--url", default=os.environ.get("WAVELOG_URL"),
                    help="Wavelog base URL (or set WAVELOG_URL)")
    ap.add_argument("--key", default=os.environ.get("WAVELOG_KEY"),
                    help="Wavelog API key (or set WAVELOG_KEY); wl2_ keys use API v2")
    ap.add_argument("--band", help="also report entities not confirmed on this band, e.g. 20m")
    ap.add_argument("--mode", help="with --band, also check band+mode, e.g. CW, SSB, FT8")
    ap.add_argument("--station-ids", help="(API v2 only) comma-separated station location IDs")
    ap.add_argument("--all", action="store_true",
                    help="show every entity found, including ones you already have")
    ap.add_argument("--include-managers", action="store_true",
                    help="also look up QSL managers, operators' home calls and credited calls")
    ap.add_argument("--ignore-case", action="store_true",
                    help="match lowercase callsigns too (more false positives)")
    ap.add_argument("--csv", metavar="FILE", help="write results to a CSV file")
    ap.add_argument("--json", action="store_true", help="print results as JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="just show the callsigns that would be looked up")
    ap.add_argument("--no-index-php", action="store_true",
                    help="don't add /index.php to the URL (if your server rewrites it)")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS certificate checks (self-signed home servers)")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds between API calls (default 0.3, be polite)")
    args = ap.parse_args()

    text = read_input(args.files)
    calls = extract_calls(text, ignore_case=args.ignore_case)
    managers = [c for c, v in calls.items() if v["manager"]]
    if not args.include_managers:
        calls = {c: v for c, v in calls.items() if not v["manager"]}

    if not calls:
        sys.exit("No callsigns found in the input.")

    if args.dry_run:
        print(f"{len(calls)} callsign(s) found:")
        for c, v in calls.items():
            print(f"  {c:<14} {v['context'][:90]}")
        if managers and not args.include_managers:
            print(f"\nSkipped (QSL managers / home calls / credits): {', '.join(managers)}")
        return

    if not args.url or not args.key:
        sys.exit("Set your Wavelog URL and API key with --url/--key or "
                 "WAVELOG_URL/WAVELOG_KEY.")

    wl = Wavelog(args.url, args.key, insecure=args.insecure,
                 use_index_php=not args.no_index_php, delay=args.delay,
                 station_ids=args.station_ids)

    def progress(i, total, call, err=None):
        if err:
            print(f"\n  {call}: {err}", file=sys.stderr)
        else:
            print(f"\rLooking up {i}/{total}: {call:<14}", end="", file=sys.stderr, flush=True)

    try:
        out = check_calls(calls, wl, band=args.band, mode=args.mode, on_progress=progress,
                          ref_date=bulletin_date(text))
    except RuntimeError as e:
        sys.exit(f"\nError: {e}")
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    entities, unresolved = out["entities"], out["unresolved"]
    n_needed = sum(1 for r in entities if r["needed"])
    n_ended = sum(1 for r in entities if r["ended"] and r["kind"] != "have")
    results = [{k: v for k, v in r.items() if k not in ("needed", "kind", "start", "end")}
               for r in entities if args.all or r["needed"]]

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else
                               ["status", "dxcc", "dxcc_id", "continent", "callsigns",
                                "already_worked_calls", "context"])
            w.writeheader()
            w.writerows(results)

    if args.json:
        print(json.dumps({"needed": results, "unresolved": unresolved,
                          "skipped_managers": [] if args.include_managers else managers},
                         indent=2))
        return

    print(f"\n{len(calls)} callsigns -> {len(entities)} DXCC entities, "
          f"{n_needed} needed"
          + (f" (counting {args.band}{' ' + args.mode.upper() if args.mode else ''} slots)"
             if args.band else "") + "\n")
    if results:
        w_status = max(len(r["status"]) for r in results)
        w_dxcc = min(32, max(len(r["dxcc"]) for r in results))
        for r in results:
            print(f"  {r['status']:<{w_status}}  {r['dxcc'][:w_dxcc]:<{w_dxcc}}  "
                  f"{r['callsigns']}" + (f"   [{r['when']}]" if r['when'] else ""))
            print(f"  {'':<{w_status}}  {'':<{w_dxcc}}  > {r['context'][:100]}")
    else:
        print("  Nothing new - you've got every entity in this bulletin confirmed.")
    print()
    if n_ended and not args.all:
        print(f"Not listed: {n_ended} needed entit{'y' if n_ended == 1 else 'ies'} whose activity has already ended.")
    if unresolved:
        print(f"Couldn't resolve a DXCC for: {', '.join(unresolved)}")
    if managers and not args.include_managers:
        print(f"Skipped (QSL managers / home calls / credits): {', '.join(managers)}")
    if args.csv:
        print(f"\nSaved to {args.csv}")


if __name__ == "__main__":
    main()
