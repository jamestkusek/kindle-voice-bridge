"""Offline tests for the book library and the EPUB sync report.

Every fixture is a small EPUB archive built in ``tmp_path`` with ``zipfile``.
Nothing here touches the network or a real book directory.
"""

import json
import os
import pathlib
import zipfile
from xml.sax.saxutils import escape

import pytest

from voice_bridge_server import library as library_module
from voice_bridge_server.context import book_key
from voice_bridge_server.library import Library

FIXED_MTIME = 1_700_000_000

CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0"
    xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
        media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def xhtml(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>ignored</title></head>"
        f"<body>{body}</body></html>"
    )


def opf_xml(
    title: str,
    authors: tuple[str, ...],
    chapters: tuple[tuple[str, str], ...],
    order: tuple[int, ...] | None,
) -> str:
    creators = "\n    ".join(
        f"<dc:creator>{escape(author)}</dc:creator>" for author in authors
    )
    manifest = "\n    ".join(
        f'<item id="c{index}" href="{name}" '
        'media-type="application/xhtml+xml"/>'
        for index, (name, _) in enumerate(chapters)
    )
    spine_order = range(len(chapters)) if order is None else order
    spine = "\n    ".join(
        f'<itemref idref="c{index}"/>' for index in spine_order
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{escape(title)}</dc:title>
    {creators}
  </metadata>
  <manifest>
    {manifest}
  </manifest>
  <spine>
    {spine}
  </spine>
</package>
"""


def build_epub(
    path: pathlib.Path,
    *,
    title: str = "The Book",
    authors: tuple[str, ...] = ("A. Author",),
    chapters: tuple[tuple[str, str], ...] = (
        ("chapter1.xhtml", "<p>First paragraph.</p>"),
    ),
    order: tuple[int, ...] | None = None,
    encryption: bool = False,
    extra: tuple[tuple[str, str], ...] = (),
) -> pathlib.Path:
    """Write one minimal EPUB and return its path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype", "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
        archive.writestr(
            "OEBPS/content.opf", opf_xml(title, authors, chapters, order)
        )
        for name, body in chapters:
            archive.writestr(f"OEBPS/{name}", xhtml(body))
        if encryption:
            archive.writestr(
                "META-INF/encryption.xml",
                '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns'
                ':container"/>',
            )
        for name, body in extra:
            archive.writestr(name, body)
    return path


@pytest.fixture()
def library(tmp_path):
    return Library(tmp_path / "library")


@pytest.fixture()
def source(tmp_path):
    path = tmp_path / "books"
    path.mkdir()
    return path


def row_for(report: dict, relative: str) -> dict:
    matches = [row for row in report["rows"] if row["path"] == relative]
    assert len(matches) == 1
    return matches[0]


def meta_of(library: Library, key: str) -> dict:
    path = library.root / key / "meta.json"
    return json.loads(path.read_text(encoding="utf-8"))


def text_of(library: Library, key: str) -> dict:
    path = library.root / key / "text.json"
    return json.loads(path.read_text(encoding="utf-8"))


def book_dirs(library: Library) -> list[str]:
    return sorted(
        path.name
        for path in library.root.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def expected_key(title: str, authors: tuple[str, ...]) -> str:
    key = book_key({"title": title, "authors": list(authors)})
    assert key is not None
    return key


# New and duplicate --------------------------------------------------------


def test_new_epub_is_indexed(library, source):
    path = build_epub(
        source / "book.epub",
        title="The Book",
        authors=("A. Author", "B. Writer"),
    )
    key = expected_key("The Book", ("A. Author", "B. Writer"))

    report = library.sync(source)

    assert report["v"] == 1
    assert report["reason"] is None
    assert report["dry_run"] is False
    assert report["counts"]["new"] == 1
    row = row_for(report, "book.epub")
    assert row["status"] == "new"
    assert row["reason"] is None
    assert row["book_key"] == key
    assert isinstance(row["ms"], float)

    meta = meta_of(library, key)
    assert meta["key"] == key
    assert meta["title"] == "The Book"
    assert meta["authors"] == ["A. Author", "B. Writer"]
    assert meta["aliases"] == []
    assert meta["sizes"] == [path.stat().st_size]
    assert meta["format"] == "epub"
    assert meta["chars"] == len("First paragraph.")
    assert meta["source_path"] == "book.epub"
    assert meta["source_size"] == path.stat().st_size
    assert meta["source_mtime_ns"] == path.stat().st_mtime_ns
    assert meta["added_at"].endswith("+00:00")
    assert meta["updated_at"].endswith("+00:00")

    text = text_of(library, key)
    assert text == {
        "paragraphs": ["First paragraph."],
        "cum_chars": [0],
    }
    assert library.load(key)["meta"] == meta


def test_second_sync_is_duplicate(library, source):
    path = build_epub(source / "book.epub")
    os.utime(path, (FIXED_MTIME, FIXED_MTIME))
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))
    first = meta_of(library, key)

    report = library.sync(source)

    row = row_for(report, "book.epub")
    assert row["status"] == "duplicate"
    assert report["counts"]["new"] == 0
    assert report["counts"]["duplicate"] == 1
    assert meta_of(library, key) == first
    assert book_dirs(library) == [key]


