#!/usr/bin/env python3
"""
monitor.py -- builds the data for the public DPU Monitor website.

Runs on GitHub's servers every few hours (see .github/workflows/update.yml):
  * First runs ("seeding"): reads every docket in the DPU File Room, a few
    thousand per run, until the whole File Room is covered.
  * After that, once a day: rechecks active dockets, your watchlist, and
    new dockets, plus 1/30 of everything else (so every docket is rechecked
    monthly), then rebuilds the activity feed and news headlines.

Everything it writes is plain JSON in the output folder:
  data/dockets.json   one row per docket (for search and browsing)
  data/d/<id>.json    every filing and file link for one docket
  data/feed.json      DPU actions, appeals, and new dockets from recent weeks
  data/watch.json     your watchlist groups, with each docket's latest filings
  data/news.json      news headlines (titles and links only)
  data/meta.json      when it last ran, and progress
"""

import argparse
import datetime as dt
import json
import math
import re
import shutil
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from fnmatch import fnmatch
from pathlib import Path

import requests

import dpu   # the same reading code as the laptop mirror

HERE = Path(__file__).resolve().parent
ET_TZ = dt.timezone(dt.timedelta(hours=-5))      # Eastern (standard time is close enough for dates)
FILEROOM_BY_NUMBER = "https://eeaonline.eea.state.ma.us/dpu/fileroom/#/dockets/bynumber/{n}"

TIME_BUDGET = 5.2 * 3600        # stop well before GitHub's 6-hour job limit
ACTIVE_DAYS = 120               # dockets with a filing this recent are rechecked daily
FEED_DAYS = 45                  # how far back the activity feed goes
SWEEP_DAYS = 30                 # every other docket is rechecked once in this many days
PROBE_MISSES = 60               # empty IDs to try past the newest docket
SEED_START = 13700              # highest docket ID seen was about 13,150 (Nov. 2026); new ones are found by the daily probe
FIRST_RUN_MINUTES = 40          # the first run stops early so the site appears within the hour

T0 = time.time()


def today():
    return dt.datetime.now(ET_TZ).date()


def out_of_time():
    return time.time() - T0 > TIME_BUDGET


# ----------------------------------------------------------------------------
# Reading one docket
# ----------------------------------------------------------------------------

DPU_FILER = re.compile(r"^(DPU|D\.P\.U\.|Department of Public Utilities)\b", re.I)


def person(v):
    """Name of a filer, which may be an organization or a person."""
    if isinstance(v, dict):
        name = dpu.clean(v.get("Name") or "")
        if not name:
            name = " ".join(dpu.clean(v.get(k) or "") for k in ("FirstName", "LastName")).strip()
        if not name:
            name = dpu.clean(v.get("Company") or "")
        return name
    return dpu.label(v)


def parse_docket(data):
    rec = dpu.find_docket_record(data)
    g = lambda *k: dpu.iget(rec, *k)
    filings = []
    for f in (g("Filings") or []):
        if not isinstance(f, dict):
            continue
        ftype = dpu.label(dpu.iget(f, "FilingType", "Type", "DocumentType")) or ""
        atts = []
        for a in (dpu.iget(f, "Attachments") or []):
            if not isinstance(a, dict):
                continue
            fsid = dpu.iget(a, "FileServiceId")
            url = dpu.doc_url_from("FileServiceId", fsid, a) if fsid else None
            if not url:   # older formats
                for k, v in a.items():
                    url = dpu.doc_url_from(k, v, a)
                    if url:
                        break
            if url:
                atts.append([dpu.clean(dpu.iget(a, "Name") or "file"), url])
        filings.append({
            "date": dpu.label(dpu.iget(f, "FiledDate", "DateFiled", "filedAt"))[:10],
            "filer": person(dpu.iget(f, "Filer", "FiledBy")),
            "type": ftype,
            "title": dpu.label(dpu.iget(f, "Description", "Title")),
            "files": atts,
        })
    filings.sort(key=lambda x: x["date"], reverse=True)
    related = []
    for r in (g("RelatedDockets") or []):
        n = dpu.label(dpu.iget(r, "Number", "DocketNumber")) if isinstance(r, dict) else dpu.label(r)
        if n:
            related.append(n)
    ho = g("HearingOfficer")
    return {
        "number": dpu.label(g("Number", "DocketNumber")),
        "title": dpu.label(g("Description", "Title")),
        "petitioner": person(g("Petitioner", "Petitioners")),
        "industry": dpu.label(g("Industry")),
        "type": dpu.label(g("Type", "DocketType")),
        "division": dpu.label(g("Division")),
        "opened": dpu.label(g("OpenedDate"))[:10],
        "closed": dpu.label(g("ClosedDate"))[:10],
        "hearing_officer": person(ho) if ho else "",
        "related": related,
        "filings": filings,
    }


