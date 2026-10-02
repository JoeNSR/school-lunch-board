#!/usr/bin/env python3
"""Email tomorrow's school lunch menu (one email per school).

Runs from GitHub Actions at about 5pm Central (GitHub may start it late; see scheduled_gate).

Where the menus come from: the district's "Newsletter and Calendar" page lists a monthly
PDF for each school (e.g. "OCT. 26 HS MENU", "OCT. 26 MERRILLAN"). This script finds those
links, downloads the PDF for the right month, reads the calendar grid out of it, and emails
tomorrow's lunch.

Usage:
  python send_lunch_menu.py                      # normal run (only acts during the 5pm Central hour)
  (in GitHub Actions the CRON_SCHEDULE env var tells the script which cron entry fired; see scheduled_gate)
  python send_lunch_menu.py --force              # ignore the 5pm check
  python send_lunch_menu.py --dry-run            # print messages instead of sending (no 5pm check)
  python send_lunch_menu.py --dry-run --date 2026-10-14   # pretend "tomorrow" is this date
  python send_lunch_menu.py --dump 2026-10       # print every parsed lunch for a month (to eyeball)
  python send_lunch_menu.py --export-json menu.json   # write this + next month's lunches as JSON (for the DAKboard page)
"""
import argparse
import calendar
import datetime as dt
import io
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import pdfplumber

TZ = ZoneInfo("America/Chicago")
SEND_HOUR = 17  # 5pm Central

MENU_PAGE = "https://www.lincolnhornets.org/apps/pages/index.jsp?uREC_ID=791328&type=d&pREC_ID=1184113"

# One entry per email. Each school gets its own message.
#   label       - used in the first line of the email body ("<label> Lunch Menu for ...")
#   match       - regex matched against the PDF link's text on the menu page
#   recipients  - name of the env var / GitHub secret holding comma-separated email addresses
#                 (falls back to EMAIL_RECIPIENTS if that one is empty)
SCHOOLS = [
    {
        "name": "Lincoln Elementary",
        "key": "elementary",
        "label": "ACHM Elementary",
        "match": r"merrillan|elementary",
        "recipients": "ELEMENTARY_RECIPIENTS",
    },
    {
        "name": "Lincoln High School",
        "key": "highschool",
        "label": "ACHM High School",
        "match": r"\bhs\b|high\s*school|alma\s*center",
        "recipients": "HS_RECIPIENTS",
    },
]

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
FOOTER_WORDS = {"served", "choices:", "institution", "subject", "garden"}


# ----------------------------------------------------------------------------
# Finding and downloading the menu PDFs
# ----------------------------------------------------------------------------
def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (lunch-menu-bot)"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


