import hashlib
from contextlib import contextmanager
import lzma
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import unicodedata
import zipfile
import zlib


MAX_ENTRIES = 20000
MAX_BYTES = 4 * 1024 * 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_PATH_DEPTH = 64
MAX_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 4096
CHUNK = 1024 * 1024


class ZipError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fold(value):
    return unicodedata.normalize("NFC", value).casefold()


def _member_path(name):
    value = name.replace("\\", "/")
    path = PurePosixPath(value)
    parts = value.split("/")
    if value.endswith("/"):
        parts.pop()
    if (not value or value.startswith("/") or path.is_absolute()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or any(part in ("", ".", "..") or part.endswith((".", " "))
                   or len(part.encode("utf-8")) > MAX_COMPONENT_BYTES for part in parts)
            or len(parts) > MAX_PATH_DEPTH or len(value.encode("utf-8")) > MAX_PATH_BYTES
            or ":" in value):
        raise ZipError("unsafe_archive_path", "The ZIP contains an absolute or traversing path.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{index}" for index in range(1, 10)),
                *(f"LPT{index}" for index in range(1, 10))}
    if any(part.split(".", 1)[0].upper() in reserved for part in parts):
        raise ZipError("unsafe_archive_path", "The ZIP contains a reserved Windows device name.")
    return path, path.as_posix()


def _entries(archive):
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ZipError("archive_entry_limit", "The ZIP contains too many entries to inspect safely.")
    total = 0
    names = {}
    spellings = {}
    files = set()
    for info in infos:
        if "\x00" in info.orig_filename:
            raise ZipError("unsafe_archive_path", "The ZIP contains a name with a NUL character.")
        _, name = _member_path(info.orig_filename)
        parts = name.rstrip("/").split("/")
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            key = _fold(spelling)
            previous = spellings.setdefault(key, spelling)
            if previous != spelling:
                raise ZipError("archive_path_collision", "The ZIP contains paths that collide by case or Unicode spelling.")
        key = _fold(name.rstrip("/"))
        if not key or key in names:
            raise ZipError("archive_path_collision", "The ZIP contains duplicate or colliding paths.")
        names[key] = name
        mode = (info.external_attr >> 16) & 0xffff
        kind = stat.S_IFMT(mode)
        is_dir = info.is_dir()
        if kind not in (0, stat.S_IFDIR if is_dir else stat.S_IFREG):
            raise ZipError("archive_special_file", "The ZIP contains a symbolic link or special file.")
        if info.flag_bits & 1:
            raise ZipError("encrypted_archive_entry", "Encrypted ZIP entries are not supported.")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                      zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            raise ZipError("unsupported_archive_codec", "The ZIP uses an unsupported compression method.")
        if is_dir:
            continue
        if kind == stat.S_IFDIR:
            raise ZipError("archive_special_file", "The ZIP contains a directory with a file name.")
        files.add(key)
        total += info.file_size
        if total > MAX_BYTES:
            raise ZipError("archive_size_limit", "The ZIP declares more uncompressed data than the safety limit.")
    for name in list(names.values()):
        parts = name.rstrip("/").split("/")
        for index in range(1, len(parts)):
            parent = _fold("/".join(parts[:index]))
            if parent in files:
                raise ZipError("archive_path_collision", "A ZIP file path conflicts with a directory path.")
    return [(info, *_member_path(info.orig_filename)) for info in infos], total


