from contextlib import contextmanager
import ctypes
import ctypes.util
import os
from pathlib import Path
import re
import shutil
import stat
import sys

import install_jobs
import source_inspection
from inno_adapter import InstallerAdapter, InnoError, MAX_BYTES, MAX_ENTRIES, _fold
from zip_adapter import _member_path, ZipError


def library():
    root = Path(__file__).resolve().parents[1]
    name = "unshield.dll" if os.name == "nt" else "libunshield.dylib" if sys.platform == "darwin" else "libunshield.so"
    candidates = [Path(sys.executable).parent / name]
    if os.environ.get("RECOMP_UNSHIELD_LIBRARY"):
        candidates.append(Path(os.environ["RECOMP_UNSHIELD_LIBRARY"]))
    candidates += [base / "build/toolkit/extractors/unshield/lib" / name for base in (root, root.parent)]
    found = next((str(path) for path in candidates if path.is_file()), None) or ctypes.util.find_library("unshield")
    if not found:
        raise InnoError("missing_unshield", "InstallShield extraction is unavailable. Reinstall the toolkit or install libunshield.")
    try:
        lib = ctypes.CDLL(found)
        for name, result, arguments in (
                ("open", ctypes.c_void_p, [ctypes.c_char_p]),
                ("close", None, [ctypes.c_void_p]),
                ("set_log_level", None, [ctypes.c_int]),
                ("file_count", ctypes.c_int, [ctypes.c_void_p]),
                ("directory_count", ctypes.c_int, [ctypes.c_void_p]),
                ("directory_name", ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int]),
                ("file_name", ctypes.c_char_p, [ctypes.c_void_p, ctypes.c_int]),
                ("file_directory", ctypes.c_int, [ctypes.c_void_p, ctypes.c_int]),
                ("file_size", ctypes.c_size_t, [ctypes.c_void_p, ctypes.c_int]),
                ("file_is_valid", ctypes.c_bool, [ctypes.c_void_p, ctypes.c_int]),
                ("file_save", ctypes.c_bool, [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p])):
            function = getattr(lib, "unshield_" + name)
            function.restype, function.argtypes = result, arguments
        lib.unshield_set_log_level(0)
        return lib
    except (OSError, AttributeError) as error:
        raise InnoError("unshield_unavailable", f"The InstallShield extraction library could not be loaded: {error}") from error


def _siblings(path):
    entries = {}
    with os.scandir(path.parent) as iterator:
        for index, entry in enumerate(iterator):
            if index >= source_inspection.MAX_SIBLING_ENTRIES:
                raise InnoError("sibling_limit", "The InstallShield source folder contains too many entries.")
            key = _fold(entry.name)
            if key in entries:
                raise InnoError("installer_path_collision", "Installer source names collide by case or Unicode spelling.")
            entries[key] = Path(entry.path)
    return entries


def _cabinet(path, siblings):
    if source_inspection.probe_bytes(path)["kind"] == "installshield_cab":
        return path
    cabinet = siblings.get("data1.cab")
    if cabinet and source_inspection.probe_bytes(cabinet)["kind"] == "installshield_cab":
        return cabinet
    raise InnoError("missing_installshield_cabinet", "Keep Setup.exe beside its data1.cab, header and additional cabinet files.")


def _sources(path):
    path = Path(path).resolve(strict=True)
    siblings = _siblings(path)
    cabinet = _cabinet(path, siblings)
    stem = re.sub(r"\d+$", "", cabinet.stem)
    volumes = re.compile(re.escape(stem) + r"\d*\.(cab|hdr)", re.IGNORECASE)
    rows, total = [], 0
    for source in siblings.values():
        if source != path and not volumes.fullmatch(source.name) and source.suffix.casefold() not in (".exe", ".dll"):
            continue
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise InnoError("unsafe_installer_input", "Installer inputs must be regular files without symbolic links.")
        total += info.st_size
        if len(rows) >= source_inspection.MAX_SIBLINGS or total > MAX_BYTES:
            raise InnoError("installer_size_limit", "The InstallShield inputs exceed the import limits.")
        rows.append((source, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)))
    return sorted(rows, key=lambda item: _fold(item[0].name))