class LinkParser(HTMLParser):
    """Collects (href, text) for every link on the page."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), ""

    def handle_data(self, data):
        if self._href is not None:
            self._text += data

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, re.sub(r"\s+", " ", self._text).strip()))
            self._href = None


def pdf_links(school):
    parser = LinkParser()
    parser.feed(http_get(MENU_PAGE).decode("utf-8", errors="replace"))
    out = []
    for href, text in parser.links:
        if ".pdf" in href.lower() and re.search(school["match"], text, re.I):
            out.append((urllib.parse.urljoin(MENU_PAGE, href), text))
    return out


# ----------------------------------------------------------------------------
# Reading the calendar grid out of a menu PDF
# ----------------------------------------------------------------------------
def group_lines(words):
    """Group words into text lines; words far apart horizontally become separate lines."""
    words = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    rows, cur = [], []
    for w in words:
        if cur and abs(w["top"] - cur[0]["top"]) > 3.5:
            rows.append(cur)
            cur = []
        cur.append(w)
    if cur:
        rows.append(cur)
    lines = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        seg = [row[0]]
        for w in row[1:]:
            if w["x0"] - seg[-1]["x1"] > 14:  # big gap: a different cell's text
                lines.append(seg)
                seg = []
            seg.append(w)
        lines.append(seg)
    return lines


def parse_menu_pdf(data):
    """Return (year, month, {day: [text lines]}) from a monthly menu PDF."""
    days = {}
    year = month = None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            if not words:
                continue
            if year is None:
                m = re.search(r"(%s)\s+(\d{4})" % "|".join(calendar.month_name[1:]),
                              page.extract_text() or "", re.I)
                if m:
                    month, year = MONTHS[m.group(1).lower()], int(m.group(2))

            # Column centers come from the Monday..Friday header words.
            centers = {}
            header_bottom = 0
            for w in words:
                if w["text"] in WEEKDAYS and w["text"] not in centers:
                    centers[w["text"]] = (w["x0"] + w["x1"]) / 2
                    header_bottom = max(header_bottom, w["bottom"])
            if len(centers) < 5:
                continue
            xs = [centers[d] for d in WEEKDAYS]

            def col_of(x):
                return min(range(5), key=lambda i: abs(x - xs[i]))

            # Day numbers sit centered at the top of each cell.
            nums = []
            for w in words:
                if re.fullmatch(r"\d{1,2}", w["text"]) and w["top"] > header_bottom:
                    xc = (w["x0"] + w["x1"]) / 2
                    if 1 <= int(w["text"]) <= 31 and abs(xc - xs[col_of(xc)]) < 20:
                        nums.append((w, col_of(xc)))
            if not nums:
                continue
            row_tops = sorted({round(w["top"]) for w, _ in nums})
            footer_tops = [w["top"] for w in words
                           if w["text"].lower().strip("*") in FOOTER_WORDS and w["top"] > row_tops[-1]]
            page_bottom = min(footer_tops) - 1 if footer_tops else page.height

            for w, col in nums:
                top = w["top"]
                nxt = [t for t in row_tops if t > round(top) + 5]
                bottom = min(nxt[0] - 1 if nxt else page_bottom, page_bottom)
                cell = [x for x in words
                        if x is not w and top - 1 <= x["top"] < bottom]
                # Keep text whose line is centered over this column.
                mine = []
                for seg in group_lines(cell):
                    xc = (seg[0]["x0"] + seg[-1]["x1"]) / 2
                    if col_of(xc) == col:
                        mine.append(" ".join(s["text"] for s in seg))
                days[int(w["text"])] = mine
    return year, month, days


def lunch_from_lines(lines):
    """Return (status, lunch_items, alt_line). status: OK | NO_SCHOOL | MISSING."""
    if re.search(r"\bno\s+school\b", " ".join(lines), re.I):
        return "NO_SCHOOL", [], None
    idx = next((i for i, l in enumerate(lines) if l.lower().rstrip(":") == "lunch"), None)
    if idx is None:
        return "MISSING", [], None
    items = lines[idx + 1:]
    if not items:
        return "MISSING", [], None
    alt = lines[idx - 1] if idx > 0 and lines[idx - 1].lower().endswith("line") else None
    return "OK", items, alt


def load_month(school, year, month):
    """Find the school's PDF for this month; return {day: lines} or None if not posted."""
    for url, text in pdf_links(school):
        y, m, days = parse_menu_pdf(http_get(url))
        if (y, m) == (year, month):
            return days
        print(f"  (skipping '{text}': it's for {calendar.month_name[m] if m else '?'} {y})")
    return None


def menu_for_date(school, target):
    days = load_month(school, target.year, target.month)
    if days is None or target.day not in days:
        return "MISSING", [], None
    return lunch_from_lines(days[target.day])


def build_message(school, target, status, items, alt):
    """Return (subject, body) for the email."""
    name = school["name"]
    short = f"{target:%a %b} {target.day}"
    heading = f"{school['label']} Lunch Menu for {target:%A}, {target:%B} {target.day}"
    if status == "NO_SCHOOL":
        return f"{name}: no school {short}", f"{heading}\n\nNo school."
    body = f"{heading}\n\n" + "\n".join(f"- {i}" for i in items)
    if alt:
        body += f"\n\nAlso offered: {alt}"
    return f"{name} lunch, {short}", body


# ----------------------------------------------------------------------------
# Email (SMTP)
# ----------------------------------------------------------------------------
def parse_addresses(raw):
    out = []
    for a in re.split(r"[,\s;]+", raw or ""):
        a = a.strip()
        if not a:
            continue
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a):
            out.append(a)
        else:
            print(f"  ! ignoring '{a}' (not a valid email address)")
    return out


def send_email(recipients, subject, body):
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    sender = os.environ.get("EMAIL_FROM") or user

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = sender  # recipients go in the envelope only, so nobody sees the others' addresses
    msg["Subject"] = subject
    msg.set_content(body)

    # Gmail sometimes answers with a temporary "try again later" error (421 / 4xx),
    # especially on new accounts or when logins come in quick succession. Retry those.
    waits = [30, 90, 180]
    for attempt in range(len(waits) + 1):
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.starttls()
            with server:
                server.login(user, password)
                server.send_message(msg, from_addr=sender, to_addrs=recipients)
            return
        except (smtplib.SMTPResponseException, smtplib.SMTPServerDisconnected, TimeoutError, OSError) as e:
            code = getattr(e, "smtp_code", None)
            temporary = code is None or 400 <= code < 500
            if not temporary or attempt == len(waits):
                raise
            print(f"  temporary email error ({e}); retrying in {waits[attempt]}s...")
            time.sleep(waits[attempt])


