# zotero-autoimport

A drop folder for Zotero on macOS. Put a PDF, DjVu or EPUB into
`~/Downloads/to-zotero`. A few seconds later it is a Zotero item with correct
metadata and the file attached, tagged `auto-import`, filed into a collection
by topic. A macOS notification tells you what was added.

It is meant for files the Zotero browser connector cannot save: books from
shadow libraries, scans without a text layer, articles from sites with no
Zotero translator (e.g. mathnet.ru), Russian-language books whose ISBNs are
missing from the usual catalogues.

## How metadata are found

For each file:

1. **DOI.** A DOI on the first two pages is looked up in Crossref. The record
   is accepted only if at least 60% of the words of its title (words longer
   than 3 letters) occur on those pages; this rejects DOIs of cited works.
2. **Otherwise, a model reads the document.** The first 4 and the last 3
   pages go to `claude -p` (Claude Code in non-interactive mode, model
   `haiku` by default). It returns Zotero fields: item type, creators, title,
   publisher, place, year, ISBN, journal, volume, pages. For scans without a
   text layer the pages are rendered to images and read as images; for EPUB
   the OPF metadata and the first chapters are used. The prompt asks for
   names and titles in the script of the document and for the imprint data
   of the edition at hand, not of the original.
3. **Topic.** The same call returns a topic, `math`, `nonfic` or `other`,
   which picks the collection.

An item whose title already exists in the library is not added again.

Cost: one model call per file without a usable DOI, typically $0.04–0.07.

## What happens to the file

| outcome | file moves to | Zotero |
|---|---|---|
| imported | `to-zotero/processed/` | new item + attachment |
| title already in library | `to-zotero/duplicates/` | unchanged |
| error | `to-zotero/failed/` | unchanged |

Every run is logged to `~/Library/Logs/zotero-autoimport.log`.

## Requirements

- macOS, Zotero 7 or later, running (the importer talks to it on
  `localhost:23119`; if Zotero is closed, files wait in the folder).
- Python 3.9+, standard library only.
- [Claude Code](https://claude.com/claude-code), logged in (`claude` on PATH).
- Poppler: `brew install poppler`. For DjVu also `brew install djvulibre`.

## Install

```sh
git clone https://github.com/kiktres/zotero-autoimport
cd zotero-autoimport
./install.sh                  # or ./install.sh /path/to/another/folder
```

`install.sh` writes a launchd agent to
`~/Library/LaunchAgents/local.zotero-autoimport.plist`. launchd starts the
script whenever the folder changes; between runs nothing is running. On the
first run macOS may ask whether `python3` may access the Downloads folder.

`./uninstall.sh` removes the agent. A single file can be imported by hand
with `./zotero_autoimport.py file.pdf` (the file is then left where it is).

## Settings

Environment variables; for the launchd agent add them to the
`EnvironmentVariables` block of the plist and rerun `install.sh`.

| variable | default | meaning |
|---|---|---|
| `ZAI_INBOX` | `~/Downloads/to-zotero` | drop folder |
| `ZAI_COLLECTION_MATH` | `Inbox math` | collection for topic `math` |
| `ZAI_COLLECTION_NONFIC` | `Inbox nonfiction` | collection for topic `nonfic` |
| `ZAI_COLLECTION_OTHER` | `Inbox other` | collection for topic `other` |
| `ZAI_COLLECTION_FALLBACK` | `Inbox` | used if the topic's collection does not exist |
| `ZAI_TAG` | `auto-import` | tag put on every imported item |
| `ZAI_MODEL` | `haiku` | model passed to `claude -p --model` |
| `ZAI_CROSSREF_MAILTO` | empty | your e-mail for Crossref's polite pool |

Collections are matched by name. If neither the topic's collection nor the
fallback exists, the item lands in the collection currently selected in
Zotero.

## How it writes to Zotero

Through the endpoints of Zotero's browser connector (`/connector/saveItems`,
`/connector/updateSession`, `/connector/saveAttachment`): no API key, no
authorization dialog, and the item goes through Zotero's normal save
pipeline, so it syncs like anything saved from the browser. These endpoints
are not a documented public API and may change between Zotero versions.

## Limitations

- The model sees 7 pages. If the imprint is elsewhere (common in scans), the
  year or publisher stays empty.
- Model output can be wrong in details: swapped first and last names, year of
  the original instead of the translation. The `auto-import` tag is there for
  a quick review.
- Duplicate detection compares titles only.

## License

MIT
