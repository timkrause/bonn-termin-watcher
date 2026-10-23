#!/usr/bin/env python3
"""Watch termine.bonn.de for an early "Umschreibung eines auslaendischen
Fuehrerscheins" slot and (optionally) book it automatically.

Run once per invocation; launchd calls it every 5 minutes with --jitter 60, so
checks land 4-6 minutes apart. After a 403/429 or two errors in a row it pauses
for BACKOFF_MIN minutes. Every run also rewrites status.html, a local dashboard
of what the watcher has been doing.

  python3 termin_watch.py            normal run (books if auto_book is on)
  python3 termin_watch.py --jitter 60  wait a random 0-60 s first
  python3 termin_watch.py --dry-run  show what would match, never books
  python3 termin_watch.py --dry-run --config other.json

Cloud mode (GitHub Actions, see run_loop.sh) is driven by environment variables:
WATCH_DATA_DIR / WATCH_STATUS_FILE move the data files and the status page,
WATCH_PUBLIC=1 keeps contact details off the (public) status page,
CONTACT_FIRST_NAME / CONTACT_LAST_NAME / CONTACT_MAIL fill in the booking form,
NTFY_TOPIC sends alerts to the ntfy phone app instead of macOS notifications.
Exits with DONE_EXIT once there is nothing left to search for.
"""
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("WATCH_DATA_DIR") or HERE)
CONFIG = HERE / "config.json"
STATE = DATA / "state.json"
LOG = DATA / "termin_watch.log"
HISTORY = DATA / "history.json"
LAST_SLOTS = DATA / "last_slots.json"
TEMPLATE = HERE / "status_template.html"
STATUS = Path(os.environ.get("WATCH_STATUS_FILE") or HERE / "status.html")
PUBLIC = os.environ.get("WATCH_PUBLIC") == "1"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
INTERVAL_MIN = 5
BACKOFF_MIN = 30
BROKEN_AFTER = 3  # errors in a row (with 30 min pauses in between) before an urgent alert
EMAIL_LIMIT_TEXT = "maximale mögliche Anzahl an Terminen"  # city refuses more bookings for this email
EMAIL_LIMIT_PAUSE_H = 3
DONE_EXIT = 10

HOST = "https://termine.bonn.de"
BASE = HOST + "/m/dlz/extern/calendar/"
# No wsid: that is a browser session which expires after 10 idle minutes;
# without it the site hands out a fresh one on every check.
START_QS = "uid=b91bb67b-15cf-44df-ab0b-96ebe25c1ae3&lang=de"
PUBLIC_URL = BASE + "?" + START_QS
SERVICE = "ffd89947-51b0-43ff-bcf9-1d271dc9f2d9"   # Umschreibung eines auslaendischen Fuehrerscheins
SERVICE_MIN = 25
LOCATION = "b164ae03-a5ea-4b01-8845-f582dabb947c"  # Dienstleistungszentrum, Berliner Platz 2
STEPS = "serviceslocationssearch_resultsbookingfinish"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def notify(title, text, url=None, alert=True):
    """Phone push via ntfy if NTFY_TOPIC is set, else a macOS banner + sound,
    plus (if alert) a dialog that stays on screen until dismissed."""
    if NTFY_TOPIC:
        if url and url.startswith("file:"):  # local result pages don't exist in the cloud
            url = os.environ.get("WATCH_STATUS_URL") or PUBLIC_URL
        msg = {"topic": NTFY_TOPIC, "title": title, "message": text, "priority": 5 if alert else 3,
               "tags": ["date"], **({"click": url} if url else {})}
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(msg).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20).close()
        except Exception as e:
            log(f"ntfy failed: {type(e).__name__}: {e}")
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{esc(text)}" with title "{esc(title)}" sound name "Glass"'])
    if alert:
        subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
        subprocess.Popen(["osascript", "-e", f'display alert "{esc(title)}" message "{esc(text)}" giving up after 3600'])
    if url:
        subprocess.Popen(["open", url])


class Session:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.url = None

    def request(self, url, data=None):
        body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept-Language": "de-DE,de"})
        with self.opener.open(req, timeout=30) as r:
            self.url = r.geturl()
            return r.read().decode("utf-8", "replace")


