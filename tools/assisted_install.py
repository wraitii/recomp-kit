import hashlib
import json
import os
import errno
from pathlib import Path
import stat
import time
import unicodedata
import uuid

import game_config
import install_jobs
import source_inspection


STABLE_SECONDS = 2.0
MAX_TEXT_BYTES = 256 * 1024


def checklist_text(cfg, candidate=None, drop_path=None):
    lines = [f"{cfg['game']['name']} assisted preparation", "",
             "Copy files you own into the content folder using the relative paths below.",
             "The library checks these files and never runs anything from this folder.",
             "The exact drop folder is the folder containing this checklist; copy game files under its content/ subfolder.", ""]
    if drop_path is not None:
        lines.insert(4, f"Drop folder: {Path(drop_path)}")
    preparations = (candidate.get("version", {}).get("preparations", []) if candidate else
                    list(cfg["setup"].get("preparations", {})))
    for preparation_id in preparations:
        preparation = cfg["setup"]["preparations"].get(preparation_id)
        if preparation:
            lines.extend([preparation["explanation"], ""])
    for item in cfg["setup"]["checklist"]["items"]:
        lines.extend([f"[{ 'required' if item.get('required', True) else 'optional' }] {item['id']}",
                      f"Purpose: {item['purpose']}", f"Where to find it: {item['acquisition']}",
                      f"Expected content path: content/{item['path']}"])
        if item.get("sha256"):
            lines.append(f"Required SHA-256: {item['sha256']}")
        lines.append("Accepted forms: " + "; ".join(item["accepted_forms"]))
        lines.append("")
    result = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    if len(result) > MAX_TEXT_BYTES:
        raise install_jobs.JobError("checklist_size_limit", "The game preparation checklist is too large to create.")
    return result


def _write_owner(path, identifier):
    info = path.stat()
    install_jobs.write_json(path / "owner.json", {"schema": install_jobs.SCHEMA, "jobId": identifier,
                                                    "device": info.st_dev, "inode": info.st_ino})
    return install_jobs.directory_token(path)


def _validate_assisted_owner(job_paths, journal):
    stage = install_jobs._check_owned_dir(job_paths["staging"], job_paths["meta"], journal["jobId"],
                                          token=journal.get("ownedTokens", {}).get("staging"))
    assisted = journal.get("assisted")
    if not isinstance(assisted, dict):
        raise install_jobs.JobError("invalid_assisted_job", "The assisted job has no preparation record.")
    drop = stage / "assisted" / "drop"
    if assisted.get("dropRelative") != f"staging-{journal['jobId']}/assisted/drop":
        raise install_jobs.JobError("invalid_assisted_path", "The job journal contains an unexpected drop-folder path.")
    assisted_root = stage / "assisted"
    if assisted_root.is_symlink() or drop.is_symlink() or not assisted_root.is_dir() or not drop.is_dir():
        raise install_jobs.JobError("invalid_assisted_path", "The owned preparation folder is missing or unsafe.")
    install_jobs._check_owned_dir(drop, drop.parent, journal["jobId"],
                                  token=assisted.get("dropToken"))
    return stage, drop


