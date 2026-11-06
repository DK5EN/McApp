"""Persistent raw-frame log ("Wire record v1").

One JSON object per line, one line per datagram or BLE notification, written
to daily files ``<dir>/<YYYY-MM-DD>.jsonl`` (UTC date of ``ts``). The previous
day is gzipped on rotation; nothing is ever pruned.

The writer never blocks or raises into the caller: ``log()`` only does a
``put_nowait`` on a bounded queue (a full queue drops the record and counts
it), and a background thread does all file I/O. I/O errors are logged at most
once per minute.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import queue
import sys
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from .logging_setup import get_logger

logger = get_logger(__name__)

DEFAULT_QUEUE_SIZE = 10_000
ERROR_LOG_INTERVAL_S = 60.0
_GET_TIMEOUT_S = 0.5
_TAG_LEN = 4
_INDS = (b"KEEP", b"BEAT", b"DATA", b"GATE")

Clock = Callable[[], datetime]
_Item = tuple[datetime, str, str, str, str | None, bytes]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ind(payload: bytes) -> str | None:
    head = payload[:4]
    if head in _INDS or (len(head) == _TAG_LEN and head[:3] == b"HBC" and head[3:4].isascii()):
        return head.decode("ascii")
    return None


_COMPRESS_LEVEL = 6


def _gz_candidates(path: Path) -> Iterator[Path]:
    """`<day>.jsonl.gz`, then `<day>.1.jsonl.gz`, `<day>.2.jsonl.gz`, ..."""
    day = path.stem
    yield path.with_name(f"{day}.jsonl.gz")
    n = 1
    while True:
        yield path.with_name(f"{day}.{n}.jsonl.gz")
        n += 1


def _gz_contains(gz: Path, data: bytes) -> bool:
    """True iff the member file `gz` decompresses to exactly `data` (unreadable = False)."""
    try:
        return gzip.decompress(gz.read_bytes()) == data
    except (OSError, EOFError, zlib.error):
        return False


def _fsync_dir(directory: Path) -> None:
    """Best effort: make the rename itself durable."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _gzip_file(path: Path) -> None:
    """Atomically compress the plain day file `path` into a gzip member file, then remove it.

    Writes ``<day>.jsonl.gz.tmp`` (fsynced), renames it onto the first free
    ``<day>[.<n>].jsonl.gz`` and only then unlinks `path`: a power cut leaves
    either the plain file (re-compressed at next start) or a finished member,
    never a truncated or appended-to archive. A plain file whose content is
    already in an existing member (cut between rename and unlink) is just removed.
    """
    data = path.read_bytes()
    tmp = path.with_name(f"{path.stem}.jsonl.gz.tmp")
    final: Path | None = None
    for candidate in _gz_candidates(path):
        if not candidate.exists():
            final = candidate
            break
        if _gz_contains(candidate, data):
            path.unlink()
            return
    assert final is not None  # noqa: S101 - the candidate generator is infinite
    tmp.unlink(missing_ok=True)
    with tmp.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=_COMPRESS_LEVEL) as gz:
            gz.write(data)
        raw.flush()
        os.fsync(raw.fileno())
    tmp.replace(final)
    _fsync_dir(path.parent)
    path.unlink()


