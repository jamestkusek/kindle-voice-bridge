"""Local book library and EPUB sync.

The library keeps one folder per book under the storage root::

    <root>/<book_key>/meta.json
    <root>/<book_key>/text.json
    <root>/.sync/<timestamp>.json

``book_key`` comes from :func:`voice_bridge_server.context.book_key`, so the
Kindle context and the server agree on identity. Size and partial md5 are
aliases for matching a stored copy, never identity.

Text extraction uses only the standard library: ``zipfile`` for the archive,
``xml.etree.ElementTree`` for the container and OPF, and ``html.parser`` for the
XHTML. Every read is bounded by three limits: entry count, decompressed bytes
and extracted characters. An archive over a limit fails as ``too_large``.

A sync report records one row per file with ``new``, ``duplicate``, ``changed``,
``removed``, ``unsupported``, ``drm`` or ``parse_failed``. A failed extraction
never creates a book folder, and a removed book never deletes its folder.
``dry_run`` computes the same report and writes nothing.
"""

from __future__ import annotations

import json
import os
import pathlib
import posixpath
import re
import tempfile
import time
import zipfile
import zlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree

from .context import book_key

#: Schema version of the sync report.
LIBRARY_VERSION = 1

#: File names inside one book folder and the report directory.
META_NAME = "meta.json"
TEXT_NAME = "text.json"
SYNC_DIR_NAME = ".sync"

#: Most zip entries one EPUB may hold.
MAX_ZIP_ENTRIES = 2000

#: Most decompressed bytes one EPUB may produce.
MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024

#: Most extracted characters one EPUB may produce.
MAX_TEXT_CHARS = 2 * 1024 * 1024

#: Confirmed statuses of one sync row.
STATUSES = (
    "new",
    "duplicate",
    "changed",
    "removed",
    "unsupported",
    "drm",
    "parse_failed",
)

#: A book key is always ``bk_`` plus 16 lowercase hex characters.
_KEY_PATTERN = re.compile(r"bk_[0-9a-f]{16}")

_CONTAINER_PATH = "META-INF/container.xml"
_ENCRYPTION_PATH = "meta-inf/encryption.xml"
_OPF_MEDIA_TYPE = "application/oebps-package+xml"

#: Manifest media types whose content is XHTML text.
_HTML_TYPES = frozenset(
    {"application/xhtml+xml", "text/html", "application/x-dtbook+xml"}
)

#: Errors raised by reading a member of a zip archive.
_ZIP_READ_ERRORS = (
    zipfile.BadZipFile,
    zlib.error,
    OSError,
    RuntimeError,
    NotImplementedError,
)

#: Elements whose content is never book text.
_SKIP_TAGS = frozenset({"head", "script", "style", "title", "svg"})

#: Elements that end a paragraph.
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "br", "dd", "div",
        "dl", "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
        "section", "table", "td", "th", "tr", "ul",
    }
)


