#!/usr/bin/env python3
"""Drop-folder importer for Zotero.

Every PDF / DjVu / EPUB placed in INBOX becomes a Zotero item with the file
attached, tagged TAG and filed into one of COLLECTIONS by topic. Metadata
come from Crossref when a DOI on the first pages checks out against the text,
otherwise from a headless `claude -p` call that reads the title pages (as
text, or as images for scans without a text layer).

Writes go through the endpoints of Zotero's browser connector on
localhost:23119, which need no API key and therefore never pop an
authorization dialog.

Usage: zotero_autoimport.py [FILE ...]   (no arguments: process INBOX)
Settings: the ZAI_* environment variables below; see README.md.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path


def load_config(path):
    """KEY=VALUE lines from a local, untracked file; the real environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_config(Path(__file__).resolve().parent / "config.env")
env = os.environ.get
INBOX = Path(env("ZAI_INBOX", Path.home() / "Downloads" / "to-zotero")).expanduser()
FAILED = INBOX / "failed"
DUPS = INBOX / "duplicates"
DONE = INBOX / "processed"
# topic -> collection name; a missing collection falls back to FALLBACK, then
# to whatever collection is selected in the Zotero pane
COLLECTIONS = {"math": env("ZAI_COLLECTION_MATH", "Inbox math"),
               "nonfic": env("ZAI_COLLECTION_NONFIC", "Inbox nonfiction"),
               "other": env("ZAI_COLLECTION_OTHER", "Inbox other")}
FALLBACK = env("ZAI_COLLECTION_FALLBACK", "Inbox")
TAG = env("ZAI_TAG", "auto-import")
MODEL = env("ZAI_MODEL", "haiku")
MAILTO = env("ZAI_CROSSREF_MAILTO", "")  # Crossref's "polite pool", optional
LOG = Path.home() / "Library" / "Logs" / "zotero-autoimport.log"
ZOTERO = "http://localhost:23119"


def tool(name):
    path = shutil.which(name)
    if not path:
        sys.exit(f"zotero-autoimport: '{name}' not found on PATH")
    return path

EXTS = {".pdf": "application/pdf", ".djvu": "image/vnd.djvu",
        ".djv": "image/vnd.djvu", ".epub": "application/epub+zip"}
HEAD_PAGES, TAIL_PAGES = 4, 3
MIN_TEXT = 300  # fewer non-space chars than this on the sampled pages = scan

FIELDS = {  # Zotero fields the model may fill, per item type
    "book": ["title", "date", "publisher", "place", "ISBN", "edition", "series",
             "seriesNumber", "numPages", "language"],
    "bookSection": ["title", "bookTitle", "date", "publisher", "place", "ISBN",
                    "pages", "language"],
    "journalArticle": ["title", "publicationTitle", "journalAbbreviation", "date",
                       "volume", "issue", "pages", "DOI", "ISSN", "language"],
    "conferencePaper": ["title", "proceedingsTitle", "conferenceName", "date",
                        "publisher", "place", "pages", "DOI", "ISBN", "language"],
    "thesis": ["title", "thesisType", "university", "place", "date", "numPages",
               "language"],
    "report": ["title", "institution", "reportNumber", "place", "date", "pages",
               "language"],
    "preprint": ["title", "repository", "archiveID", "date", "DOI", "language"],
    "manuscript": ["title", "place", "date", "numPages", "language"],
}
CREATOR_TYPES = {"author", "editor", "translator", "contributor", "bookAuthor"}

TOPICS = """\
- topic: "math" for mathematics, mathematical physics, theoretical CS; "nonfic" \
for non-fiction outside mathematics (history, philosophy, psychoanalysis, \
popular science, essays, memoirs); "other" for fiction, poetry and everything else."""

PROMPT = """You are cataloguing a file for a Zotero library. Below are the first \
and last pages of the document{how}. The original file name was: {name}

Return ONLY a JSON object, no prose, no code fence:
{{"itemType": one of {types},
  "topic": "math" | "nonfic" | "other",
  "title": "...",
  "creators": [{{"lastName": "...", "firstName": "...", "creatorType": "author|editor|translator"}}],
  ...other fields from this list that the pages actually state: {fields}}}

Rules.
- Copy title and names in the language and script of the document itself, as printed \
on the title page. Russian names: lastName = фамилия, firstName = имя and отчество \
(or initials, exactly as printed).
- Books printed in Russia carry full imprint data (выходные данные, often on the \
back of the title page or on the last page): publisher, city, year, ISBN, page count. \
Use them.
- date = the year (or full date) of this edition, not of the original.
- Describe the document you are holding. For a translation, give the journal or \
book it appears in, its publisher and year; details of the original (often in a \
footnote: "first published in ...", "перевод по изданию ...") go nowhere. Never \
transliterate: a name printed in Cyrillic stays in Cyrillic.
- An article from a journal (e.g. Математический сборник, Известия РАН, ТМФ, \
from mathnet.ru) is journalArticle; include publicationTitle, volume, issue, pages \
and DOI when printed.
{topics}
- Omit any field you cannot read from the pages. Never guess an ISBN or DOI.
- If the document cannot be identified at all, return {{"error": "why"}}.

--- PAGES ---
{text}"""


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def notify(title, msg):
    script = f"display notification {json.dumps(msg)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def run(*cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- extraction

