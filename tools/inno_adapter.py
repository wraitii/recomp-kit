import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata

import install_jobs
import source_inspection
from zip_adapter import ZipError, _member_path


MAX_BYTES = install_jobs.MAX_PAYLOAD_BYTES
MAX_ENTRIES = source_inspection.MAX_ENTRIES
OPTIONS = ["--silent", "--exclude-temp", "--no-extract-unknown", "--collisions", "error",
           "--color=0", "--progress=0"]


class InnoError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def helper():
    local = Path(sys.executable).parent / ("innoextract.exe" if os.name == "nt" else "innoextract")
    found = str(local) if local.is_file() else shutil.which("innoextract")
    if not found:
        raise InnoError("missing_innoextract", "Install innoextract 1.9 or later (on macOS: brew install innoextract), then choose the installer again.")
    return str(Path(found).resolve(strict=True))


def _fold(name):
    return unicodedata.normalize("NFC", name).casefold()


def _run(path, arguments, cancel=None, destination=None, limit=0, progress=None):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen([helper(), *OPTIONS, *arguments, "--", str(path)],
                                   cwd=path.parent, stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
        started, next_monitor = time.monotonic(), 0
        try:
            while True:
                if cancel:
                    cancel()
                now = time.monotonic()
                if now - started > 1800:
                    raise InnoError("installer_timeout", "Installer processing exceeded 30 minutes.")
                if (os.fstat(output.fileno()).st_size > 16 * 1024 * 1024
                        or os.fstat(errors.fileno()).st_size > 1024 * 1024):
                    raise InnoError("installer_output_limit", "The extractor produced too much output.")
                if destination is not None and now >= next_monitor:
                    size = install_jobs.tree_size(destination, cancel)
                    if size > limit:
                        raise InnoError("installer_size_limit", "The installer exceeded its declared expanded size.")
                    install_jobs.disk_space(destination.parent, install_jobs.SAFETY_RESERVE_BYTES, "extracting")
                    if progress:
                        progress(size, "Extracting game files")
                    next_monitor = now + 0.5
                if process.poll() is not None:
                    break
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                errors.seek(0)
                detail = errors.read(8192).decode("utf-8", errors="replace").strip()
                raise InnoError("installer_extraction_failed", f"innoextract failed: {detail or process.returncode}")
            output.seek(0)
            return output.read().decode("utf-8")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _sources(path):
    path = Path(path).resolve(strict=True)
    pattern = re.compile(re.escape(path.stem) + r"(?:-\d+[a-z]?)?\.bin", re.IGNORECASE)
    sources = [path]
    with os.scandir(path.parent) as siblings:
        for index, entry in enumerate(siblings):
            if index >= source_inspection.MAX_SIBLING_ENTRIES:
                raise InnoError("sibling_limit", "The installer folder contains too many entries. Put its EXE and BIN files in a separate folder.")
            if pattern.fullmatch(entry.name):
                sources.append(Path(entry.path))
    if len(sources) > source_inspection.MAX_SIBLINGS:
        raise InnoError("sibling_limit", "The installer has too many payload files.")
    rows, names, total = [], set(), 0
    for source in sources:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or _fold(source.name) in names:
            raise InnoError("unsafe_installer_input", "Installer inputs must be regular files with distinct names.")
        names.add(_fold(source.name))
        total += info.st_size
        if total > MAX_BYTES:
            raise InnoError("installer_size_limit", "The installer inputs exceed the import size limit.")
        if source != path:
            with source.open("rb") as stream:
                if stream.read(4) == b"Rar!":
                    raise InnoError("unsupported_installer_payload", "This GOG installer uses external RAR payloads. Extract it outside the toolkit and choose the game folder.")
        rows.append((source, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)))
    return sorted(rows, key=lambda item: _fold(item[0].name))


def _fingerprint(path, cancel=None, destination=None):
    sources = _sources(path)
    digests = []
    for source, initial in sources:
        digest, count = hashlib.sha256(), 0
        with source.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != initial:
                raise InnoError("source_changed", "The installer changed while it was being read.")
            target = (destination / source.name).open("xb") if destination else None
            try:
                while True:
                    if cancel:
                        cancel()
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > initial[2]:
                        raise InnoError("source_changed", "The installer changed while it was being read.")
                    digest.update(chunk)
                    if target:
                        target.write(chunk)
            finally:
                if target:
                    target.close()
        if count != initial[2]:
            raise InnoError("source_changed", "The installer changed while it was being read.")
        digests.append((source.name, digest.hexdigest()))
    if _sources(path) != sources:
        raise InnoError("source_changed", "The installer inputs changed while they were being read.")
    return digests[0][1] if len(digests) == 1 else hashlib.sha256(json.dumps(digests).encode()).hexdigest()