def token(page):
    m = re.search(r"name='__RequestVerificationToken' value='([^']+)'", page)
    if not m:
        raise RuntimeError("no verification token on page")
    return m.group(1)


def form_action(page):
    m = re.search(r'<form[^>]*method="post"[^>]*action="([^"]+)"', page)
    if not m:
        raise RuntimeError(f"no form action on page '{page_title(page)}'")
    return BASE + html.unescape(m.group(1)).split("#")[0]


def page_title(page):
    m = re.search(r"<title>\s*([^<]*?)\s*</title>", page, re.S)
    return m.group(1) if m else "?"


def page_text(page):
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def fetch_slots(s):
    """Walk services -> location -> search; return (slots, booking_base_url)."""
    p = s.request(PUBLIC_URL)
    p = s.request(form_action(p), {
        "__RequestVerificationToken": token(p), "action_type": "", "steps": STEPS,
        "step_current": "services", "step_current_index": "0", "step_goto": "+1",
        "access_code_key": "", "services": SERVICE, f"service_{SERVICE}_amount": "1",
    })
    if 'name="locations"' not in p:
        raise RuntimeError(f"service step failed, got page '{page_title(p)}'")
    p = s.request(form_action(p), {
        "__RequestVerificationToken": token(p), "action_type": "search", "steps": STEPS,
        "step_current": "locations", "step_current_index": "1", "step_goto": "+1",
        "access_code_key": "", "locations": ["", LOCATION],
    })
    m = re.search(r"window\.location='(/m/dlz/extern/calendar/booking\?[^']*appointment_datetime=)'", p)
    if not m:
        if "search_results" in p:
            return [], None  # results page without any bookable slot
        raise RuntimeError(f"search step failed, got page '{page_title(p)}'")
    booking_base = HOST + m.group(1)
    slots = []
    for raw_dt, dur, loc, res in re.findall(
            r"appointment_reserve\('([^']+)',\s*'([^']+)',\s*'([^']+)',\s*'([^']+)'\)", p):
        start = dt.datetime.fromisoformat(urllib.parse.unquote(raw_dt))
        slots.append({"start": start, "raw": raw_dt, "duration": dur, "location": loc, "resource": res})
    slots.sort(key=lambda x: x["start"])
    return slots, booking_base


def local_naive(t):
    return t.astimezone().replace(tzinfo=None)


def in_window(start, dur_min, w):
    """A window is one day plus one rule: starts "before", is over by "end_by", or starts "after" (or at)."""
    if start.date().isoformat() != w["date"]:
        return False
    if "before" in w:
        return start.strftime("%H:%M") < w["before"]
    if "end_by" in w:
        return (start + dt.timedelta(minutes=dur_min)).strftime("%H:%M") <= w["end_by"]
    return start.strftime("%H:%M") >= w["after"]


def match(slot, cfg, now):
    """The window a slot falls into, or None."""
    start = local_naive(slot["start"])
    if start < now + dt.timedelta(minutes=cfg.get("min_lead_minutes", 30)):
        return None
    return next((w for w in cfg["windows"] if in_window(start, int(slot["duration"]), w)), None)


def deadline(w, lead):
    """Last moment a check can still find a slot for this window."""
    h, m = map(int, (w.get("before") or w.get("end_by") or "23:59").split(":"))
    lead += SERVICE_MIN if "end_by" in w else 0
    return dt.datetime.fromisoformat(w["date"]).replace(hour=h, minute=m) - dt.timedelta(minutes=lead)


def search_until(cfg, state):
    """When nothing is left worth checking for: goal booked, or every open window is past."""
    if state.get("booked"):
        return dt.datetime.min
    lead = cfg.get("min_lead_minutes", 30)
    wins = [w for w in cfg["windows"] if not (w.get("backup") and state.get("backup_booked"))]
    stop = dt.datetime.strptime(cfg["stop_after"], "%Y-%m-%d %H:%M")
    return min(stop, max((deadline(w, lead) for w in wins), default=dt.datetime.min))