def page_count(path):
    if path.suffix == ".pdf":
        m = re.search(r"^Pages:\s+(\d+)", run(tool("pdfinfo"), str(path)).stdout, re.M)
    else:
        m = re.match(r"\s*(\d+)", run(tool("djvused"), "-e", "n", str(path)).stdout)
    return int(m.group(1)) if m else 0


def sample_pages(n):
    head = list(range(1, min(HEAD_PAGES, n) + 1))
    tail = [p for p in range(max(n - TAIL_PAGES + 1, 1), n + 1) if p not in head]
    return head, tail


def page_text(path, p):
    if path.suffix == ".pdf":
        r = run(tool("pdftotext"), "-layout", "-f", str(p), "-l", str(p), str(path), "-")
    else:
        r = run(tool("djvutxt"), f"--page={p}", str(path))
    return r.stdout


def page_image(path, p, tmp):
    out = Path(tmp) / f"page-{p:05d}"
    if path.suffix == ".pdf":
        run(tool("pdftoppm"), "-f", str(p), "-l", str(p), "-r", "110", "-gray",
            "-png", "-singlefile", str(path), str(out))
        return out.with_suffix(".png")
    tif = out.with_suffix(".tif")
    run(tool("ddjvu"), "-format=tiff", f"-page={p}", "-size=1100x1500", str(path), str(tif))
    run("sips", "-s", "format", "png", str(tif), "--out", str(out.with_suffix(".png")))
    return out.with_suffix(".png")


def epub_text(path):
    """OPF metadata block plus the opening of the first content files."""
    with zipfile.ZipFile(path) as z:
        container = z.read("META-INF/container.xml").decode("utf-8", "replace")
        opf_name = re.search(r'full-path="([^"]+)"', container).group(1)
        opf = z.read(opf_name).decode("utf-8", "replace")
        meta = re.search(r"<(?:\w+:)?metadata.*?</(?:\w+:)?metadata>", opf, re.S)
        parts = ["[OPF metadata]\n" + (meta.group(0) if meta else "")]
        base = os.path.dirname(opf_name)
        hrefs = re.findall(r'href="([^"]+\.x?html?)"', opf)[:4]
        for h in hrefs:
            name = os.path.normpath(os.path.join(base, urllib.parse.unquote(h)))
            try:
                html = z.read(name).decode("utf-8", "replace")
            except KeyError:
                continue
            txt = re.sub(r"<[^>]+>", " ", html)
            parts.append(f"[{h}]\n" + re.sub(r"\s+", " ", txt)[:3000])
    return "\n\n".join(parts)


# ---------------------------------------------------------------- Crossref

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def words(s):
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) > 3}


def crossref(doi, first_text):
    """Crossref record mapped to Zotero, or None unless its title is on page 1."""
    doi = doi.rstrip(".,;)")
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    agent = "zotero-autoimport" + (f" (mailto:{MAILTO})" if MAILTO else "")
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            m = json.load(r)["message"]
    except Exception as e:
        log(f"  crossref {doi}: {e}")
        return None
    title = (m.get("title") or [""])[0]
    tw = words(title)
    if not tw or len(tw & words(first_text)) < 0.6 * len(tw):
        log(f"  crossref {doi}: title '{title[:60]}' not found on the first pages, ignored")
        return None
    kind = {"journal-article": "journalArticle", "book-chapter": "bookSection",
            "proceedings-article": "conferencePaper", "book": "book",
            "monograph": "book", "posted-content": "preprint"}.get(m.get("type"), "journalArticle")
    parts = (m.get("issued") or {}).get("date-parts") or [[]]
    item = {
        "itemType": kind, "title": title, "DOI": m.get("DOI", doi),
        "date": "-".join(f"{x:02d}" if i else str(x) for i, x in enumerate(parts[0])),
        "volume": m.get("volume"), "issue": m.get("issue"), "pages": m.get("page"),
        "publisher": m.get("publisher") if kind in ("book", "bookSection", "conferencePaper") else None,
        "language": m.get("language"),
        "creators": [{"lastName": a.get("family", a.get("name", "")),
                      "firstName": a.get("given", ""), "creatorType": role}
                     for role in ("author", "editor") for a in m.get(role, [])],
    }
    cyr = lambda s: bool(re.search(r"[а-яё]", s, re.I))
    if cyr(title):  # mathnet lists every author twice, in Cyrillic and transliterated
        item["creators"] = [c for c in item["creators"] if cyr(c["lastName"])] or item["creators"]
    container = (m.get("container-title") or [None])[0]
    item[{"journalArticle": "publicationTitle", "bookSection": "bookTitle",
          "conferencePaper": "proceedingsTitle"}.get(kind, "_")] = container
    if kind == "journalArticle" and m.get("ISSN"):
        item["ISSN"] = m["ISSN"][0]
    if kind in ("book", "bookSection") and m.get("ISBN"):
        item["ISBN"] = m["ISBN"][0]
    return item