def _validate_assisted_paths(job_paths, journal):
    _, drop = _validate_assisted_owner(job_paths, journal)
    assisted = journal.get("assisted")
    if not isinstance(assisted, dict):
        raise install_jobs.JobError("invalid_assisted_job", "The assisted job has no preparation record.")
    content = drop / "content"
    if content.is_symlink() or not content.is_dir():
        raise install_jobs.JobError("invalid_assisted_path", "The preparation content folder is missing or unsafe.")
    if install_jobs.directory_token(content) != assisted.get("contentToken"):
        raise install_jobs.JobError("assisted_content_identity_changed", "The preparation content folder was replaced.")
    checklist = drop / "CHECKLIST.txt"
    if checklist.is_symlink() or not checklist.is_file():
        raise install_jobs.JobError("assisted_checklist_missing", "The preparation checklist is missing or unsafe.")
    size = checklist.stat(follow_symlinks=False).st_size
    if size > MAX_TEXT_BYTES:
        raise install_jobs.JobError("assisted_checklist_limit", "The preparation checklist exceeds its size limit.")
    with checklist.open("rb") as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise install_jobs.JobError("assisted_checklist_limit", "The preparation checklist exceeds its size limit.")
    digest = hashlib.sha256(data).hexdigest()
    if digest != assisted.get("checklistSha256"):
        raise install_jobs.JobError("assisted_checklist_changed", "The preparation checklist was changed.")
    allowed = {"owner.json", "CHECKLIST.txt", "content"}
    try:
        with os.scandir(drop) as entries:
            unexpected = []
            for entry in entries:
                if entry.name not in allowed:
                    unexpected.append(entry.name)
                    if len(unexpected) >= 32:
                        break
    except OSError as error:
        raise install_jobs.JobError("assisted_drop_unreadable", "The preparation folder cannot be listed safely.",
                                    details={"errno": error.errno}) from error
    if unexpected:
        raise install_jobs.JobError("assisted_unexpected_entry", "The preparation folder contains files outside content/.",
                                    details={"entries": unexpected[:32]})
    return drop, content


def _snapshot(root, cancel=None):
    root = Path(root).resolve(strict=True)
    stack = [(root, 0)]
    rows = []
    total = 0
    files = 0
    entries_seen = 0
    directories = {}
    invalid = []
    changed = False
    changed_paths = []
    while stack:
        directory, depth = stack.pop()
        names = []
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    if cancel:
                        cancel()
                    entries_seen += 1
                    if entries_seen > install_jobs.MAX_PAYLOAD_FILES * 2:
                        raise install_jobs.JobError("assisted_entry_limit", "The preparation folder has too many entries.")
                    names.append(entry)
        except OSError as error:
            if error.errno == errno.ENOENT:
                changed = True
                changed_paths.append(directory.relative_to(root).as_posix() or ".")
            else:
                invalid.append({"path": directory.relative_to(root).as_posix() or ".",
                                "code": "unreadable", "errno": error.errno})
            continue
        names.sort(key=lambda item: (unicodedata.normalize("NFC", item.name).casefold(),
                                     unicodedata.normalize("NFC", item.name), item.name))
        folded = {}
        for entry in names:
            key = unicodedata.normalize("NFC", entry.name).casefold()
            folded.setdefault(key, []).append(entry.name)
        collisions = {name for group in folded.values() if len(group) > 1 for name in group}
        for entry in names:
            if cancel:
                cancel()
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if any(ord(character) < 32 or ord(character) == 127 for character in entry.name):
                invalid.append({"path": relative, "code": "invalid_name"})
                rows.append((relative, "x", 0, 0, 0, 0))
                continue
            portable_name = entry.name.rstrip(" .")
            if not portable_name or portable_name != entry.name:
                invalid.append({"path": relative, "code": "portable_name_collision"})
                rows.append((relative, "x", 0, 0, 0, 0))
                continue
            if len(relative.encode("utf-8")) > install_jobs.MAX_PATH_BYTES:
                invalid.append({"path": relative, "code": "path_limit"})
                continue
            if entry.name in collisions:
                invalid.append({"path": relative, "code": "path_collision"})
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                changed = True
                changed_paths.append(relative)
                continue
            if stat.S_ISLNK(info.st_mode):
                invalid.append({"path": relative, "code": "symlink"})
                rows.append((relative, "x", info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino))
                continue
            if stat.S_ISDIR(info.st_mode):
                if depth >= install_jobs.MAX_PATH_DEPTH:
                    invalid.append({"path": relative, "code": "depth_limit"})
                    continue
                directories[unicodedata.normalize("NFC", relative).casefold()] = path
                rows.append((relative, "d", 0, info.st_mtime_ns, info.st_dev, info.st_ino))
                stack.append((path, depth + 1))
            elif stat.S_ISREG(info.st_mode):
                files += 1
                total += info.st_size
                if files > install_jobs.MAX_PAYLOAD_FILES or total > install_jobs.MAX_PAYLOAD_BYTES:
                    raise install_jobs.JobError("assisted_content_limit", "The preparation content exceeds its size or file limit.",
                                                details={"files": files, "bytes": total})
                rows.append((relative, "f", info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino))
            else:
                invalid.append({"path": relative, "code": "special_file"})
                rows.append((relative, "x", info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino))
        for relative in (item["path"] for item in invalid if item.get("code") in ("path_limit", "path_collision")):
            rows.append((relative, "x", 0, 0, 0, 0))
    rows.sort(key=lambda row: (unicodedata.normalize("NFC", row[0]).casefold(),
                               unicodedata.normalize("NFC", row[0]), row[0]))
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"digest": digest, "rows": rows, "files": files, "bytes": total, "entries": entries_seen,
            "directories": directories, "invalid": invalid, "changed": changed,
            "changedPaths": changed_paths}