def test_changed_file_is_re_extracted(library, source):
    path = build_epub(
        source / "book.epub",
        chapters=(("chapter1.xhtml", "<p>Old text.</p>"),),
    )
    os.utime(path, (FIXED_MTIME, FIXED_MTIME))
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))
    first = meta_of(library, key)
    old_size = first["source_size"]

    build_epub(
        source / "book.epub",
        chapters=(("chapter1.xhtml", "<p>New and longer text.</p>"),),
    )
    os.utime(path, (FIXED_MTIME + 10, FIXED_MTIME + 10))

    report = library.sync(source)

    row = row_for(report, "book.epub")
    assert row["status"] == "changed"
    assert row["book_key"] == key
    meta = meta_of(library, key)
    assert meta["added_at"] == first["added_at"]
    assert meta["chars"] == len("New and longer text.")
    assert meta["sizes"] == [old_size, path.stat().st_size]
    assert text_of(library, key)["paragraphs"] == ["New and longer text."]


def test_removed_book_keeps_its_folder(library, source):
    first = build_epub(source / "first.epub", title="First")
    second = build_epub(source / "second.epub", title="Second")
    os.utime(first, (FIXED_MTIME, FIXED_MTIME))
    os.utime(second, (FIXED_MTIME, FIXED_MTIME))
    library.sync(source)
    removed_key = expected_key("Second", ("A. Author",))
    kept_key = expected_key("First", ("A. Author",))

    second.unlink()
    report = library.sync(source)

    removed = [row for row in report["rows"] if row["status"] == "removed"]
    assert len(removed) == 1
    assert removed[0]["path"] == "second.epub"
    assert removed[0]["book_key"] == removed_key
    assert "second.epub" not in [row["path"] for row in report["rows"]
                                 if row["status"] != "removed"]
    assert report["counts"]["removed"] == 1
    assert report["counts"]["duplicate"] == 1
    assert row_for(report, "first.epub")["book_key"] == kept_key
    assert book_dirs(library) == sorted([kept_key, removed_key])
    assert (library.root / removed_key / "text.json").is_file()


def test_parse_failure_keeps_the_previous_copy(library, source):
    path = build_epub(source / "book.epub")
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))
    path.write_bytes(b"not a zip file")

    report = library.sync(source)

    row = row_for(report, "book.epub")
    assert row["status"] == "parse_failed"
    assert row["reason"] == "bad_zip"
    assert report["counts"]["removed"] == 0
    assert text_of(library, key)["paragraphs"] == ["First paragraph."]


# Report rows --------------------------------------------------------------


def test_unsupported_files_are_reported_in_path_order(library, source):
    build_epub(source / "b.epub", title="Bee")
    (source / "a.txt").write_text("notes", encoding="utf-8")
    (source / "sub").mkdir()
    (source / "sub" / "cover.jpg").write_bytes(b"\xff\xd8")

    report = library.sync(source)

    assert [row["path"] for row in report["rows"]] == [
        "a.txt",
        "b.epub",
        "sub/cover.jpg",
    ]
    assert row_for(report, "a.txt")["status"] == "unsupported"
    assert row_for(report, "a.txt")["reason"] == "format"
    assert row_for(report, "sub/cover.jpg")["status"] == "unsupported"
    assert report["counts"]["unsupported"] == 2
    assert report["counts"]["new"] == 1


def test_extension_match_is_case_insensitive(library, source):
    build_epub(source / "BOOK.EPUB")

    report = library.sync(source)

    assert row_for(report, "BOOK.EPUB")["status"] == "new"


def test_empty_source_produces_an_empty_report(library, source):
    report = library.sync(source)

    assert report["rows"] == []
    assert report["reason"] is None
    assert set(report["counts"].values()) == {0}
    assert library.list_books() == []
    assert library.last_report()["rows"] == []


