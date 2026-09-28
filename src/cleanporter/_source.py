r"""Source file I/O: bytes in, bytes out, and nothing translated on the way.

A file is read as bytes and decoded the way CPython decodes it at import time
-- `tokenize.detect_encoding`, so a UTF-8 BOM or a PEP 263 cookie
(``# -*- coding: latin-1 -*-``) is honoured -- and *without* universal-newline
translation, so a CRLF file's ``\r\n`` reaches libCST intact. libCST keeps every
newline it parsed and gives the lines the fixer inserts the file's first
newline, so the rewritten text keeps the file's convention; encoding it back
with the same codec (``utf-8-sig`` re-adds the BOM) makes every line the fix
did not touch byte-identical to what was there.

Why decode here rather than hand libCST the bytes: libCST's bytes path runs
the very same `tokenize.detect_encoding`, but the rest of the tool --
guards, skip regions, diffs, the post-fix re-parse -- works on
`FileRecord.source`, a ``str``. Decoding once, here, means there is exactly
one decoder and the text the guards read is the text libCST parsed.

``Path.read_text``/``write_text`` did the opposite on every count: they assumed
UTF-8 (one Latin-1 file aborted the whole run with an error naming no file)
and translated newlines (``--fix`` turned a one-line change in a CRLF file
into a whole-file one, and printed a diff, computed on the translated text,
that did not apply to the file on disk).

A file that cannot be read or decoded raises `SourceError`, which the caller
turns into a per-file error finding: one bad file must not stop the others
being checked.

Writes are atomic (`write_atomic`): a temporary file in the same directory,
then `os.replace`, so a crash or a full disk mid-write leaves the original
whole rather than truncated. What that does and does not preserve is listed
on `write_atomic`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import io
import os
import pathlib
import re
import stat
import tempfile
import tokenize


class SourceError(Exception):
    """A file could not be read or decoded. The message is the reason."""

    def __init__(self, reason: str, line: int = 1) -> None:
        super().__init__(reason)
        #: 1-based line the problem is on, so the report points at it.
        self.line = line


@dataclasses.dataclass(frozen=True)
class Decoded:
    """A source file's text, and what it takes to write it back unchanged."""

    text: str
    #: The codec `tokenize.detect_encoding` chose -- ``utf-8-sig`` when the
    #: file starts with a BOM, so encoding with it puts the BOM back.
    encoding: str
    #: The bytes exactly as read.
    raw: bytes


def read(path: pathlib.Path) -> Decoded:
    """Read *path* and decode it as Python would. Raises `SourceError`."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SourceError(f"cannot read file: {exc.strerror or exc}") from exc
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    except SyntaxError as exc:
        line, reason = _declaration_problem(raw, exc.msg)
        raise SourceError(f"cannot decode file: {reason}", line) from exc
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        line = raw.count(b"\n", 0, exc.start) + 1
        bad = exc.object[exc.start : exc.end]
        reason = f"cannot decode file as {encoding}: {exc.reason} ({bad!r})"
        raise SourceError(reason, line) from exc
    return Decoded(text, encoding, raw)


#: PEP 263's coding declaration, as `tokenize` matches it, on bytes.
_COOKIE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+", re.ASCII)


def _declaration_problem(raw: bytes, message: str) -> tuple[int, str]:
    """Where `tokenize.detect_encoding`'s *message* applies, and what it means.

    `detect_encoding` reads at most two lines and raises with no line number:
    an unknown codec or a BOM contradicting the cookie (the problem is on the
    cookie's line), or a line that is not UTF-8 with no cookie to say what it
    is (the problem is that line, and the byte in it). Recover both.
    """
    for number, line in enumerate(raw.splitlines(keepends=True)[:2], start=1):
        if _COOKIE.match(line):
            return number, message
        try:
            line.decode("utf-8")
        except UnicodeDecodeError as exc:
            bad = exc.object[exc.start : exc.end]
            return number, f"not UTF-8 and no coding declaration: {exc.reason} ({bad!r})"
    return 1, message


def round_trips(text: str, encoding: str, raw: bytes) -> bool:
    """Whether encoding *text* with *encoding* reproduces *raw* exactly.

    Always true for UTF-8 and the single-byte codecs; a few legacy multi-byte
    codecs map more than one byte sequence to the same character, and a file
    using one of those cannot be written back byte-identical.
    """
    try:
        return text.encode(encoding) == raw
    except UnicodeEncodeError:
        return False


def write_atomic(path: pathlib.Path, data: bytes) -> None:
    """Replace the contents of *path* with *data*, all at once or not at all.

    The bytes go to a temporary file beside the target (same directory, so
    the same filesystem, so `os.replace` is an atomic rename), which is then
    renamed over it with the original's permission bits and, where the process
    may set them, its owner and group; the directory is then fsynced so the
    rename itself survives a crash. A symlink is written *through*, as
    ``write_text`` did, rather than replaced by a regular file.

    A file the process may not write is refused with `PermissionError`, as
    ``write_text`` refused it. The rename only needs a writable *directory*,
    so without this check a read-only file would be silently replaced.

    Not preserved: extended attributes and ACLs, which the new file does not
    inherit, and hard links -- the rename gives the path a new inode, so any
    other name for the old one keeps the old contents.
    """
    target = pathlib.Path(os.path.realpath(path))
    status = target.stat()
    if not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = pathlib.Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(stat.S_IMODE(status.st_mode))
        # Best effort: only root may give a file away, and Windows has no chown.
        with contextlib.suppress(OSError, AttributeError):
            os.chown(tmp, status.st_uid, status.st_gid)
        tmp.replace(target)
    except BaseException as exc:
        _discard(tmp, exc)
        raise
    _fsync_directory(target.parent)


def _discard(tmp: pathlib.Path, exc: BaseException) -> None:
    """Delete the temporary file after a failed write, or say where it is.

    On Windows a read-only file cannot be deleted, and the permission bits
    have possibly just been copied onto it, so make it writable and retry.
    If it still cannot go, the error that is about to propagate names it
    rather than leaving a stray file behind with no word about it.
    """
    for attempt in range(2):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            if attempt == 0:
                with contextlib.suppress(OSError):
                    tmp.chmod(stat.S_IWRITE | stat.S_IREAD)
        else:
            return
    exc.add_note(f"cleanporter: could not remove the temporary file {tmp}")


def _fsync_directory(directory: pathlib.Path) -> None:
    """Best effort: make a rename in *directory* durable (POSIX only)."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def patch_lines(data: bytes) -> list[bytes]:
    r"""*data* split into lines the way ``patch`` and ``git apply`` count them.

    Only ``\n`` ends a line: a CRLF line keeps its ``\r`` as content, which
    is what lets the patch apply to the CRLF file byte for byte.
    ``str.splitlines`` also breaks on form feeds and ``\x1c``-``\x1e``, and
    ``bytes.splitlines`` on a lone ``\r``; either would make hunks whose lines
    are not lines of the file.
    """
    lines = [line + b"\n" for line in data.split(b"\n")]
    last = lines.pop()[:-1]
    if last:
        lines.append(last)
    return lines