def _item_states(cfg, checks, snapshot, base_states=None):
    by_id = {item["id"]: item for item in checks}
    base_by_id = {item["id"]: item for item in (base_states or [])}
    states = []
    for item in cfg["setup"]["checklist"]["items"]:
        check = by_id.get(item["id"])
        diagnostic = None
        if check is None:
            base = base_by_id.get(item["id"], {})
            status = base.get("status", "missing")
            message = base.get("message", "Waiting for this file or folder.")
        elif snapshot["invalid"]:
            status = "unreadable"
            message = "The preparation folder contains an unsafe or unreadable path."
        else:
            status = check["status"]
            if status == "limit":
                status = "unreadable"
            elif status == "changed":
                status = "settling"
            message = {
                "verified": "Verified.", "missing": "Waiting for this file or folder.",
                "settling": "Wait for files to finish copying.",
                "wrong_version": "This item does not match the configured supported content.",
                "unreadable": "This item could not be read safely.", "limit": "This item exceeds the inspection limit.",
                "changed": "This item changed during validation."
            }.get(status, "This item could not be verified.")
            if status not in ("verified", "missing", "settling"):
                diagnostic = {"code": "required_artifact_" + status, "stage": "checking",
                              "path": check.get("actualPath") or item["path"], "message": message,
                              "details": {"role": item["role"], "expectedSha256": check.get("expectedSha256"),
                                          "actualPath": check.get("actualPath")},
                              "recovery": ["retry_check", "open_drop_folder", "cancel_job"]}
        state = {"id": item["id"], "role": item["role"], "required": item.get("required", True),
                 "status": status, "message": message, "path": item["path"]}
        if diagnostic:
            state["diagnostic"] = diagnostic
        states.append(state)
    return states


def _item_digest(snapshot, item_path, optional_paths=()):
    prefix = unicodedata.normalize("NFC", item_path.replace("\\", "/")).strip("/").casefold()
    optional = [unicodedata.normalize("NFC", path.replace("\\", "/")).strip("/").casefold()
                for path in optional_paths]
    rows = []
    for row in snapshot["rows"]:
        relative = unicodedata.normalize("NFC", row[0]).casefold()
        if not (relative == prefix or relative.startswith(prefix + "/")):
            continue
        if any(relative == path or relative.startswith(path + "/") for path in optional):
            continue
        rows.append((row[0], row[1], row[2], 0 if row[1] == "d" else row[3], row[4], row[5]))
    if not rows:
        return None
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _item_changed(snapshot, item_path, optional_paths=()):
    prefix = unicodedata.normalize("NFC", item_path.replace("\\", "/")).strip("/").casefold()
    optional = [unicodedata.normalize("NFC", path.replace("\\", "/")).strip("/").casefold()
                for path in optional_paths]
    for changed in snapshot["changedPaths"]:
        relative = unicodedata.normalize("NFC", changed).casefold()
        if relative != prefix and not relative.startswith(prefix + "/"):
            continue
        if any(relative == path or relative.startswith(path + "/") for path in optional):
            continue
        return True
    return False


def _status_response(journal, drop, content, items, can_continue, diagnostics=None, **fields):
    return {"schema": install_jobs.SCHEMA, "jobId": journal["jobId"],
            "state": "waiting_for_input", "dropFolder": str(drop), "contentFolder": str(content),
            "items": items, "canContinue": can_continue,
            "result": "OK" if can_continue else "FAIL", "diagnostics": diagnostics or [], **fields}