# Failures -----------------------------------------------------------------


def test_drm_archive_is_reported_and_not_indexed(library, source):
    build_epub(source / "locked.epub", encryption=True)

    report = library.sync(source)

    row = row_for(report, "locked.epub")
    assert row["status"] == "drm"
    assert row["reason"] is None
    assert row["book_key"] is None
    assert report["counts"]["drm"] == 1
    assert book_dirs(library) == []
    assert library.list_books() == []


def test_truncated_archive_is_parse_failed(library, source):
    path = build_epub(source / "broken.epub")
    body = path.read_bytes()
    path.write_bytes(body[: len(body) // 2])

    report = library.sync(source)

    row = row_for(report, "broken.epub")
    assert row["status"] == "parse_failed"
    assert row["reason"] == "bad_zip"
    assert book_dirs(library) == []


def test_zip_entry_limit_is_too_large(library, source, monkeypatch):
    monkeypatch.setattr(library_module, "MAX_ZIP_ENTRIES", 10)
    extras = tuple((f"extra{index}.bin", "x") for index in range(7))
    build_epub(source / "crowded.epub", extra=extras)

    report = library.sync(source)

    row = row_for(report, "crowded.epub")
    assert row["status"] == "parse_failed"
    assert row["reason"] == "too_large"
    assert book_dirs(library) == []


def test_decompressed_limit_is_too_large(library, source, monkeypatch):
    monkeypatch.setattr(library_module, "MAX_DECOMPRESSED_BYTES", 100)
    build_epub(source / "big.epub")

    report = library.sync(source)

    row = row_for(report, "big.epub")
    assert row["status"] == "parse_failed"
    assert row["reason"] == "too_large"
    assert book_dirs(library) == []


def test_extracted_text_limit_is_too_large(library, source, monkeypatch):
    monkeypatch.setattr(library_module, "MAX_TEXT_CHARS", 20)
    build_epub(
        source / "long.epub",
        chapters=(
            ("chapter1.xhtml", "<p>This paragraph is longer than twenty.</p>"),
        ),
    )

    report = library.sync(source)

    row = row_for(report, "long.epub")
    assert row["status"] == "parse_failed"
    assert row["reason"] == "too_large"
    assert book_dirs(library) == []


# Extraction ---------------------------------------------------------------


def test_paragraphs_follow_spine_order(library, source):
    build_epub(
        source / "book.epub",
        chapters=(
            ("one.xhtml", "<p>One</p>"),
            ("two.xhtml", "<p>Two</p>"),
            ("three.xhtml", "<p>Three</p>"),
        ),
        order=(2, 0, 1),
    )

    library.sync(source)

    key = expected_key("The Book", ("A. Author",))
    text = text_of(library, key)
    assert text["paragraphs"] == ["Three", "One", "Two"]
    assert text["cum_chars"] == [0, 5, 8]


def test_html_blocks_and_skipped_elements(library, source):
    build_epub(
        source / "book.epub",
        chapters=(
            (
                "chapter1.xhtml",
                "<div><p>Alpha <em>beta</em></p><script>bad()</script>"
                "<p>Gamma</p><p>  spaced\n words </p></div>",
            ),
        ),
    )

    library.sync(source)

    key = expected_key("The Book", ("A. Author",))
    assert text_of(library, key)["paragraphs"] == [
        "Alpha beta",
        "Gamma",
        "spaced words",
    ]


def test_metadata_title_and_authors_are_normalised(library, source):
    build_epub(
        source / "book.epub",
        title="  Tom & Jerry\n",
        authors=("  First  Writer ", "Second Writer", "First Writer"),
    )

    report = library.sync(source)

    key = expected_key("Tom & Jerry", ("First Writer", "Second Writer"))
    row = row_for(report, "book.epub")
    assert row["book_key"] == key
    meta = meta_of(library, key)
    assert meta["title"] == "Tom & Jerry"
    assert meta["authors"] == ["First Writer", "Second Writer"]


# Dry run ------------------------------------------------------------------


def test_dry_run_computes_the_report_without_writing(library, source):
    build_epub(source / "book.epub")
    key = expected_key("The Book", ("A. Author",))

    report = library.sync(source, dry_run=True)

    assert report["dry_run"] is True
    assert row_for(report, "book.epub")["status"] == "new"
    assert report["counts"]["new"] == 1
    assert not library.root.exists()
    assert library.last_report() is None

    real = library.sync(source)
    assert real["dry_run"] is False
    assert row_for(real, "book.epub")["status"] == "new"
    assert book_dirs(library) == [key]


def test_dry_run_leaves_a_changed_book_untouched(library, source):
    path = build_epub(source / "book.epub")
    os.utime(path, (FIXED_MTIME, FIXED_MTIME))
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))
    before = meta_of(library, key)

    build_epub(
        source / "book.epub",
        chapters=(("chapter1.xhtml", "<p>Replacement text.</p>"),),
    )
    os.utime(path, (FIXED_MTIME + 10, FIXED_MTIME + 10))

    report = library.sync(source, dry_run=True)

    assert row_for(report, "book.epub")["status"] == "changed"
    assert meta_of(library, key) == before
    assert text_of(library, key)["paragraphs"] == ["First paragraph."]
    assert library.last_report() is not None