@contextmanager
def _members(path, cancel=None, loose_names=()):
    sources = _sources(path)
    lib = library()
    cabinet = _cabinet(path, _siblings(path))
    handle = lib.unshield_open(os.fsencode(cabinet))
    if not handle:
        raise InnoError("invalid_installshield_cabinet", "The InstallShield cabinet or its header could not be opened.")
    try:
        count, directories = lib.unshield_file_count(handle), lib.unshield_directory_count(handle)
        if not 0 <= count <= MAX_ENTRIES or not 0 <= directories <= MAX_ENTRIES:
            raise InnoError("installer_size_limit", "The InstallShield listing exceeds the import limits.")
        members, spellings, total = {}, {}, 0

        def add(name, size, index=None, source=None):
            nonlocal total
            try:
                _, name = _member_path(name)
            except ZipError as error:
                raise InnoError(error.code, str(error).replace("ZIP", "installer")) from error
            key = _fold(name)
            if key in members:
                raise InnoError("installer_path_collision", "The installer contains duplicate or colliding destination paths.")
            parts = name.split("/")
            for length in range(1, len(parts) + 1):
                spelling = "/".join(parts[:length])
                if spellings.setdefault(_fold(spelling), spelling) != spelling:
                    raise InnoError("installer_path_collision", "Installer paths collide by case or Unicode spelling.")
            members[key] = (name, size, index, source)
            total += size
            if total > MAX_BYTES or len(members) > MAX_ENTRIES or len(spellings) > MAX_ENTRIES:
                raise InnoError("installer_size_limit", "The InstallShield listing exceeds the import limits.")

        for index in range(count):
            if cancel:
                cancel()
            if not lib.unshield_file_is_valid(handle, index):
                continue
            directory = lib.unshield_file_directory(handle, index)
            if not 0 <= directory < directories:
                raise InnoError("installer_listing_invalid", "An InstallShield file references an invalid directory.")
            folder, name = lib.unshield_directory_name(handle, directory), lib.unshield_file_name(handle, index)
            if folder is None or name is None:
                raise InnoError("installer_listing_invalid", "An InstallShield file has no destination name.")
            try:
                relative = ((folder + b"\\") if folder else b"") + name
                add(relative.decode("utf-8"), lib.unshield_file_size(handle, index), index=index)
            except UnicodeDecodeError as error:
                raise InnoError("installer_filename_encoding", "This InstallShield cabinet needs an unsupported filename encoding.") from error
        for source, info in sources:
            if _fold(source.name) in loose_names and _fold(source.name) not in members:
                add(source.name, info[2], source=source)
        for name, _, _, _ in members.values():
            if any(_fold(parent.as_posix()) in members for parent in Path(name).parents if parent != Path(".")):
                raise InnoError("installer_path_collision", "An installer file conflicts with a destination directory.")
        yield lib, handle, members
        if _sources(path) != sources:
            raise InnoError("source_changed", "The InstallShield inputs changed while they were being read.")
    finally:
        lib.unshield_close(handle)


def _listing(path, cancel=None, loose_names=()):
    with _members(path, cancel, loose_names) as (_, _, members):
        return {name: size for name, size, _, _ in members.values()}


def _extract(path, destination, entries, cancel=None, selected=None, progress=None, loose_names=()):
    destination.mkdir(exist_ok=False)
    try:
        with _members(path, cancel, loose_names) as (lib, handle, members):
            if {name: size for name, size, _, _ in members.values()} != entries:
                raise InnoError("source_changed", "The InstallShield listing changed before extraction.")
            total = sum(size for name, size, _, _ in members.values() if selected is None or name in selected)
            install_jobs.disk_space(destination.parent, total + install_jobs.SAFETY_RESERVE_BYTES, "extracting")
            completed = 0
            for name, size, index, source in members.values():
                if cancel:
                    cancel()
                if selected is not None and name not in selected:
                    continue
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source:
                    shutil.copyfile(source, target)
                elif not lib.unshield_file_save(handle, index, os.fsencode(target)):
                    raise InnoError("installer_extraction_failed", f"InstallShield extraction failed for {name}.")
                if not target.is_file() or target.stat().st_size != size:
                    raise InnoError("installer_contents_mismatch", f"The extracted size is incorrect for {name}.")
                completed += size
                install_jobs.disk_space(destination.parent, install_jobs.SAFETY_RESERVE_BYTES, "extracting")
                if progress:
                    progress(completed, "Extracting game files")
        return destination
    except BaseException:
        shutil.rmtree(destination)
        raise


class UnshieldAdapter(InstallerAdapter):
    sources = staticmethod(_sources)

    def __init__(self, cfg):
        super().__init__()
        self.loose_names = {_fold(name) for name in cfg["launcher"]["candidate_executables"]}
        self.loose_names.update(_fold(Path(role["path"]).name) for role in cfg["setup"]["required_roles"])

    def listing(self, path, cancel=None):
        return _listing(path, cancel, self.loose_names)

    def extract_entries(self, path, destination, entries, cancel=None, selected=None, progress=None):
        return _extract(path, destination, entries, cancel, selected, progress, self.loose_names)
