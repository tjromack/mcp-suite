"""The ranged-zip reader is the piece that makes a 2.5 GB archive a 330 MB pull.

If it mis-parses the central directory it reads the wrong bytes and every row
downstream is garbage, so these tests target that: real archives built by
`zipfile`, both compression methods, and the line-splitting edge cases the two
upstreams actually ship (CRLF from FAERS, a BOM, no trailing newline).
"""

from __future__ import annotations

import time

import pytest

from staging.remote_zip import RemoteZip, UnsupportedZipError
from tests.staging.conftest import BytesRangeFetcher, build_zip


def _archive(members: dict[str, bytes], *, compress: bool = True) -> tuple[RemoteZip, object]:
    fetcher = BytesRangeFetcher(build_zip(members, compress=compress))
    return RemoteZip(fetcher), fetcher


def test_lists_members_with_sizes() -> None:
    archive, _ = _archive({"a.txt": b"hello\n", "dir/b.txt": b"x" * 1000})
    assert set(archive.members) == {"a.txt", "dir/b.txt"}
    assert archive.members["dir/b.txt"].uncompressed_size == 1000


def test_directory_entries_are_not_members() -> None:
    # FAERS archives carry explicit `ASCII/` and `Deleted/` directory entries;
    # treating one as a file would hand the loader an empty member.
    archive, _ = _archive({"ASCII/": b"", "ASCII/DEMO25Q1.txt": b"primaryid\n1\n"})
    assert list(archive.members) == ["ASCII/DEMO25Q1.txt"]


@pytest.mark.parametrize("compress", [True, False])
def test_reads_body_under_both_compression_methods(compress: bool) -> None:
    body = b"".join(f"line {i}\n".encode() for i in range(500))
    archive, _ = _archive({"big.txt": body}, compress=compress)
    assert list(archive.iter_lines("big.txt")) == [f"line {i}" for i in range(500)]


def test_only_the_wanted_member_is_transferred() -> None:
    """The whole point: unread members cost nothing but their directory entry."""
    wanted = b"keep\n" * 100
    unwanted = b"z" * 2_000_000  # incompressible-ish filler
    archive, fetcher = _archive({"small.txt": wanted, "huge.bin": unwanted}, compress=False)

    before = fetcher.bytes_served
    list(archive.iter_lines("small.txt"))
    transferred = fetcher.bytes_served - before

    assert transferred < 10_000, f"read {transferred} bytes for a {len(wanted)}-byte member"


def test_find_matches_on_basename_case_insensitively() -> None:
    # FAERS nests under `ASCII/` and upper-cases the quarter; AACT puts files
    # at the root. Callers should not have to know which.
    archive, _ = _archive({"ASCII/DEMO25Q1.txt": b"h\n1\n"})
    assert archive.find("demo25q1.txt").name == "ASCII/DEMO25Q1.txt"
    assert archive.find("ASCII/DEMO25Q1.txt").name == "ASCII/DEMO25Q1.txt"


def test_missing_member_names_what_is_available() -> None:
    archive, _ = _archive({"a.txt": b"x\n"})
    with pytest.raises(KeyError, match="a.txt"):
        archive.find("nope.txt")


def test_crlf_and_bom_are_stripped() -> None:
    """A BOM left on line 1 corrupts the first column name and misaligns rows."""
    archive, _ = _archive({"f.txt": b"\xef\xbb\xbfid|name\r\n1|ada\r\n"})
    assert list(archive.iter_lines("f.txt")) == ["id|name", "1|ada"]


def test_final_line_without_a_newline_is_yielded() -> None:
    archive, _ = _archive({"f.txt": b"a\nb\nc"})
    assert list(archive.iter_lines("f.txt")) == ["a", "b", "c"]


def test_lines_split_correctly_across_chunk_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Members are fetched in chunks; a line straddling two must not be cut."""
    monkeypatch.setattr("staging.remote_zip._CHUNK_BYTES", 64)
    body = b"".join(f"row-{i:04d}-padding\n".encode() for i in range(400))
    archive, _ = _archive({"f.txt": body}, compress=False)
    lines = list(archive.iter_lines("f.txt"))
    assert len(lines) == 400
    assert lines[0] == "row-0000-padding"
    assert lines[-1] == "row-0399-padding"


def test_unsupported_compression_method_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    archive, _ = _archive({"f.txt": b"a\n"})
    member = archive.members["f.txt"]
    monkeypatch.setitem(archive.members, "f.txt", type(member)(**{**member.__dict__, "method": 99}))
    with pytest.raises(UnsupportedZipError, match="method 99"):
        list(archive.iter_lines("f.txt"))


def test_truncated_archive_is_refused_rather_than_misread() -> None:
    data = build_zip({"f.txt": b"a\n"})
    with pytest.raises(UnsupportedZipError, match="end-of-central-directory"):
        RemoteZip(BytesRangeFetcher(data[: len(data) // 2]))


def test_line_splitting_is_linear_not_quadratic() -> None:
    """Regression: re-slicing the buffer per line ran the loader at ~450 rows/s.

    The member below holds 200k lines inside a couple of chunks. Linear
    splitting does it in well under a second; the quadratic version moved
    hundreds of gigabytes and took minutes. The bound is deliberately loose —
    it is here to catch an algorithmic regression, not to benchmark the box.
    """
    body = b"".join(b"%d|some padding to make the line realistic\n" % i for i in range(200_000))
    archive, _ = _archive({"big.txt": body}, compress=False)

    started = time.monotonic()
    count = sum(1 for _ in archive.iter_lines("big.txt"))
    elapsed = time.monotonic() - started

    assert count == 200_000
    assert elapsed < 10, f"200k lines took {elapsed:.1f}s — line splitting has gone quadratic"


def test_a_missing_member_is_detectable_before_any_row_is_pulled() -> None:
    """`iter_lines` is lazy, so callers must probe with `find` to skip a table.

    Discovering the absence at first `next()` would be too late: a load run is
    open by then, and one missing member would abort the whole partition.
    """
    archive, _ = _archive({"present.txt": b"a\n"})
    with pytest.raises(KeyError):
        archive.find("absent.txt")
    # The generator itself stays silent until consumed — which is the trap.
    lines = archive.iter_lines("absent.txt")
    with pytest.raises(KeyError):
        next(lines)
