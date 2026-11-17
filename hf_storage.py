"""Durable range checkpoints for a single partially downloaded file."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading


def write_all(stream, data):
    """Raw FileIO.write may write fewer bytes than requested."""
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None or written <= 0:
            raise OSError("文件写入未取得进展")
        view = view[written:]


@dataclass
class Segment:
    start: int
    end: int  # exclusive
    done: int = 0
    digest: str = ""
    hasher: object = field(default_factory=hashlib.sha256, repr=False)

    @property
    def length(self):
        return self.end - self.start

    @property
    def remaining(self):
        return self.length - self.done


class PartialFile:
    """Only checkpointed, rehashed prefixes are trusted after a restart.

    Each segment has one writer with an independent file handle. Data is fsynced
    before its offset and prefix hash are committed in SQLite. Bytes written after
    the last transaction are deliberately downloaded again after a crash.
    """

    def __init__(self, directory: Path, size: int, digest: str, block_size: int):
        self.path = directory / "download.incomplete"
        self.db_path = directory / "ranges.sqlite3"
        self.size = size
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS ranges (
                start INTEGER PRIMARY KEY, end INTEGER NOT NULL,
                done INTEGER NOT NULL, digest TEXT NOT NULL)""")
            identity = json.dumps([2, size, digest, block_size])
            saved = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            if saved and saved[0] != identity:
                raise ValueError("续传记录与文件版本或分块配置不一致")
            if not saved:
                with self.db:
                    self.db.execute("INSERT INTO meta VALUES ('identity', ?)", (identity,))
                    self.db.execute("INSERT INTO meta VALUES ('mode', 'range')")
                    self.db.executemany("INSERT INTO ranges VALUES (?, ?, 0, '')",
                                        ((s, min(size, s + block_size)) for s in range(0, size, block_size)))
            self.segments = [Segment(*row) for row in self.db.execute(
                "SELECT start, end, done, digest FROM ranges ORDER BY start")]
            cursor = 0
            for segment in self.segments:
                if (segment.start != cursor or segment.end <= segment.start or segment.end > size
                        or not 0 <= segment.done <= segment.length):
                    raise ValueError("续传区间记录损坏")
                cursor = segment.end
            if cursor != size:
                raise ValueError("续传记录没有覆盖完整文件")
            row = self.db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
            if not row or row[0] not in ("range", "full"):
                raise ValueError("续传模式记录损坏")
            self.ranged = row[0] == "range"
            # Do not preallocate: migrations can copy and remove one legacy part
            # at a time, and file size must never be used as completion evidence.
            self.path.touch(exist_ok=True)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def checkpoint(self, segment: Segment, stream, done: int, hasher):
        stream.flush()
        os.fsync(stream.fileno())
        checksum = hasher.hexdigest() if done else ""
        with self.lock, self.db:
            self.db.execute("UPDATE ranges SET done=?, digest=? WHERE start=?", (done, checksum, segment.start))
        segment.done, segment.digest = done, checksum
        segment.hasher = hasher.copy()

    def forget(self, segment: Segment):
        with self.lock, self.db:
            self.db.execute("UPDATE ranges SET done=0, digest='' WHERE start=?", (segment.start,))
        segment.done, segment.digest, segment.hasher = 0, "", hashlib.sha256()

    def restore(self, check_stop, advance):
        """Validate only recorded prefixes; never trust holes or uncommitted tails."""
        actual_size = self.path.stat().st_size
        with self.path.open("rb") as stream:
            for segment in self.segments:
                check_stop()
                if not segment.done:
                    continue
                if segment.start + segment.done > actual_size:
                    advance(segment.done)
                    self.forget(segment)
                    continue
                stream.seek(segment.start)
                hasher, remaining = hashlib.sha256(), segment.done
                while remaining:
                    check_stop()
                    data = stream.read(min(1024 * 1024, remaining))
                    if not data:
                        break
                    hasher.update(data)
                    remaining -= len(data)
                    advance(len(data))
                if remaining or hasher.hexdigest() != segment.digest:
                    self.forget(segment)
                else:
                    segment.hasher = hasher

    def reset(self, *, full: bool = False):
        with self.lock, self.db:
            if full:
                self.db.execute("DELETE FROM ranges")
                if self.size:
                    self.db.execute("INSERT INTO ranges VALUES (0, ?, 0, '')", (self.size,))
                self.db.execute("UPDATE meta SET value='full' WHERE key='mode'")
            else:
                self.db.execute("UPDATE ranges SET done=0, digest=''")
        if full:
            self.ranged = False
            self.segments = [Segment(0, self.size)] if self.size else []
        else:
            for segment in self.segments:
                segment.done, segment.digest, segment.hasher = 0, "", hashlib.sha256()

    def split(self, segment: Segment) -> Segment:
        """Split only an idle segment's uncommitted tail, never an active writer."""
        middle = segment.start + segment.done + segment.remaining // 2
        suffix = Segment(middle, segment.end)
        with self.lock, self.db:
            self.db.execute("UPDATE ranges SET end=? WHERE start=?", (middle, segment.start))
            self.db.execute("INSERT INTO ranges VALUES (?, ?, 0, '')", (middle, suffix.end))
        segment.end = middle
        self.segments.append(suffix)
        self.segments.sort(key=lambda part: part.start)
        return suffix