def _game(kit, game_id):
    directory = Path(kit) / "games" / game_id
    if directory.parent != Path(kit) / "games" or game_id == "stub" or not (directory / "game.toml").is_file():
        raise install_jobs.JobError("unknown_game", "The requested game is unavailable.")
    return directory


def _activity(storage, game_id):
    registry_dir = install_jobs.registry_game_dir(storage, game_id)
    path = registry_dir / "activity.json"
    value = install_jobs.read_versioned(path, "activity") if path.is_file() else {"schema": install_jobs.SCHEMA, "activeJobs": []}
    if not isinstance(value.get("activeJobs"), list):
        raise install_jobs.JobError("invalid_activity", "The active job registry is invalid.")
    return registry_dir, path, value


def _locate(storage, game_id, identifier):
    identifier = install_jobs.job_id(identifier)
    _, _, activity = _activity(storage, game_id)
    for locator in activity["activeJobs"]:
        if isinstance(locator, dict) and locator.get("jobId") == identifier:
            destination = locator.get("destination")
            if not isinstance(destination, str):
                break
            root = Path(destination).expanduser().resolve()
            job_paths, journal = install_jobs.load_job(root, identifier)
            if journal.get("mode") != "assisted" or journal.get("gameId") != game_id:
                raise install_jobs.JobError("job_mode_mismatch", "The saved job is not an assisted job for this game.")
            return root, job_paths, journal
    raise install_jobs.JobError("job_not_found", "The saved assisted job is no longer available.")


def _register(storage, game_id, root, journal):
    registry_dir, activity_path, activity = _activity(storage, game_id)
    active = []
    for locator in activity["activeJobs"]:
        if isinstance(locator, dict) and locator.get("jobId") != journal["jobId"]:
            active.append(locator)
    active.append({"jobId": journal["jobId"], "destination": str(root)})
    registry_dir.mkdir(parents=True, exist_ok=True)
    install_jobs.write_json(activity_path, {"schema": install_jobs.SCHEMA, "activeJobs": active})