def _listing(path, cancel=None):
    sources = _sources(path)
    text = _run(path, ["--list", "--list-sizes"], cancel)
    if _sources(path) != sources:
        raise InnoError("source_changed", "The installer changed while it was being inspected.")
    entries, spellings, total = {}, {}, 0
    for line in text.splitlines():
        if cancel:
            cancel()
        if line.endswith("/"):
            name, size = line, None
        else:
            size_text, separator, name = line.partition(" ")
            if not separator or len(size_text) > 20 or not size_text.isascii() or not size_text.isdecimal():
                raise InnoError("installer_listing_invalid", "The installer file listing could not be read safely.")
            size = int(size_text)
        try:
            _, name = _member_path(name)
        except ZipError as error:
            raise InnoError(error.code, str(error).replace("ZIP", "installer")) from error
        key = _fold(name)
        if key in entries:
            raise InnoError("installer_path_collision", "The installer contains duplicate or colliding paths.")
        parts = name.split("/")
        for index in range(1, len(parts) + 1):
            spelling = "/".join(parts[:index])
            if spellings.setdefault(_fold(spelling), spelling) != spelling:
                raise InnoError("installer_path_collision", "Installer paths collide by case or Unicode spelling.")
        entries[key] = (name, size)
        total += size or 0
        if len(entries) > MAX_ENTRIES or len(spellings) > MAX_ENTRIES or total > MAX_BYTES:
            raise InnoError("installer_size_limit", "The installer listing exceeds the import limits.")
    for name, _ in entries.values():
        for parent in Path(name).parents:
            entry = entries.get(_fold(parent.as_posix()))
            if entry and entry[1] is not None:
                raise InnoError("installer_path_collision", "An installer file conflicts with a directory path.")
    return dict(entries.values())


def _extract(path, destination, entries, cancel=None, selected=None, progress=None):
    destination.mkdir(exist_ok=False)
    expected = {name: size for name, size in entries.items() if size is not None
                and (selected is None or name in selected)}
    total = sum(expected.values())
    try:
        install_jobs.disk_space(destination.parent, total + install_jobs.SAFETY_RESERVE_BYTES, "extracting")
        if expected:
            arguments = ["--extract", "--test", "--output-dir", str(destination)]
            if selected is not None:
                for name in sorted(selected):
                    arguments.extend(["--include", "/" + name])
            _run(path, arguments, cancel, destination, total, progress)
        _, files = install_jobs.payload_files(destination, cancel)
        actual = {p.relative_to(destination).as_posix(): p.stat().st_size for p in files}
        if actual != expected:
            raise InnoError("installer_contents_mismatch", "The extracted files do not match the installer listing.")
        return destination
    except BaseException:
        shutil.rmtree(destination)
        raise


class InnoAdapter:
    def __init__(self, installers):
        self.installers = installers

    def archive_digest(self, path, cancel=None):
        return _fingerprint(Path(path).resolve(strict=True), cancel)

    def source_size(self, path):
        return sum(info[2] for _, info in _sources(path))

    def archive_uncompressed_size(self, path, cancel=None):
        return sum(size or 0 for size in _listing(Path(path).resolve(strict=True), cancel).values())

    def inspect(self, cfg, path, destination, cancel=None):
        path, destination = Path(path).resolve(strict=True), Path(destination).resolve()
        sources = _sources(path)
        entries = _listing(path, cancel)
        names = {_fold(name) for name in cfg["launcher"]["candidate_executables"]}
        names.update(_fold(Path(item["path"]).name) for item in cfg["setup"]["required_roles"] if item.get("sha256"))
        selected = {name for name, size in entries.items() if size is not None and _fold(Path(name).name) in names}
        if (len(selected) > source_inspection.MAX_CANDIDATES
                or sum(entries[name] for name in selected) > source_inspection.MAX_HASH_BYTES):
            raise InnoError("installer_candidate_limit", "The installer contains too many or too large executable candidates.")
        _extract(path, destination, entries, cancel, selected)
        if _sources(path) != sources:
            raise InnoError("source_changed", "The installer changed while its executable files were being inspected.")
        result = source_inspection.inspect_folder(cfg, destination, cancel, archive_entries=entries)
        result["source"]["verification"] = "executable_hashes_and_archive_listing"
        return result

    def extract(self, path, destination, cancel=None, progress=None):
        path, destination = Path(path).resolve(strict=True), Path(destination).resolve()
        digest = self.archive_digest(path, cancel)
        entries = _listing(path, cancel)
        total = sum(size or 0 for size in entries.values())
        required = self.source_size(path) + total + install_jobs.SAFETY_RESERVE_BYTES
        install_jobs.disk_space(destination.parent, required, "extracting")
        with tempfile.TemporaryDirectory(prefix="inno-input-", dir=destination.parent) as temporary:
            snapshot = Path(temporary) / path.name
            if _fingerprint(path, cancel, Path(temporary)) != digest:
                raise InnoError("source_changed", "The installer changed before extraction.")
            if _listing(snapshot, cancel) != entries:
                raise InnoError("source_changed", "The installer listing changed before extraction.")
            _extract(snapshot, destination, entries, cancel, progress=progress)
            try:
                recipe = next((item for item in self.installers if item["sha256"].lower() == digest), None)
                if recipe:
                    if total != recipe["expanded_size"]:
                        raise InnoError("installer_size_mismatch", "The extracted files do not match the verified installer size.")
                    for old, new in recipe["move_files"].items():
                        source_file, target = destination / old, destination / new
                        if not source_file.is_file() or target.exists():
                            raise InnoError("installer_layout_mismatch", "The extracted files do not match the verified installer layout.")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source_file.rename(target)
                if self.archive_digest(path, cancel) != digest:
                    raise InnoError("source_changed", "The installer changed during extraction.")
                if progress:
                    progress(total, "Game files extracted")
                return destination
            except BaseException:
                shutil.rmtree(destination)
                raise
