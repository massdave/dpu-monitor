#!/usr/bin/env python3
"""
dpu.py -- a local mirror of the Massachusetts DPU File Room, with search.

Everything is stored as ordinary files plus one SQLite database:
  data/raw/<id>.json.gz      the exact API response for each docket (never altered)
  data/files/<docket>/...    downloaded PDFs and other filings
  data/text/<docket>/...     extracted text, one .txt per filing, with page markers
  data/dpu.sqlite            index of dockets, documents, and searchable page text
  digests/<date>.md          what changed in your tracked dockets
  tracked.txt                the docket numbers you follow (one per line)

Run "python dpu.py help" for the list of commands.
"""

import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing the 'requests' package. Run:  pip install -r requirements.txt")

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

HOME = Path(__file__).resolve().parent
DATA = HOME / "data"
RAW = DATA / "raw"
FILES = DATA / "files"
TEXT = DATA / "text"
DIGESTS = HOME / "digests"
EXPORTS = HOME / "exports"
DB_PATH = DATA / "dpu.sqlite"
TRACKED = HOME / "tracked.txt"
CURL_FILE = HOME / "request_headers.txt"   # optional: paste "Copy as cURL" here
SETTINGS = HOME / "settings.json"          # written by the dashboard's Settings page


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d):
    SETTINGS.write_text(json.dumps(d, indent=2), encoding="utf-8")

API = "https://eeaonline.eea.state.ma.us/dpu/fileroom/api/dockets/get?id={id}"
APP_PAGE = "https://eeaonline.eea.state.ma.us/dpu/fileroom/"
CDN = "https://d3i1pf1zzeh4ol.cloudfront.net/"
SAMPLE_ID = 12806          # DPU 26-50, known to exist

DELAY = 1.0                # seconds between requests to the DPU (be polite)

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EXT_RE = re.compile(r"\.(pdf|xml|docx?|xlsx?|xlsm|csv|txt|htm|html|zip|pptx?|rtf|msg|jpg|png)$", re.I)

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": APP_PAGE,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
}


def headers_from_curl(text):
    """Read headers and cookies out of a Chrome 'Copy as cURL' (bash or cmd)."""
    if '^"' in text:                      # Windows "cmd" flavor uses ^ escapes
        text = re.sub(r"\^(.)", r"\1", text)
    headers = {}
    for m in re.finditer(r"""(?:-H|--header)\s+(['"])(.*?)\1""", text, re.S):
        line = m.group(2)
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
    m = re.search(r"""(?:-b|--cookie)\s+(['"])(.*?)\1""", text, re.S)
    if m:
        headers["Cookie"] = m.group(2).strip()
    # Never send these; requests computes them itself.
    for k in list(headers):
        if k.lower() in ("content-length", "host", "accept-encoding"):
            headers.pop(k)
    return headers


def make_session():
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    if CURL_FILE.exists():
        extra = headers_from_curl(CURL_FILE.read_text(encoding="utf-8", errors="ignore"))
        if extra:
            s.headers.update(extra)
            print(f"(Using {len(extra)} headers from {CURL_FILE.name})")
    return s


_last_request = [0.0]