# ----------------------------------------------------------------------------
# Making sense of filings: categories, milestones, stage
# ----------------------------------------------------------------------------

def is_dpu(f):
    return bool(DPU_FILER.search(f["filer"] or ""))


def category(f):
    """What kind of event a filing is, for the feed and timeline. None = routine."""
    t, title = f["type"].lower(), f["title"].lower()
    if not is_dpu(f):
        if re.search(r"\bappeal|supreme judicial court|\bsjc\b|petition for review|reconsideration", title + " " + t):
            return "appeal"
        return None
    if t in ("order",) or title.startswith("letter order") or (t == "letter" and "order" in title):
        return "order"
    if t in ("notice", "order of notice") or title.startswith("notice") or "order of notice" in title:
        return "notice"
    if t in ("hearing officer memorandum", "hearing officer ruling", "memorandum"):
        return "memo"
    if t in ("stamp grant", "stamp approval"):
        return "approval"
    if t == "hearing officer assignment":
        return "assignment"
    return "dpu"


FINAL_ORDER_EXCLUDE = re.compile(r"interlocutory|instituting|opening|protective|procedural|ground rules|of notice|"
                                 r"reconsideration|clarification|intervention|confidential|suspension|suspending|"
                                 r"motion|extension|appeal|hearing officer|schedule", re.I)


def milestones(p):
    """Dated steps in the case, oldest first."""
    ms, seen = [], set()

    def add(date, label, detail="", once=None):
        if once and once in seen:
            return
        if once:
            seen.add(once)
        ms.append({"date": date, "label": label, "detail": detail})

    if p["opened"]:
        add(p["opened"], "Docket opened", "")
    for f in sorted(p["filings"], key=lambda x: x["date"]):
        t, title, c = f["type"].lower(), f["title"], category(f)
        low = title.lower()
        if c in ("approval", "notice") and ROUTINE.search(low):
            continue
        if t == "initial filing":
            add(f["date"], "Initial filing", f["filer"], once="initial")
        elif c == "notice" and "hearing" in low:
            add(f["date"], "Notice of public hearing", title)
        elif c == "notice":
            add(f["date"], "Notice", title)
        elif c == "assignment":
            add(f["date"], "Hearing officer assigned", title, once="assign")
        elif c == "memo" and "procedural schedule" in low:
            add(f["date"], "Procedural schedule", title)
        elif c == "memo" and re.search(r"technical (conference|session)", low):
            add(f["date"], "Technical conference", title)
        elif c == "memo":
            add(f["date"], "Hearing officer memo", title)
        elif t == "transcript":
            add(f["date"], "Hearing transcript", title, once=f"tr{f['date']}")
        elif t == "brief":
            kind = "Reply briefs" if "reply" in low else "Initial briefs" if "initial" in low else "Briefs"
            add(f["date"], kind, "", once=f"{kind}{f['date']}")
        elif c == "order":
            add(f["date"], "Order", title, once=f"o{f['date']}{title[:60]}")
        elif c == "approval":
            add(f["date"], "Approval (stamp)", title, once=f"s{f['date']}")
        elif c == "appeal":
            add(f["date"], "Reconsideration or appeal", f"{f['filer']}: {title}", once=f"a{f['date']}{f['filer']}")
        elif t == "compliance filing":
            add(f["date"], "Compliance filing", f["filer"], once=f"c{f['date']}")
    if p["closed"]:
        add(p["closed"], "Docket closed", "")
    # Collapse runs of the same step (several hearing days, briefs filed by many parties) into one entry.
    merged, last = [], {}
    for m in ms:
        prev = last.get(m["label"])
        if (prev and m["label"] in RANGED
                and (dt.date.fromisoformat(m["date"]) - dt.date.fromisoformat(prev["end"])).days <= 21):
            prev["end"] = m["date"]
            continue
        merged.append(dict(m, end=m["date"]))
        last[m["label"]] = merged[-1]
    for m in merged:
        if m["label"] in RANGED:
            if m["label"] == "Hearing transcript":
                m["label"] = "Evidentiary hearings"
            m["detail"] = f"through {m['end']}" if m["end"] != m["date"] else ""
        m.pop("end")
    return merged


