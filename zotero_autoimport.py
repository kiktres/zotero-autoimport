#!/usr/bin/env python3
"""Drop-folder importer for Zotero.

Every PDF / DjVu / EPUB placed in INBOX becomes a Zotero item with the file
attached, tagged TAG and filed into COLLECTION (or, if topics are configured,
into the collection of its topic). Metadata come from Crossref when a DOI on
the first pages checks out against the text, otherwise from a language model
that reads the title pages: `claude -p` by default (as text, or as images for
scans without a text layer), or any command given in ZAI_LLM_CMD (text only).

Writes go through the endpoints of Zotero's browser connector on
localhost:23119, which need no API key and therefore never pop an
authorization dialog.

Usage: zotero_autoimport.py [FILE ...]   (no arguments: process INBOX)
Settings: the ZAI_* environment variables below; see README.md.
"""

import html
import json
import os
import re
import shlex
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
DONE = INBOX / "processed"
# every item goes to COLLECTION unless topics are configured; a missing
# collection leaves the item in whatever collection is selected in Zotero
COLLECTION = env("ZAI_COLLECTION", "Inbox")


def read_topics():
    """ZAI_TOPIC_<NAME>=<collection> | <description for the model>"""
    topics = {}
    for key, value in sorted(os.environ.items()):
        if key.startswith("ZAI_TOPIC_"):
            coll, _, desc = value.partition("|")
            topics[key[len("ZAI_TOPIC_"):].lower()] = (coll.strip(), desc.strip())
    return topics


TOPICS = read_topics()
LLM_CMD = env("ZAI_LLM_CMD", "")  # empty: built-in claude backend
TAG = env("ZAI_TAG", "auto-import")
DUP_TAG = env("ZAI_DUP_TAG", "possible-duplicate")  # added when a likely twin exists
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
HEAD_PAGES, TAIL_PAGES = 6, 3
MIN_TEXT = 300  # fewer non-space chars than this on the sampled pages = scan