# ---------------------------------------------------------------- the model

def ask_topic(item):
    """One short call for items whose metadata came from Crossref."""
    desc = json.dumps({k: item.get(k) for k in ("itemType", "title", "publicationTitle",
                       "bookTitle", "proceedingsTitle")}, ensure_ascii=False)
    prompt = f"Classify this bibliographic item.\n{TOPICS}\n\nItem: {desc}\n\nReply with the topic word only."
    r = subprocess.run([tool("claude"), "-p", "--model", MODEL, "--tools", "", "--strict-mcp-config",
                        "--no-session-persistence"], input=prompt, capture_output=True,
                       text=True, timeout=120, cwd=tempfile.gettempdir())
    m = re.search(r"\b(math|nonfic|other)\b", r.stdout)
    return m.group(1) if m else "math"


def ask_claude(name, text, images):
    how = ""
    if images:
        how = (" — it is a scan without a text layer, so the pages are given as images; "
               "read each of these files with the Read tool: " + ", ".join(map(str, images)))
    prompt = PROMPT.format(name=name, text=text or "(see images)", how=how,
                           types=" | ".join(FIELDS),
                           fields=", ".join(sorted({f for v in FIELDS.values() for f in v})),
                           topics=TOPICS)
    cmd = [tool("claude"), "-p", "--model", MODEL, "--output-format", "json",
           "--strict-mcp-config", "--no-session-persistence"]
    if images:
        cmd += ["--allowedTools", "Read", "--add-dir", str(images[0].parent)]
    else:
        cmd += ["--tools", ""]
    cwd = images[0].parent if images else tempfile.gettempdir()
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       timeout=300, cwd=cwd)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude failed: {(r.stderr or r.stdout)[:300]}")
    if out.get("is_error"):
        raise RuntimeError(f"claude: {out.get('result', '')[:300]}")
    res = out.get("result", "")
    m = re.search(r"\{.*\}", res, re.S)
    if not m:
        raise RuntimeError(f"no JSON in model reply: {res[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict) or "error" in data:
        raise RuntimeError(f"model could not identify the file: {data['error']}")
    log(f"  claude: ${out.get('total_cost_usd', 0):.3f}")
    return data


# ---------------------------------------------------------------- Zotero

def clean(item):
    kind = item.get("itemType") if item.get("itemType") in FIELDS else "book"
    out = {"itemType": kind,
           "topic": item.get("topic") if item.get("topic") in COLLECTIONS else None}
    for f in FIELDS[kind]:
        v = item.get(f)
        if v not in (None, "", []):
            out[f] = str(v).strip()
    if not out.get("title"):
        raise RuntimeError("no title")
    out["creators"] = [
        {"lastName": c.get("lastName", "").strip(), "firstName": c.get("firstName", "").strip(),
         "creatorType": c.get("creatorType") if c.get("creatorType") in CREATOR_TYPES else "author"}
        for c in item.get("creators") or [] if c.get("lastName")]
    return out


def zpost(endpoint, body, headers=None, raw=None):
    h = {"X-Zotero-Connector-API-Version": "3", "Content-Type": "application/json"}
    h.update(headers or {})
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(f"{ZOTERO}/connector/{endpoint}", data=data, headers=h)
    with urllib.request.urlopen(req, timeout=300) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt.strip().startswith(("{", "[")) else None


def zotero_up():
    try:
        with urllib.request.urlopen(f"{ZOTERO}/connector/ping", timeout=5):
            return True
    except Exception:
        return False


def find_duplicate(title):
    q = urllib.parse.urlencode({"q": title, "itemType": "-attachment", "limit": 10,
                                "format": "json"})
    try:
        with urllib.request.urlopen(f"{ZOTERO}/api/users/0/items?{q}", timeout=20) as r:
            items = json.load(r)
    except Exception:
        return None
    norm = lambda s: re.sub(r"\W+", " ", s).strip().lower()
    for it in items:
        if norm(it["data"].get("title", "")) == norm(title):
            return it["key"]
    return None


def save(item, path, topic):
    sel = zpost("getSelectedCollection", {})
    ids = {t["name"]: t["id"] for t in sel["targets"]}
    name = COLLECTIONS[topic] if COLLECTIONS[topic] in ids else FALLBACK
    target = ids.get(name)
    sid = f"autoimport-{time.time_ns()}"
    zitem = dict(item, id="it", attachments=[], notes=[])
    zpost("saveItems", {"sessionID": sid, "uri": "file:///", "items": [zitem]})
    # tags given in saveItems are dropped; the session's own tags stick
    session = {"sessionID": sid, "tags": TAG}
    if target:
        session["target"] = target
    else:
        log(f"  collection '{name}' not found, item left in '{sel['name']}'")
    zpost("updateSession", session)
    meta = {"sessionID": sid, "parentItemID": "it", "title": "Full Text", "url": path.as_uri()}
    zpost(f"saveAttachment?sessionID={sid}", None, raw=path.read_bytes(),
          headers={"Content-Type": EXTS[path.suffix],
                   "X-Metadata": json.dumps(meta, ensure_ascii=True)})
    return name


# ---------------------------------------------------------------- pipeline

def metadata(path):
    if path.suffix == ".epub":
        return clean(ask_claude(path.name, epub_text(path), []))
    n = page_count(path)
    if not n:
        raise RuntimeError("cannot read page count")
    head, tail = sample_pages(n)
    texts = {p: page_text(path, p) for p in head + tail}
    first = "\n".join(texts[p] for p in head[:2])
    if path.suffix == ".pdf":
        for doi in dict.fromkeys(DOI_RE.findall(first)):
            item = crossref(doi, first)
            if item:
                log(f"  metadata from Crossref ({doi})")
                return clean(item)
    body = "\n".join(f"[page {p} of {n}]\n{texts[p]}" for p in head + tail)
    if len(re.sub(r"\s", "", body)) >= MIN_TEXT:
        return clean(ask_claude(path.name, body[:40000], []))
    with tempfile.TemporaryDirectory(prefix="zai-") as tmp:
        imgs = [page_image(path, p, tmp) for p in head + tail]
        imgs = [i for i in imgs if i.exists()]
        if not imgs:
            raise RuntimeError("no text layer and page rendering failed")
        log(f"  no text layer, reading {len(imgs)} page images")
        return clean(ask_claude(path.name, "", imgs))


def move_to(path, folder):
    folder.mkdir(exist_ok=True)
    dest = folder / path.name
    if dest.exists():
        dest = dest.with_name(f"{path.stem} {time.time_ns()}{path.suffix}")
    shutil.move(str(path), dest)


def process(path):
    log(f"{path.name}")
    item = metadata(path)
    topic = item.pop("topic") or ask_topic(item)
    who = ", ".join(c["lastName"] for c in item["creators"][:3])
    label = f"{who} — {item['title']}" if who else item["title"]
    dup = find_duplicate(item["title"])
    if dup:
        log(f"  duplicate of {dup}, skipped")
        notify("Zotero: already there", label)
        return "dup"
    where = save(item, path, topic)
    log(f"  saved to '{where}': {item['itemType']}: {label} ({item.get('date', '?')})")
    notify(f"Zotero → {where}", label)
    return "ok"


def ready(path):
    """The path to process (suffix lower-cased), or None if not a finished download."""
    if path.name.startswith(".") or path.suffix.lower() not in EXTS or not path.is_file():
        return False
    if path.suffix not in EXTS:  # .PDF -> .pdf, the rest of the code expects lower case
        path = path.rename(path.with_suffix(path.suffix.lower()))
    s = path.stat().st_size
    time.sleep(2)
    return path if path.exists() and s > 0 and path.stat().st_size == s else None


def main(args):
    INBOX.mkdir(parents=True, exist_ok=True)
    if not zotero_up():
        log("Zotero is not running, nothing done")
        notify("Zotero autoimport", f"Zotero is not running; files wait in {INBOX.name}")
        return 1
    explicit = bool(args)
    files = [Path(a).resolve() for a in args] or sorted(INBOX.iterdir())
    for path in files:
        if path.is_dir():
            continue
        path = ready(path)
        if not path:
            continue
        try:
            status = process(path)
            if not explicit:
                move_to(path, DUPS if status == "dup" else DONE)
        except Exception as e:
            log(f"  FAILED: {e}")
            notify("Zotero: import failed", f"{path.name}: {e}"[:200])
            if not explicit:
                move_to(path, FAILED)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