ROUTINE = re.compile(r"intervene|intervention|pro hac vice|appearance|retention of experts|extension of time|"
                     r"limited participant|protective treatment|confidential", re.I)
RANGED = {"Hearing transcript", "Initial briefs", "Reply briefs", "Briefs", "Compliance filing"}


STAGES = ["Opened", "Discovery", "Hearings", "Briefs", "Order", "Closed"]


def stage(p):
    s = 0
    briefs = [f["date"] for f in p["filings"] if f["type"].lower() == "brief"]
    last_brief = max(briefs) if briefs else ""
    discovery = any(f["type"].lower() in ("information requests", "information request responses")
                    for f in p["filings"])
    for f in p["filings"]:
        t = f["type"].lower()
        if t in ("information requests", "information request responses", "record request responses"):
            s = max(s, 1)
        elif t == "transcript":
            s = max(s, 2)
        elif t == "brief":
            s = max(s, 3)
        # A stamp approval decides a simple filing; in a contested case it is usually procedural.
        elif ((category(f) == "order" or (category(f) == "approval" and not discovery))
              and not FINAL_ORDER_EXCLUDE.search(f["title"])
              and f["date"] >= last_brief):          # in a litigated case, the decision comes after the briefs
            s = max(s, 4)
    if p["closed"]:
        s = 5
    return STAGES[s]


def last_dpu_action(p):
    for f in p["filings"]:                      # newest first
        c = category(f)
        if c and c != "appeal":
            return [f["date"], f["type"] or "DPU action", f["title"][:200]]
    return None


def summary_row(did, p):
    return [did, p["number"], p["title"][:220], p["petitioner"][:90], p["industry"], p["type"],
            p["opened"], p["closed"], p["filings"][0]["date"] if p["filings"] else "",
            len(p["filings"]), stage(p), last_dpu_action(p)]


COLS = ["id", "number", "title", "petitioner", "industry", "type", "opened", "closed",
        "last", "filings", "stage", "last_dpu"]


def recent_events(did, p, since):
    ev = []
    for f in p["filings"]:
        if f["date"] < since:
            break
        c = category(f)
        if c and c not in ("assignment", "dpu"):      # routine DPU items (e.g. its information requests) stay on the docket page
            ev.append({"date": f["date"], "cat": c, "id": did, "number": p["number"], "type": f["type"],
                       "filer": f["filer"], "title": f["title"][:300],
                       "file": f["files"][0] if f["files"] else None})
    return ev


# ----------------------------------------------------------------------------
# Storage
# ----------------------------------------------------------------------------

class Store:
    def __init__(self, out):
        self.out = out
        self.data = out / "data"
        self.det = self.data / "d"
        self.det.mkdir(parents=True, exist_ok=True)
        self.meta = self.load("meta.json", {"seed_next": 0, "seed_done": False, "max_id": 0, "misses": [],
                                            "sweep_next": 0, "last_daily": "", "runs": 0})
        rows = self.load("dockets.json", {"cols": COLS, "rows": []})["rows"]
        self.rows = {r[0]: r for r in rows}
        self.events = {int(k): v for k, v in self.load("events.json", {}).items()}
        self.misses = set(self.meta.get("misses", []))

    def load(self, name, default):
        p = self.data / name
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default

    def save(self, name, obj):
        (self.data / name).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def put(self, did, p):
        since = (today() - dt.timedelta(days=FEED_DAYS)).isoformat()
        self.rows[did] = summary_row(did, p)
        ev = recent_events(did, p, since)
        if ev:
            self.events[did] = ev
        else:
            self.events.pop(did, None)
        self.misses.discard(did)
        detail = {
            "id": did, "number": p["number"], "title": p["title"], "petitioner": p["petitioner"],
            "industry": p["industry"], "type": p["type"], "division": p["division"],
            "opened": p["opened"], "closed": p["closed"], "hearing_officer": p["hearing_officer"],
            "related": p["related"], "stage": stage(p), "milestones": milestones(p),
            "fileroom": FILEROOM_BY_NUMBER.format(n=urllib.parse.quote(p["number"], safe="")),
            "filings": [[f["date"], f["filer"], f["type"], f["title"], f["files"], category(f)] for f in p["filings"]],
        }
        (self.det / f"{did}.json").write_text(json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                                               encoding="utf-8")

    def flush(self):
        self.meta["misses"] = sorted(self.misses)
        self.meta["max_id"] = max(self.rows) if self.rows else 0
        self.save("dockets.json", {"cols": COLS, "rows": sorted(self.rows.values(), key=lambda r: r[0])})
        self.save("events.json", {str(k): v for k, v in self.events.items()})
        self.save("meta.json", self.meta)