def slot_changes(slots, now):
    """Compare with the previous run: slots that freed up and slots that got taken.

    Only slots up to the previous run's last listed day count as freed, since the
    site lists just the first ten days and a new eleventh day is not a cancellation.
    """
    cur = sorted({local_naive(x["start"]).isoformat(timespec="minutes") for x in slots})
    prev = load_json(LAST_SLOTS, None)
    save_json(LAST_SLOTS, cur)
    if prev is None:
        return None, None
    horizon = max(prev)[:10] if prev else ""
    now_s = now.isoformat(timespec="minutes")
    freed = [x for x in cur if x not in set(prev) and x[:10] <= horizon]
    taken = [x for x in prev if x not in set(cur) and x > now_s]
    return freed, len(taken)


def visible_lines(page):
    page = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    return [line for line in (re.sub(r"\s+", " ", x).strip() for x in text.split("\n")) if line]


def server_message(before, after):
    """Text the site added after the form was sent, i.e. its reason for refusing.
    (The form's own error texts are always in the page, so they say nothing.)"""
    seen = set(visible_lines(before))
    return "; ".join(line for line in visible_lines(after) if line not in seen)


def release(s, page):
    """Leave the booking step so the held slot is free again, e.g. for a manual booking."""
    m = re.search(r'data-testid="button_back" href="([^"]+)"', page)
    if m:
        try:
            s.request(BASE + html.unescape(m.group(1)).split("#")[0])
        except Exception as e:
            log(f"releasing the slot failed: {type(e).__name__}: {e}")


def mail_key(cfg):
    """Identifies the booking email in the (public) state without revealing it."""
    return hashlib.sha256(cfg["contact"].get("mail", "").strip().lower().encode()).hexdigest()[:12]


def book(s, booking_base, slot, cfg):
    """Returns (outcome, info, page) with outcome True=booked, None=unclear, False=failed."""
    url = (booking_base + slot["raw"] + "&appointment_duration=" + slot["duration"]
           + "&location=" + slot["location"] + "&resources=" + slot["resource"])
    form = s.request(url)  # reserves the slot for this session
    if 'id="booking_form"' not in form:
        return False, f"slot no longer available (page '{page_title(form)}')", form
    c = cfg["contact"]
    p = s.request(s.url, {
        "__RequestVerificationToken": token(form), "action_type": "booking",
        "first_name": c["first_name"], "last_name": c["last_name"], "mail": c["mail"],
        "phone": c.get("phone", ""), "comment": c.get("comment", ""), "accept_data_privacy": "1",
    })
    if 'id="booking_form"' in p:
        reason = server_message(form, p) or f"no reason given (page '{page_title(p)}')"
        for v in (c["first_name"], c["last_name"], c["mail"]):
            reason = reason.replace(v, "…") if v else reason
        release(s, p)
        return False, f"refused: {reason}", p
    if "Bestätigung" in page_title(p) or re.search(
            r"erfolgreich|Buchungsnummer|Vorgangsnummer|Termin wurde|bestätigt", page_text(p), re.I):
        return True, page_title(p), p
    return None, f"no booking form, no confirmation text (page '{page_title(p)}')", p