def _check_directory(stream, size):
    stream.seek(max(0, size - 65557))
    tail = stream.read()
    marker = tail.rfind(b"PK\x05\x06")
    if marker < 0 or marker + 22 > len(tail):
        raise ZipError("unreadable_archive", "The ZIP directory could not be located safely.")
    disk, directory_disk, disk_entries, entries, directory_size, directory_offset, comment_size = struct.unpack_from(
        "<4H2IH", tail, marker + 4)
    if marker + 22 + comment_size != len(tail) or disk or directory_disk or disk_entries != entries:
        raise ZipError("unsupported_archive_layout", "The ZIP uses a split or malformed directory layout.")
    locator = marker - 20
    has_zip64 = locator >= 0 and tail[locator:locator + 4] == b"PK\x06\x07"
    if has_zip64:
        locator_disk, zip64_offset, disk_count = struct.unpack_from("<IQI", tail, locator + 4)
        if locator_disk or disk_count != 1 or zip64_offset + 56 > size:
            raise ZipError("unsupported_archive_layout", "The ZIP64 directory layout is unsupported.")
        stream.seek(zip64_offset)
        record = stream.read(12)
        if len(record) != 12 or record[:4] != b"PK\x06\x06":
            raise ZipError("unreadable_archive", "The ZIP64 directory record is invalid.")
        record_size = struct.unpack_from("<Q", record, 4)[0]
        if record_size < 44 or record_size > 1024 * 1024 or zip64_offset + 12 + record_size > size:
            raise ZipError("unsupported_archive_layout", "The ZIP64 directory record size is invalid.")
        remainder = stream.read(record_size)
        if len(remainder) != record_size:
            raise ZipError("unreadable_archive", "The ZIP64 directory record is truncated.")
        disk, directory_disk, disk_entries, entries64, size64, offset64 = struct.unpack_from(
            "<2I4Q", remainder, 4)
        if disk or directory_disk or disk_entries != entries64:
            raise ZipError("unsupported_archive_layout", "Split ZIP64 archives are not supported.")
        if ((entries != 0xffff and entries != entries64)
                or (directory_size != 0xffffffff and directory_size != size64)
                or (directory_offset != 0xffffffff and directory_offset != offset64)):
            raise ZipError("unreadable_archive", "The ZIP and ZIP64 directory records disagree.")
        entries, directory_size, directory_offset = entries64, size64, offset64
    elif entries == 0xffff or directory_size == 0xffffffff or directory_offset == 0xffffffff:
        raise ZipError("unreadable_archive", "The ZIP64 directory record is missing.")
    if directory_offset + directory_size > size:
        raise ZipError("unreadable_archive", "The ZIP central directory lies outside the archive.")
    if entries > MAX_ENTRIES or directory_size > MAX_DIRECTORY_BYTES:
        raise ZipError("archive_entry_limit", "The ZIP directory exceeds the inspection limit.")


@contextmanager
def open_archive(path):
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ZipError("archive_size_limit", "The ZIP file exceeds the inspection size limit.")
    stream = path.open("rb")
    archive = None
    try:
        _check_directory(stream, size)
        archive = zipfile.ZipFile(stream)
        yield archive, _entries(archive)
    except BaseException:
        if archive is not None:
            archive.close()
        stream.close()
        raise
    else:
        archive.close()
        stream.close()


def list_archive(path):
    with open_archive(path) as (_, entries):
        return entries


def archive_uncompressed_size(path, cancel=None):
    if cancel:
        cancel()
    with open_archive(path) as (_, (_, declared)):
        return declared


def archive_digest(path, cancel=None):
    path = Path(path)
    before = path.stat()
    if before.st_size > MAX_BYTES:
        raise ZipError("archive_size_limit", "The ZIP file exceeds the inspection size limit.")
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        initial = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != initial:
            raise ZipError("source_changed", "The ZIP changed while it was being inspected.")
        while True:
            if cancel:
                cancel()
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            count += len(chunk)
            if count > MAX_BYTES:
                raise ZipError("archive_size_limit", "The ZIP file exceeds the inspection size limit.")
            digest.update(chunk)
    after = path.stat()
    if count != before.st_size or initial != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ZipError("source_changed", "The ZIP changed while it was being inspected.")
    return digest.hexdigest()


def extract(path, destination, cancel=None, budget=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    budget = budget if budget is not None else [0, 0]
    try:
        with open_archive(path) as (archive, listed):
            entries, declared = listed
            if budget[0] + len(entries) > MAX_ENTRIES or budget[1] + declared > MAX_BYTES:
                raise ZipError("archive_work_limit", "The archive extraction work limit was reached.")
            budget[0] += len(entries)
            root = destination.resolve(strict=True)
            for info, member, name in entries:
                if cancel:
                    cancel()
                target = destination.joinpath(*member.parts)
                if not target.resolve(strict=False).is_relative_to(root):
                    raise ZipError("unsafe_archive_path", "The ZIP contains a path outside its extraction folder.")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        if cancel:
                            cancel()
                        chunk = source.read(CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        budget[1] += len(chunk)
                        if budget[1] > MAX_BYTES or written > info.file_size:
                            raise ZipError("archive_size_limit", "The ZIP exceeded its streamed extraction limit.")
                        output.write(chunk)
                if written != info.file_size:
                    raise ZipError("archive_truncated", "A ZIP entry did not match its declared size.")
        return destination
    except BaseException as error:
        import shutil
        shutil.rmtree(destination, ignore_errors=True)
        if isinstance(error, (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError,
                              zlib.error, lzma.LZMAError)):
            raise ZipError("unreadable_archive", "A ZIP entry could not be read or verified.") from error
        raise