def fetch(session, did):
    try:
        return dpu.fetch_docket_json(session, did)
    except RuntimeError:
        sys.exit("The DPU server sent back its web page instead of data. It may be refusing requests from "
                 "GitHub's servers. See README, 'If the DPU blocks GitHub'.")


def refresh(store, session, did):
    data = fetch(session, did)
    if data is None:
        store.misses.add(did)
        return False
    store.put(did, parse_docket(data))
    return True


# ----------------------------------------------------------------------------
# Watchlist, dates, news
# ----------------------------------------------------------------------------

def read_watchlist():
    groups, cur = [], None
    p = HERE / "watchlist.txt"
    if not p.exists():
        return groups
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            cur = {"name": m.group(1).strip(), "patterns": [], "labels": {}}
            groups.append(cur)
        else:
            if cur is None:
                cur = {"name": "Watchlist", "patterns": [], "labels": {}}
                groups.append(cur)
            num, _, short = line.partition("|")
            pat = num.upper().replace("D.P.U.", "").replace("DPU", "").strip()
            cur["patterns"].append(pat)
            if short.strip():
                cur.setdefault("labels", {})[pat] = short.strip()
    return groups


def match_ids(store, patterns):
    ids = []
    for did, r in store.rows.items():
        n = (r[1] or "").upper()
        if any(fnmatch(n, pat) for pat in patterns):
            ids.append(did)
    return sorted(ids, key=lambda i: store.rows[i][1])


def read_dates():
    out = []
    p = HERE / "dates.txt"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        parts = [x.strip() for x in line.split("|")]
        if len(parts) >= 3 and re.match(r"\d{4}-\d{2}-\d{2}$", parts[1]):
            out.append({"number": parts[0].upper(), "date": parts[1], "what": " | ".join(parts[2:])})
    return sorted(out, key=lambda d: d["date"])


def fetch_news():
    p = HERE / "news.txt"
    if not p.exists():
        return []
    items, seen = [], set()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    for url in p.read_text(encoding="utf-8").splitlines():
        url = url.split("#", 1)[0].strip()
        if not url.startswith("http"):
            continue
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": dpu.DEFAULT_HEADERS["User-Agent"]})
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"  news feed failed ({e.__class__.__name__}): {url[:80]}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            src_el = it.find("source")
            source = (src_el.text or "").strip() if src_el is not None else ""
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            try:
                when = dt.datetime.strptime((it.findtext("pubDate") or "")[:25].strip(), "%a, %d %b %Y %H:%M:%S")
                when = when.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            key = re.sub(r"\W+", "", title.lower())[:80]
            if when < cutoff or not title or key in seen:
                continue
            seen.add(key)
            items.append({"date": when.date().isoformat(), "title": title, "source": source, "link": link})
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:60]


# ----------------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------------

def seed(store, session):
    """Walk docket IDs from newest to oldest, continuing where the last run stopped,
    so recent dockets appear on the site first."""
    i = store.meta.get("seed_next") or SEED_START
    if i == 1 and not store.rows:          # older saved state started at 1; begin at the top instead
        i = SEED_START
    print(f"Seeding downward from ID {i}...")
    n = 0
    while i >= 1 and not out_of_time():
        refresh(store, session, i)
        n += 1
        i -= 1
        store.meta["seed_next"] = i
        if n % 200 == 0:
            store.flush()
            print(f"  down to ID {i + 1}: {len(store.rows)} dockets so far")
    if i < 1:
        store.meta["seed_done"] = True
        print(f"Seeding finished: {len(store.rows)} dockets.")
    store.flush()