def start(request, storage, kit, cancel=None, recipe=None, inspect_folder=None, emit=None):
    game_id = request["game"]
    directory = _game(kit, game_id)
    cfg = game_config.load(directory)
    root = Path(request.get("destination") or install_jobs.registry_game_dir(storage, game_id) / "pending").expanduser().resolve()
    source_value = request.get("source")
    source_record = None
    source_identity = {"kind": "none"}
    candidate = None
    if source_value:
        source = Path(source_value).expanduser().resolve(strict=True)
        if source.is_dir():
            source_record = {"kind": "folder", "path": str(source)}
            if (root == source or root.is_relative_to(source) or source.is_relative_to(root)):
                raise install_jobs.JobError("source_destination_overlap", "The selected source overlaps the preparation destination.",
                                            ["Choose a separate empty destination."])
            if Path(storage).expanduser().resolve().is_relative_to(source):
                raise install_jobs.JobError("storage_source_overlap", "The library storage folder is inside the selected source.",
                                            ["Choose storage outside the source folder before preparing it."])
            info = source.stat()
            source_identity = {"kind": "folder_reference", "modifiedNs": info.st_mtime_ns,
                               "device": info.st_dev, "inode": info.st_ino}
        elif source.is_file():
            if source.is_relative_to(root):
                raise install_jobs.JobError("source_destination_overlap", "The selected source file is inside the preparation destination.",
                                            ["Choose a separate empty destination."])
            info = source.stat()
            source_record = {"kind": "media_reference", "path": str(source)}
            source_identity = {"kind": "media_reference", "size": info.st_size,
                               "modifiedNs": info.st_mtime_ns, "device": info.st_dev, "inode": info.st_ino}
        else:
            raise install_jobs.JobError("unsupported_source", "Choose a regular file or folder as a reference.")
    recipe_identity = recipe(directory) if recipe else install_jobs.recipe_identity(kit, directory)
    with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
        with install_jobs.DestinationLock(root):
            facts = root / ".recomp" / "installation.json"
            if facts.is_file():
                saved = install_jobs.read_versioned(facts, "installation")
                if saved.get("gameId") != game_id:
                    raise install_jobs.JobError("destination_game_mismatch", "The destination belongs to another game.")
                raise install_jobs.JobError("assisted_update_unsupported", "Assisted preparation cannot replace an installed content folder yet.",
                                            ["Choose an empty destination, or use its current installed folder for a regular rebuild."])
            for child in root.iterdir():
                if child.name == ".recomp" and child.is_dir() and not child.is_symlink():
                    allowed = {"install.lock", "registry.lock", "jobs", "logs"}
                    extras = [entry.name for entry in child.iterdir()
                              if entry.name not in allowed]
                    if extras:
                        raise install_jobs.JobError("destination_not_empty", "The destination has unrelated files.",
                                                    ["Choose an empty destination."], {"entries": extras})
                elif child.name != ".recomp":
                    raise install_jobs.JobError("destination_not_empty", "The destination has unrelated files.",
                                                ["Choose an empty destination."], {"entry": child.name})
            job_paths, journal = install_jobs.create_job(root, game_id, source_record,
                source_identity, recipe_identity, candidate.get("id") if candidate else None, mode="assisted")
            try:
                staging = job_paths["staging"]
                assisted_root = staging / "assisted"
                assisted_root.mkdir()
                drop = assisted_root / "drop"
                drop.mkdir()
                drop_token = _write_owner(drop, journal["jobId"])
                text = checklist_text(cfg, candidate, drop)
                checklist = drop / "CHECKLIST.txt"
                with checklist.open("xb") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                content = drop / "content"
                content.mkdir()
                content_token = install_jobs.directory_token(content)
                journal["assisted"] = {"dropRelative": f"staging-{journal['jobId']}/assisted/drop",
                    "dropToken": drop_token, "contentToken": content_token,
                    "checklistSha256": hashlib.sha256(text).hexdigest(),
                    "items": [item["id"] for item in cfg["setup"]["checklist"]["items"]],
                    "sourceReferenceIdentity": source_identity, "sourceReference": source_record,
                    "settle": None, "states": []}
                install_jobs.update_job(job_paths, journal, "waiting_for_input")
                _register(storage, game_id, root, journal)
            except BaseException:
                try:
                    install_jobs.remove_owned(job_paths["staging"], job_paths["meta"], journal["jobId"],
                                              journal.get("ownedTokens", {}).get("staging"))
                except (OSError, ValueError):
                    pass
                job_paths["journal"].unlink(missing_ok=True)
                raise
    if emit:
        emit("stage", stage="waiting_for_input", message="Copy the listed files into the preparation folder",
             jobId=journal["jobId"], destination=str(root))
    return {"schema": install_jobs.SCHEMA, "game": game_id, "state": "waiting_for_input", "jobId": journal["jobId"],
            "destination": str(root), "dropFolder": str(drop), "contentFolder": str(content),
            "checklist": str(checklist), "candidate": candidate}