class _ExtractError(Exception):
    """One EPUB could not be read. ``reason`` is a report reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Library:
    """Read and write the book store under ``root``.

    ``sync`` is the only writer. ``list_books``, ``load`` and ``find`` only
    read. A missing or unreadable file returns ``None`` or an empty list
    rather than raising.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = pathlib.Path(root)

    @property
    def root(self) -> pathlib.Path:
        """Directory that holds the book folders and the sync reports."""

        return self._root

    # Sync -----------------------------------------------------------------

    def sync(
        self, source_dir: pathlib.Path, *, dry_run: bool = False
    ) -> dict:
        """Scan ``source_dir`` for EPUB files and update the library.

        Returns the report that is written under ``.sync`` unless ``dry_run``.
        A missing source directory is reported as ``source_missing`` and is not
        an error.
        """

        source = pathlib.Path(source_dir)
        report = {
            "v": LIBRARY_VERSION,
            "dry_run": bool(dry_run),
            "source_dir": str(source),
            "started_at": _utc_now(),
            "finished_at": None,
            "reason": None,
            "counts": _empty_counts(),
            "rows": [],
        }

        if not source.is_dir():
            report["reason"] = "source_missing"
            report["finished_at"] = _utc_now()
            if not dry_run:
                self._save_report(report)
            return report

        entries = _scan_entries(source)
        indexed = self._meta_by_source_path()
        seen_keys: set[str] = set()

        for path, relative in entries:
            started = time.perf_counter()
            if path.suffix.lower() != ".epub":
                report["rows"].append(
                    _row(relative, "unsupported", "format", None, started)
                )
                continue

            previous_for_path = indexed.get(relative)
            if previous_for_path is not None:
                seen_keys.add(previous_for_path["key"])

            try:
                extracted = _extract_epub(path)
            except _ExtractError as error:
                if error.reason == "drm":
                    report["rows"].append(
                        _row(relative, "drm", None, None, started)
                    )
                else:
                    report["rows"].append(
                        _row(relative, "parse_failed", error.reason, None, started)
                    )
                continue

            key = book_key(
                {"title": extracted["title"], "authors": extracted["authors"]}
            )
            if key is None:
                report["rows"].append(
                    _row(relative, "parse_failed", "no_metadata", None, started)
                )
                continue
            seen_keys.add(key)

            try:
                stat = path.stat()
            except OSError:
                report["rows"].append(
                    _row(relative, "parse_failed", "read_error", key, started)
                )
                continue

            existing = self._read_meta(key)
            if existing is None:
                status = "new"
            elif (
                existing.get("source_size") == stat.st_size
                and existing.get("source_mtime_ns") == stat.st_mtime_ns
            ):
                status = "duplicate"
            else:
                status = "changed"

            if status != "duplicate" and not dry_run:
                try:
                    self._write_book(key, extracted, relative, stat, existing)
                except OSError:
                    report["rows"].append(
                        _row(relative, "parse_failed", "write_failed", key, started)
                    )
                    continue

            report["rows"].append(_row(relative, status, None, key, started))

        removed = []
        for meta in self._iter_meta():
            key = meta.get("key")
            if not isinstance(key, str) or key in seen_keys:
                continue
            removed.append(
                {
                    "path": _as_text(meta.get("source_path")) or key,
                    "status": "removed",
                    "reason": None,
                    "book_key": key,
                    "ms": 0.0,
                }
            )
        removed.sort(key=lambda row: row["path"])
        report["rows"].extend(removed)

        report["counts"] = _count_rows(report["rows"])
        report["finished_at"] = _utc_now()
        if not dry_run:
            self._save_report(report)
        return report

    # Reads ----------------------------------------------------------------

    def list_books(self) -> list[dict]:
        """Return every stored book, sorted by title and then by key."""

        books = self._iter_meta()
        books.sort(
            key=lambda meta: (
                _as_text(meta.get("title")).casefold(),
                _as_text(meta.get("key")),
            )
        )
        return books

    def load(self, book_key: str) -> dict | None:
        """Return ``{"meta": {...}, "text": {...}}``, or ``None`` if unknown."""

        key = _safe_key(book_key)
        if key is None:
            return None
        meta = self._read_meta(key)
        if meta is None:
            return None

        text = _read_json(self._root / key / TEXT_NAME)
        if not isinstance(text, dict):
            text = {}
        paragraphs = text.get("paragraphs")
        if not isinstance(paragraphs, list):
            paragraphs = []
        paragraphs = [item for item in paragraphs if isinstance(item, str)]
        cum_chars = text.get("cum_chars")
        if not _valid_offsets(cum_chars, len(paragraphs)):
            cum_chars = _cum_chars(paragraphs)
        return {
            "meta": meta,
            "text": {"paragraphs": paragraphs, "cum_chars": cum_chars},
        }

    def find(
        self,
        title: object,
        authors: object,
        *,
        size_bytes: int | None = None,
        partial_md5: str | None = None,
    ) -> str | None:
        """Return the key of a stored book matching one context, or ``None``.

        The normalised title and authors are tried first, then the md5 alias,
        then the file size alias.
        """

        key = book_key(
            {
                "title": title if isinstance(title, str) else "",
                "authors": authors,
            }
        )
        if key is not None and self._read_meta(key) is not None:
            return key

        needle = partial_md5.casefold() if isinstance(partial_md5, str) else ""
        if needle:
            for meta in self._iter_meta():
                aliases = meta.get("aliases")
                if not isinstance(aliases, list):
                    continue
                if any(
                    isinstance(alias, str) and alias.casefold() == needle
                    for alias in aliases
                ):
                    stored = _safe_key(meta.get("key"))
                    if stored is not None:
                        return stored

        if isinstance(size_bytes, int) and not isinstance(size_bytes, bool):
            for meta in self._iter_meta():
                sizes = meta.get("sizes")
                if not isinstance(sizes, list):
                    continue
                if size_bytes in sizes:
                    stored = _safe_key(meta.get("key"))
                    if stored is not None:
                        return stored

        return None

    def last_report(self) -> dict | None:
        """Return the newest sync report, or ``None`` when there is none."""

        try:
            paths = sorted(
                (self._root / SYNC_DIR_NAME).glob("*.json"),
                key=lambda path: path.name,
            )
        except OSError:  # pragma: no cover - an unreadable report directory
            return None
        for path in reversed(paths):
            report = _read_json(path)
            if report is not None:
                return report
        return None

    # Book files -----------------------------------------------------------

    def _book_dir(self, key: str) -> pathlib.Path:
        return self._root / key

    def _read_meta(self, key: str) -> dict | None:
        if _safe_key(key) is None:
            return None
        meta = _read_json(self._book_dir(key) / META_NAME)
        return meta if isinstance(meta, dict) else None

    def _iter_meta(self) -> list[dict]:
        """Return every readable ``meta.json``, sorted by key."""

        metas: list[dict] = []
        try:
            entries = sorted(self._root.iterdir(), key=lambda path: path.name)
        except OSError:
            return metas
        for entry in entries:
            if _safe_key(entry.name) is None:
                continue
            meta = _read_json(entry / META_NAME)
            if isinstance(meta, dict):
                metas.append(meta)
        return metas

    def _meta_by_source_path(self) -> dict[str, dict]:
        """Index the stored books by their recorded relative source path."""

        indexed: dict[str, dict] = {}
        for meta in self._iter_meta():
            source_path = _as_text(meta.get("source_path"))
            key = _safe_key(meta.get("key"))
            if source_path and key is not None:
                indexed[source_path] = meta
        return indexed

    def _write_book(
        self,
        key: str,
        extracted: dict,
        relative: str,
        stat: os.stat_result,
        existing: dict | None,
    ) -> None:
        """Write ``text.json`` then ``meta.json`` for one extracted book."""

        now = _utc_now()
        directory = self._book_dir(key)
        paragraphs = extracted["paragraphs"]

        text = {"paragraphs": paragraphs, "cum_chars": _cum_chars(paragraphs)}
        _write_json(directory / TEXT_NAME, text)

        raw_aliases = existing.get("aliases") if existing else None
        aliases = raw_aliases if isinstance(raw_aliases, list) else []
        aliases = [alias for alias in aliases if isinstance(alias, str)]

        raw_sizes = existing.get("sizes") if existing else None
        sizes = raw_sizes if isinstance(raw_sizes, list) else []
        sizes = [size for size in sizes if _is_int(size)]
        if stat.st_size not in sizes:
            sizes.append(stat.st_size)

        added_at = _as_text(existing.get("added_at")) if existing else ""
        meta = {
            "key": key,
            "title": extracted["title"],
            "authors": extracted["authors"],
            "aliases": aliases,
            "sizes": sizes,
            "format": "epub",
            "chars": extracted["chars"],
            "added_at": added_at or now,
            "updated_at": now,
            "source_path": relative,
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
        }
        _write_json(directory / META_NAME, meta)

    def _save_report(self, report: dict) -> None:
        # The Windows wall clock is coarse enough that two fast syncs can share
        # a tick. The high-resolution counter keeps names unique and ordered.
        stamp = f"{time.time_ns():020d}-{time.perf_counter_ns():020d}"
        path = self._root / SYNC_DIR_NAME / f"{stamp}.json"
        _write_json(path, report)