FIELDS = {  # Zotero fields the model may fill, per item type
    "book": ["title", "volume", "numberOfVolumes", "date", "publisher", "place", "ISBN",
             "edition", "series", "seriesNumber", "numPages", "language"],
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
for _f in FIELDS.values():  # every type may carry these
    _f += ["abstractNote", "originalDate"]
CREATOR_TYPES = {"author", "editor", "translator", "bookAuthor"}
LANGUAGES = {"russian": "ru", "русский": "ru", "rus": "ru", "english": "en",
             "английский": "en", "eng": "en", "french": "fr", "français": "fr",
             "французский": "fr", "fre": "fr", "fra": "fr", "german": "de", "deutsch": "de",
             "немецкий": "de", "ger": "de", "deu": "de", "ukrainian": "uk",
             "украинский": "uk", "ukr": "uk", "italian": "it", "итальянский": "it",
             "ita": "it", "spanish": "es", "испанский": "es", "spa": "es"}



def topics_rule():
    return "- topic: one of\n" + "\n".join(
        f'  "{name}": {desc or coll}' for name, (coll, desc) in TOPICS.items())

PROMPT = """You are cataloguing a file for a Zotero library. Below are the first \
and last pages of the document{how}. The original file name was: {name}

Return ONLY a JSON object, no prose, no code fence:
{{"itemType": one of {types},
{topic_field}  "title": "...",
  "creators": [{{"lastName": "...", "firstName": "...", "creatorType": "author|editor|translator"}}],
  ...other fields from this list that the pages actually state: {fields}}}

Rules.
- Copy title and names in the language and script of the document itself, as printed \
on the title page. Russian names: lastName = фамилия, firstName = имя and отчество \
(or initials, exactly as printed).
- creators: authors, editors (под редакцией, составитель, ответственный редактор) \
and translators only. Never artists, proofreaders, typesetters, technical or \
managing editors and the like listed in the imprint.
- Books printed in Russia carry full imprint data (выходные данные, often on the \
back of the title page or on the last page): publisher, city, year, ISBN, page count. \
Use them.
- date = the year of this edition as printed on the title page or in the imprint \
line ("М.: Эксмо, 2022"); only if there is none, the year of "подписано в печать". \
Not the year of the original.
- publisher = the publisher's name only: no city, no quotes, no generic word \
before a name in quotes (Издательство «Эксмо» -> Эксмо; but Издательство \
Московского университета stays as it is, the word is part of that name).
- language = two-letter ISO 639-1 code: ru, en, fr, de.
- ISBN: only an ISBN of this book, printed on the back of the title page or in the \
imprint. The last pages often advertise other books with their ISBNs; ignore those \
pages entirely. One volume of a set: the ISBN of the volume, not of the set.
- abstractNote: an abstract or annotation printed in the document (abstract of an \
article, аннотация on the back of a Russian title page), copied verbatim in its \
original language. Never write a summary yourself; omit if none is printed.
- originalDate: for a translation or a reissue of an older work, the year the \
work was first published — as printed ("перевод с издания 1890 г."), or, for a \
well-known work, from your own knowledge if you are certain (Marx, Das Kapital, \
vol. 1 -> 1867). Omit otherwise.
- numPages only when a page count is printed in the book (e.g. "288 с.", \
"xii+288 pp."); never count pages of the file, scans and PDFs rarely match the edition.
- One volume of a multi-volume work: volume = its number in arabic digits \
("Том III", "Vol. 3", "Книга третья" -> "3"); keep the volume designation out of \
the title. Give numberOfVolumes if printed.
- A title printed in capitals is written in normal case ("КАПИТАЛ. КРИТИКА" -> \
"Капитал. Критика").
- Describe the document you are holding. For a translation, give the journal or \
book it appears in, its publisher and year; details of the original (often in a \
footnote: "first published in ...", "перевод по изданию ...") go only into \
originalDate. Never transliterate: a name printed in Cyrillic stays in Cyrillic.
- The file name and EPUB metadata (OPF) are hints, often wrong: shadow libraries \
put their site names there, and OPF dates are usually the date the file was made. \
When they disagree with the text, trust the text; never take a website as \
publisher or part of a title.
- An article from a journal (e.g. Математический сборник, Известия РАН, ТМФ, \
from mathnet.ru) is journalArticle; include publicationTitle, volume, issue, pages \
and DOI when printed.
{topics}- Omit any field you cannot read from the pages. Never guess an ISBN or DOI.
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


def page_image(path, p, tmp, label):
    out = Path(tmp) / label
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
        "language": m.get("language"), "abstractNote": m.get("abstract"),
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

def llm(prompt, images=()):
    """The model's reply as text. Images are read only by the built-in claude backend."""
    with tempfile.TemporaryDirectory(prefix="zai-") as empty:  # nothing for an agent to explore
        if LLM_CMD:
            if images:
                raise RuntimeError("no text layer; reading page images needs the claude backend")
            cmd = shlex.split(LLM_CMD)
            stdin = prompt
            if "{prompt}" in cmd:
                cmd = [prompt if a == "{prompt}" else a for a in cmd]
                stdin = None
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                               timeout=300, cwd=empty)
            if r.returncode:
                raise RuntimeError(f"{cmd[0]} failed: {(r.stderr or r.stdout)[:300]}")
            return r.stdout
        cmd = [tool("claude"), "-p", "--model", MODEL, "--output-format", "json",
               "--strict-mcp-config", "--no-session-persistence"]
        if images:
            cmd += ["--allowedTools", "Read", "--add-dir", str(images[0].parent)]
        else:
            cmd += ["--tools", ""]
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300,
                           cwd=images[0].parent if images else empty)
        try:
            out = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude failed: {(r.stderr or r.stdout)[:300]}")
        if out.get("is_error"):
            raise RuntimeError(f"claude: {out.get('result', '')[:300]}")
        log(f"  claude: ${out.get('total_cost_usd', 0):.3f}")
        return out.get("result", "")