def status(request, storage, kit, session_id, cancel=None):
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as error:
        raise install_jobs.JobError("invalid_watch_session", "The preparation watch session is invalid.") from error
    game_id = request["game"]
    directory = _game(kit, game_id)
    cfg = game_config.load(directory)
    root, _, _ = _locate(storage, game_id, request.get("jobId"))
    with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
        with install_jobs.DestinationLock(root):
            root, job_paths, journal = _locate(storage, game_id, request.get("jobId"))
            if journal.get("stage") != "waiting_for_input":
                raise install_jobs.JobError("assisted_not_waiting", "This assisted job is not waiting for files.")
            drop, content = _validate_assisted_paths(job_paths, journal)
            snapshot = _snapshot(content, cancel)
            assisted = journal["assisted"]
            settle = assisted.get("settle")
            now = time.time()
            new_session = settle is None or settle.get("sessionId") != session_id
            if new_session:
                settle = {"sessionId": session_id, "items": {}, "checkPending": False}
                assisted["recipeSessionId"] = None
            item_records = settle.setdefault("items", {})
            optional_paths = [item["path"] for item in cfg["setup"]["checklist"]["items"]
                              if not item.get("required", True)]
            states = []
            item_changed = False
            for item in cfg["setup"]["checklist"]["items"]:
                ignored_optional = (optional_paths if item.get("required", True) else
                                    [path for path in optional_paths if path != item["path"]])
                digest = _item_digest(snapshot, item["path"], ignored_optional)
                record = item_records.get(item["id"])
                if record is None or record.get("digest") != digest:
                    record = {"digest": digest, "since": now}
                    item_records[item["id"]] = record
                    item_changed = item_changed or bool(item.get("required", True))
                present = digest is not None
                stable = (present and not _item_changed(snapshot, item["path"], ignored_optional)
                          and now - record["since"] >= STABLE_SECONDS)
                status_value = "missing" if not present else "settling" if not stable else "checking"
                message = "Waiting for this file or folder." if not present else (
                    "Wait for files to finish copying." if not stable else "Checking this item.")
                states.append({"id": item["id"], "role": item["role"], "required": item.get("required", True),
                               "path": item["path"], "status": status_value, "message": message})
            if snapshot["invalid"]:
                states = [{**item, "status": "unreadable",
                           "message": "The preparation folder contains an unsafe or unreadable path."}
                          for item in states]
            assisted["settle"] = settle
            assisted["states"] = states
            if item_changed:
                settle["checkPending"] = False
            invalid_diagnostics = [{"code": item.get("code", "preparation_invalid"), "stage": "checking",
                                    "path": item.get("path", "."), "message": "This path cannot be used safely.",
                                    "details": item, "recovery": ["retry_check", "open_drop_folder", "cancel_job"]}
                                   for item in snapshot["invalid"]]
            if new_session or snapshot["invalid"]:
                install_jobs.update_job(job_paths, journal, "waiting_for_input", assisted=assisted)
                return _status_response(journal, drop, content, states, False, invalid_diagnostics,
                                        sessionReset=new_session, checking=False)
            if assisted.get("recipeSessionId") != session_id:
                recipe_now = install_jobs.recipe_identity(kit, directory)
                assisted["recipeSessionId"] = session_id
                if recipe_now.get("sha256") != journal.get("recipeIdentity", {}).get("sha256"):
                    assisted["recipeChanged"] = True
                    install_jobs.update_job(job_paths, journal, "waiting_for_input", assisted=assisted)
                    return _status_response(journal, drop, content, assisted.get("states", []), False,
                                            recipeChanged=True)
            if assisted.get("recipeChanged"):
                install_jobs.update_job(job_paths, journal, "waiting_for_input", assisted=assisted)
                return _status_response(journal, drop, content, states, False, recipeChanged=True)
            stable_items = [item for item, state in zip(cfg["setup"]["checklist"]["items"], states)
                            if state["status"] == "checking"]
            if not stable_items:
                install_jobs.update_job(job_paths, journal, "waiting_for_input", assisted=assisted)
                return _status_response(journal, drop, content, states, False)
            if not settle.get("checkPending"):
                settle["checkPending"] = True
                assisted["states"] = states
                install_jobs.update_job(job_paths, journal, "waiting_for_input", assisted=assisted)
                return _status_response(journal, drop, content, assisted["states"], False, checking=True)
            checks, diagnostics, _, _ = source_inspection.validate_required_roles(
                cfg, content, artifacts=stable_items)
            states = _item_states(cfg, checks, snapshot, states)
            layout_diagnostics = []
            all_required = all(not item["required"] or item["status"] == "verified" for item in states)
            installable = False
            if all_required:
                inspection = source_inspection.inspect_folder(cfg, content, cancel)
                viable = [candidate for candidate in inspection["candidates"] if candidate.get("installable")]
                installable = (inspection.get("scanComplete") is True and len(viable) == 1
                               and Path(viable[0]["contentRoot"]).resolve() == content.resolve())
                if not installable:
                    layout_diagnostics = [diagnostic for candidate in inspection["candidates"]
                                          for diagnostic in candidate.get("diagnostics", [])]
                    layout_diagnostics = layout_diagnostics or inspection.get("diagnostics", [])
                    if not layout_diagnostics:
                        layout_diagnostics = [{"code": "candidate_not_installable", "stage": "checking",
                            "path": ".", "message": "The supplied files do not form one complete supported installation layout.",
                            "details": {"viableCandidates": len(viable),
                                        "scanComplete": inspection.get("scanComplete")},
                            "recovery": ["retry_check", "open_drop_folder", "cancel_job"]}]
            all_required = all_required and installable
            after = _snapshot(content, cancel)
            before_items = {item["id"]: _item_digest(
                                snapshot, item["path"], optional_paths if item.get("required", True) else
                                [path for path in optional_paths if path != item["path"]])
                            for item in cfg["setup"]["checklist"]["items"]}
            after_items = {item["id"]: _item_digest(
                               after, item["path"], optional_paths if item.get("required", True) else
                               [path for path in optional_paths if path != item["path"]])
                           for item in cfg["setup"]["checklist"]["items"]}
            if any(_item_changed(after, item["path"], optional_paths)
                   or before_items[item["id"]] != after_items[item["id"]]
                                        for item in cfg["setup"]["checklist"]["items"]
                                        if item.get("required", True)):
                states = [{**item, "status": "settling", "message": "Files changed during checking; wait for them to settle."}
                          if item["required"] else item for item in states]
                settle["items"] = {item["id"]: {"digest": after_items[item["id"]], "since": now}
                                    for item in cfg["setup"]["checklist"]["items"]}
                all_required = False
            assisted["states"] = states
            structured = [{"code": item.get("code", "preparation_invalid"), "stage": "checking",
                           "path": item.get("path", "."), "message": "This path cannot be used safely.",
                           "details": item, "recovery": ["retry_check", "open_drop_folder", "cancel_job"]}
                          for item in snapshot["invalid"]]
            structured.extend({**item, "stage": "checking", "recovery": ["retry_check", "open_drop_folder", "cancel_job"]}
                              for item in diagnostics)
            structured.extend(layout_diagnostics)
            assisted["lastDiagnostics"] = structured
            install_jobs.update_job(job_paths, journal, "waiting_for_input", assisted=assisted)
            return _status_response(journal, drop, content, states, all_required,
                                    assisted["lastDiagnostics"])