def test_missing_source_is_reported_without_an_exception(library, tmp_path):
    report = library.sync(tmp_path / "missing")

    assert report["reason"] == "source_missing"
    assert report["rows"] == []
    assert set(report["counts"].values()) == {0}
    assert report["finished_at"] is not None
    assert library.last_report()["reason"] == "source_missing"


def test_missing_source_dry_run_writes_nothing(library, tmp_path):
    report = library.sync(tmp_path / "missing", dry_run=True)

    assert report["reason"] == "source_missing"
    assert not library.root.exists()


# Reports ------------------------------------------------------------------


def test_last_report_is_the_newest_file(library, source):
    build_epub(source / "book.epub")
    os.utime(source / "book.epub", (FIXED_MTIME, FIXED_MTIME))
    first = library.sync(source)
    second = library.sync(source)

    assert first["counts"]["new"] == 1
    assert second["counts"]["duplicate"] == 1
    latest = library.last_report()
    assert latest["counts"] == second["counts"]
    assert latest["rows"] == second["rows"]
    reports = sorted((library.root / ".sync").glob("*.json"))
    assert len(reports) == 2


def test_last_report_is_none_when_absent(library):
    assert library.last_report() is None


# Queries ------------------------------------------------------------------


def test_list_books_is_sorted_by_title(library, source):
    build_epub(source / "zebra.epub", title="Zebra")
    build_epub(source / "apple.epub", title="Apple")

    library.sync(source)

    assert [meta["title"] for meta in library.list_books()] == ["Apple", "Zebra"]


def test_load_returns_meta_and_text(library, source):
    build_epub(source / "book.epub")
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))

    loaded = library.load(key)

    assert loaded["meta"]["key"] == key
    assert loaded["text"]["paragraphs"] == ["First paragraph."]


@pytest.mark.parametrize(
    "bad_key", [None, "", "../escape", "bk_0123456789ABCDEF", "bk_short", "other"]
)
def test_load_rejects_bad_keys(library, bad_key):
    assert library.load(bad_key) is None


def test_load_unknown_key_returns_none(library):
    assert library.load("bk_" + "0" * 16) is None


def test_find_matches_normalised_title_and_authors(library, source):
    build_epub(source / "book.epub", title="The Book", authors=("A. Author",))
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))

    assert library.find("The Book", ["A. Author"]) == key
    assert library.find("  the   BOOK ", "a. AUTHOR") == key
    assert library.find("Other Book", ["A. Author"]) is None
    assert library.find("The Book", ["Other Author"]) is None


def test_find_matches_an_md5_alias(library, source):
    build_epub(source / "book.epub")
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))
    meta_path = library.root / key / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["aliases"] = ["abcdef0123456789"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert library.find("Other", [], partial_md5="ABCDEF0123456789") == key
    assert library.find("Other", [], partial_md5="0000000000000000") is None


def test_find_matches_a_size_alias(library, source):
    path = build_epub(source / "book.epub")
    library.sync(source)
    key = expected_key("The Book", ("A. Author",))

    assert library.find("Other", [], size_bytes=path.stat().st_size) == key
    assert library.find("Other", [], size_bytes=1) is None


def test_find_prefers_an_alias_over_a_size(library, source):
    build_epub(source / "a.epub", title="Alpha")
    second = build_epub(source / "b.epub", title="Beta")
    library.sync(source)
    second_key = expected_key("Beta", ("A. Author",))
    meta_path = library.root / second_key / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["aliases"] = ["deadbeefdeadbeef"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    found = library.find(
        "No Match",
        [],
        size_bytes=second.stat().st_size,
        partial_md5="deadbeefdeadbeef",
    )

    assert found == second_key