def run(cfg, state, rec, dry):
    """One check. Fills rec with what happened."""
    now = dt.datetime.now()
    if state.get("booked"):
        rec.update(result="idle", msg=f"already booked {state['booked']}")
        return
    if now >= search_until(cfg, state):
        rec.update(result="idle", msg=f"search over, keeping backup {state['backup_booked']}"
                   if state.get("backup_booked") else "search over")
        return
    if state.get("pause_until", "") > now.isoformat(timespec="seconds"):
        rec.update(result="paused", msg=f"backing off until {state['pause_until'][11:16]}")
        return

    s = Session()
    slots, booking_base = fetch_slots(s)
    # Once a backup is booked, only goal windows (today/tomorrow) still count.
    hits = [(x, w) for x in slots for w in [match(x, cfg, now)]
            if w and not (w.get("backup") and state.get("backup_booked"))]
    rec.update(slots=len(slots), matching=len(hits), result="no_match",
               earliest=local_naive(slots[0]["start"]).isoformat(timespec="minutes") if slots else None)
    if not dry:
        freed, taken = slot_changes(slots, now)
        rec.update(freed=freed and freed[:20], freed_count=None if freed is None else len(freed), taken_count=taken)
    if dry:
        rec["msg"] = "dry run: " + (", ".join(
            f"{x['start']:%a %d.%m. %H:%M}{' (backup)' if w.get('backup') else ''}" for x, w in hits) or "nothing")
        return
    if not hits:
        return

    # A goal slot always wins; otherwise the earliest backup slot.
    slot, win = next(((x, w) for x, w in hits if not w.get("backup")), hits[0])
    backup = bool(win.get("backup"))
    when = slot["start"].strftime("%a %d.%m. %H:%M")
    rec.update(slot=local_naive(slot["start"]).isoformat(timespec="minutes"), tier="backup" if backup else "goal")
    limit = state.get("email_limit") or {}
    limited = limit.get("mail") == mail_key(cfg) and limit.get("until", "") > now.isoformat(timespec="seconds")
    auto = (cfg.get("auto_book") and cfg.get("contact", {}).get("mail")
            and not state.get("unclear") and not limited)
    if not auto:
        if state.get("alerted") != slot["raw"]:
            notify("Bonn Termin frei!", f"Führerschein-Umschreibung {when} – jetzt selbst buchen"
                   + (" (Auto-Buchung pausiert: E-Mail-Limit)" if limited else ""), PUBLIC_URL)
            state["alerted"] = slot["raw"]
            save_json(STATE, state)
        rec.update(result="alerted", msg=f"alerted for {when}")
        return

    try:
        outcome, info, page = book(s, booking_base, slot, cfg)
    except Exception as e:  # may have gone through; treat as unclear so it never books twice
        outcome, info, page = None, f"{type(e).__name__}: {e}", ""
    out = DATA / f"booking_result_{now:%Y%m%d_%H%M%S}.html"  # has personal data; never committed
    out.write_text(page, encoding="utf-8")
    if not PUBLIC:
        rec["page"] = out.name
    if backup and outcome is not False:
        # Booked or unclear: either way stop booking backups, keep hunting for a goal slot.
        state["backup_booked"] = when if outcome else f"{when} (unclear)"
        save_json(STATE, state)
        rec.update(result="booked_backup" if outcome else "unclear", msg=f"backup {when}: {info}")
        notify("Bonn Backup-Termin GEBUCHT" if outcome else "Bonn Backup-Buchung unklar",
               f"{when}. Bitte E-Mail prüfen. Suche weiter nach heute/morgen.", out.as_uri())
    elif outcome:
        state["booked"] = when
        save_json(STATE, state)
        rec.update(result="booked", msg=f"booked {when}: {info}")
        cancel = (f" Backup {state['backup_booked']} bitte über den Link in dessen E-Mail stornieren!"
                  if state.get("backup_booked") else " Bitte E-Mail prüfen.")
        notify("Bonn Termin GEBUCHT", f"{when}, Stadthaus Berliner Platz 2.{cancel}", out.as_uri())
    elif outcome is None:
        # Might have booked; stop auto-booking so it never books a second slot.
        state["unclear"] = when
        save_json(STATE, state)
        rec.update(result="unclear", msg=f"booking {when} unclear: {info}")
        notify("Bonn Termin: Buchung unklar", f"{when}: bitte E-Mail prüfen. Auto-Buchung pausiert.", out.as_uri())
    else:
        rec.update(result="failed", msg=f"booking {when} failed: {info}")
        if EMAIL_LIMIT_TEXT in info:
            # Retrying can't help: alert for every match instead and retry auto-booking later.
            until = now + dt.timedelta(hours=EMAIL_LIMIT_PAUSE_H)
            state.update(email_limit={"mail": mail_key(cfg), "until": until.isoformat(timespec="seconds")},
                         alerted=slot["raw"])
            save_json(STATE, state)
            notify("Bonn Termin frei – E-Mail-Limit!",
                   f"{when} ist frei, aber die Stadt lehnt die E-Mail ab: maximale Anzahl Termine erreicht. "
                   f"Slot ist wieder freigegeben – jetzt selbst buchen! Auto-Buchung pausiert bis {until:%H:%M}.",
                   PUBLIC_URL)
        else:
            notify("Bonn Termin: Buchung fehlgeschlagen",
                   f"{when}: {info}. Slot ist wieder freigegeben – selbst versuchen!", PUBLIC_URL)


