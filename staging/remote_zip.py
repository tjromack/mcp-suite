"""Read individual members out of a remote ZIP without downloading the archive.

AACT publishes its monthly archive as one 2.5 GB zip of 49 pipe-delimited
files. The relational staging layer needs five of them (~330 MB compressed),
so pulling the whole archive would waste ~87% of the transfer on tables
nothing here reads.

A zip's directory lives at the *end* of the file, and every member is stored
as an independently-addressable byte range. Given an HTTP server that honours
`Range` requests — both fis.fda.gov and AACT's object store do — the central
directory can be read first and then only the wanted members fetched:

    archive = RemoteZip(HttpRangeFetcher(url))
    for line in archive.iter_lines("studies.txt"):
        ...

The same class reads a local file through `FileRangeFetcher`, which is what
the tests and `--from-zip` use, so there is one code path for remote and
local archives.

Deliberately stdlib-only apart from httpx (already a dependency): `zipfile`
cannot read from a non-seekable remote source, and the alternative
(`remotezip`) would add a dependency to replace ~150 lines.
"""

from __future__ import annotations

import logging
import struct
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from core.http_retry import delay_for, is_retryable_status, retry_after_seconds

logger = logging.getLogger("staging.remote_zip")

# Signatures.
_EOCD = b"PK\x05\x06"
_EOCD64 = b"PK\x06\x06"
_CENTRAL = b"PK\x01\x02"

# The end-of-central-directory record is 22 bytes plus a comment of up to
# 64 KiB. Reading 128 KiB from the tail finds it in one request even with a
# maximal comment and a ZIP64 locator in front of it.
_TAIL_BYTES = 128 * 1024

# Download granularity for member bodies. Large enough that a 110 MB member
# is ~14 requests, small enough that a failed chunk is cheap to retry.
_CHUNK_BYTES = 8 * 1024 * 1024

_MAX_ATTEMPTS = 4


class RangeFetcher(Protocol):
    """Reads `[start, end]` inclusive, and reports the resource's total size."""

    def size(self) -> int: ...

    def fetch(self, start: int, end: int) -> bytes: ...


class FileRangeFetcher:
    """Range reads against a local file — used by tests and `--from-zip`."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def size(self) -> int:
        return self._path.stat().st_size

    def fetch(self, start: int, end: int) -> bytes:
        with self._path.open("rb") as fh:
            fh.seek(start)
            return fh.read(end - start + 1)


class HttpRangeFetcher:
    """Range reads over HTTP, retrying on 429/5xx with the shared policy.

    Reuses `core.http_retry` so the staging downloads back off exactly the way
    the MCP connectors do — one retry taxonomy for every upstream in the repo.
    """

    def __init__(self, url: str, *, timeout: float = 300.0) -> None:
        self.url = url
        self._size: int | None = None
        # One client for the whole archive. A load issues dozens of range
        # requests; a fresh client each time would redo the TLS handshake every
        # one of them, and would drop the cookies AACT's CDN sets on the first
        # response. Measured round-trip on an 8 MiB range: 0.6s per-request
        # client, 0.2s shared.
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpRangeFetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # Sent on every request. `identity` is not politeness — fis.fda.gov sits
    # behind a proxy that accepts a ranged request advertising `gzip, deflate`,
    # returns 206, streams most of the body and then stalls until the client
    # times out. Asking for no content-coding fixes it, and is the right thing
    # to ask for anyway: these are byte ranges of an already-compressed zip, so
    # a transfer encoding on top would buy nothing even where it worked.
    _BASE_HEADERS = {"Accept": "*/*", "Accept-Encoding": "identity"}

    def _request(self, headers: dict[str, str]) -> httpx.Response:
        headers = {**self._BASE_HEADERS, **headers}
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._client.get(self.url, headers=headers)
            except httpx.HTTPError as exc:  # transport-level: worth another try
                last_exc = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                wait = delay_for(attempt)
                logger.warning("%s on %s — retrying in %.0fs", type(exc).__name__, self.url, wait)
                time.sleep(wait)
                continue

            if is_retryable_status(response.status_code) and attempt < _MAX_ATTEMPTS:
                wait = delay_for(attempt, retry_after_seconds(response.headers.get("retry-after")))
                logger.warning(
                    "HTTP %s from %s — retrying in %.0fs", response.status_code, self.url, wait
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response

        raise RuntimeError(f"unreachable: {last_exc}")

    def size(self) -> int:
        if self._size is None:
            response = self._request({"Range": "bytes=0-0"})
            content_range = response.headers.get("content-range")
            if not content_range or "/" not in content_range:
                raise RuntimeError(
                    f"{self.url} did not answer a range request with Content-Range; "
                    "this reader needs a server that supports byte ranges"
                )
            self._size = int(content_range.rsplit("/", 1)[1])
        return self._size

    def fetch(self, start: int, end: int) -> bytes:
        response = self._request({"Range": f"bytes={start}-{end}"})
        body = response.content
        expected = end - start + 1
        if len(body) != expected:
            raise RuntimeError(
                f"{self.url} returned {len(body)} bytes for a {expected}-byte range request "
                f"({start}-{end}); the server is ignoring Range"
            )
        return body


@dataclass(frozen=True)
class ZipMember:
    """One entry from the archive's central directory."""

    name: str
    method: int  # 0 = stored, 8 = deflate
    compressed_size: int
    uncompressed_size: int
    header_offset: int