def polite_get(session, url, **kw):
    wait = DELAY - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(4):
        try:
            r = session.get(url, timeout=120, **kw)
            _last_request[0] = time.time()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(10 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            _last_request[0] = time.time()
            if attempt == 3:
                raise
            print(f"  network error ({e.__class__.__name__}); retrying...")
            time.sleep(10 * (attempt + 1))
    return r


def fetch_docket_json(session, docket_id):
    """Return parsed JSON for one docket, or None if the ID has no docket.
    Raises RuntimeError if the server sent back the web page instead of data."""
    r = polite_get(session, API.format(id=docket_id))
    body = r.text.strip()
    if r.status_code == 404 or not body or body in ("null", "{}", "[]"):
        return None
    if body.startswith("<"):
        raise RuntimeError("HTML_NOT_JSON")
    if r.status_code != 200:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError("HTML_NOT_JSON")
    if not data:
        return None
    return data


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS dockets (
    id INTEGER PRIMARY KEY,        -- the file room's internal ID (e.g. 12806)
    number TEXT,                   -- the public docket number (e.g. 26-50)
    description TEXT,
    opened TEXT, closed TEXT,
    type TEXT, division TEXT, industry TEXT, petitioner TEXT,
    n_documents INTEGER,
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS dockets_number ON dockets(number);
CREATE TABLE IF NOT EXISTS misses (id INTEGER PRIMARY KEY, checked_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
    doc_key TEXT PRIMARY KEY,      -- stable ID (attachment ID, or link without its token)
    url TEXT,                      -- the most recent download link
    docket_id INTEGER, docket_number TEXT,
    filed_date TEXT, filer TEXT, title TEXT, doc_type TEXT, file_name TEXT,
    first_seen TEXT,
    context TEXT                   -- the JSON record the link was found in
);
CREATE INDEX IF NOT EXISTS documents_docket ON documents(docket_number);
CREATE TABLE IF NOT EXISTS downloads (
    doc_key TEXT PRIMARY KEY, file_path TEXT, text_path TEXT,
    pages INTEGER, empty_pages INTEGER, done_at TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    docket_number TEXT PRIMARY KEY,
    notes TEXT,                    -- your own notes on the docket
    links TEXT,                    -- related pages, one per line
    updated_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    body, doc_key UNINDEXED, url UNINDEXED, page UNINDEXED, docket_number UNINDEXED,
    title UNINDEXED, filed_date UNINDEXED, filer UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------------
# Reading the docket JSON
#
# We know the docket record has fields like Number, Description, OpenedDate,
# Type, Division, Industry, Petitioner. We do not yet know the exact names of
# the fields inside each filing, so the parser looks for document links
# wherever they are and reads nearby fields by pattern. The raw JSON is
# always kept, so if the parser needs a fix, "python dpu.py rebuild" re-reads
# everything without contacting the DPU again.
# ----------------------------------------------------------------------------


def clean(s):
    """Plain text from a value that may contain HTML, e.g. '<p>Petition of ...&nbsp;</p>'."""
    s = str(s)
    if "<" in s or "&" in s:
        s = re.sub(r"<[^>]+>", " ", s)
        s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def label(v):
    """Turn a value (possibly an object like {"Name": ..., "Id": ...}) into text."""
    if v is None:
        return ""
    if isinstance(v, str):
        return clean(v)
    if isinstance(v, dict):
        for k in ("Name", "name", "Description", "description", "Title", "title",
                  "Value", "value", "Text", "text", "DisplayName", "FullName"):
            if isinstance(v.get(k), (str, int, float)) and str(v.get(k)).strip():
                return clean(v[k])
        parts = [str(x) for x in v.values() if isinstance(x, str) and x.strip()]
        return clean(parts[0]) if parts else ""
    if isinstance(v, list):
        return "; ".join(filter(None, (label(x) for x in v)))
    return clean(v)


def iget(d, *names):
    """Case-insensitive dictionary lookup over several candidate names."""
    low = {k.lower(): k for k in d}
    for n in names:
        if n.lower() in low:
            return d[low[n.lower()]]
    return None


def find_docket_record(data):
    """Find the dict that carries the docket's 'Number' field."""
    stack = [data]
    while stack:
        x = stack.pop(0)
        if isinstance(x, dict):
            if iget(x, "Number", "DocketNumber") is not None and iget(x, "Description", "Title") is not None:
                return x
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return data if isinstance(data, dict) else {}


FILESERVICE = "https://fileservice.eea.comacloud.net/V3.1.0/FileService.Api/file/"
FILE_HOSTS = ("fileservice.eea.comacloud.net", "cloudfront.net")
NOT_DOCS = ("eeaonline.eea.state.ma.us/dpu/fileroom/#", "mass.gov")


def doc_url_from(key, value, node):
    """Decide whether a string value is a link to a document file; return the URL.

    The DPU serves documents from its File Service, with links like
      https://fileservice.eea.comacloud.net/V3.1.0/FileService.Api/file/affaafiaj?GTOy...
    (seen in the TSK export, November 2026). Older links used CloudFront with UUID
    file names. Both are recognized, plus any web link under a url/link/file key."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    # The DPU API's own format (confirmed Nov. 2026): each attachment carries
    # FileServiceId and PublicViewSecretKey. The download link is
    #   FILESERVICE + FileServiceId + "?" + the key, URL-encoded.
    if key.lower() == "fileserviceid":
        if not v or node.get("FileMissing") is True:
            return None
        secret = node.get("PublicViewSecretKey") or ""
        return FILESERVICE + v + ("?" + urllib.parse.quote(secret, safe="") if secret else "")
    if v.startswith("//"):
        v = "https:" + v
    if v.startswith("http"):
        if any(n in v for n in NOT_DOCS):
            return None
        if any(h in v for h in FILE_HOSTS):
            return v
        k = key.lower()
        if any(w in k for w in ("url", "link", "file", "download", "href", "path")) and \
                ("/file" in v.lower() or EXT_RE.search(v.split("?")[0])):
            return v
        return None
    # A bare CloudFront-style file name, e.g. "69466636-....pdf"
    if UUID_RE.search(v) and EXT_RE.search(v) and len(v) < 200 and "/" not in v:
        return CDN + v
    return None


def doc_key_for(url, node, n_links_in_node):
    """A stable identity for a document, so a changed access token in its link
    does not make an old document look new.
    Preference: the attachment's own ID; else the link without its query string."""
    if n_links_in_node == 1:
        low = {k.lower(): k for k in node}
        for want in ("dpuattachmentid", "attachmentid", "fileid", "documentid", "id"):
            if want in low and isinstance(node[low[want]], (int, str)) and str(node[low[want]]).strip():
                return f"att:{node[low[want]]}"
    return url.split("?")[0]


def file_name_for(url, node, key):
    """The document's own file name (e.g. 'Initial Filing.pdf'), if the record gives one."""
    low = {k.lower(): k for k in node}
    for want in ("name", "filename", "originalfilename", "documentname", "displayname", "title"):
        if want in low and low[want] != key and isinstance(node[low[want]], str) and node[low[want]].strip():
            return node[low[want]].strip()
    return url.split("?")[0].split("/")[-1]


KEYS_DATE = ("filedat", "fileddate", "datefiled", "filingdate", "receiveddate", "datereceived",
             "submitteddate", "submitdate", "posteddate", "documentdate", "date", "createdat", "createddate")
KEYS_FILER = ("filer", "filedby", "filername", "submitter", "submittedby", "party",
              "partyname", "author", "company", "organization", "filingparty", "from")
KEYS_TITLE = ("description", "title", "documenttitle", "subject", "documentname",
              "displayname", "name", "filename", "originalfilename")
KEYS_TYPE = ("documenttype", "filingtype", "type", "category", "doctype", "subtype")


def pick(dicts, keys, avoid=()):
    # Exact key names first, in order of preference, checking the document's own
    # record and then its parents (so a filing's Description beats a file name).
    for want in keys:
        for d in dicts:
            low = {k.lower(): k for k in d}
            if want in low and low[want] not in avoid:
                val = label(d[low[want]])
                if val and not UUID_RE.fullmatch(val):
                    return val
    for d in dicts:
        low = {k.lower(): k for k in d}
        # looser match: key contains the word
        for want in keys:
            for lk, k in low.items():
                if want in lk and k not in avoid and not isinstance(d[k], (list,)):
                    val = label(d[k])
                    if val and not UUID_RE.fullmatch(val):
                        return val
    return ""


def extract_documents(data):
    """Walk the JSON and return a list of document records found in it."""
    found = {}

    def walk(x, ancestors):
        if isinstance(x, dict):
            links = [(k, doc_url_from(k, v, x)) for k, v in x.items()]
            links = [(k, u) for k, u in links if u]
            for k, url in links:
                key = doc_key_for(url, x, len(links))
                if key in found:
                    continue
                # Look in the record holding the link and its immediate parent (the
                # filing, when the link sits in an attachment). Not higher: the
                # docket's own description and dates must not label a document.
                chain = [x] + ancestors[::-1][:1]
                date = pick(chain, KEYS_DATE)
                found[key] = {
                    "doc_key": key,
                    "url": url,
                    "filed_date": date[:10] if re.match(r"\d{4}-\d{2}-\d{2}", date) else date,
                    "filer": pick(chain, KEYS_FILER),
                    "title": pick(chain, KEYS_TITLE, avoid=(k,)) or file_name_for(url, x, k),
                    "doc_type": pick(chain, KEYS_TYPE),
                    "file_name": file_name_for(url, x, k),
                    "context": json.dumps(x, ensure_ascii=False)[:4000],
                }
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v, ancestors + [x])
        elif isinstance(x, list):
            for v in x:
                walk(v, ancestors)

    walk(data, [])
    return list(found.values())


def store_docket(con, docket_id, data, mark_new=True):
    """Save one docket's parsed contents. Returns the list of documents not seen before."""
    rec = find_docket_record(data)
    number = label(iget(rec, "Number", "DocketNumber"))
    docs = extract_documents(data)
    con.execute(
        """INSERT OR REPLACE INTO dockets
           (id, number, description, opened, closed, type, division, industry, petitioner,
            n_documents, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (docket_id, number,
         label(iget(rec, "Description", "Title")),
         label(iget(rec, "OpenedDate", "DateOpened", "Opened"))[:10],
         label(iget(rec, "ClosedDate", "DateClosed", "Closed"))[:10],
         label(iget(rec, "Type", "DocketType")),
         label(iget(rec, "Division")),
         label(iget(rec, "Industry")),
         label(iget(rec, "Petitioner", "Petitioners", "Company")),
         len(docs), now()))
    con.execute("DELETE FROM misses WHERE id=?", (docket_id,))
    new = []
    for d in docs:
        exists = con.execute("SELECT 1 FROM documents WHERE doc_key=?", (d["doc_key"],)).fetchone()
        if exists:
            con.execute("""UPDATE documents SET url=?, docket_id=?, docket_number=?, filed_date=?, filer=?,
                           title=?, doc_type=?, file_name=?, context=? WHERE doc_key=?""",
                        (d["url"], docket_id, number, d["filed_date"], d["filer"], d["title"], d["doc_type"],
                         d["file_name"], d["context"], d["doc_key"]))
        else:
            con.execute("""INSERT INTO documents (doc_key, url, docket_id, docket_number, filed_date, filer,
                           title, doc_type, file_name, first_seen, context) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (d["doc_key"], d["url"], docket_id, number, d["filed_date"], d["filer"], d["title"],
                         d["doc_type"], d["file_name"], now() if mark_new else "", d["context"]))
            new.append(dict(d, docket_number=number))
    con.commit()
    return number, new


def save_raw(docket_id, data):
    RAW.mkdir(parents=True, exist_ok=True)
    with gzip.open(RAW / f"{docket_id}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_raw(docket_id):
    p = RAW / f"{docket_id}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


HEADER_HELP = """
The DPU server sent back its web page instead of data. It wants the same
headers your browser sends. Fix it once, like this:

  1. In Chrome, open https://eeaonline.eea.state.ma.us/dpu/fileroom/#/dockets/docket/12806
  2. Press F12, click the Network tab, then Fetch/XHR, then reload the page.
  3. Right-click the row named  get?id=12806  ->  Copy  ->  Copy as cURL (bash)
     (on Windows "Copy as cURL (cmd)" also works).
  4. Paste it into a new text file named  request_headers.txt  in this folder
     (the same folder as dpu.py), and save.
  5. Run  python dpu.py test  again.
"""

# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------


def cmd_test(args):
    """Fetch DPU 26-50 and show what the program can read from it."""
    s = make_session()
    print(f"Contacting the DPU File Room API for docket ID {SAMPLE_ID} (DPU 26-50)...")
    try:
        data = fetch_docket_json(s, SAMPLE_ID)
    except RuntimeError:
        print(HEADER_HELP)
        return 1
    if data is None:
        print("The API returned no data for 12806. Paste request_headers.txt as described:")
        print(HEADER_HELP)
        return 1
    save_raw(SAMPLE_ID, data)
    con = db()
    number, _ = store_docket(con, SAMPLE_ID, data, mark_new=False)
    row = con.execute("SELECT * FROM dockets WHERE id=?", (SAMPLE_ID,)).fetchone()
    print("\nSUCCESS: the API answered with data.\n")
    print("Docket fields read:")
    for k in ("number", "description", "opened", "closed", "type", "division", "industry", "petitioner"):
        print(f"  {k:12} {str(row[k])[:100]}")
    docs = con.execute("SELECT * FROM documents WHERE docket_id=? ORDER BY filed_date DESC", (SAMPLE_ID,)).fetchall()
    print(f"\nDocuments found: {len(docs)}  (as of Nov. 2026, 26-50 had about 363 filings with about 4,700 attachments)")
    for d in docs[:8]:
        print(f"  {d['filed_date'] or '?':10} | {(d['filer'] or '?')[:28]:28} | {(d['title'] or '?')[:60]}")
    if len(docs) > 8:
        print("  ...")

    raw = json.dumps(data, ensure_ascii=False)
    print(f"\nTop-level structure of the response: {describe_shape(data)}")
    i = raw.find("fileservice")
    if i < 0:
        i = raw.lower().find("evans")
    if i >= 0:
        print("\nSample of the raw data around a document link (for checking the parser):")
        print("  " + raw[max(0, i - 900): i + 300].replace("\n", " "))
    else:
        print("\nNote: no File Service link or 'Evans' filing found in the response.")
        print("Sample of the raw data:\n  " + raw[:1500])
    if docs:
        d = docs[0]
        print(f"\nTrying to download one document: {d['file_name']}")
        try:
            fs = requests.Session()
            fs.headers["User-Agent"] = DEFAULT_HEADERS["User-Agent"]
            r = polite_get(fs, d["url"])
            kind = "a PDF" if r.content[:4] == b"%PDF" else f"{len(r.content):,} bytes"
            if r.status_code == 200 and not r.content[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
                print(f"  Download works (HTTP 200, received {kind}).")
            else:
                print(f"  Download did not work (HTTP {r.status_code}). Send this output to Claude.")
        except requests.RequestException as e:
            print(f"  Download did not work ({e}). Send this output to Claude.")
    print("\nIf the counts or fields above look wrong, send the 'Sample of the raw data' block"
          " to Claude and ask for a parser fix. Nothing needs to be downloaded again.")
    return 0


def describe_shape(x, depth=0):
    if isinstance(x, dict):
        if depth > 1:
            return "{...}"
        return "{" + ", ".join(f"{k}: {describe_shape(v, depth + 1)}" for k, v in list(x.items())[:25]) + "}"
    if isinstance(x, list):
        return f"[{len(x)} items]"
    return type(x).__name__


def cmd_index(args):
    """Walk docket IDs and save every docket record."""
    s = make_session()
    con = db()
    start, end = args.start, args.end
    misses_in_row = 0
    max_known = con.execute("SELECT MAX(id) FROM dockets").fetchone()[0] or 0
    print(f"Indexing docket IDs {start} to {end}. Stop at any time with Ctrl+C; "
          f"running the same command again picks up where it left off.")
    t0 = time.time()
    done = 0
    for i in range(start, end + 1):
        if not args.refresh:
            if (RAW / f"{i}.json.gz").exists() or con.execute("SELECT 1 FROM misses WHERE id=?", (i,)).fetchone():
                continue
        try:
            data = fetch_docket_json(s, i)
        except RuntimeError:
            print(HEADER_HELP)
            return 1
        done += 1
        if data is None:
            con.execute("INSERT OR REPLACE INTO misses VALUES (?,?)", (i, now()))
            con.commit()
            misses_in_row += 1
            if i > max_known and misses_in_row >= args.stop_after:
                print(f"\n{args.stop_after} empty IDs in a row past the last known docket. Stopping at {i}.")
                break
            continue
        misses_in_row = 0
        save_raw(i, data)
        number, _ = store_docket(con, i, data, mark_new=False)
        max_known = max(max_known, i)
        if done % 25 == 0 or args.verbose:
            rate = done / max(1, time.time() - t0)
            print(f"  ID {i}: DPU {number}   ({done} requests, ~{rate * 3600:.0f}/hour)")
    total = con.execute("SELECT COUNT(*) FROM dockets").fetchone()[0]
    docs = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    print(f"\nIndex now holds {total} dockets and {docs} document links.")
    return 0


def cmd_rebuild(args):
    """Re-read all saved raw JSON into the database (after a parser fix)."""
    con = db()
    files = sorted(RAW.glob("*.json.gz"))
    print(f"Re-reading {len(files)} saved dockets...")
    for n, p in enumerate(files, 1):
        i = int(p.name.split(".")[0])
        store_docket(con, i, load_raw(i), mark_new=False)
        if n % 500 == 0:
            print(f"  {n}")
    print("Done.")


def read_tracked():
    if not TRACKED.exists():
        return []
    out = []
    for line in TRACKED.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out


def resolve(con, number):
    rows = con.execute("SELECT id FROM dockets WHERE number=?", (number,)).fetchall()
    return [r["id"] for r in rows]


def cmd_track(args):
    con = db()
    current = read_tracked()
    for n in args.numbers:
        n = n.strip().upper().replace("DPU", "").strip()
        ids = resolve(con, n)
        if not ids:
            print(f"  {n}: not in the index yet. Run 'python dpu.py index' first "
                  f"(or 'python dpu.py update', which also checks for new dockets).")
            continue
        if n in current:
            print(f"  {n}: already tracked")
            continue
        current.append(n)
        print(f"  {n}: now tracked (internal ID {ids[0]})")
    TRACKED.write_text("\n".join(current) + "\n", encoding="utf-8")


def cmd_untrack(args):
    current = [n for n in read_tracked() if n not in args.numbers]
    TRACKED.write_text("\n".join(current) + "\n", encoding="utf-8")
    print("Tracked dockets:", ", ".join(current) or "(none)")


def cmd_update(args):
    """Check tracked dockets for new filings, look for new dockets, write a digest."""
    s = make_session()
    con = db()
    today = dt.date.today().isoformat()
    tracked = read_tracked()
    report = [f"# DPU File Room digest -- {today}\n"]
    all_new_docs = []

    # 1. Tracked dockets
    for number in tracked:
        for i in resolve(con, number):
            try:
                data = fetch_docket_json(s, i)
            except RuntimeError:
                print(HEADER_HELP)
                return 1
            if data is None:
                continue
            save_raw(i, data)
            _, new = store_docket(con, i, data, mark_new=True)
            all_new_docs += new
            print(f"  DPU {number}: {len(new)} new document(s)")

    if tracked:
        report.append("## New filings in tracked dockets\n")
        if not all_new_docs:
            report.append("No new filings since the last update.\n")
        by = {}
        for d in all_new_docs:
            by.setdefault(d["docket_number"], []).append(d)
        for number in tracked:
            if number in by:
                report.append(f"### DPU {number}\n")
                for d in sorted(by[number], key=lambda d: d["filed_date"] or "", reverse=True):
                    report.append(f"- {d['filed_date'] or '(no date)'} | {d['filer'] or '(filer unknown)'} | "
                                  f"[{d['title'] or d['file_name']}]({d['url']})")
                report.append("")

    # 2. New dockets: probe IDs past the highest one known
    max_known = con.execute("SELECT MAX(id) FROM dockets").fetchone()[0] or 0
    new_dockets = []
    if max_known:
        misses, i = 0, max_known
        while misses < args.probe:
            i += 1
            try:
                data = fetch_docket_json(s, i)
            except RuntimeError:
                print(HEADER_HELP)
                return 1
            if data is None:
                misses += 1
                continue
            misses = 0
            save_raw(i, data)
            number, _ = store_docket(con, i, data, mark_new=False)
            row = con.execute("SELECT * FROM dockets WHERE id=?", (i,)).fetchone()
            new_dockets.append(row)
            print(f"  New docket: DPU {number}")
    report.append("## Newly opened dockets\n")
    if not new_dockets:
        report.append("None found.\n")
    for r in new_dockets:
        report.append(f"- **DPU {r['number']}** ({r['opened']}) {r['petitioner']}: {r['description']}")
    report.append("")

    DIGESTS.mkdir(exist_ok=True)
    path = DIGESTS / f"{today}.md"
    mode = "a" if path.exists() else "w"
    with open(path, mode, encoding="utf-8") as f:
        if mode == "a":
            f.write(f"\n\n---\n_Later run at {now()}_\n\n")
        f.write("\n".join(report))
    print(f"\nDigest written to {path}")

    if args.download and all_new_docs:
        download_docs(con, s, [d["doc_key"] for d in all_new_docs])
    return 0


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120]


def extract_text(path):
    """Return a list of page texts. PDFs give one entry per page."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            import pymupdf as fitz
        except ImportError:
            try:
                import fitz  # older PyMuPDF
            except ImportError:
                fitz = None
        if fitz is None:
            sys.exit("Missing PyMuPDF. Run:  pip install -r requirements.txt")
        pages = []
        try:
            with fitz.open(path) as pdf:
                for p in pdf:
                    pages.append(p.get_text("text"))
        except Exception as e:
            print(f"    could not read PDF ({e})")
        return pages
    if suffix in (".xml", ".htm", ".html", ".txt", ".csv"):
        raw = path.read_text(encoding="utf-8", errors="ignore")
        if suffix != ".txt" and suffix != ".csv":
            raw = re.sub(r"<[^>]+>", " ", raw)
        return [re.sub(r"[ \t]+", " ", raw)]
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
            xml = re.sub(r"</w:p>", "\n", xml)
            return [re.sub(r"<[^>]+>", "", xml)]
        except Exception:
            return []
    return []   # spreadsheets, images, etc.: kept as files, not searched


def download_docs(con, s, keys):
    pending = [k for k in keys if not con.execute("SELECT 1 FROM downloads WHERE doc_key=?", (k,)).fetchone()]
    files_session = requests.Session()   # documents are public; send no DPU cookies to the CDN
    files_session.headers["User-Agent"] = DEFAULT_HEADERS["User-Agent"]
    print(f"Downloading {len(pending)} document(s) (skipping ones already downloaded)...")
    for n, key in enumerate(pending, 1):
        d = con.execute("SELECT * FROM documents WHERE doc_key=?", (key,)).fetchone()
        url = d["url"]
        number = d["docket_number"] or "unknown"
        folder = FILES / safe_name(number)
        folder.mkdir(parents=True, exist_ok=True)
        # File name: the document's own name, prefixed with its stable ID so names never collide.
        own = d["file_name"] or "document"
        stem_id = safe_name(key.replace("att:", "").split("/")[-1])[:40]
        fname = safe_name(f"{stem_id}_{own}")
        fpath = folder / fname
        if not fpath.exists():
            try:
                r = polite_get(files_session, url)
                if r.status_code in (401, 403):          # the File Service may want the DPU headers
                    r = polite_get(s, url)
            except requests.RequestException as e:
                print(f"  [{n}/{len(pending)}] failed: {e}")
                continue
            if r.status_code != 200 or r.content[:15].lstrip().lower().startswith((b"<!doctype", b"<html")):
                print(f"  [{n}/{len(pending)}] could not download (HTTP {r.status_code}): {own}")
                continue
            if not EXT_RE.search(fpath.name):            # no extension in the name: tell by content
                head = r.content[:8]
                ext = ".pdf" if head.startswith(b"%PDF") else ".zip" if head.startswith(b"PK") else ".bin"
                fpath = fpath.with_name(fpath.name + ext)
            fpath.write_bytes(r.content)
        pages = extract_text(fpath)
        tfolder = TEXT / safe_name(number)
        tfolder.mkdir(parents=True, exist_ok=True)
        tpath = tfolder / (fpath.stem + ".txt")
        header = (f"DPU {number}\nFiled: {d['filed_date']}\nFiler: {d['filer']}\n"
                  f"Title: {d['title']}\nSource: {url}\n\n")
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(header)
            for i, t in enumerate(pages, 1):
                f.write(f"\n=== Page {i} ===\n{t}")
        con.execute("DELETE FROM pages WHERE doc_key=?", (key,))
        empty = 0
        for i, t in enumerate(pages, 1):
            if len(t.strip()) < 30:
                empty += 1
                continue
            con.execute("INSERT INTO pages (body, doc_key, url, page, docket_number, title, filed_date, filer) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (t, key, url, i, number, d["title"], d["filed_date"], d["filer"]))
        con.execute("INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,?,?)",
                    (key, str(fpath), str(tpath), len(pages), empty, now()))
        con.commit()
        note = f"  ({empty} page(s) with no text: likely scanned)" if empty else ""
        print(f"  [{n}/{len(pending)}] DPU {number}: {(d['title'] or fname)[:60]} -- {len(pages)} pages{note}")


def cmd_download(args):
    s = make_session()
    con = db()
    if getattr(args, "id", None):
        # Asked for by the DPU Monitor website: refresh this docket first, so new filings are included.
        print(f"Getting the latest list of filings for docket ID {args.id}...")
        try:
            data = fetch_docket_json(s, args.id)
        except RuntimeError:
            print(HEADER_HELP)
            return 1
        if data is None:
            print("The DPU File Room has no docket with that ID.")
            return 1
        save_raw(args.id, data)
        number, new = store_docket(con, args.id, data, mark_new=True)
        print(f"DPU {number}: {len(new)} filing(s) not seen before.")
        args.dockets = [number]
    numbers = args.dockets or read_tracked()
    if not numbers:
        print("No dockets given and tracked.txt is empty. Example:  python dpu.py download 26-50")
        return 1
    keys = []
    for n in numbers:
        rows = con.execute("SELECT doc_key FROM documents WHERE docket_number=?", (n,)).fetchall()
        if not rows:
            print(f"  {n}: no documents in the index (run index/update first)")
        keys += [r["doc_key"] for r in rows]
    download_docs(con, s, keys)
    return 0


def fts_query(text):
    """Pass through quoted phrases and AND/OR/NOT; otherwise search all words."""
    if re.search(r'"|\bAND\b|\bOR\b|\bNOT\b|\*', text):
        return text
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", text)
    return " ".join(f'"{w}"' for w in words)


def cmd_search(args):
    """Full-text search over downloaded filings."""
    con = db()
    q = fts_query(" ".join(args.query))
    sql = ("SELECT docket_number, filed_date, filer, title, url, page, "
           "snippet(pages, 0, '[', ']', ' ... ', 24) AS snip FROM pages WHERE pages MATCH ?")
    params = [q]
    if args.docket:
        sql += " AND docket_number = ?"
        params.append(args.docket)
    sql += " ORDER BY rank LIMIT ?"
    params.append(args.limit)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"Search syntax problem: {e}. Try plain words, or put a phrase in double quotes.")
        return 1
    if not rows:
        print("No matches in downloaded filings. (Only downloaded dockets are searchable by text; "
              "use 'find' to search docket and document titles across the whole index.)")
    for r in rows:
        print(f"\nDPU {r['docket_number']} | {r['filed_date']} | {r['filer']}")
        print(f"  {r['title']}  -- page {r['page']}")
        print(f"  {r['url']}#page={r['page']}")
        print("  " + " ".join(r["snip"].split()))
    return 0


def cmd_find(args):
    """Search docket descriptions and document titles across the whole index."""
    con = db()
    terms = args.query
    where = " AND ".join(["(COALESCE(number,'') || ' ' || COALESCE(description,'') || ' ' || COALESCE(petitioner,'') || ' ' || COALESCE(type,'')) LIKE ?"] * len(terms))
    params = [f"%{t}%" for t in terms]
    if args.open:
        where += " AND (closed IS NULL OR closed = '')"
    rows = con.execute(f"SELECT * FROM dockets WHERE {where} ORDER BY opened DESC LIMIT ?",
                       params + [args.limit]).fetchall()
    print(f"Dockets matching {' + '.join(terms)}: {len(rows)}")
    for r in rows:
        status = "open" if not r["closed"] else f"closed {r['closed']}"
        print(f"  DPU {r['number']:8} {r['opened'] or '':10} ({status}) {(r['petitioner'] or '')[:30]} -- "
              f"{(r['description'] or '')[:110]}")
    if not args.dockets_only:
        where = " AND ".join(["(COALESCE(title,'') || ' ' || COALESCE(filer,'')) LIKE ?"] * len(terms))
        drows = con.execute(f"SELECT * FROM documents WHERE {where} ORDER BY filed_date DESC LIMIT ?",
                            params + [args.limit]).fetchall()
        print(f"\nDocuments with matching titles or filers: {len(drows)}")
        for d in drows:
            print(f"  DPU {d['docket_number']:8} {d['filed_date'] or '':10} {(d['filer'] or '')[:25]:25} "
                  f"{(d['title'] or '')[:70]}")
            print(f"           {d['url']}")


def cmd_status(args):
    con = db()
    q = lambda sql: con.execute(sql).fetchone()[0]
    print(f"Dockets in index:      {q('SELECT COUNT(*) FROM dockets')}")
    open_sql = "SELECT COUNT(*) FROM dockets WHERE closed IS NULL OR closed=''"
    print(f"  of which open:       {q(open_sql)}")
    print(f"Highest internal ID:   {q('SELECT MAX(id) FROM dockets')}")
    print(f"Document links:        {q('SELECT COUNT(*) FROM documents')}")
    print(f"Documents downloaded:  {q('SELECT COUNT(*) FROM downloads')}")
    print(f"Searchable pages:      {q('SELECT COUNT(*) FROM pages')}")
    print(f"Pages with no text:    {q('SELECT COALESCE(SUM(empty_pages),0) FROM downloads')}")
    print(f"Tracked dockets:       {', '.join(read_tracked()) or '(none)'}")


# ----------------------------------------------------------------------------
# Ask: the optional AI layer. Retrieval is done locally; only the retrieved
# excerpts are sent to the model. The model is swappable.
# ----------------------------------------------------------------------------

STOP = set("""a an and are as at be by for from has have how i in is it its of on or that the this to was
what when where which who why will with did does do about between compare compared versus vs than their
they them there these those my me we our you your any all can could should would""".split())


def retrieve(con, question, docket=None, k=12):
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", question.lower()) if w not in STOP and len(w) > 1]
    if not words:
        return []
    q = " OR ".join(f'"{w}"' for w in words)
    sql = "SELECT rowid, * FROM pages WHERE pages MATCH ?"
    params = [q]
    if docket:
        sql += " AND docket_number IN (%s)" % ",".join("?" * len(docket))
        params += docket
    sql += " ORDER BY rank LIMIT ?"
    params.append(k * 3)
    rows = con.execute(sql, params).fetchall()
    out, per_doc = [], {}
    for r in rows:            # no more than 3 pages from any one document
        per_doc[r["doc_key"]] = per_doc.get(r["doc_key"], 0) + 1
        if per_doc[r["doc_key"]] <= 3:
            out.append(r)
        if len(out) >= k:
            break
    return out


def call_model(prompt, model):
    if model.startswith("ollama:"):
        r = requests.post("http://localhost:11434/api/chat", timeout=600, json={
            "model": model.split(":", 1)[1], "stream": False,
            "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["message"]["content"]
    key = os.environ.get("ANTHROPIC_API_KEY") or load_settings().get("anthropic_api_key")
    if not key:
        raise RuntimeError("No Anthropic API key is set. Add one on the dashboard's Settings page, "
                           "or set ANTHROPIC_API_KEY, or use a local model (ollama:<name>).")
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=600, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise RuntimeError(f"The model service returned error {r.status_code}: {r.text[:400]}")
    return "".join(b.get("text", "") for b in r.json()["content"])


def answer_question(question, dockets=None, model=None, k=12):
    """Retrieve pages locally, send them to the model, return (answer_text, hits)."""
    model = model or os.environ.get("DPU_MODEL") or load_settings().get("model") or "claude-sonnet-5-5"
    con = db()
    hits = retrieve(con, question, dockets or None, k)
    if not hits:
        return None, []
    blocks = []
    for n, h in enumerate(hits, 1):
        blocks.append(f"[S{n}] DPU {h['docket_number']} | {h['filed_date']} | {h['filer']} | "
                      f"{h['title']} | page {h['page']}\n{h['body'][:3500]}")
    prompt = (
        "You are assisting a regulatory attorney with Massachusetts DPU proceedings. Answer the question "
        "using ONLY the excerpts below. Cite every factual statement with its source tag, e.g. [S3]. "
        "If the excerpts do not answer the question, say so plainly and say what document would be needed. "
        "Do not use outside knowledge for facts about these proceedings.\n\n"
        f"QUESTION: {question}\n\nEXCERPTS:\n\n" + "\n\n".join(blocks))
    return call_model(prompt, model), [dict(h) for h in hits]


def cmd_ask(args):
    question = " ".join(args.question)
    try:
        text, hits = answer_question(question, args.docket, args.model, args.k)
    except RuntimeError as e:
        print(e)
        return 1
    if not hits:
        print("Nothing relevant found in downloaded filings. Download the relevant dockets first.")
        return 1
    print(text)
    print("\nSources:")
    for n, h in enumerate(hits, 1):
        print(f"  [S{n}] DPU {h['docket_number']} {h['filed_date']} {h['title'][:60]} p.{h['page']}")
        print(f"        {h['url']}#page={h['page']}")



# ----------------------------------------------------------------------------
# Export for Claude: one plain-text file holding chosen filings (or chosen
# pages), each labeled with docket, date, filer, title, link, and page numbers,
# ready to upload to a chat.
# ----------------------------------------------------------------------------

def read_pages(text_path):
    """Return {page_number: text} from an extracted text file."""
    try:
        raw = Path(text_path).read_text(encoding="utf-8")
    except OSError:
        return {}
    parts = re.split(r"\n=== Page (\d+) ===\n", raw)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def build_export(con, items, title="", fetch_missing=True, session=None):
    """items: list of (doc_key, set_of_pages_or_None). None means the whole document.
    Downloads documents that are not on this computer yet (if fetch_missing).
    Returns (path, summary)."""
    keys = list(dict.fromkeys(k for k, _ in items))
    missing = [k for k in keys if not con.execute("SELECT 1 FROM downloads WHERE doc_key=?", (k,)).fetchone()]
    if missing and fetch_missing:
        download_docs(con, session or make_session(), missing)
    wanted = {}
    for k, pages in items:
        if pages is None or wanted.get(k, set()) is None:
            wanted[k] = None
        else:
            wanted.setdefault(k, set()).update(pages)

    docs = []
    for k in keys:
        d = con.execute("SELECT * FROM documents WHERE doc_key=?", (k,)).fetchone()
        if d:
            docs.append(d)
    docs.sort(key=lambda d: (d["docket_number"] or "", d["filed_date"] or "", d["title"] or ""))

    stamp = dt.datetime.now()
    body, skipped, n_pages, n_docs = [], [], 0, 0
    for d in docs:
        w = con.execute("SELECT * FROM downloads WHERE doc_key=?", (d["doc_key"],)).fetchone()
        pages = read_pages(w["text_path"]) if w else {}
        if not pages:
            skipped.append(d)
            continue
        sel = wanted.get(d["doc_key"])
        chosen = sorted(pages) if sel is None else sorted(p for p in sel if p in pages)
        total = len(pages)
        scope = ("all pages" if sel is None else
                 ("page " if len(chosen) == 1 else "pages ") + ", ".join(map(str, chosen)))
        n_docs += 1
        body.append("=" * 78)
        body.append(f"DOCUMENT {n_docs}: D.P.U. {d['docket_number']}")
        body.append(f"Filed: {d['filed_date'] or 'not listed'}")
        body.append(f"Filer: {d['filer'] or 'not listed'}")
        body.append(f"Title: {d['title'] or ''}")
        body.append(f"File: {d['file_name'] or ''} ({total} page{'' if total == 1 else 's'}; included: {scope})")
        body.append(f"Source: {d['url']}")
        body.append("=" * 78)
        for p in chosen:
            text = pages[p].strip()
            body.append(f"\n--- D.P.U. {d['docket_number']} | {d['file_name']} | page {p} of {total} ---")
            body.append(text if text else "[No text on this page. It may be a scanned image.]")
            n_pages += 1
        body.append("")

    head = [
        f"DPU File Room excerpts{(': ' + title) if title else ''}",
        f"Exported {stamp:%Y-%m-%d %H:%M} from a local mirror of the Massachusetts DPU File Room.",
        f"{len(docs) - len(skipped)} document(s), {n_pages} page(s).",
        "Each page is marked with its docket, file name, and page number. Cite those when referring to the text.",
        "Page numbers are the PDF's page count, which may differ from the numbers printed on the page.",
        "",
    ]
    if skipped:
        head.append("Not included (no extracted text; possibly a spreadsheet, image, or failed download):")
        head += [f"  - D.P.U. {d['docket_number']} {d['filed_date']} {d['title']} ({d['file_name']})" for d in skipped]
        head.append("")
    text = "\n".join(head + body)

    EXPORTS.mkdir(exist_ok=True)
    name = f"{stamp:%Y-%m-%d_%H%M%S}_{safe_name(title or 'export')[:60]}.txt"
    path = EXPORTS / name
    path.write_text(text, encoding="utf-8")
    words = len(text.split())
    return path, {"file": name, "path": str(path), "documents": len(docs) - len(skipped),
                  "pages": n_pages, "skipped": len(skipped), "words": words,
                  "tokens_est": int(words * 1.4)}


def cmd_export(args):
    """Export filings as one text file for Claude: by docket, by search, or both."""
    con = db()
    items = []
    if args.query:
        q = fts_query(" ".join(args.query))
        sql = "SELECT doc_key, page FROM pages WHERE pages MATCH ?"
        params = [q]
        if args.docket:
            sql += " AND docket_number=?"
            params.append(args.docket)
        sql += " ORDER BY rank LIMIT ?"
        params.append(args.limit)
        hits = con.execute(sql, params).fetchall()
        items = [(h["doc_key"], {h["page"]}) for h in hits]
    elif args.docket:
        sql = "SELECT doc_key FROM documents WHERE docket_number=?"
        params = [args.docket]
        if args.since:
            sql += " AND filed_date >= ?"
            params.append(args.since)
        items = [(r["doc_key"], None) for r in con.execute(sql, params).fetchall()]
    if not items:
        print("Nothing to export. Give a docket (--docket 26-50) and/or search words.")
        return 1
    title = args.title or " ".join(filter(None, [args.docket, " ".join(args.query or [])]))
    path, info = build_export(con, items, title)
    print(f"Saved {path}")
    print(f"{info['documents']} documents, {info['pages']} pages, about {info['words']:,} words.")


def cmd_serve(args):
    import dashboard
    dashboard.serve(args.port, open_browser=not args.no_browser)


# ----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(prog="dpu.py", description="Local mirror and search for the DPU File Room.")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("test", help="check that the DPU API answers, using DPU 26-50")

    a = sub.add_parser("index", help="build the index of all dockets (long first run)")
    a.add_argument("--start", type=int, default=1)
    a.add_argument("--end", type=int, default=20000)
    a.add_argument("--stop-after", type=int, default=300, help="stop after this many empty IDs past the last docket")
    a.add_argument("--refresh", action="store_true", help="re-fetch dockets already saved")
    a.add_argument("--verbose", action="store_true")

    sub.add_parser("rebuild", help="re-read saved data into the database (after a parser fix)")

    a = sub.add_parser("track", help="add docket numbers to your tracked list")
    a.add_argument("numbers", nargs="+")
    a = sub.add_parser("untrack", help="remove docket numbers from your tracked list")
    a.add_argument("numbers", nargs="+")

    a = sub.add_parser("update", help="daily run: new filings in tracked dockets + new dockets, writes a digest")
    a.add_argument("--download", action="store_true", help="also download and index the new filings")
    a.add_argument("--probe", type=int, default=40, help="empty IDs to try past the last docket")

    a = sub.add_parser("download", help="download and extract text for dockets (default: tracked ones)")
    a.add_argument("dockets", nargs="*")
    a.add_argument("--id", type=int, help="the file room's internal docket ID (used by the DPU Monitor website)")

    a = sub.add_parser("search", help="keyword search inside downloaded filings")
    a.add_argument("query", nargs="+")
    a.add_argument("--docket")
    a.add_argument("--limit", type=int, default=20)

    a = sub.add_parser("find", help="search docket descriptions and document titles across the index")
    a.add_argument("query", nargs="+")
    a.add_argument("--open", action="store_true", help="open dockets only")
    a.add_argument("--dockets-only", action="store_true")
    a.add_argument("--limit", type=int, default=40)

    a = sub.add_parser("ask", help="ask a question; answered from downloaded filings with citations")
    a.add_argument("question", nargs="+")
    a.add_argument("--docket", nargs="*", help="limit to these docket numbers")
    a.add_argument("--k", type=int, default=12, help="number of excerpts to send")
    a.add_argument("--model", default=None,
                   help="Anthropic model name, or ollama:<name> for a local model")

    sub.add_parser("status", help="show what is in the mirror")
    a = sub.add_parser("export", help="save filings as one text file to upload to Claude")
    a.add_argument("query", nargs="*", help="search words (optional); exports the matching pages")
    a.add_argument("--docket", help="docket number; alone, exports all its filings")
    a.add_argument("--since", help="with --docket: only filings on or after this date (YYYY-MM-DD)")
    a.add_argument("--limit", type=int, default=40, help="with search words: number of pages")
    a.add_argument("--title")
    a = sub.add_parser("serve", help="open the dashboard in your web browser")
    a.add_argument("--port", type=int, default=8000)
    a.add_argument("--no-browser", action="store_true")
    sub.add_parser("help")

    args = p.parse_args()
    cmds = {"test": cmd_test, "index": cmd_index, "rebuild": cmd_rebuild, "track": cmd_track,
            "untrack": cmd_untrack, "update": cmd_update, "download": cmd_download, "search": cmd_search,
            "find": cmd_find, "ask": cmd_ask, "status": cmd_status,
            "serve": cmd_serve, "export": cmd_export}
    if args.cmd not in cmds:
        p.print_help()
        return 0
    try:
        return cmds[args.cmd](args) or 0
    except KeyboardInterrupt:
        print("\nStopped. Everything saved so far is kept.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