# Candidate scanning -------------------------------------------------------


def _scan_entries(source: pathlib.Path) -> list[tuple[pathlib.Path, str]]:
    """Return every file under ``source`` as ``(path, relative posix path)``.

    The list is sorted by relative path so two scans of the same tree produce
    the same row order.
    """

    entries: list[tuple[pathlib.Path, str]] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(source).as_posix()
        except ValueError:  # pragma: no cover - defensive
            continue
        entries.append((path, relative))
    entries.sort(key=lambda entry: entry[1])
    return entries


# EPUB extraction ----------------------------------------------------------


class _Archive:
    """Read one zip archive within the decompressed-byte limit."""

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        self._names = set(archive.namelist())
        self._remaining = MAX_DECOMPRESSED_BYTES

    def resolve(self, name: str) -> str | None:
        """Return the member name that matches ``name``, or ``None``."""

        for candidate in (name, unquote(name), name.lstrip("./")):
            if candidate in self._names:
                return candidate
        return None

    def read(self, name: str, *, required: bool = True) -> bytes | None:
        """Read one member and charge it against the remaining budget."""

        resolved = self.resolve(name)
        if resolved is None:
            if required:
                raise _ExtractError("bad_epub")
            return None
        try:
            info = self._archive.getinfo(resolved)
        except KeyError:  # pragma: no cover - resolve already checked
            if required:
                raise _ExtractError("bad_epub") from None
            return None
        if info.file_size > self._remaining:
            raise _ExtractError("too_large")
        try:
            data = self._archive.read(resolved)
        except _ZIP_READ_ERRORS as error:
            raise _ExtractError("bad_zip") from error
        if len(data) > self._remaining:
            raise _ExtractError("too_large")
        self._remaining -= len(data)
        return data