def daily(store, session, watch_ids):
    t = today()
    active_since = (t - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
    open_since = (t - dt.timedelta(days=3 * 365)).isoformat()
    active = {did for did, r in store.rows.items()
              if (r[8] or "") >= active_since or (not r[7] and (r[6] or "") >= open_since)}
    active |= set(watch_ids)
    rest = sorted(set(store.rows) - active)
    chunk = math.ceil(len(rest) / SWEEP_DAYS) if rest else 0
    start = store.meta.get("sweep_next", 0) % max(1, len(rest)) if rest else 0
    sweep = rest[start:start + chunk]
    store.meta["sweep_next"] = start + chunk if start + chunk < len(rest) else 0
    print(f"Daily run: {len(active)} active/watched dockets, {len(sweep)} in today's sweep, then new dockets.")

    # 1. New dockets past the highest known ID.
    misses, i = 0, max(store.rows) if store.rows else 0
    while misses < PROBE_MISSES and not out_of_time():
        i += 1
        if refresh(store, session, i):
            misses = 0
            print(f"  new docket: {store.rows[i][1]}")
        else:
            misses += 1
    # 2. Active and watched dockets, newest activity first. 3. The sweep.
    order = sorted(active, key=lambda d: store.rows.get(d, [0] * 9)[8] or "", reverse=True) + sweep
    for n, did in enumerate(order, 1):
        if out_of_time():
            print("Out of time; the rest will be checked on the next run.")
            break
        refresh(store, session, did)
        if n % 250 == 0:
            store.flush()
            print(f"  {n} of {len(order)} checked")
    store.meta["last_daily"] = t.isoformat()
    store.flush()


def build_outputs(store, groups):
    t = today()
    since = (t - dt.timedelta(days=FEED_DAYS)).isoformat()
    feed = [e for evs in store.events.values() for e in evs if e["date"] >= since]
    # New dockets: opened within the feed window.
    for did, r in store.rows.items():
        if (r[6] or "") >= since:
            feed.append({"date": r[6], "cat": "new", "id": did, "number": r[1], "type": r[5],
                         "filer": r[3], "title": r[2], "file": None})
    feed.sort(key=lambda e: (e["date"], e["number"]), reverse=True)
    store.save("feed.json", feed)

    dates = read_dates()
    watch = []
    for g in groups:
        cards = []
        for did in match_ids(store, g["patterns"]):
            r = store.rows[did]
            try:
                d = json.loads((store.det / f"{did}.json").read_text(encoding="utf-8"))
                recent = [[f[0], f[2], f[1], f[3][:140]] for f in d["filings"][:5]]
            except Exception:
                recent = []
            short = next((v for pat, v in g.get("labels", {}).items() if fnmatch((r[1] or "").upper(), pat)), "")
            cards.append({"row": r, "recent": recent, "short": short,
                          "next": [x for x in dates if x["number"] == (r[1] or "").upper() and x["date"] >= t.isoformat()][:2]})
        watch.append({"name": g["name"], "patterns": g["patterns"], "dockets": cards})
    store.save("watch.json", watch)
    store.save("dates.json", dates)
    store.meta["updated"] = dt.datetime.now(ET_TZ).isoformat(timespec="minutes")
    store.meta["dockets"] = len(store.rows)
    store.save("meta.json", dict(store.meta, misses=sorted(store.misses)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--force-daily", action="store_true")
    ap.add_argument("--budget-minutes", type=float)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--seed-start", type=int, help=argparse.SUPPRESS)   # for testing
    args = ap.parse_args()
    global TIME_BUDGET, SEED_START
    if args.seed_start is not None:
        SEED_START = args.seed_start
    if args.budget_minutes:
        TIME_BUDGET = args.budget_minutes * 60
    dpu.DELAY = args.delay

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    store = Store(out)
    store.meta["runs"] = store.meta.get("runs", 0) + 1
    if store.meta["runs"] == 1 and not args.budget_minutes:
        TIME_BUDGET = FIRST_RUN_MINUTES * 60
    session = dpu.make_session()
    groups = read_watchlist()

    if not store.meta.get("seed_done"):
        seed(store, session)
    elif args.force_daily or store.meta.get("last_daily") != today().isoformat():
        watch_ids = [i for g in groups for i in match_ids(store, g["patterns"])]
        daily(store, session, watch_ids)
    else:
        print("Already updated today; refreshing the watchlist and news only.")
        for i in [i for g in groups for i in match_ids(store, g["patterns"])]:
            if out_of_time():
                break
            refresh(store, session, i)
        store.flush()

    print("Fetching news headlines...")
    store.save("news.json", fetch_news())
    build_outputs(store, groups)

    # The website itself
    shutil.copy(HERE / "site" / "index.html", out / "index.html")
    (out / ".nojekyll").write_text("")
    print(f"Done: {len(store.rows)} dockets, {sum(len(v) for v in store.events.values())} recent events.")


if __name__ == "__main__":
    main()