def path(request, storage):
    game_id = request["game"]
    root, job_paths, journal = _locate(storage, game_id, request.get("jobId"))
    with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
      with install_jobs.DestinationLock(root):
        root, job_paths, journal = _locate(storage, game_id, request.get("jobId"))
        _, drop = _validate_assisted_owner(job_paths, journal)
        content = drop / "content"
        safe_content = content.is_dir() and not content.is_symlink()
        validation_error = None
        if safe_content:
            try:
                _validate_assisted_paths(job_paths, journal)
            except (OSError, ValueError) as error:
                validation_error = str(error)
        else:
            validation_error = "The content folder is missing or unsafe."
        checklist_path = drop / "CHECKLIST.txt"
        checklist_text_value = None
        if checklist_path.is_file() and not checklist_path.is_symlink():
            with checklist_path.open("rb") as stream:
                checklist_data = stream.read(MAX_TEXT_BYTES + 1)
            if len(checklist_data) <= MAX_TEXT_BYTES:
                checklist_text_value = checklist_data.decode("utf-8", errors="replace")
        return {"schema": install_jobs.SCHEMA, "jobId": journal["jobId"], "dropFolder": str(drop), "contentFolder": str(content),
                "checklist": str(checklist_path), "checklistText": checklist_text_value,
                "validationError": validation_error}