def backoff(state, rec, blocked, retry_after=None):
    """Count the error; pause right away if the site pushed back, else after two in a row."""
    state["errors_in_row"] = state.get("errors_in_row", 0) + 1
    if not blocked and state["errors_in_row"] < 2:
        return
    mins = BACKOFF_MIN
    if retry_after and retry_after.isdigit():
        mins = max(mins, int(retry_after) / 60)
    until = dt.datetime.now() + dt.timedelta(minutes=mins)
    state["pause_until"] = until.isoformat(timespec="seconds")
    rec["msg"] += f" · pausing until {until:%H:%M}"
    # Quiet at first; loud once it has been failing for over half an hour.
    broken = state["errors_in_row"] == BROKEN_AFTER
    notify("Bonn Watcher: Fehler seit über 30 Min!" if broken else "Bonn Watcher pausiert",
           f"{rec['msg']}. Nächster Versuch {until:%H:%M}." + (" Bitte selbst prüfen." if broken else ""),
           alert=broken)


def write_status(cfg, state, history):
    data = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "interval_min": INTERVAL_MIN,
        "windows": [dict(w, deadline=deadline(w, cfg.get("min_lead_minutes", 30)).isoformat(timespec="minutes"))
                    for w in cfg["windows"]],
        "search_until": None if state.get("booked") else search_until(cfg, state).isoformat(timespec="minutes"),
        "duration_min": SERVICE_MIN,
        "min_lead_minutes": cfg.get("min_lead_minutes", 30),
        "auto_book": bool(cfg.get("auto_book")),
        "contact": {"name": "", "mail": ""} if PUBLIC else {
            "name": f"{cfg['contact'].get('first_name', '')} {cfg['contact'].get('last_name', '')}".strip(),
            "mail": cfg["contact"].get("mail", "")},
        "cloud": PUBLIC,
        "stop_hint": os.environ.get("WATCH_STOP_HINT") or "launchctl bootout gui/501/local.bonn-termin-watch",
        "state": state,
        "public_url": PUBLIC_URL,
        "checks": history,
    }
    page = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, STATUS)


def main():
    dry = "--dry-run" in sys.argv
    config = Path(sys.argv[sys.argv.index("--config") + 1]) if "--config" in sys.argv else CONFIG
    cfg = json.loads(config.read_text(encoding="utf-8"))
    for key in ("first_name", "last_name", "mail", "phone"):
        cfg["contact"][key] = os.environ.get(f"CONTACT_{key.upper()}") or cfg["contact"].get(key, "")
    state = load_json(STATE, {})
    jitter = int(sys.argv[sys.argv.index("--jitter") + 1]) if "--jitter" in sys.argv else 0
    if jitter and not state.get("booked") and dt.datetime.now().strftime("%Y-%m-%d %H:%M") < cfg["stop_after"]:
        time.sleep(random.uniform(0, jitter))
    rec = {"t": dt.datetime.now().isoformat(timespec="seconds"), "msg": ""}
    t0 = time.monotonic()
    code = 0
    try:
        run(cfg, state, rec, dry)
        if rec["result"] not in ("paused", "idle"):
            state.pop("errors_in_row", None)
            state.pop("pause_until", None)
    except urllib.error.HTTPError as e:
        rec.update(result="error", msg=f"HTTP {e.code} {e.reason}")
        code = 1
        if not dry:
            backoff(state, rec, e.code in (403, 429), e.headers.get("Retry-After"))
    except Exception as e:
        rec.update(result="error", msg=f"{type(e).__name__}: {e}")
        code = 1
        if not dry:
            backoff(state, rec, False)
    rec["secs"] = round(time.monotonic() - t0, 1)
    log(f"{rec['result']}: {rec.get('slots', '-')} slots, earliest {rec.get('earliest') or '-'}, "
        f"{rec.get('matching', 0)} matching, freed {rec.get('freed_count', '-')}"
        + (f" | {rec['msg']}" if rec["msg"] else ""))
    if dry:
        return code
    save_json(STATE, state)
    history = load_json(HISTORY, [])
    history.append(rec)
    save_json(HISTORY, history[-2000:])
    write_status(cfg, state, history[-2000:])
    return DONE_EXIT if rec["result"] == "idle" else code


if __name__ == "__main__":
    sys.exit(main())