class WireLog:
    def __init__(  # noqa: PLR0913 - keyword-only options of a small value class
        self,
        directory: str | Path,
        *,
        enabled: bool = True,
        src: str = "mcproxy",
        node_provider: Callable[[], str] | None = None,
        clock: Clock | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        start_thread: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.enabled = enabled
        self.src = src
        self._node_provider = node_provider
        self._clock: Clock = clock or _utc_now
        self._queue: queue.Queue[_Item | None] = queue.Queue(maxsize=queue_size)
        self.dropped = 0
        self.written = 0
        self._last_error_log = float("-inf")
        self._thread: threading.Thread | None = None
        self._fh: TextIO | None = None
        self._fh_date: str | None = None
        self._closing = threading.Event()
        if enabled and start_thread:
            self.start()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="wire-log", daemon=True)
        self._thread.start()

    # -- producer side (never blocks, never raises) --

    def log(self, direction: str, chan: str, payload: bytes, peer: str | None = None) -> None:
        if not self.enabled:
            return
        try:
            node = self._node_provider() if self._node_provider else ""
            item: _Item = (self._clock(), node or "unknown", direction, chan, peer, bytes(payload))
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1
        except Exception:
            self.dropped += 1
            self._report_error("wire log enqueue failed")

    def flush(self) -> None:
        """Block until everything queued so far is written (tests, shutdown)."""
        if self._thread is not None:
            self._queue.join()

    def close(self) -> None:
        if self._thread is not None:
            self._closing.set()
            with contextlib.suppress(queue.Full):
                self._queue.put(None, timeout=2.0)
            self._thread.join(timeout=5.0)
            self._thread = None
        self._close_file()

    # -- writer side --

    def _report_error(self, what: str) -> None:
        now = time.monotonic()
        if now - self._last_error_log >= ERROR_LOG_INTERVAL_S:
            self._last_error_log = now
            logger.warning("%s", what, exc_info=sys.exc_info())

    def _run(self) -> None:
        self._compress_stale(self._date_str(self._clock()))
        while True:
            try:
                item = self._queue.get(timeout=_GET_TIMEOUT_S)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            try:
                if item is None:
                    return
                self._write(item)
            except Exception:
                self.dropped += 1
                self._report_error("wire log write failed")
            finally:
                self._queue.task_done()

    @staticmethod
    def _date_str(ts: datetime) -> str:
        return ts.astimezone(UTC).strftime("%Y-%m-%d")

    def _compress_stale(self, today: str) -> None:
        """Gzip any leftover plain day files older than `today` (crash recovery)."""
        try:
            for tmp in self.directory.glob("*.jsonl.gz.tmp"):
                tmp.unlink(missing_ok=True)  # a cut mid-gzip; the plain file is still intact
            for path in sorted(self.directory.glob("*.jsonl")):
                if path.stem < today:
                    _gzip_file(path)
        except Exception:
            self._report_error("wire log stale-file compression failed")

    def _close_file(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is not None:
            try:
                fh.close()
            except Exception:
                self._report_error("wire log close failed")

    def _open_for(self, date: str) -> None:
        previous = self._fh_date
        self._close_file()
        self.directory.mkdir(parents=True, exist_ok=True)
        if previous is not None and previous < date:
            prev_path = self.directory / f"{previous}.jsonl"
            try:
                if prev_path.exists():
                    _gzip_file(prev_path)
            except Exception:
                self._report_error("wire log rotation gzip failed")
        self._fh = (self.directory / f"{date}.jsonl").open("a", encoding="utf-8")
        self._fh_date = date

    def _write(self, item: _Item) -> None:
        ts, node, direction, chan, peer, payload = item
        date = self._date_str(ts)
        if date != self._fh_date or self._fh is None:
            self._open_for(date)
        record: dict[str, object] = {
            "v": 1,
            "ts": _iso(ts),
            "ts_real": _iso(ts),
            "src": self.src,
            "node": node,
            "dir": direction,
            "chan": chan,
        }
        if peer is not None:
            record["peer"] = peer
        ind = _ind(payload)
        if ind is not None:
            record["ind"] = ind
        record["len"] = len(payload)
        record["hex"] = payload.hex()
        fh = self._fh
        assert fh is not None  # noqa: S101 - set by _open_for just above
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        fh.flush()
        self.written += 1


def mcproxy_node(callsign: str | None) -> str:
    """Wire record v1 `node` for MCProxy: the writer, ``mcproxy-<callsign>``."""
    return f"mcproxy-{callsign or 'unknown'}"