def cancel_job(request, storage):
    if request.get("confirmed") is not True:
        raise install_jobs.JobError("confirmation_required", "Confirm removal of files copied into the job-owned preparation folder.")
    game_id = request["game"]
    root, _, _ = _locate(storage, game_id, request.get("jobId"))
    with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
        with install_jobs.DestinationLock(root):
            root, job_paths, journal = _locate(storage, game_id, request.get("jobId"))
            if journal.get("stage") not in ("waiting_for_input", "cancelling_assisted"):
                raise install_jobs.JobError("assisted_not_waiting", "Only a waiting assisted job can be cancelled this way.")
            if job_paths["staging"].exists():
                _validate_assisted_owner(job_paths, journal)
            install_jobs.update_job(job_paths, journal, "cancelling_assisted",
                                    assisted={**journal.get("assisted", {}), "cancelledByUser": True})
            if job_paths["staging"].exists():
                install_jobs.remove_owned(job_paths["staging"], job_paths["meta"], journal["jobId"],
                                          journal.get("ownedTokens", {}).get("staging"))
            install_jobs.update_job(job_paths, journal, "cancelled",
                                    assisted={**journal.get("assisted", {}), "artifactsRemoved": True})
            _, activity_path, activity = _activity(storage, game_id)
            activity["activeJobs"] = [locator for locator in activity["activeJobs"]
                                      if not isinstance(locator, dict) or locator.get("jobId") != journal["jobId"]]
            install_jobs.write_json(activity_path, activity)
            return {"schema": install_jobs.SCHEMA, "jobId": journal["jobId"], "state": "cancelled"}


def recheck(request, storage, kit, recipe):
    game_id = request["game"]
    directory = _game(kit, game_id)
    root, _, _ = _locate(storage, game_id, request.get("jobId"))
    with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
        with install_jobs.DestinationLock(root):
            root, job_paths, journal = _locate(storage, game_id, request.get("jobId"))
            if journal.get("stage") != "waiting_for_input":
                raise install_jobs.JobError("assisted_not_waiting", "This assisted job is not waiting for files.")
            _, drop = _validate_assisted_owner(job_paths, journal)
            assisted = journal["assisted"]
            content = drop / "content"
            if (content.is_symlink() or not content.is_dir()
                    or install_jobs.directory_token(content) != assisted.get("contentToken")):
                raise install_jobs.JobError("assisted_content_identity_changed", "The preparation content folder was replaced.",
                                            ["Start a new preparation", "Cancel this job"])
            checklist = drop / "CHECKLIST.txt"
            if checklist.is_symlink() or not checklist.is_file() or checklist.stat(follow_symlinks=False).st_size > MAX_TEXT_BYTES:
                raise install_jobs.JobError("assisted_checklist_changed", "The generated checklist is missing or unsafe.",
                                            ["Start a new preparation", "Cancel this job"])
            with checklist.open("rb") as stream:
                current_text = stream.read(MAX_TEXT_BYTES + 1)
            if len(current_text) > MAX_TEXT_BYTES:
                raise install_jobs.JobError("assisted_checklist_limit", "The generated checklist exceeds its size limit.")
            if hashlib.sha256(current_text).hexdigest() != assisted.get("checklistSha256"):
                raise install_jobs.JobError("assisted_checklist_changed", "The generated checklist was edited; it was left untouched.",
                                            ["Start a new preparation", "Cancel this job"])
            current = recipe(directory)
            updated_text = checklist_text(game_config.load(directory), drop_path=drop)
            temp = drop / (".CHECKLIST-" + uuid.uuid4().hex + ".tmp")
            try:
                with temp.open("xb") as stream:
                    stream.write(updated_text)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(checklist)
                install_jobs._sync_directory(drop)
            finally:
                temp.unlink(missing_ok=True)
            work = job_paths["staging"] / "work"
            if work.exists():
                install_jobs.remove_owned(work, job_paths["staging"], journal["jobId"],
                                          journal.get("ownedTokens", {}).get("work"))
            journal.setdefault("ownedTokens", {}).pop("work", None)
            assisted.pop("validatedDropBytes", None)
            journal["recipeIdentity"] = current
            assisted = {**assisted, "recipeChanged": False, "recipeSessionId": None,
                        "checklistSha256": hashlib.sha256(updated_text).hexdigest(),
                        "items": [item["id"] for item in game_config.load(directory)["setup"]["checklist"]["items"]],
                        "states": [], "settle": None}
            journal["validatedInputs"] = {}
            install_jobs.update_job(job_paths, journal, "waiting_for_input", assisted=assisted,
                                    validatedInputs={})
            return {"schema": install_jobs.SCHEMA, "jobId": journal["jobId"],
                    "state": "waiting_for_input", "recipeChanged": False, "result": "OK"}