def _extract_epub(path: pathlib.Path) -> dict:
    """Return ``title``, ``authors``, ``paragraphs`` and ``chars`` for one file.

    Raises :class:`_ExtractError` with a report reason when the archive cannot
    be read or exceeds a limit.
    """

    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        raise _ExtractError("bad_zip") from error

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise _ExtractError("too_large")
        if sum(info.file_size for info in infos) > MAX_DECOMPRESSED_BYTES:
            raise _ExtractError("too_large")
        if any(
            info.filename.casefold() == _ENCRYPTION_PATH for info in infos
        ):
            raise _ExtractError("drm")

        reader = _Archive(archive)
        try:
            opf_name = _find_opf(reader)
            title, authors, manifest, spine = _read_opf(reader, opf_name)
            paragraphs = _read_spine(reader, opf_name, manifest, spine)
        except (ElementTree.ParseError, KeyError, ValueError) as error:
            raise _ExtractError("bad_epub") from error

    return {
        "title": title,
        "authors": authors,
        "paragraphs": paragraphs,
        "chars": sum(len(paragraph) for paragraph in paragraphs),
    }


def _find_opf(reader: _Archive) -> str:
    """Return the OPF path named by ``META-INF/container.xml``."""

    data = reader.read(_CONTAINER_PATH)
    root = ElementTree.fromstring(data)
    fallback = None
    for element in root.iter():
        if _local_name(element.tag) != "rootfile":
            continue
        full_path = element.get("full-path")
        if not full_path:
            continue
        if element.get("media-type") == _OPF_MEDIA_TYPE:
            return full_path
        if fallback is None:
            fallback = full_path
    if fallback is not None:
        return fallback
    raise _ExtractError("bad_epub")