def ask_topic(item):
    """One short call for items whose metadata came from Crossref."""
    desc = json.dumps({k: item.get(k) for k in ("itemType", "title", "publicationTitle",
                       "bookTitle", "proceedingsTitle")}, ensure_ascii=False)
    prompt = (f"Classify this bibliographic item.\n{topics_rule()}\n\nItem: {desc}\n\n"
              "Reply with the topic name only.")
    reply = llm(prompt).lower()
    return next((t for t in TOPICS if re.search(rf"\b{re.escape(t)}\b", reply)), None)


def ask_metadata(name, text, images):
    how = ""
    if images:
        how = (" — it is a scan without a text layer, so the pages are given as images; "
               "read each of these files with the Read tool: " + ", ".join(map(str, images)))
    names = " | ".join(f'"{t}"' for t in TOPICS)
    prompt = PROMPT.format(name=name, text=text or "(see images)", how=how,
                           types=" | ".join(FIELDS),
                           fields=", ".join(sorted({f for v in FIELDS.values() for f in v})),
                           topic_field=f'  "topic": {names},\n' if TOPICS else "",
                           topics=topics_rule() + "\n" if TOPICS else "")
    res = llm(prompt, images)
    m = re.search(r"\{.*\}", res, re.S)
    if not m:
        raise RuntimeError(f"no JSON in model reply: {res[:200]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected model reply: {res[:200]}")
    if "error" in data:
        raise RuntimeError(f"model could not identify the file: {data['error']}")
    return data


# ---------------------------------------------------------------- Zotero

TAG_RE = re.compile(r"</?[A-Za-z][\w:.-]*(\s[^<>]*)?/?>")  # <i>, </jats:p>, <mml:mi ...>
BLOCK_RE = re.compile(r"</?(?:\w+:)?(?:p|sec|div|br|li|list)\b[^<>]*>", re.I)
HEADING_RE = re.compile(r"<(\w+:)?title\b[^<>]*>.*?</(\w+:)?title>", re.I | re.S)  # "Abstract"


def text(v, sep="; "):
    """A field value as plain text: lists joined, markup and entities removed."""
    if isinstance(v, (list, tuple)):
        v = sep.join(str(x) for x in v if x not in (None, ""))
    v = BLOCK_RE.sub(" ", HEADING_RE.sub(" ", str(v)))
    v = html.unescape(TAG_RE.sub("", v))
    return re.sub(r"\s+", " ", v).strip()


def language(v):
    low = v.strip().lower()
    if re.fullmatch(r"[a-z]{2}([-_]\w+)?", low):  # ru, ru-RU
        return low[:2]
    return LANGUAGES.get(low, v.strip())


def publisher(v):
    m = re.fullmatch(r"(?:издательство|изд-во|publishing house)\s*[«\"“](.+)[»\"”]", v, re.I)
    return (m.group(1) if m else v).strip("«»\"“” ")


def clean(item):
    kind = item.get("itemType") if item.get("itemType") in FIELDS else "book"
    out = {"itemType": kind,
           "topic": item.get("topic") if item.get("topic") in TOPICS else None}
    for f in FIELDS[kind]:
        v = item.get(f)
        if v not in (None, "", []):
            v = text(v, " " if f == "ISBN" else "; ")
            if v:
                out[f] = v
    if not out.get("title"):
        raise RuntimeError("no title")
    if "language" in out:
        out["language"] = language(out["language"])
    if "publisher" in out:
        out["publisher"] = publisher(out["publisher"])
    if "originalDate" in out:  # Zotero has no such field; CSL and Better BibTeX read it from Extra
        out["extra"] = f"original-date: {out.pop('originalDate')}"
    out["creators"] = [
        {"lastName": text(c.get("lastName", "")), "firstName": text(c.get("firstName", "")),
         "creatorType": c["creatorType"]}
        for c in item.get("creators") or []
        if c.get("lastName") and c.get("creatorType", "author") in CREATOR_TYPES
        for c in [dict(c, creatorType=c.get("creatorType", "author"))]]
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


ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
         "ix": 9, "x": 10}


