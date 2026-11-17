import hashlib
import sqlite3

import pytest

from hf_storage import PartialFile, write_all


def ignore(*args):
    pass


def test_uncommitted_bytes_are_never_used_as_a_resume_offset(tmp_path):
    data = b"verified-prefix-uncommitted-tail"
    partial = PartialFile(tmp_path, len(data), hashlib.sha256(data).hexdigest(), 64)
    with partial.path.open("r+b", buffering=0) as stream:
        write_all(stream, data[:8])
        partial.checkpoint(partial.segments[0], stream, 8, hashlib.sha256(data[:8]))
        write_all(stream, data[8:])
    partial.close()
    partial = PartialFile(tmp_path, len(data), hashlib.sha256(data).hexdigest(), 64)
    try:
        partial.restore(ignore, ignore)
        assert partial.path.stat().st_size == len(data)
        assert partial.segments[0].done == 8
        assert partial.segments[0].hasher.hexdigest() == hashlib.sha256(data[:8]).hexdigest()
    finally:
        partial.close()


def test_corrupt_saved_prefix_is_discarded_but_other_ranges_survive(tmp_path):
    data = b"abcdefghijklmnop"
    checksum = hashlib.sha256(data).hexdigest()
    partial = PartialFile(tmp_path, len(data), checksum, 8)
    with partial.path.open("r+b", buffering=0) as stream:
        for segment in partial.segments:
            block = data[segment.start:segment.end]
            stream.seek(segment.start)
            write_all(stream, block)
            partial.checkpoint(segment, stream, len(block), hashlib.sha256(block))
        stream.seek(1)
        write_all(stream, b"X")
    partial.close()
    partial = PartialFile(tmp_path, len(data), checksum, 8)
    try:
        partial.restore(ignore, ignore)
        assert [s.done for s in partial.segments] == [0, 8]
    finally:
        partial.close()


def test_failed_data_flush_cannot_commit_progress(tmp_path, monkeypatch):
    partial = PartialFile(tmp_path, 8, "expected", 8)
    try:
        with partial.path.open("r+b", buffering=0) as stream:
            write_all(stream, b"12345678")

            def failed_flush(fd):
                raise OSError("disk failure")

            monkeypatch.setattr("hf_storage.os.fsync", failed_flush)
            with pytest.raises(OSError, match="disk failure"):
                partial.checkpoint(partial.segments[0], stream, 8, hashlib.sha256(b"12345678"))
        assert partial.segments[0].done == 0
        assert partial.db.execute("SELECT done FROM ranges").fetchone()[0] == 0
    finally:
        partial.close()


def test_missing_data_file_invalidates_old_complete_records(tmp_path):
    partial = PartialFile(tmp_path, 4, "expected", 4)
    with partial.path.open("r+b", buffering=0) as stream:
        write_all(stream, b"abcd")
        partial.checkpoint(partial.segments[0], stream, 4, hashlib.sha256(b"abcd"))
    partial.path.unlink()
    partial.close()
    partial = PartialFile(tmp_path, 4, "expected", 4)
    try:
        partial.restore(ignore, ignore)
        assert partial.segments[0].done == 0
    finally:
        partial.close()


def test_split_preserves_saved_prefix_and_covers_each_byte_once(tmp_path):
    partial = PartialFile(tmp_path, 100, "expected", 100)
    with partial.path.open("r+b", buffering=0) as stream:
        write_all(stream, b"x" * 20)
        partial.checkpoint(partial.segments[0], stream, 20, hashlib.sha256(b"x" * 20))
    suffix = partial.split(partial.segments[0])
    assert (suffix.start, suffix.end, suffix.done) == (60, 100, 0)
    partial.close()
    partial = PartialFile(tmp_path, 100, "expected", 100)
    try:
        partial.restore(ignore, ignore)
        assert [(s.start, s.end, s.done) for s in partial.segments] == [(0, 60, 20), (60, 100, 0)]
    finally:
        partial.close()


def test_corrupt_range_layout_is_rejected(tmp_path):
    partial = PartialFile(tmp_path, 100, "expected", 50)
    partial.close()
    with sqlite3.connect(tmp_path / "ranges.sqlite3") as db:
        db.execute("UPDATE ranges SET end=80 WHERE start=0")
    with pytest.raises(ValueError, match="区间记录损坏"):
        PartialFile(tmp_path, 100, "expected", 50)


def test_short_raw_writes_are_completed():
    class ShortWriter:
        def __init__(self):
            self.data = bytearray()

        def write(self, data):
            self.data.extend(data[:2])
            return min(2, len(data))

    stream = ShortWriter()
    write_all(stream, b"123456789")
    assert stream.data == b"123456789"