def _read_opf(
    reader: _Archive, opf_name: str
) -> tuple[str, list[str], dict[str, dict], list[str]]:
    """Return ``(title, authors, manifest, spine)`` from one OPF document."""

    data = reader.read(opf_name)
    root = ElementTree.fromstring(data)

    title = ""
    authors: list[str] = []
    manifest: dict[str, dict] = {}
    spine: list[str] = []

    for element in root.iter():
        name = _local_name(element.tag)
        if name == "title":
            if not title:
                title = _clean_text(element)
        elif name == "creator":
            author = _clean_text(element)
            if author and author not in authors:
                authors.append(author)
        elif name == "item":
            item_id = element.get("id")
            href = element.get("href")
            if item_id and href:
                manifest[item_id] = {
                    "href": href,
                    "media_type": (element.get("media-type") or "").strip(),
                    "properties": (element.get("properties") or "").strip(),
                }
        elif name == "itemref":
            idref = element.get("idref")
            if idref:
                spine.append(idref)

    if not spine:
        spine = [
            item_id
            for item_id, item in manifest.items()
            if item["media_type"] in _HTML_TYPES
        ]
    return title, authors, manifest, spine


def _read_spine(
    reader: _Archive,
    opf_name: str,
    manifest: dict[str, dict],
    spine: list[str],
) -> list[str]:
    """Return the paragraphs of every spine document, in spine order."""

    paragraphs: list[str] = []
    chars = 0
    base = posixpath.dirname(opf_name)

    for idref in spine:
        item = manifest.get(idref)
        if item is None or item["media_type"] not in _HTML_TYPES:
            continue
        member = _member_name(base, item["href"])
        if member is None:
            continue
        data = reader.read(member, required=False)
        if data is None:
            continue
        for paragraph in _parse_html(data):
            chars += len(paragraph)
            if chars > MAX_TEXT_CHARS:
                raise _ExtractError("too_large")
            paragraphs.append(paragraph)
    return paragraphs


def _member_name(base: str, href: str) -> str | None:
    """Resolve a manifest href against the OPF directory."""

    path = href.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return None
    joined = posixpath.normpath(posixpath.join(base, unquote(path)))
    if joined == ".." or joined.startswith("../"):
        return None
    return joined.lstrip("/")


def _parse_html(data: bytes) -> list[str]:
    """Return the paragraphs of one XHTML document."""

    parser = _TextParser()
    parser.feed(data.decode("utf-8-sig", "replace"))
    parser.close()
    return parser.paragraphs


class _TextParser(HTMLParser):
    """Collect visible text as paragraphs, skipping head, script and style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._current: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._current.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        text = " ".join("".join(self._current).split())
        if text:
            self.paragraphs.append(text)
        self._current = []


def _local_name(tag: object) -> str:
    """Return the local part of an XML tag such as ``{ns}title``."""

    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _clean_text(element: ElementTree.Element) -> str:
    """Flatten one element's text to a single line."""

    return " ".join("".join(element.itertext()).split())


# Small helpers ------------------------------------------------------------


def _valid_offsets(value: object, count: int) -> bool:
    """Return True for a ``cum_chars`` list that fits ``count`` paragraphs."""

    if not isinstance(value, list) or len(value) != count:
        return False
    previous = -1
    for offset in value:
        if not _is_int(offset) or offset < previous:
            return False
        previous = offset
    return True


def _cum_chars(paragraphs: list[str]) -> list[int]:
    """Return the start offset of each paragraph in the joined text."""

    offsets: list[int] = []
    total = 0
    for paragraph in paragraphs:
        offsets.append(total)
        total += len(paragraph)
    return offsets


def _row(
    path: str,
    status: str,
    reason: str | None,
    book_key: str | None,
    started: float,
) -> dict:
    return {
        "path": path,
        "status": status,
        "reason": reason,
        "book_key": book_key,
        "ms": round((time.perf_counter() - started) * 1000.0, 3),
    }


def _count_rows(rows: list[dict]) -> dict[str, int]:
    counts = _empty_counts()
    for row in rows:
        status = row.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _empty_counts() -> dict[str, int]:
    return {status: 0 for status in STATUSES}


def _safe_key(value: object) -> str | None:
    if isinstance(value, str) and _KEY_PATTERN.fullmatch(value):
        return value
    return None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: pathlib.Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: pathlib.Path, data: dict) -> None:
    """Write ``data`` through a temporary file and an atomic rename."""

    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=2, sort_keys=True) + "\n"
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=".library-", suffix=".part"
    )
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