def volume_key(v):
    v = str(v or "").strip().lower()
    return str(ROMAN.get(v, v))


def year(date):
    m = re.search(r"\b(1[5-9]|20)\d\d\b", str(date or ""))
    return m.group(0) if m else None


def find_duplicate(item):
    """Key of an item with the same title, volume and (if both are known) year."""
    title = item["title"]
    q = urllib.parse.urlencode({"q": title, "itemType": "-attachment", "limit": 25,
                                "format": "json"})
    try:
        with urllib.request.urlopen(f"{ZOTERO}/api/users/0/items?{q}", timeout=20) as r:
            items = json.load(r)
    except Exception:
        return None
    norm = lambda s: re.sub(r"\W+", " ", s).strip().lower()
    for it in items:
        d = it["data"]
        if norm(d.get("title", "")) != norm(title):
            continue
        if volume_key(d.get("volume")) != volume_key(item.get("volume")):
            continue
        y1, y2 = year(d.get("date")), year(item.get("date"))
        if y1 and y2 and y1 != y2:
            continue
        return it["key"]
    return None


def save(item, path, topic, tags):
    sel = zpost("getSelectedCollection", {})
    ids = {t["name"]: t["id"] for t in sel["targets"]}
    name = TOPICS[topic][0] if topic in TOPICS and TOPICS[topic][0] in ids else COLLECTION
    target = ids.get(name)
    sid = f"autoimport-{time.time_ns()}"
    zitem = dict(item, id="it", attachments=[], notes=[])
    zpost("saveItems", {"sessionID": sid, "uri": "file:///", "items": [zitem]})
    # tags given in saveItems are dropped; the session's own tags stick
    session = {"sessionID": sid, "tags": ", ".join(tags)}
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
        return clean(ask_metadata(path.name, epub_text(path), []))
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
    # no PDF page numbers here: the model would report the file's length as numPages
    body = "\n".join([f"[page {i} from the start]\n{texts[p]}" for i, p in enumerate(head, 1)]
                     + [f"[page {n - p + 1} from the end]\n{texts[p]}" for p in tail])
    if len(re.sub(r"\s", "", body)) >= MIN_TEXT:
        return clean(ask_metadata(path.name, body[:40000], []))
    with tempfile.TemporaryDirectory(prefix="zai-") as tmp:
        imgs = ([page_image(path, p, tmp, f"start-{i}") for i, p in enumerate(head, 1)]
                + [page_image(path, p, tmp, f"end-{n - p + 1}") for p in tail])
        imgs = [i for i in imgs if i.exists()]
        if not imgs:
            raise RuntimeError("no text layer and page rendering failed")
        log(f"  no text layer, reading {len(imgs)} page images")
        return clean(ask_metadata(path.name, "", imgs))


def move_to(path, folder):
    folder.mkdir(exist_ok=True)
    dest = folder / path.name
    if dest.exists():
        dest = dest.with_name(f"{path.stem} {time.time_ns()}{path.suffix}")
    shutil.move(str(path), dest)


def process(path):
    log(f"{path.name}")
    item = metadata(path)
    topic = item.pop("topic") or (ask_topic(item) if TOPICS else None)
    who = ", ".join(c["lastName"] for c in item["creators"][:3])
    label = f"{who} — {item['title']}" if who else item["title"]
    if item.get("volume") and item["itemType"] == "book":
        label += f", vol. {item['volume']}"
    dup = find_duplicate(item)
    where = save(item, path, topic, [TAG, DUP_TAG] if dup else [TAG])
    log(f"  saved to '{where}': {item['itemType']}: {label} ({item.get('date', '?')})"
        + (f"; possible duplicate of {dup}" if dup else ""))
    notify(f"Zotero → {where}" + (" (possible duplicate)" if dup else ""), label)


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
            process(path)
            if not explicit:
                move_to(path, DONE)
        except Exception as e:
            log(f"  FAILED: {e}")
            notify("Zotero: import failed", f"{path.name}: {e}"[:200])
            if not explicit:
                move_to(path, FAILED)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