class UnsupportedZipError(RuntimeError):
    """The archive uses a feature this reader deliberately does not implement."""


def _u16(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<H", buf, off)[0])


def _u32(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<I", buf, off)[0])


def _u64(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<Q", buf, off)[0])


def _zip64_fields(extra: bytes, needed: list[str]) -> dict[str, int]:
    """Pull the ZIP64 extended-information fields that the 32-bit slots flagged.

    The 0x0001 extra field packs only the values that overflowed, in a fixed
    order (uncompressed, compressed, header offset, disk). `needed` says which
    of those were 0xFFFFFFFF, so they are read back in that same order.
    """
    out: dict[str, int] = {}
    pos = 0
    while pos + 4 <= len(extra):
        tag = _u16(extra, pos)
        size = _u16(extra, pos + 2)
        body = extra[pos + 4 : pos + 4 + size]
        if tag == 0x0001:
            cursor = 0
            for field in needed:
                if cursor + 8 > len(body):
                    break
                out[field] = _u64(body, cursor)
                cursor += 8
            break
        pos += 4 + size
    return out


class RemoteZip:
    """Random access to a zip's members over a `RangeFetcher`."""

    def __init__(self, fetcher: RangeFetcher) -> None:
        self._fetcher = fetcher
        self.total_bytes = fetcher.size()
        self.members: dict[str, ZipMember] = {}
        self.bytes_fetched = 0
        self._read_central_directory()

    def close(self) -> None:
        """Release the fetcher's connection, if it holds one."""
        closer = getattr(self._fetcher, "close", None)
        if callable(closer):
            closer()

    # --- directory ---------------------------------------------------------

    def _read(self, start: int, end: int) -> bytes:
        chunk = self._fetcher.fetch(start, end)
        self.bytes_fetched += len(chunk)
        return chunk

    def _read_central_directory(self) -> None:
        tail_start = max(0, self.total_bytes - _TAIL_BYTES)
        tail = self._read(tail_start, self.total_bytes - 1)

        eocd = tail.rfind(_EOCD)
        if eocd < 0:
            raise UnsupportedZipError("no end-of-central-directory record in the archive tail")

        entries = _u16(tail, eocd + 10)
        cd_size = _u32(tail, eocd + 12)
        cd_offset = _u32(tail, eocd + 16)

        # ZIP64: any overflowed field is 0xFFFF/0xFFFFFFFF and the real values
        # live in the ZIP64 EOCD record earlier in the tail.
        if entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
            eocd64 = tail.rfind(_EOCD64)
            if eocd64 < 0:
                raise UnsupportedZipError("archive needs ZIP64 but has no ZIP64 EOCD record")
            entries = _u64(tail, eocd64 + 32)
            cd_size = _u64(tail, eocd64 + 40)
            cd_offset = _u64(tail, eocd64 + 48)

        directory = self._read(cd_offset, cd_offset + cd_size - 1)

        pos = 0
        while pos + 46 <= len(directory) and directory[pos : pos + 4] == _CENTRAL:
            method = _u16(directory, pos + 10)
            compressed = _u32(directory, pos + 20)
            uncompressed = _u32(directory, pos + 24)
            name_len = _u16(directory, pos + 28)
            extra_len = _u16(directory, pos + 30)
            comment_len = _u16(directory, pos + 32)
            header_offset = _u32(directory, pos + 42)
            name = directory[pos + 46 : pos + 46 + name_len].decode("utf-8", "replace")
            extra = directory[pos + 46 + name_len : pos + 46 + name_len + extra_len]

            overflowed = [
                field
                for field, value in (
                    ("uncompressed", uncompressed),
                    ("compressed", compressed),
                    ("header_offset", header_offset),
                )
                if value == 0xFFFFFFFF
            ]
            if overflowed:
                wide = _zip64_fields(extra, overflowed)
                uncompressed = wide.get("uncompressed", uncompressed)
                compressed = wide.get("compressed", compressed)
                header_offset = wide.get("header_offset", header_offset)

            if not name.endswith("/"):
                self.members[name] = ZipMember(
                    name=name,
                    method=method,
                    compressed_size=compressed,
                    uncompressed_size=uncompressed,
                    header_offset=header_offset,
                )
            pos += 46 + name_len + extra_len + comment_len

        if len(self.members) == 0 and entries:
            raise UnsupportedZipError("central directory parsed to zero members")

    # --- member bodies -----------------------------------------------------

    def find(self, *candidates: str) -> ZipMember:
        """Resolve a member by exact name, then by case-insensitive basename.

        FAERS nests its tables under `ASCII/` and capitalises the quarter
        (`ASCII/DEMO25Q1.txt`); AACT puts `studies.txt` at the root. Matching
        on basename keeps the callers from hard-coding either layout.
        """
        for candidate in candidates:
            if candidate in self.members:
                return self.members[candidate]
        wanted = {c.lower().rsplit("/", 1)[-1] for c in candidates}
        for name, member in self.members.items():
            if name.lower().rsplit("/", 1)[-1] in wanted:
                return member
        raise KeyError(
            f"none of {list(candidates)} in archive; members: {sorted(self.members)[:12]}…"
        )

    def _body_offset(self, member: ZipMember) -> int:
        """Where the member's bytes actually start.

        The central directory records the *local header* offset, and the local
        header carries its own name and extra-field lengths, which can differ
        from the central copy — so they have to be read rather than assumed.
        """
        header = self._read(member.header_offset, member.header_offset + 29)
        name_len = _u16(header, 26)
        extra_len = _u16(header, 28)
        return member.header_offset + 30 + name_len + extra_len

    def stream(self, member: ZipMember) -> Iterator[bytes]:
        """Yield the member's decompressed bytes, one chunk at a time."""
        if member.method not in (0, 8):
            raise UnsupportedZipError(
                f"{member.name} uses compression method {member.method}; "
                "only stored (0) and deflate (8) are supported"
            )

        start = self._body_offset(member)
        remaining = member.compressed_size
        decompressor = zlib.decompressobj(-15) if member.method == 8 else None

        while remaining > 0:
            take = min(_CHUNK_BYTES, remaining)
            raw = self._read(start, start + take - 1)
            start += take
            remaining -= take
            yield decompressor.decompress(raw) if decompressor else raw

        if decompressor:
            trailing = decompressor.flush()
            if trailing:
                yield trailing

    def iter_lines(self, *candidates: str, encoding: str = "utf-8") -> Iterator[str]:
        """Yield the member's text lines, newline-stripped.

        Splits on `\\n` and strips a trailing `\\r`, so CRLF archives (FAERS)
        and LF archives (AACT) both come out clean. A stray BOM on the first
        line is dropped — it would otherwise corrupt the first header column
        name and silently misalign every row.
        """
        member = self.find(*candidates)
        pending = b""
        first = True
        for chunk in self.stream(member):
            pending += chunk
            if b"\n" not in pending:
                continue
            # One split per chunk, not one slice per line. Re-slicing the
            # buffer after every line (`pending = pending[i + 1:]`) copies what
            # is left each time, which is quadratic in the chunk: an 8 MiB
            # compressed chunk inflates to ~40 MB and holds ~300k lines, so
            # that version moved terabytes and ran the loader at ~450 rows/s
            # against a database doing 390k/s. `split` keeps it linear.
            *complete, pending = pending.split(b"\n")
            for line in complete:
                if first:
                    line = line.removeprefix(b"\xef\xbb\xbf")
                    first = False
                yield line.removesuffix(b"\r").decode(encoding, "replace")
        if pending:
            if first:
                pending = pending.removeprefix(b"\xef\xbb\xbf")
            yield pending.removesuffix(b"\r").decode(encoding, "replace")