# ----------------------------------------------------------------------------
# Deciding whether a scheduled run should send
# ----------------------------------------------------------------------------
def scheduled_gate(now, schedule):
    """Decide whether a cron-triggered run should send. Returns (ok, reason).

    GitHub cron is in UTC and has no daylight saving, so the workflow has two entries:
    one for 5pm Central daylight time (22 UTC) and one for 5pm Central standard time (23 UTC).
    Whichever entry matches the current season is the one that sends; the other is ignored.
    This does not depend on what time the run actually starts, because GitHub often starts
    scheduled runs late (sometimes by more than an hour).
    """
    try:
        cron_hour = int(schedule.split()[1])
    except (IndexError, ValueError):
        return False, f"unrecognized schedule '{schedule}'"
    expected = int((SEND_HOUR - now.utcoffset().total_seconds() / 3600) % 24)
    if cron_hour != expected:
        return False, f"cron entry for {cron_hour}:00 UTC is not the one for the current season ({expected}:00 UTC)"
    if now.hour < 12:
        return False, "run slipped past midnight; skipping so the wrong day isn't sent"
    return True, ""


# ----------------------------------------------------------------------------
def dump_month(ym):
    year, month = map(int, ym.split("-"))
    for school in SCHOOLS:
        print(f"\n===== {school['name']} - {calendar.month_name[month]} {year} =====")
        days = load_month(school, year, month)
        if days is None:
            print("  no PDF posted for that month")
            continue
        for d in sorted(days):
            status, items, alt = lunch_from_lines(days[d])
            shown = "NO SCHOOL" if status == "NO_SCHOOL" else (", ".join(items) or "(nothing found)")
            print(f"  {d:>2}: {shown}" + (f"   [{alt}]" if alt else ""))


def export_json(path):
    """Write this month's and next month's lunches for every school to a JSON file."""
    today = dt.datetime.now(TZ).date()
    nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    months = [(today.year, today.month), (nxt.year, nxt.month)]
    out = {"schools": {}}
    for school in SCHOOLS:
        days = {}
        for y, m in months:
            month_days = load_month(school, y, m)
            for d, lines in (month_days or {}).items():
                status, items, alt = lunch_from_lines(lines)
                days[f"{y:04d}-{m:02d}-{d:02d}"] = {"status": status, "items": items, "alt": alt}
        out["schools"][school["key"]] = {
            "name": school["name"],
            "label": school["label"],
            "days": dict(sorted(days.items())),
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, sort_keys=False)
        f.write("\n")
    print(f"wrote {path}: " + ", ".join(f"{k}={len(v['days'])} days" for k, v in out["schools"].items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="skip the 5pm Central check")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--date", help="treat this YYYY-MM-DD as 'tomorrow'")
    ap.add_argument("--dump", metavar="YYYY-MM", help="print every parsed lunch for a month and exit")
    ap.add_argument("--export-json", metavar="FILE", help="write this and next month's lunches to a JSON file and exit")
    args = ap.parse_args()

    if args.export_json:
        export_json(args.export_json)
        return 0

    if args.dump:
        dump_month(args.dump)
        return 0

    now = dt.datetime.now(TZ)
    schedule = os.environ.get("CRON_SCHEDULE", "").strip()
    if not (args.force or args.dry_run):
        if schedule:  # triggered by GitHub's cron
            ok, why = scheduled_gate(now, schedule)
            if not ok:
                print(f"{now:%H:%M} Central: nothing to do ({why}).")
                return 0
            print(f"{now:%H:%M} Central: scheduled run accepted (cron '{schedule}').")
        elif now.hour != SEND_HOUR:  # running by hand without --force
            print(f"{now:%H:%M} Central is not the {SEND_HOUR}:00 hour; nothing to do.")
            return 0

    target = dt.date.fromisoformat(args.date) if args.date else now.date() + dt.timedelta(days=1)
    if target.weekday() >= 5:
        print(f"{target} is a weekend; no message.")
        return 0

    failures = 0
    for school in SCHOOLS:
        name = school["name"]
        recipients = parse_addresses(os.environ.get(school["recipients"]) or os.environ.get("EMAIL_RECIPIENTS"))
        if not recipients and not args.dry_run:
            print(f"[{name}] no recipients set ({school['recipients']} or EMAIL_RECIPIENTS); skipping.")
            failures += 1
            continue
        try:
            status, items, alt = menu_for_date(school, target)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] ERROR reading menu: {e}")
            failures += 1
            continue
        if status == "MISSING":
            # Fail loudly so GitHub emails you that the school hasn't posted the menu.
            print(f"[{name}] no menu found for {target}; nothing sent.")
            failures += 1
            continue

        subject, body = build_message(school, target, status, items, alt)
        print(f"[{name}] subject: {subject}\n{body}\n")
        if args.dry_run:
            continue
        try:
            send_email(recipients, subject, body)
            print(f"  sent to {len(recipients)} recipient(s)")
            time.sleep(5)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED to send: {e}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
