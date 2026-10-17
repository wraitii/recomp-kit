import argparse
from contextlib import nullcontext
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import queue
import time
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile

import game_config
from setup import validate_game
from web_launcher import game_entry
from source_inspection import (MAX_DISCOVERY_CANDIDATES, MAX_DISCOVERY_ENTRIES,
                               MAX_DISCOVERY_HASH_BYTES, MAX_DISCOVERY_ROOTS,
                               expand_discovery_roots, inspect_folder, probe_bytes,
                               sibling_payload_facts)
from zip_adapter import (ZipError, archive_digest, archive_uncompressed_size,
                         extract as extract_zip)

CANCEL_FILE = None
KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "tools/recomp"))
import install_jobs
import assisted_install


class SourceError(ValueError):
    def __init__(self, code, message, recovery, path=None, details=None):
        super().__init__(message)
        self.diagnostic = {"code": code, "stage": "inspecting", "path": path or ".",
                           "message": message, "details": details or {}, "recovery": recovery}


def emit(kind, **fields):
    print(json.dumps({"kind": kind, **fields}), flush=True)


def cancel(signum, frame):
    raise InterruptedError("Installation cancelled")


def check_cancel():
    if CANCEL_FILE is not None and CANCEL_FILE.exists():
        raise InterruptedError("Installation cancelled")


DESTINATION_LOCKS = []


class BuildFailure(ValueError):
    def __init__(self, command, returncode, log_path):
        self.command = [str(part) for part in command]
        self.returncode = returncode
        self.log_path = str(log_path) if log_path else None
        super().__init__(f"{Path(command[0]).name} failed with exit code {returncode}")


def run(command, log_path=None, build_monitor=None, env=None):
    check_cancel()
    log = None
    if log_path:
        flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        log_fd = os.open(log_path, flags, 0o600)
        if not stat.S_ISREG(os.fstat(log_fd).st_mode):
            os.close(log_fd)
            raise install_jobs.JobError("unsafe_log_path", "The job log is not a regular file.")
        log = os.fdopen(log_fd, "wb")
    inherited = [lock.fd for lock in DESTINATION_LOCKS if lock.fd is not None] if os.name != "nt" else []
    try:
        process = subprocess.Popen(command, cwd=KIT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace",
                                   start_new_session=os.name != "nt", pass_fds=inherited, env=env)
    except BaseException:
        if log:
            log.close()
        raise
    output = queue.Queue(maxsize=32)

    def read_output():
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                while True:
                    try:
                        output.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        if process.poll() is not None:
                            return
        finally:
            while True:
                try:
                    output.put(None, timeout=0.1)
                    break
                except queue.Full:
                    if process.poll() is not None:
                        try:
                            output.get_nowait()
                        except queue.Empty:
                            pass

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        logged = os.fstat(log.fileno()).st_size if log else 0
        max_log_bytes = install_jobs.MAX_ATTEMPT_LOG_BYTES - install_jobs.MAX_LOG_FAILURE_RESERVE
        next_monitor = 0.0

        def monitor(force=False):
            nonlocal next_monitor
            if not build_monitor or (not force and time.monotonic() < next_monitor):
                return
            started = time.monotonic()
            current = install_jobs.tree_size(build_monitor["root"], check_cancel)
            growth = max(0, current - build_monitor["baselineBytes"])
            if growth > install_jobs.MAX_BUILD_BYTES:
                raise install_jobs.JobError("build_output_limit", "The staged build exceeded its configured output allowance.",
                    ["Review the build log and free destination space before retrying."],
                    {"path": str(build_monitor["root"]), "baselineBytes": build_monitor["baselineBytes"],
                     "currentBytes": current, "growthBytes": growth,
                     "limitBytes": install_jobs.MAX_BUILD_BYTES, "sampleIntervalSeconds": 0.5})
            free = shutil.disk_usage(build_monitor["volume"]).free
            if free < install_jobs.SAFETY_RESERVE_BYTES:
                raise install_jobs.JobError("disk_safety_reserve", "The destination volume reached the configured free-space reserve during the build.",
                    ["Free destination space and retry the installation."],
                    {"path": str(build_monitor["volume"]), "freeBytes": free,
                     "safetyReserveBytes": install_jobs.SAFETY_RESERVE_BYTES,
                     "sampleIntervalSeconds": 0.5})
            next_monitor = max(time.monotonic(), started + 0.5)

        with (log if log else nullcontext()) as active_log:
            while True:
                check_cancel()
                try:
                    line = output.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None:
                        reader.join(timeout=2)
                        if reader.is_alive():
                            if os.name != "nt":
                                try:
                                    os.killpg(process.pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                            break
                    monitor()
                    continue
                if line is None:
                    break
                if active_log and logged < max_log_bytes:
                    encoded = line.encode("utf-8", errors="replace")
                    active_log.write(encoded[:max_log_bytes - logged])
                    active_log.flush()
                    logged += min(len(encoded), max_log_bytes - logged)
                for displayed in line.splitlines() or [line]:
                    emit("log", text=displayed[:65536])
                monitor()
            monitor(force=True)
        while process.poll() is None:
            check_cancel()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            monitor()
        monitor(force=True)
        if process.returncode:
            raise BuildFailure(command, process.returncode, log_path)
    except BaseException:
        if os.name == "nt":
            if process.poll() is None:
                stopped = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                         capture_output=True, text=True)
                if stopped.returncode and process.poll() is None:
                    emit("log", text=f"Cannot stop build process: {stopped.stdout}{stopped.stderr}")
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if os.name == "nt" or process.poll() is None:
            process.wait(timeout=10)
        raise
    finally:
        reader.join(timeout=2)
        if not reader.is_alive():
            process.stdout.close()


def installation_root(storage, game_id):
    registry_dir = install_jobs.registry_game_dir(storage, game_id)
    record = registry_dir / "registry.json"
    if record.is_file() and not record.is_symlink():
        value = install_jobs.read_versioned(record, "registry")
        if value.get("gameId") != game_id or not isinstance(value.get("destination"), str):
            raise install_jobs.JobError("invalid_registry", "The installation registry does not match this game.")
        return Path(value["destination"]).expanduser().resolve()
    return storage / game_id


def artifact(cfg, root, installed=True):
    name = cfg["game"]["app_name"]
    if platform.system() == "Darwin":
        return root / ("app" if installed else "") / f"{name}.app/Contents/MacOS/{name}"
    directory = root / "app" if installed else root / "package" / name
    return directory / (name + (".exe" if os.name == "nt" else ""))


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def recipe(game_dir):
    return install_jobs.recipe_identity(KIT, game_dir)


def log_event(path, stage, event, details=None, failure=False):
    try:
        install_jobs.append_job_log(path, stage, event, details, failure)
    except (OSError, ValueError) as error:
        emit("log", text=f"Could not persist the {stage} job log event: {error}")


def check_space(job_paths, journal, root, phase, additional_bytes, log_path, plan):
    try:
        log_size = log_path.stat().st_size
    except FileNotFoundError:
        log_size = 0
    log_remaining = max(0, install_jobs.MAX_ATTEMPT_LOG_BYTES - log_size)
    required = max(0, additional_bytes) + install_jobs.SAFETY_RESERVE_BYTES + log_remaining
    check = install_jobs.disk_space(root, required, phase, plan)
    checks = journal.setdefault("spaceChecks", [])
    checks.append(check)
    del checks[:-32]
    journal["spaceBudget"] = plan
    install_jobs.update_job(job_paths, journal, journal.get("stage", phase),
                            spaceChecks=checks, spaceBudget=plan)
    log_event(log_path, phase, "destination space preflight passed",
              {"freeBytes": check["freeBytes"], "requiredBytes": check["requiredBytes"]})
    return check


def error_diagnostic(error, stage, root, log_path):
    log_relative = f".recomp/logs/{Path(log_path).name}"
    if isinstance(error, InterruptedError):
        return {"code": "cancelled", "stage": "cancelled", "path": ".",
                "message": "The installation was cancelled.",
                "details": {"logPath": log_relative}, "recovery": ["resume", "choose_source"]}
    if isinstance(error, SourceError):
        result = dict(error.diagnostic)
        result["stage"] = stage
        affected = result.get("path", ".")
        if isinstance(affected, str) and Path(affected).is_absolute():
            try:
                result["path"] = Path(affected).resolve(strict=False).relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                result["path"] = Path(affected).name
        result.setdefault("details", {})["logPath"] = log_relative
        return result
    if isinstance(error, BuildFailure):
        tool = error.command[1] if len(error.command) > 1 and error.command[1].endswith(".py") else error.command[0]
        return {"code": "build_failed", "stage": stage, "path": ".",
                "message": str(error), "details": {"tool": Path(tool).name,
                    "command": error.command, "returnCode": error.returncode,
                    "logPath": log_relative}, "recovery": ["open_log", "retry"]}
    if isinstance(error, install_jobs.JobError):
        details = dict(error.details)
        affected = details.get("path", ".")
        if isinstance(affected, str):
            try:
                affected = Path(affected).resolve(strict=False).relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                affected = Path(affected).name if Path(affected).is_absolute() else affected
        details["logPath"] = log_relative
        return {"code": error.code, "stage": stage, "path": affected,
                "message": str(error), "details": details,
                "recovery": error.recovery or ["open_log", "retry"]}
    if isinstance(error, OSError):
        code = "insufficient_space" if error.errno == errno.ENOSPC else (
            "permission_denied" if error.errno in (errno.EACCES, errno.EPERM)
            else "filesystem_error")
        affected = os.fsdecode(error.filename) if error.filename else "."
        try:
            affected = Path(affected).resolve(strict=False).relative_to(Path(root).resolve()).as_posix()
        except (ValueError, TypeError):
            affected = Path(os.fsdecode(affected)).name if isinstance(affected, (str, bytes, os.PathLike)) else "."
        return {"code": code, "stage": stage, "path": affected, "message": str(error),
                "details": {"errno": error.errno, "logPath": log_relative},
                "recovery": ["free_space", "open_log", "retry"] if code == "insufficient_space"
                            else ["check_permissions", "open_log", "retry"]}
    return {"code": "installation_failed", "stage": stage, "path": ".",
            "message": str(error), "details": {"logPath": log_relative},
            "recovery": ["open_log", "retry"]}


def recover_publication(root, identifier):
    root = Path(root).expanduser().resolve()
    job_paths, journal = install_jobs.load_job(root, identifier)
    publication = journal.get("publication")
    facts_path = root / ".recomp" / "installed.json"
    if journal.get("stage") == "complete":
        committed_facts = journal.get("validatedOutputs", {}).get("installed", {})
        if committed_facts.get("jobId") != identifier:
            raise install_jobs.JobError("completed_job_mismatch", "The completed job record does not identify its commit.")
        for key in ("staging", "backup"):
            if job_paths[key].exists():
                install_jobs.remove_owned(job_paths[key], job_paths["meta"], identifier,
                                          journal.get("ownedTokens", {}).get(key))
        return journal
    if not publication:
        return journal
    if facts_path.is_file():
        facts = install_jobs.read_versioned(facts_path, "installed_facts")
        if facts.get("jobId") == identifier:
            if (facts.get("gameId") != journal.get("gameId")
                    or facts.get("appIdentity") != publication.get("appIdentity")
                    or facts.get("originalIdentity") != publication.get("originalIdentity")
                    or facts.get("appToken") != publication.get("stagedAppToken")
                    or facts.get("originalToken") != publication.get("installedOriginalToken")):
                raise install_jobs.JobError("publication_commit_mismatch", "Installed facts do not match the job publication record.")
            for key in ("staging", "backup"):
                owned = job_paths[key]
                if owned.exists():
                    install_jobs.remove_owned(owned, job_paths["meta"], identifier,
                                              journal.get("ownedTokens", {}).get(key))
            install_jobs.update_job(job_paths, journal, "complete", validatedOutputs={"installed": facts}, publication=None)
            return journal
    app = root / "app"
    backup_app = job_paths["backup"] / "app"
    if app.exists():
        if install_jobs.matches_directory_token(app, publication.get("stagedAppToken", {})):
            current = install_jobs.owned_tree_identity(app)
            if current.get("sha256") != publication.get("appIdentity", {}).get("sha256"):
                raise install_jobs.JobError("publication_app_mismatch", "The published app changed during recovery.")
            if publication.get("hadApp") and not backup_app.exists():
                raise install_jobs.JobError("publication_backup_missing", "The previous app is missing and its backup is unavailable.")
            remove(app)
            install_jobs._sync_directory(root)
        elif publication.get("hadApp") and install_jobs.matches_directory_token(app, publication.get("previousAppToken", {})):
            pass
        else:
            raise install_jobs.JobError("publication_conflict", "The destination app is not owned by this interrupted publication.")
    if backup_app.exists():
        if not install_jobs.matches_directory_token(backup_app, publication.get("previousAppToken", {})):
            raise install_jobs.JobError("publication_backup_mismatch", "The backup app no longer matches this job.")
        if app.exists():
            raise install_jobs.JobError("publication_conflict", "The previous app cannot be restored because the destination changed.")
        backup_app.replace(app)
        install_jobs._sync_directory(job_paths["backup"])
        install_jobs._sync_directory(root)
    elif publication.get("hadApp") and not app.exists():
        raise install_jobs.JobError("publication_backup_missing", "The previous app is missing and its backup is unavailable.")
    data = root / "original"
    if publication.get("publishOriginal") and data.exists():
        if install_jobs.matches_directory_token(data, publication.get("stagedDataToken", {})):
            current = install_jobs.payload_identity(data)
            if current.get("sha256") != publication.get("originalIdentity", {}).get("sha256"):
                raise install_jobs.JobError("publication_data_mismatch", "The published game copy changed during recovery.")
            remove(data)
            install_jobs._sync_directory(root)
        else:
            raise install_jobs.JobError("publication_data_conflict", "The destination game copy is not owned by this publication.")
    install_jobs.update_job(job_paths, journal, "failed",
                            errors=[*journal.get("errors", []), {"code": "publication_recovered",
                                     "stage": "publishing", "message": "The previous app was restored after an interrupted update.",
                                     "recovery": ["retry"]}], publication=None)
    return journal


def catalog(storage, repair=False):
    games = []
    for directory in sorted((KIT / "games").iterdir()):
        if directory.name == "stub" or not (directory / "game.toml").is_file():
            continue
        item = {"id": directory.name, "title": directory.name, "state": "unavailable"}
        destination_busy = False
        recoverable_jobs = []
        try:
            game_registry_dir = install_jobs.registry_game_dir(storage, directory.name)
            registry_file = game_registry_dir / "registry.json"
            registry = {}
            if registry_file.is_file() and not registry_file.is_symlink():
                registry = install_jobs.read_versioned(registry_file, "registry")
            activity_path = game_registry_dir / "activity.json"
            activity = install_jobs.read_versioned(activity_path, "activity") if activity_path.is_file() else {}
            locator_destination = activity.get("destination", storage / directory.name)
            if activity.get("activeJobs") and isinstance(activity["activeJobs"][0], dict):
                locator_destination = activity["activeJobs"][0].get("destination", locator_destination)
            root = Path(registry.get("destination", locator_destination)).expanduser().resolve()
            if root.exists() and (root / ".recomp").exists():
                try:
                    with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
                        activity = (install_jobs.read_versioned(activity_path, "activity")
                                    if activity_path.is_file() else {})
                        remaining = []
                        for locator in activity.get("activeJobs", []):
                            identifier = locator.get("jobId") if isinstance(locator, dict) else locator
                            job_destination = locator.get("destination", root) if isinstance(locator, dict) else root
                            with install_jobs.DestinationLock(job_destination):
                                journal = recover_publication(job_destination, identifier)
                            if journal.get("stage") != "complete":
                                remaining.append(locator)
                                if journal.get("mode") == "assisted" and journal.get("stage") in ("waiting_for_input", "cancelling_assisted"):
                                    assisted_journal = journal
                                    try:
                                        job_paths, journal = install_jobs.load_job(job_destination, identifier)
                                        _, drop = assisted_install._validate_assisted_owner(job_paths, journal)
                                        content = drop / "content"
                                        validation_error = None
                                        try:
                                            assisted_install._validate_assisted_paths(job_paths, journal)
                                        except (OSError, ValueError) as error:
                                            validation_error = str(error)
                                        recover_actions = ["resume_preparation", "open_assisted_folder", "cancel_assisted_job"]
                                        log_path = Path(job_destination) / journal.get("log", "")
                                        if log_path.is_file() and not log_path.is_symlink():
                                            recover_actions.append("open_log")
                                        recoverable_jobs.append({"jobId": identifier, "mode": "assisted",
                                            "stage": journal["stage"], "destination": str(Path(job_destination).resolve()),
                                            "sourceReference": journal.get("assisted", {}).get("sourceReference"),
                                            "dropFolder": str(drop),
                                            "contentFolder": str(content) if content.is_dir() and not content.is_symlink() else None,
                                            "validationError": validation_error,
                                            "canCancel": True, "canRecheck": validation_error is None,
                                            "actions": recover_actions,
                                            "log": str(log_path) if log_path.is_file() and not log_path.is_symlink() else None,
                                            "errors": journal.get("errors", [])[-1:]})
                                    except (OSError, ValueError) as error:
                                        cancelling = assisted_journal.get("stage") == "cancelling_assisted"
                                        recoverable_jobs.append({"jobId": identifier, "mode": "assisted",
                                            "stage": assisted_journal.get("stage"), "destination": str(Path(job_destination).resolve()),
                                            "sourceReference": assisted_journal.get("assisted", {}).get("sourceReference"),
                                            "validationError": str(error), "canCancel": cancelling,
                                            "canRecheck": False,
                                            "actions": ["cancel_assisted_job"] if cancelling else [],
                                            "errors": assisted_journal.get("errors", [])[-1:]})
                        if remaining != activity.get("activeJobs", []):
                            activity["activeJobs"] = remaining
                            install_jobs.write_json(activity_path, activity)
                except install_jobs.JobError as error:
                    if error.code == "destination_busy":
                        destination_busy = True
                        item["detail"] = str(error)
                    else:
                        item["recoveryError"] = str(error)
            cfg = game_config.load(directory, root)
            known = bool(registry.get("destination"))
            destination = root if known else Path.home() / ("Applications" if platform.system() == "Darwin" else "Games") / directory.name
            item.update(game_entry(cfg), id=directory.name, installation=str(destination),
                        needsRust=True)
            item["state"] = "notInstalled"
            if recoverable_jobs:
                item["recoverableJobs"] = recoverable_jobs
            facts_path = root / ".recomp" / "installed.json"
            facts = {}
            if not destination_busy and root.exists():
                try:
                    with install_jobs.DestinationLock(root):
                        facts = install_jobs.read_versioned(facts_path, "installed_facts") if facts_path.is_file() else {}
                        if facts and facts.get("gameId") != directory.name:
                            raise install_jobs.JobError("installed_identity_mismatch", "Installed facts belong to another game.")
                        if (facts and artifact(cfg, root).is_file()
                                and (root / "original" / cfg["game"]["executable"]).is_file()):
                            app_identity = install_jobs.owned_tree_identity(root / "app")
                            validate_game(root / "original", cfg["game"]["executable"],
                                          cfg["game"]["sha256"], cfg.get("setup", {}).get("required_dirs", []))
                            auxiliaries_valid = all(
                                (root / "original" / module["path"].relative_to(cfg["developer_exe_path"].parent)).is_file()
                                and hashlib.sha256((root / "original" / module["path"].relative_to(
                                    cfg["developer_exe_path"].parent)).read_bytes()).hexdigest() == module["sha256"]
                                for module in cfg["aux_modules"])
                            if app_identity == facts.get("appIdentity") and auxiliaries_valid:
                                item["state"] = "ready"
                            else:
                                item["detail"] = "Installed files do not match their saved fingerprints."
                except install_jobs.JobError as error:
                    if error.code == "destination_busy":
                        destination_busy = True
                        item["detail"] = str(error)
                    else:
                        raise
            if item["state"] == "ready":
                try:
                    item["managedSource"] = str(root / "original")
                    item["buildLog"] = str(root / facts.get("log", f".recomp/logs/{facts.get('jobId', '')}.log"))
                    item["updateAvailable"] = facts.get("recipeIdentity", {}).get("sha256") != recipe(directory).get("sha256")
                except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
                    item["updateCheckError"] = str(error)
                if registry.get("destination") != str(root):
                    try:
                        with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
                            registry_file.parent.mkdir(parents=True, exist_ok=True)
                            install_jobs.write_json(registry_file, {"schema": install_jobs.SCHEMA,
                                                   "gameId": directory.name, "destination": str(root)})
                    except (OSError, ValueError) as error:
                        item["registrationError"] = str(error)
            if activity_path.is_file() and not destination_busy:
                recoverable = []
                for locator in activity.get("activeJobs", []):
                    identifier = locator.get("jobId") if isinstance(locator, dict) else locator
                    job_destination = locator.get("destination", activity.get("destination", root)) if isinstance(locator, dict) else activity.get("destination", root)
                    try:
                        _, journal = install_jobs.load_job(job_destination, identifier)
                        if journal.get("stage") != "complete":
                            attempt = journal.get("attempt")
                            can_resume = (journal.get("stage") not in ("publishing", "cleaning", "complete")
                                          and isinstance(attempt, int) and attempt > 0)
                            actions = ["locate_missing_file", "choose_another_copy", "change_destination",
                                       "recheck_preparation", "open_log"]
                            if can_resume:
                                actions.insert(0, "retry_failed_stage")
                            recoverable.append({"jobId": identifier, "stage": journal.get("stage"),
                                                "destination": str(Path(job_destination).resolve()),
                                                "source": journal.get("source"),
                                                "candidateId": journal.get("candidateId"),
                                                "log": journal.get("log"),
                                                "error": journal.get("errors", [])[-1] if journal.get("errors") else None,
                                                "canResume": can_resume, "actions": actions})
                    except (OSError, ValueError, KeyError) as error:
                        item.setdefault("recoveryErrors", []).append(str(error))
                if recoverable:
                    item["recoverableJobs"] = recoverable
                    if item["state"] != "ready":
                        item["state"] = "needsBuild"
            if destination_busy:
                item["state"] = "busy"
            if item["state"] == "notInstalled" and (cfg["code_map_path"] is None or not cfg["code_map_path"].exists()):
                item["state"] = "unavailable"
                item["detail"] = "Translation metadata is not available in this checkout."
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            item["state"] = "unavailable"
            item["detail"] = str(error)
        games.append(item)
    return games


def job_log(request, storage):
    game_id = request.get("game")
    identifier = install_jobs.job_id(request.get("jobId"))
    game_dir = KIT / "games" / str(game_id)
    if game_dir.parent != KIT / "games" or game_id == "stub" or not (game_dir / "game.toml").is_file():
        raise install_jobs.JobError("unknown_game", "The requested game is unavailable.")
    registry_dir = install_jobs.registry_game_dir(storage, game_id)
    with install_jobs.DestinationLock(storage, lock_name="registry.lock"):
        activity_path = registry_dir / "activity.json"
        activity = install_jobs.read_versioned(activity_path, "activity") if activity_path.is_file() else {}
        declared = next((item for item in activity.get("activeJobs", [])
                         if isinstance(item, dict) and item.get("jobId") == identifier), None)
        if declared is not None:
            destination_input = Path(declared.get("destination", "")).expanduser()
            destination = destination_input.resolve(strict=True)
            if destination_input != destination or not destination.is_dir():
                raise install_jobs.JobError("unsafe_job_destination", "The saved job destination is not canonical.")
        else:
            registry_path = registry_dir / "registry.json"
            if not registry_path.is_file():
                raise install_jobs.JobError("unknown_job", "The requested job is not registered for this game.")
            registry = install_jobs.read_versioned(registry_path, "registry")
            if registry.get("gameId") != game_id or not isinstance(registry.get("destination"), str):
                raise install_jobs.JobError("invalid_registry", "The installation registry does not match this game.")
            destination_input = Path(registry["destination"]).expanduser()
            destination = destination_input.resolve(strict=True)
            if destination_input != destination or not destination.is_dir():
                raise install_jobs.JobError("unsafe_job_destination", "The saved job destination is not canonical.")
            facts_path = destination / ".recomp" / "installed.json"
            if not facts_path.is_file() or install_jobs.read_versioned(facts_path, "installed_facts").get("jobId") != identifier:
                raise install_jobs.JobError("unknown_job", "The requested job is not registered for this game.")
        with install_jobs.DestinationLock(destination):
            job_paths, journal = install_jobs.load_job(destination, identifier)
            attempt = journal.get("attempt")
            expected_name = f"{identifier}-{attempt}.log"
            expected_log = f".recomp/logs/{expected_name}"
            if (type(attempt) is not int or attempt < 1 or journal.get("log") != expected_log
                    or journal.get("gameId") != game_id
                    or Path(journal.get("destination", "")).resolve() != destination):
                raise install_jobs.JobError("invalid_job_log", "The saved job log reference is invalid.")
            log_path = job_paths["logs"] / expected_name
            if (job_paths["logs"].is_symlink() or log_path.is_symlink()
                    or log_path.resolve(strict=True).parent != job_paths["logs"].resolve()
                    or not stat.S_ISREG(log_path.stat(follow_symlinks=False).st_mode)):
                raise install_jobs.JobError("unsafe_job_log", "The saved job log is not a safe regular file.")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(log_path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise install_jobs.JobError("unsafe_job_log", "The saved job log is not a safe regular file.")
                size = info.st_size
                os.lseek(fd, max(0, size - 256 * 1024), os.SEEK_SET)
                content = os.read(fd, 256 * 1024 + 1)
            finally:
                os.close(fd)
            truncated = size > 256 * 1024
            text = content[-256 * 1024:].decode("utf-8", errors="replace")
            return {"jobId": identifier, "text": text, "truncated": truncated,
                    "log": expected_log}


def prerequisites(needs_rust, needs_innoextract=False):
    checks = []
    brew = shutil.which("brew")
    if not brew and os.name != "nt":
        brew = next((str(path) for path in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew"),
                                           Path("/home/linuxbrew/.linuxbrew/bin/brew")) if path.is_file()), None)
    if brew:
        try:
            prefix = subprocess.run([brew, "--prefix"], capture_output=True, text=True, timeout=15)
            if prefix.returncode or not prefix.stdout.strip():
                brew = None
            else:
                base = Path(prefix.stdout.strip())
                paths = [base / "bin", base / "opt/llvm/bin", base / "opt/make/libexec/gnubin"]
                os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH", ""), *map(str, paths)])
        except (OSError, subprocess.TimeoutExpired):
            brew = None
    tools = [("clang", "C compiler", "https://releases.llvm.org/"),
             ("clang++", "C++ compiler", "https://releases.llvm.org/"),
             ("cmake", "CMake", "https://cmake.org/download/"),
             ("ninja", "Ninja", "https://ninja-build.org/"),
             ("git", "Git", "https://git-scm.com/downloads")]
    if needs_rust:
        tools += [("cargo", "Cargo", "https://rustup.rs/"),
                  ("rustc", "Rust compiler", "https://rustup.rs/")]
    if needs_innoextract:
        tools += [("innoextract", "GOG installer extraction", "https://constexpr.org/innoextract/")]
    if os.name != "nt":
        tools += [("make", "Make (video dependency)", "https://www.gnu.org/software/make/")]
    def shell_command(arguments):
        return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)

    for command, label, url in tools:
        check_cancel()
        local = Path(sys.executable).parent / (command + (".exe" if os.name == "nt" else ""))
        found = str(local) if local.is_file() else shutil.which(command)
        ok, detail = False, "Not found"
        if found:
            try:
                version = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=15)
                ok = version.returncode == 0
                detail = (version.stdout or version.stderr).splitlines()[0] if (version.stdout or version.stderr) else found
            except (OSError, subprocess.TimeoutExpired) as error:
                detail = str(error)
        help = f"Open the setup guide, install {label}, then restart the library and choose Check again."
        install_command = ""
        if command in ("cmake", "ninja"):
            help = f"{label} helps build your game app. Open {'Command Prompt' if os.name == 'nt' else 'Terminal'}, paste the command below and press Enter. When it finishes, choose Check again."
            install_command = shell_command([sys.executable, "-m", "pip", "install", command])
        elif platform.system() == "Darwin" and command in ("clang", "clang++", "git", "make"):
            help = "Open Terminal (Applications → Utilities), paste the command below and press Enter. Choose Install in the window that appears. When it finishes, choose Check again."
            install_command = "xcode-select --install"
            url = "https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/"
        elif command in ("cargo", "rustc"):
            help = "Open the Rust setup guide and follow the instructions for your computer. Rust builds this game’s graphics support. Restart the library when setup finishes, then choose Check again."
        elif command == "innoextract":
            help = "Install innoextract using the setup guide to unpack your GOG installer. Restart the library afterwards. You can also go back and choose an installed game folder or ZIP, which needs no extra extraction tool."
        elif command in ("clang", "clang++"):
            help = "Install LLVM using the setup guide. On Windows, enable its option to add LLVM to PATH. PATH lets the library find installed tools. Restart the library, then choose Check again."
        methods = []
        package = {"clang": "llvm", "clang++": "llvm", "cargo": "rust", "rustc": "rust"}.get(command, command)
        if brew:
            methods.append({"label": "Homebrew", "help": "Open Terminal, paste this command and press Enter. When it finishes, choose Check again.",
                            "command": shell_command([brew, "install", package]),
                            "url": f"https://formulae.brew.sh/formula/{package}"})
        if url:
            methods.append({"label": "Download", "help": f"Open the official {label} page and follow the instructions for your system. Restart the library after setup, then choose Check again.", "url": url})
        methods.append({"label": "Manual", "help": help, "command": install_command, "url": url})
        checks.append({"label": label, "ok": ok, "detail": detail, "methods": methods})
    if platform.system() == "Darwin":
        sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True, timeout=15)
        checks.append({"label": "Apple command line tools", "ok": sdk.returncode == 0,
                       "detail": sdk.stdout.strip() if sdk.returncode == 0 else "Install with xcode-select --install",
                       "url": "https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/",
                       "help": "Open Terminal (Applications → Utilities), paste this command and press Enter. Choose Install in the window that appears, then check again when it finishes.",
                       "command": "xcode-select --install"})
    modules = ("capstone", "pefile", "numpy", "pypcode")
    missing = [module for module in modules if importlib.util.find_spec(module) is None]
    checks.append({"label": "Game conversion tools", "ok": not missing,
                   "detail": ", ".join(missing) if missing else "Installed", "url": "",
                   "help": f"Open {'Command Prompt' if os.name == 'nt' else 'Terminal'}, paste the command below and press Enter. This installs the tools that read and convert your game. When it finishes, choose Check again.",
                   "command": shell_command([sys.executable, "-m", "pip", "install", "-r", str(KIT / "tools/requirements.txt")])})
    for check in checks:
        if "methods" not in check:
            check["methods"] = [{"label": "Manual", "help": check["help"], "command": check["command"]}]
            if check["url"]:
                check["methods"].append({"label": "Download", "help": "Open the official setup page and follow the instructions for your system.", "url": check["url"]})
    return {"checks": checks, "ready": all(check["ok"] for check in checks), "homebrew": brew,
            "python": sys.executable, "platform": platform.system()}


def inspect_source(cfg, source):
    source = Path(source).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as error:
        raise SourceError("source_unavailable", "The selected source is missing or unreadable.",
                          ["Check the source path and try again."], str(source), {"error": str(error)}) from error
    if source.is_dir():
        result = inspect_folder(cfg, source, check_cancel)
        result["source"]["kind"] = "folder"
        return result
    if not source.is_file():
        raise SourceError("unsupported_source", "Choose a game folder, supported archive, or game installer.",
                          ["Choose an installed game folder or a supported source file."], str(source))
    probe = probe_bytes(source, check_cancel)
    if probe["kind"] == "pe":
        result = inspect_folder(cfg, source, check_cancel, selected_executable=source)
        result["source"]["kind"] = "executable"
        result["source"]["probe"] = probe["evidence"]
        return _add_sibling_facts(result, source)
    if probe["kind"] == "zip":
        try:
            digest = archive_digest(source, check_cancel)
            with tempfile.TemporaryDirectory(prefix="recomp-library-inspect-") as temporary:
                extracted = Path(temporary) / "content"
                extract_zip(source, extracted, check_cancel)
                if archive_digest(source, check_cancel) != digest:
                    raise SourceError("source_changed", "The ZIP changed while it was being inspected.",
                                      ["Inspect the archive again and retry the import."], str(source))
                result = inspect_folder(cfg, extracted, check_cancel)
            _set_archive_candidate_ids(result, digest)
            result["source"].update(kind="zip", path=str(source), archiveSha256=digest)
            result["limits"].update({"maxArchiveEntries": 20000,
                                     "maxArchiveBytes": 4 * 1024 * 1024 * 1024})
            return result
        except ZipError as error:
            return _source_diagnostic(source, error.code, str(error),
                                      ["Choose a ZIP without unsafe paths, special files, or excessive contents."],
                                      source_kind="zip")
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            return _source_diagnostic(source, "unreadable_archive", "The ZIP could not be read safely.",
                                      ["Check the archive and try again."], {"error": str(error)}, "zip")
    messages = {
        "zip": ("unsupported_archive", "The ZIP did not contain a supported game copy.",
                ["Choose a complete installed game folder or a ZIP containing the game files."]),
        "unsupported_inno": ("unsupported_installer_version", "An Inno Setup installer was detected with an unsupported loader signature.",
                             ["Choose an installed game folder or a supported installer version."]),
        "probe_limit": ("source_probe_limit", "The source signature could not be verified within the inspection limit.",
                        ["Choose an installed game folder or a smaller source file."]),
        "iso9660": ("unsupported_media", "An ISO 9660 disc image was recognized, but no verified import adapter is available.",
                    ["Choose an installed game folder."]),
        "cabinet": ("unsupported_media", "A Cabinet archive was recognized, but no verified import adapter is available.",
                    ["Choose an installed game folder."]),
        "installshield_cab": ("unsupported_media", "An InstallShield Cabinet archive was recognized, but no verified import adapter is available.",
                               ["Choose an installed game folder."]),
        "cue": ("unsupported_media", "A CUE disc layout was recognized, but no verified import adapter is available.",
                ["Choose an installed game folder."]),
        "raw_cd": ("unsupported_media", "Raw CD sector data was recognized, but no verified import adapter is available.",
                   ["Choose an installed game folder."]),
        "unreadable": ("unreadable_source", "The selected source could not be read.",
                       ["Check the source file and try again."]),
        "unknown": ("unsupported_source", "The selected file is not a recognized game executable or supported archive.",
                    ["Choose an installed game folder or a supported source file."]),
    }
    if probe["kind"] == "inno":
        helper_available = shutil.which("innoextract") is not None
        message = ("An Inno Setup installer was recognized, but safe installer import is not available yet."
                   if helper_available else
                   "An Inno Setup installer was recognized, but innoextract is not installed and safe installer import is not available yet.")
        recovery = ["Choose an installed game folder."]
        if not helper_available:
            recovery.append("Install innoextract before using this installer after installer import becomes available.")
        result = _source_diagnostic(source, "installer_adapter_pending", message, recovery,
                                    {"probe": probe["evidence"], "innoextractAvailable": helper_available}, "inno")
        return _add_sibling_facts(result, source)
    code, message, recovery = messages.get(probe["kind"], messages["unknown"])
    result = _source_diagnostic(source, code, message, recovery, {"probe": probe["evidence"],
                                                                  "sourceKind": probe["kind"]}, probe["kind"])
    return _add_sibling_facts(result, source)


def _set_archive_candidate_ids(result, digest, clear_content_root=True):
    replacements = {}
    for candidate in result["candidates"]:
        member_root = candidate["contentRootRelative"]
        previous_id = candidate["id"]
        candidate["id"] = hashlib.sha256((digest + "\0" + member_root + "\0"
                                           + candidate["executable"]["path"] + "\0"
                                           + str(candidate["executable"].get("sha256"))).encode()).hexdigest()[:24]
        replacements[previous_id] = candidate["id"]
        candidate["archiveMemberRoot"] = member_root
        candidate["archiveSha256"] = digest
        if clear_content_root:
            candidate["contentRoot"] = ""
    for diagnostic in result["diagnostics"]:
        previous_id = diagnostic.get("candidateId")
        if previous_id in replacements:
            diagnostic["candidateId"] = replacements[previous_id]


def _add_sibling_facts(result, source):
    siblings, diagnostics = sibling_payload_facts(source, check_cancel)
    result["source"]["siblings"] = siblings
    try:
        info = Path(source).stat()
        result["source"]["selectedFile"] = {"name": Path(source).name,
                                            "extensionHint": Path(source).suffix.casefold().lstrip("."),
                                            "size": info.st_size, "metadataOnly": True}
    except OSError as error:
        diagnostics.append({"code": "source_changed", "stage": "inspecting", "path": str(source),
                            "message": "The selected source changed while its metadata was inspected.",
                            "details": {"error": str(error)},
                            "recovery": ["Check the source file and inspect it again."]})
    result["diagnostics"].extend(diagnostics)
    if diagnostics:
        result["scanComplete"] = False
    result["limits"].update({"maxProbeBytes": 4 * 1024 * 1024,
                             "maxSiblingEntries": 4096, "maxSiblings": 64})
    return result


def _source_diagnostic(source, code, message, recovery, details=None, source_kind="file"):
    return {"schema": {"major": 1, "minor": 0},
            "source": {"kind": source_kind, "path": str(source),
                       "identity": {"payload": "pending", "reason": "The source is not yet prepared."}},
            "candidates": [], "diagnostics": [{"code": code, "stage": "inspecting", "path": str(source),
                "message": message, "details": details or {}, "recovery": recovery}],
            "limits": {"maxProbeBytes": 4 * 1024 * 1024}, "scanComplete": True}


def select_candidate(result, candidate_id=None, explicit_source=None):
    candidates = result.get("candidates", [])
    if explicit_source is not None:
        wanted = Path(explicit_source).resolve()
        explicit_candidate = None
        for item in candidates:
            image = Path(item["contentRoot"]) / Path(*item["executable"]["path"].split("/"))
            if image.resolve() == wanted:
                explicit_candidate = item
                break
        if candidate_id and (not explicit_candidate or explicit_candidate["id"] != candidate_id):
            raise SourceError("invalid_candidate", "The selected ID does not refer to the explicitly selected executable.",
                              ["Inspect the selected executable again and choose its game copy."], str(wanted))
        if explicit_candidate and explicit_candidate.get("installable"):
            return explicit_candidate
        raise SourceError("selected_executable_unavailable", "The selected executable is not a complete supported game copy.",
                          ["Choose the installed game executable with its full content folder."], str(wanted),
                          {"diagnostics": explicit_candidate.get("diagnostics", []) if explicit_candidate else []})
    if candidate_id is not None:
        chosen = next((item for item in candidates if item["id"] == candidate_id), None)
        if not chosen or not chosen.get("installable"):
            raise SourceError("invalid_candidate", "The selected game copy is no longer available or complete.",
                              ["Inspect the source again and choose a complete supported game copy."],
                              details={"candidateId": candidate_id})
        return chosen
    if not result.get("scanComplete", False):
        raise SourceError("inspection_incomplete", "The source contains too many entries to select a game copy safely.",
                          ["Choose the installed game folder or select its game executable directly."],
                          details=result.get("limits", {}))
    viable = [item for item in candidates if item.get("installable")]
    if len(viable) == 1:
        return viable[0]
    if len(viable) > 1:
        raise SourceError("candidate_selection_required", "More than one supported game copy was found.",
                          ["Inspect the source and choose which game copy to import."],
                          details={"candidates": [{"id": item["id"], "path": item["contentRootRelative"],
                                                   "executable": item["executable"]["path"]}
                                                  for item in viable]})
    if not candidates and result.get("diagnostics"):
        diagnostic = result["diagnostics"][0]
        raise SourceError(diagnostic["code"], diagnostic["message"], diagnostic["recovery"],
                          diagnostic.get("path"), diagnostic.get("details"))
    raise SourceError("no_supported_candidate", "No complete supported game copy was found in this source.",
                      ["Choose a complete installed game folder or a supported installer for this game."],
                      details={"candidates": [{"id": item["id"], "status": item["status"],
                                               "diagnostics": item["diagnostics"]} for item in candidates]})


def prepare_install(request, storage):
    validate_inspection_request(request)
    game_id = request["game"]
    directory = KIT / "games" / game_id
    if directory.parent != KIT / "games" or not (directory / "game.toml").is_file() or game_id == "stub":
        raise ValueError("Unknown game")
    source_path = Path(request["source"]).expanduser()
    try:
        source_path = source_path.resolve(strict=True)
    except OSError as error:
        raise SourceError("source_unavailable", "The selected source is missing or unreadable.",
                          ["Check the source path and try again."], str(source_path), {"error": str(error)}) from error
    previous_root = installation_root(storage, game_id).resolve()
    root = Path(request.get("destination") or previous_root).expanduser().resolve()
    cfg = game_config.load(directory, root)
    inspection = inspect_source(cfg, source_path)
    source_kind = inspection["source"]["kind"]
    explicit_source = source_path if source_kind == "executable" else None
    selected = select_candidate(inspection, request.get("candidateId"), explicit_source)
    executable_relative = Path(*selected["executable"]["path"].split("/"))
    if executable_relative.parent != Path("."):
        raise SourceError("nested_layout_unsupported",
                          "This game's executable is inside a nested content folder that the installer cannot preserve yet.",
                          ["Keep the existing game copy in place and cancel this import. The folder layout must be preserved before it can be imported."],
                          selected["executable"]["path"],
                          {"contentRoot": selected["contentRootRelative"],
                           "executable": selected["executable"]["path"]})
    if cfg["code_map_path"] is None or not cfg["code_map_path"].exists():
        raise ValueError("This game has no translation metadata")
    if cfg["game"].get("developer_exe"):
        raise ValueError("Library installation requires a recipe without a fixed developer_exe")
    source_root = Path(selected["contentRoot"]) if source_kind in ("folder", "executable") else None
    data = root / "original"
    if source_root is not None:
        if Path(storage).expanduser().resolve().is_relative_to(source_root.resolve()):
            raise SourceError("storage_source_overlap",
                              "The library storage folder is inside the selected game source.",
                              ["Choose storage outside the source folder before importing."],
                              str(storage), {"source": str(source_root)})
        destination_path = root.resolve()
        source_path_resolved = source_root.resolve()
        inside_destination = source_path_resolved.is_relative_to(destination_path)
        destination_inside_source = destination_path.is_relative_to(source_path_resolved)
        exact_managed_data = source_path_resolved == data.resolve() and (root / ".recomp" / "installed.json").is_file()
        if ((inside_destination or destination_inside_source) and not exact_managed_data):
            raise SourceError("source_destination_overlap",
                              "The selected source overlaps the library destination or its cleanup folders.",
                              ["Choose a complete game folder outside the library destination."],
                              str(source_root), {"destination": str(root)})
    elif source_kind == "zip":
        if source_path.is_relative_to(root):
            raise SourceError("source_destination_overlap",
                              "The selected archive overlaps the library destination.",
                              ["Choose an archive and destination in separate folders."], str(source_path),
                              {"destination": str(root)})
    assembly_mismatches = [{"role": item["role"], "expectedPath": item["path"],
                            "actualPath": item["actualPath"]}
                           for item in selected["requiredRoles"]
                           if item["found"] and item.get("actualPath") != item["path"]]
    if assembly_mismatches:
        raise SourceError("assembly_pending",
                          "The installed copy uses path spelling that this installer cannot preserve yet.",
                          ["Keep the existing game copy in place. Import can continue after path-preserving assembly is supported."],
                          details={"paths": assembly_mismatches})
    facts = root / ".recomp" / "installed.json"
    unrelated = []
    if root.exists() and not facts.is_file():
        for entry in root.iterdir():
            if entry.name != ".recomp":
                unrelated.append(entry.name)
                continue
            if entry.is_symlink() or not entry.is_dir():
                unrelated.append(entry.name)
                continue
            if any(child.name not in {"install.lock", "jobs", "logs", "staging-" + str(request.get("jobId", "")),
                                      "backup-" + str(request.get("jobId", ""))}
                   for child in entry.iterdir()):
                unrelated.append(entry.name)
    if unrelated:
        raise SourceError("legacy_install_requires_recreate",
                          "This folder contains a legacy installation that the new job system cannot safely update.",
                          ["Choose an empty destination. Existing files will not be changed."], str(root),
                          {"entries": unrelated})
    if facts.is_file():
        saved = install_jobs.read_versioned(facts, "installed_facts")
        if saved.get("gameId") != game_id:
            raise SourceError("destination_game_mismatch", "This destination belongs to a different game.",
                              ["Choose the original destination or an empty folder."], str(root))
    return {"gameId": game_id, "directory": directory, "sourcePath": source_path,
            "root": root, "cfg": cfg,
            "sourceKind": source_kind, "explicitSource": explicit_source, "selected": selected}


def install(request, storage, prepared=None):
    prepared = prepared or prepare_install(request, storage)
    game_id = prepared["gameId"]
    directory = prepared["directory"]
    source_path = prepared["sourcePath"]
    root = prepared["root"]
    cfg = prepared["cfg"]
    source_kind = prepared["sourceKind"]
    explicit_source = prepared["explicitSource"]
    selected = prepared["selected"]
    exclude_paths = prepared.get("excludePaths", [])
    assisted_request = request.get("mode") == "assisted"
    data = root / "original"
    emit("stage", stage="checking", message="Checking build prerequisites")
    checks = prerequisites(True, source_kind == "inno")
    if not checks["ready"]:
        raise ValueError("Missing prerequisites: " + ", ".join(c["label"] for c in checks["checks"] if not c["ok"]))
    check_cancel()
    install_jobs.ensure_metadata(root)
    job_identifier = request.get("jobId")
    recipe_facts = recipe(directory)
    source_identity = ({"kind": "zip", "sha256": archive_digest(source_path, check_cancel)}
                       if source_kind == "zip" else
                       {"kind": "folder", **install_jobs.payload_identity(selected["contentRoot"], check_cancel,
                                                                                exclude_paths=exclude_paths)})
    if assisted_request:
        source_identity["excludedOptionalPaths"] = list(exclude_paths)
    archive_bytes = archive_uncompressed_size(source_path) if source_kind == "zip" else 0
    copy_estimate = source_identity.get("bytes", archive_bytes)
    resumed = bool(job_identifier)
    if job_identifier:
        job_paths, journal = install_jobs.load_job(root, job_identifier)
        if (journal.get("gameId") != game_id or journal.get("candidateId") != selected["id"]
                or journal.get("sourceIdentity") != source_identity
                or journal.get("recipeIdentity", {}).get("sha256") != recipe_facts.get("sha256")):
            raise SourceError("resume_identity_changed", "The source or build recipe changed since this job was saved.",
                              ["Choose the original source and retry, or start a new installation."],
                              details={"jobId": job_identifier, "recovery": ["restart", "choose_source"]})
        if journal.get("publication"):
            journal = recover_publication(root, job_identifier)
            job_paths, journal = install_jobs.load_job(root, job_identifier)
        if journal.get("stage") == "complete":
            return {"game": game_id, "state": "ready", "jobId": job_identifier}
        if journal.get("stage") in ("publishing", "cleaning"):
            raise SourceError("job_requires_recovery", "This job is at a publication recovery point.",
                              ["Refresh the library to recover the saved installation state."],
                              details={"jobId": job_identifier, "stage": journal.get("stage")})
        if job_paths["backup"].exists() and not journal.get("publication"):
            install_jobs.remove_owned(job_paths["backup"], job_paths["meta"], job_identifier,
                                      journal.get("ownedTokens", {}).get("backup"))
        journal["attempt"] = int(journal.get("attempt", 1)) + 1
    else:
        job_paths, journal = install_jobs.create_job(root, game_id,
            {"kind": source_kind, "path": str(source_path)}, source_identity, recipe_facts,
            selected["id"])
        activity_dir = install_jobs.registry_game_dir(storage, game_id)
        activity_dir.mkdir(parents=True, exist_ok=True)
        activity_path = activity_dir / "activity.json"
        activity = install_jobs.read_versioned(activity_path, "activity") if activity_path.is_file() else {
            "schema": install_jobs.SCHEMA, "activeJobs": []}
        active_jobs = []
        for locator in activity.get("activeJobs", []):
            identifier = locator.get("jobId") if isinstance(locator, dict) else locator
            destination = locator.get("destination", activity.get("destination", root)) if isinstance(locator, dict) else activity.get("destination", root)
            try:
                _, existing = install_jobs.load_job(destination, identifier)
                if existing.get("stage") != "complete":
                    active_jobs.append({"jobId": identifier, "destination": str(Path(destination).resolve())})
            except (OSError, ValueError):
                active_jobs.append({"jobId": identifier, "destination": str(Path(destination).resolve())})
        active_jobs.append({"jobId": journal["jobId"], "destination": str(root)})
        activity["activeJobs"] = active_jobs
        activity.pop("destination", None)
        try:
            install_jobs.write_json(activity_path, activity)
        except BaseException:
            install_jobs.remove_owned(job_paths["staging"], job_paths["meta"], journal["jobId"],
                                      journal.get("ownedTokens", {}).get("staging"))
            if not job_paths["journal"].is_symlink():
                job_paths["journal"].unlink(missing_ok=True)
                install_jobs._sync_directory(job_paths["jobs"])
            raise
    job_identifier = journal["jobId"]
    log_dir = job_paths["logs"]
    log_path = log_dir / f"{job_identifier}-{journal.get('attempt', 1)}.log"
    journal["log"] = f".recomp/logs/{log_path.name}"
    space_plan = {"policy": "install-space-v1", "volume": str(root),
                  "archiveExpansionEstimateBytes": archive_bytes,
                  "sourceCopyEstimateBytes": copy_estimate,
                  "buildAllowanceBytes": install_jobs.MAX_BUILD_BYTES,
                  "safetyReserveBytes": install_jobs.SAFETY_RESERVE_BYTES,
                  "attemptLogMaxBytes": install_jobs.MAX_ATTEMPT_LOG_BYTES,
                  "retainedLogMaxBytes": install_jobs.MAX_RETAINED_LOG_BYTES,
                  "retainedLogsAdditionalBytes": 0,
                  "retainedBackupAdditionalBytes": 0,
                  "existingAllocatedBytesExcludedByFilesystemFreeSpace": True,
                  "buildMonitorSampleIntervalSeconds": 0.5,
                  "monitorRoots": [".recomp/" + job_paths["staging"].name,
                                   ".recomp/" + job_paths["logs"].name],
                  "externalCachesOutsideMonitor": ["user toolchain/configuration caches"]}
    install_jobs.update_job(job_paths, journal, journal.get("stage", "inspecting"), log=journal["log"])
    stage = "copying"
    committed = False
    emit("stage", stage="copying", message="Installation job started",
         jobId=job_identifier, destination=str(root))
    try:
        install_jobs.rotate_logs(log_dir, current=log_path)
        log_event(log_path, "inspecting", "installation attempt started",
                  {"jobId": job_identifier, "attempt": journal.get("attempt", 1)})
        staging = job_paths["staging"]
        assisted_job = journal.get("mode") == "assisted"
        work_root = staging / "work" if assisted_job else staging
        build_root = work_root / "build"
        staged_data = build_root / "original"
        reusable_copy = False
        if resumed and staged_data.is_dir() and journal.get("validatedInputs", {}).get("stagedOriginal"):
            current_copy = install_jobs.payload_identity(staged_data, check_cancel)
            reusable_copy = current_copy == journal["validatedInputs"]["stagedOriginal"]
            if reusable_copy:
                validate_game(staged_data, cfg["game"]["executable"], cfg["game"]["sha256"],
                              cfg.get("setup", {}).get("required_dirs", []))
                for module in cfg["aux_modules"]:
                    image = staged_data / module["path"].relative_to(cfg["developer_exe_path"].parent)
                    if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != module["sha256"]:
                        reusable_copy = False
                        break
        if not reusable_copy:
            if assisted_job:
                if work_root.exists():
                    install_jobs.remove_owned(work_root, staging, job_identifier,
                                              journal.get("ownedTokens", {}).get("work"))
                work_root.mkdir()
                info = work_root.stat()
                install_jobs.write_json(work_root / "owner.json", {"schema": install_jobs.SCHEMA,
                                         "jobId": job_identifier, "device": info.st_dev, "inode": info.st_ino})
                journal.setdefault("ownedTokens", {})["work"] = install_jobs.directory_token(work_root)
                install_jobs.update_job(job_paths, journal, journal.get("stage", "copying"),
                                        ownedTokens=journal["ownedTokens"])
            else:
                if staging.exists():
                    install_jobs.remove_owned(staging, job_paths["meta"], job_identifier,
                                              journal.get("ownedTokens", {}).get("staging"))
                journal.setdefault("ownedTokens", {})["staging"] = None
                install_jobs.update_job(job_paths, journal, journal.get("stage", "copying"),
                                        ownedTokens=journal["ownedTokens"])
                staging.mkdir()
                info = staging.stat()
                install_jobs.write_json(staging / "owner.json", {"schema": install_jobs.SCHEMA,
                                         "jobId": job_identifier, "device": info.st_dev, "inode": info.st_ino})
                journal.setdefault("ownedTokens", {})["staging"] = install_jobs.directory_token(staging)
                install_jobs.update_job(job_paths, journal, journal.get("stage", "copying"),
                                        ownedTokens=journal["ownedTokens"])
            build_root.mkdir()
        else:
            stage = "validating"
            install_jobs.update_job(job_paths, journal, stage)
        if source_kind == "zip" and not reusable_copy:
            stage = "extracting"
            install_jobs.update_job(job_paths, journal, stage)
            check_space(job_paths, journal, root, "extracting",
                        archive_bytes + copy_estimate + install_jobs.MAX_BUILD_BYTES,
                        log_path, space_plan)
            log_event(log_path, stage, "archive extraction started",
                      {"declaredExpansionBytes": archive_bytes})
            extracted = staging / "extracted"
            try:
                extract_zip(source_path, extracted, check_cancel)
            except ZipError as error:
                raise SourceError(error.code, str(error), ["Check the archive and inspect it again."], str(source_path)) from error
            extracted_result = inspect_folder(cfg, extracted, check_cancel)
            _set_archive_candidate_ids(extracted_result, source_identity["sha256"], clear_content_root=False)
            selected_now = select_candidate(extracted_result, selected["id"])
            source = Path(selected_now["contentRoot"])
            if archive_digest(source_path, check_cancel) != selected.get("archiveSha256"):
                raise SourceError("source_changed", "The ZIP changed after its contents were inspected.",
                                  ["Inspect the archive again and retry the import."], str(source_path))
            if archive_digest(source_path, check_cancel) != source_identity["sha256"]:
                raise SourceError("source_changed", "The ZIP changed while its contents were being extracted.",
                                  ["Inspect the archive again and retry the import."], str(source_path))
        elif source_kind != "zip":
            source = Path(selected["contentRoot"])
            current = inspect_source(cfg, source_path)
            selected_now = select_candidate(current, selected["id"], explicit_source)
            source = Path(selected_now["contentRoot"])
            if selected_now["executable"].get("sha256") != selected["executable"].get("sha256"):
                raise SourceError("source_changed", "The selected executable changed after source inspection.",
                                  ["Inspect the source again and retry the import."], str(source_path))
        if reusable_copy:
            source = staged_data
        emit("stage", stage="validating", message="Verifying the supported game version")
        stage = "validating"
        install_jobs.update_job(job_paths, journal, stage)
        required = cfg.get("setup", {}).get("required_dirs", [])
        validate_game(source, cfg["game"]["executable"], cfg["game"]["sha256"], required)
        for module in cfg["aux_modules"]:
            image = source / module["path"].relative_to(cfg["developer_exe_path"].parent)
            if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != module["sha256"]:
                raise ValueError(f"Missing or unsupported auxiliary module: {module['name']}")
        check_cancel()
        stage = "copying"
        install_jobs.update_job(job_paths, journal, stage)
        if data.is_dir() and (root / ".recomp" / "installed.json").is_file():
            source = data
        if reusable_copy:
            source_facts = journal["validatedInputs"]["source"]
            copied = journal["validatedInputs"]["stagedOriginal"]
        else:
            source_facts = install_jobs.payload_identity(source, check_cancel, exclude_paths=exclude_paths)
            space_plan["sourceCopyBytesEstimate"] = source_facts["bytes"]
            check_space(job_paths, journal, root, "copying",
                        source_facts["bytes"] + install_jobs.MAX_BUILD_BYTES,
                        log_path, space_plan)
            log_event(log_path, "copying", "validated source copy started",
                      {"files": source_facts["files"], "bytes": source_facts["bytes"]})
            install_jobs.copy_payload(source, staged_data, check_cancel, source_facts["bytes"],
                                      exclude_paths=exclude_paths)
            copied = install_jobs.payload_identity(staged_data, check_cancel)
        if copied["sha256"] != source_facts["sha256"]:
            raise SourceError("copy_validation_failed", "The staged game copy differs from its source.",
                              ["Check storage space and source permissions."], str(source))
        if source_kind == "zip" and not reusable_copy:
            shutil.rmtree(staging / "extracted")
        validate_game(staged_data, cfg["game"]["executable"], cfg["game"]["sha256"], required)
        for module in cfg["aux_modules"]:
            image = staged_data / module["path"].relative_to(cfg["developer_exe_path"].parent)
            if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != module["sha256"]:
                raise ValueError(f"Missing or unsupported auxiliary module: {module['name']}")
        journal["validatedInputs"] = {"source": source_facts, "stagedOriginal": copied,
                                      "executableSha256": cfg["game"]["sha256"]}
        log_event(log_path, "copying", "staged original validated",
                  {"files": copied["files"], "bytes": copied["bytes"], "sha256": copied["sha256"]})
        emit("stage", stage="building", message="Building the game app")
        stage = "building"
        install_jobs.update_job(job_paths, journal, stage)
        check_space(job_paths, journal, root, "building", install_jobs.MAX_BUILD_BYTES,
                    log_path, space_plan)
        for child in list(build_root.iterdir()):
            if child.name != "original":
                remove(child)
        publish_dir = staging / "publish"
        if publish_dir.exists():
            remove(publish_dir)
        isolated_env = os.environ.copy()
        isolated_paths = {"TMPDIR": staging / "tmp", "TMP": staging / "tmp", "TEMP": staging / "tmp",
                          "CARGO_HOME": staging / "cargo-home", "XDG_CACHE_HOME": staging / "xdg-cache",
                          "CCACHE_DIR": staging / "ccache"}
        for key, value in isolated_paths.items():
            value.mkdir(parents=True, exist_ok=True)
            isolated_env[key] = str(value)
        monitor_baseline = (copied["bytes"] + journal.get("assisted", {}).get("validatedDropBytes", 0)
                            if assisted_job else copied["bytes"])
        log_event(log_path, "building", "build started",
                  {"stagingBaselineBytes": monitor_baseline,
                   "buildAllowanceBytes": install_jobs.MAX_BUILD_BYTES,
                   "isolatedEnvironmentRoots": {key: value.relative_to(root).as_posix()
                                                 for key, value in isolated_paths.items()}})
        run([sys.executable, str(KIT / "tools/build.py"), "--game", game_id,
             "--build-root", str(build_root), "--regenerate", "--target", "app"], log_path,
            build_monitor={"root": staging, "volume": root,
                           "baselineBytes": monitor_baseline}, env=isolated_env)
        log_event(log_path, "building", "build completed")
        binary = artifact(cfg, build_root, installed=False)
        if not binary.is_file():
            raise ValueError("Build finished without the expected game application")
        staged_app = staging / "publish" / "app"
        staged_app.parent.mkdir()
        built = build_root / (cfg["game"]["app_name"] + ".app") if platform.system() == "Darwin" else binary.parent
        if platform.system() == "Darwin":
            staged_app.mkdir()
            built.replace(staged_app / built.name)
        else:
            built.replace(staged_app)
        relative_binary = artifact(cfg, root).relative_to(root / "app")
        if not (staged_app / relative_binary).is_file():
            raise ValueError("Installation staging is missing the game application")
        check_cancel()
        current_recipe = recipe(directory)
        current_source = ({"kind": "zip", "sha256": archive_digest(source_path, check_cancel)}
                          if source_kind == "zip" else
                          {"kind": "folder", **install_jobs.payload_identity(selected["contentRoot"], check_cancel,
                                                                               exclude_paths=exclude_paths)})
        if assisted_job:
            current_source["excludedOptionalPaths"] = list(exclude_paths)
        if current_recipe.get("sha256") != recipe_facts.get("sha256") or current_source != source_identity:
            raise SourceError("inputs_changed_before_publish", "The source or build recipe changed during the build.",
                              ["Review the source and start a new installation."], str(source_path))
        final_original = install_jobs.payload_identity(staged_data, check_cancel)
        if final_original != copied:
            raise SourceError("staged_input_changed", "The staged game copy changed during the build.",
                              ["Retry the installation from a clean staged copy."], str(staged_data))
        validate_game(staged_data, cfg["game"]["executable"], cfg["game"]["sha256"], required)
        for module in cfg["aux_modules"]:
            image = staged_data / module["path"].relative_to(cfg["developer_exe_path"].parent)
            if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != module["sha256"]:
                raise ValueError(f"Missing or unsupported auxiliary module before publication: {module['name']}")
        app_facts = install_jobs.owned_tree_identity(staged_app, check_cancel)
        staged_app_token = install_jobs.directory_token(staged_app)
        staged_data_token = install_jobs.directory_token(staged_data)
        installed_original_token = staged_data_token if not data.exists() else install_jobs.directory_token(data)
        had_app = (root / "app").exists()
        previous_app_token = install_jobs.directory_token(root / "app") if had_app else None
        stage = "publishing"
        install_jobs.update_job(job_paths, journal, stage)
        check_space(job_paths, journal, root, "publishing", 0, log_path, space_plan)
        log_event(log_path, "publishing", "staged app and original validated",
                  {"appSha256": app_facts["sha256"], "originalSha256": copied["sha256"]})
        facts = {"schema": install_jobs.SCHEMA, "jobId": job_identifier, "gameId": game_id,
                 "sourceIdentity": source_identity,
                 "recipeIdentity": {"algorithm": recipe_facts["algorithm"],
                                    "sha256": recipe_facts["sha256"], "platform": recipe_facts["platform"]},
                 "originalIdentity": copied, "appIdentity": app_facts,
                 "originalToken": installed_original_token, "appToken": staged_app_token,
                 "log": journal["log"],
                 "launch": {"executable": cfg["game"]["executable"], "sha256": cfg["game"]["sha256"],
                            "requiredDirs": required}, "committedAt": int(time.time())}
        if job_paths["backup"].exists():
            install_jobs.remove_owned(job_paths["backup"], job_paths["meta"], job_identifier,
                                      journal.get("ownedTokens", {}).get("backup"))
        _, backup_token = install_jobs.create_backup(job_paths, job_identifier)
        journal.setdefault("ownedTokens", {})["backup"] = backup_token
        install_jobs.update_job(job_paths, journal, stage,
                                ownedTokens=journal["ownedTokens"],
                                publication={"hadApp": had_app,
                                             "publishOriginal": not data.exists(),
                                             "appIdentity": app_facts, "originalIdentity": copied,
                                             "stagedAppToken": staged_app_token,
                                             "stagedDataToken": staged_data_token,
                                             "installedOriginalToken": installed_original_token,
                                             "previousAppToken": previous_app_token})
        backup_app = job_paths["backup"] / "app"
        if (root / "app").exists():
            (root / "app").replace(backup_app)
            install_jobs._sync_directory(root)
            install_jobs._sync_directory(job_paths["backup"])
        if not data.exists():
            staged_data.replace(data)
            install_jobs._sync_directory(build_root)
            install_jobs._sync_directory(root)
        staged_app.replace(root / "app")
        install_jobs._sync_directory(staged_app.parent)
        install_jobs._sync_directory(root)
        install_jobs.write_json(root / ".recomp" / "installed.json", facts)
        committed = True
        log_event(log_path, "publishing", "installed facts committed",
                  {"jobId": job_identifier, "gameId": game_id})
        try:
            registry_dir = install_jobs.registry_game_dir(storage, game_id)
            registry_dir.mkdir(parents=True, exist_ok=True)
            registry_path = registry_dir / "registry.json"
            registry = install_jobs.read_versioned(registry_path, "registry") if registry_path.is_file() else {
                "schema": install_jobs.SCHEMA, "gameId": game_id, "activeJobs": []}
            registry.update(destination=str(root), gameId=game_id)
            install_jobs.write_json(registry_path, registry)
        except (OSError, ValueError) as error:
            emit("log", text=f"Installed, but library registration needs repair: {error}")
        cleanup_complete = False
        try:
            stage = "cleaning"
            install_jobs.update_job(job_paths, journal, stage)
            log_event(log_path, stage, "owned staging cleanup started")
            install_jobs.remove_owned(job_paths["staging"], job_paths["meta"], job_identifier,
                                      journal.get("ownedTokens", {}).get("staging"))
            if job_paths["backup"].exists():
                install_jobs.remove_owned(job_paths["backup"], job_paths["meta"], job_identifier,
                                          journal.get("ownedTokens", {}).get("backup"))
            cleanup_complete = True
            log_event(log_path, stage, "owned staging cleanup completed")
        except (OSError, ValueError) as error:
            log_event(log_path, "cleaning", "cleanup needs attention", {"message": str(error)}, failure=True)
            emit("log", text=f"Published installation; cleanup needs attention: {error}")
        if cleanup_complete:
            try:
                install_jobs.update_job(job_paths, journal, "complete", validatedOutputs={"installed": facts}, publication=None)
                emit("stage", stage="complete", message="Installation complete")
            except OSError as error:
                emit("log", text=f"Published installation; job journal cleanup needs attention: {error}")
        else:
            emit("stage", stage="cleaning", message="Installation complete; cleanup will be retried")
        return {"game": game_id, "state": "ready", "jobId": job_identifier}
    except BaseException as error:
        diagnostic = error_diagnostic(error, stage, root, log_path)
        diagnostic.setdefault("details", {})["jobId"] = job_identifier
        try:
            error.diagnostic = diagnostic
        except AttributeError:
            pass
        log_event(log_path, stage, "installation attempt failed", diagnostic, failure=True)
        if not committed:
            if journal.get("publication"):
                try:
                    journal = recover_publication(root, job_identifier)
                    committed = journal.get("stage") == "complete"
                except (OSError, ValueError, KeyError) as recovery_error:
                    emit("log", text=f"Publication recovery is required: {recovery_error}")
        if not committed:
            assisted_retry = journal.get("mode") == "assisted" and not journal.get("publication")
            failure_stage = "waiting_for_input" if assisted_retry else (
                "cancelled" if diagnostic["code"] == "cancelled" else "failed")
            try:
                if assisted_retry:
                    assisted = journal.get("assisted", {})
                    assisted["settle"] = None
                    assisted["recipeSessionId"] = None
                    assisted["states"] = []
                    install_jobs.update_job(job_paths, journal, failure_stage,
                                            errors=[*journal.get("errors", []), diagnostic], assisted=assisted)
                else:
                    install_jobs.update_job(job_paths, journal, failure_stage,
                                            errors=[*journal.get("errors", []), diagnostic])
            except OSError as journal_error:
                emit("log", text=f"Could not persist the failure journal: {journal_error}")
        raise


def inspect_request(request):
    validate_inspection_request(request)
    game_id = request["game"]
    directory = KIT / "games" / game_id
    if directory.parent != KIT / "games" or not (directory / "game.toml").is_file() or game_id == "stub":
        raise ValueError("Unknown game")
    cfg = game_config.load(directory)
    return inspect_source(cfg, request["source"])


def discover_request(request):
    validate_inspection_request(request)
    game_id = request["game"]
    directory = KIT / "games" / game_id
    if directory.parent != KIT / "games" or not (directory / "game.toml").is_file() or game_id == "stub":
        raise ValueError("Unknown game")
    cfg = game_config.load(directory)
    host = platform.system()
    platform_name = "windows" if host == "Windows" else "macos" if host == "Darwin" else "linux"
    roots, diagnostics, expansion_entries = expand_discovery_roots(
        platform_name, Path.home(), os.environ, check_cancel, cfg["launcher"]["install_names"])
    budget = [expansion_entries, [0, MAX_DISCOVERY_HASH_BYTES], 0]
    candidates = []
    seen = set()
    candidate_keys = set()
    skipped_symlinks = 0
    for root in roots:
        check_cancel()
        if budget[0] > MAX_DISCOVERY_ENTRIES:
            break
        try:
            if not root.exists():
                continue
            canonical = root.resolve(strict=True)
            info = canonical.stat()
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            if not canonical.is_dir():
                continue
            result = inspect_folder(cfg, canonical, check_cancel, discovery_budget=budget)
            for diagnostic in result["diagnostics"]:
                if diagnostic["code"] == "symlink_skipped":
                    skipped_symlinks += 1
                else:
                    diagnostics.append(diagnostic)
            for candidate in result["candidates"]:
                candidate_key = (os.path.normcase(str(Path(candidate["contentRoot"]).resolve(strict=True))),
                                 os.path.normcase(candidate["executable"]["path"]),
                                 candidate["executable"].get("sha256"))
                if candidate_key in candidate_keys:
                    continue
                candidate_keys.add(candidate_key)
                candidate["discoveryRoot"] = str(canonical)
                candidates.append(candidate)
            if budget[0] >= MAX_DISCOVERY_ENTRIES or budget[2] >= MAX_DISCOVERY_CANDIDATES:
                break
        except OSError as error:
            diagnostics.append({"code": "discovery_path_unavailable", "stage": "inspecting",
                                "path": str(root), "message": "A configured game-search location could not be read.",
                                "details": {"error": str(error)},
                                "recovery": ["Check the search location permissions."]})
    if skipped_symlinks:
        diagnostics.append({"code": "symlink_skipped", "stage": "inspecting", "path": ".",
                            "message": "Symbolic links were skipped at configured search locations.",
                            "details": {"count": skipped_symlinks},
                            "recovery": ["Choose a source without symbolic links."]})
    scan_complete = not any(item["code"] in ("search_limit", "candidate_limit", "hash_limit",
                                              "required_artifact_limit", "inaccessible_path",
                                              "path_collision", "symlink_skipped", "unsafe_path",
                                              "discovery_entry_limit", "discovery_root_limit",
                                              "discovery_path_unavailable", "steam_library_file_limit",
                                              "steam_library_file_unavailable") for item in diagnostics)
    return {"schema": {"major": 1, "minor": 0}, "source": {"kind": "discovery"},
            "candidates": candidates, "diagnostics": diagnostics,
            "limits": {"maxDiscoveryRoots": MAX_DISCOVERY_ROOTS,
                       "maxDiscoveryEntries": MAX_DISCOVERY_ENTRIES,
                       "maxDiscoveryCandidates": MAX_DISCOVERY_CANDIDATES,
                       "maxDiscoveryHashBytes": MAX_DISCOVERY_HASH_BYTES,
                       "rootsVisited": len(seen), "entriesSeen": budget[0],
                       "candidatesInspected": budget[2], "hashBytes": budget[1][0]},
            "scanComplete": scan_complete}


def validate_inspection_request(request):
    schema = request.get("schema")
    if schema is None:
        return
    if (not isinstance(schema, dict) or type(schema.get("major")) is not int
            or type(schema.get("minor")) is not int or schema["minor"] < 0):
        raise SourceError("invalid_request_schema", "The source request has an invalid schema version.",
                          ["Update the library client and retry the inspection."])
    if schema["major"] != 1:
        raise SourceError("unsupported_request_schema", "The source request uses an unsupported schema version.",
                          ["Update the library client to use a supported source request version."],
                          details={"received": schema, "supportedMajor": 1})


def resume_install(request, storage):
    if request.get("mode") == "assisted":
        return continue_assisted(request, storage)
    validate_inspection_request(request)
    game_id = request["game"]
    directory = KIT / "games" / game_id
    if directory.parent != KIT / "games" or game_id == "stub" or not (directory / "game.toml").is_file():
        raise install_jobs.JobError("unknown_game", "The requested game is unavailable.")
    source_path = Path(request["source"]).expanduser().resolve(strict=True)
    cfg = game_config.load(directory)
    inspection = inspect_source(cfg, source_path)
    if inspection["source"]["kind"] in ("folder", "executable"):
        selected = select_candidate(inspection, request.get("candidateId"),
                                    source_path if inspection["source"]["kind"] == "executable" else None)
        source_root = Path(selected["contentRoot"]).resolve()
        if Path(storage).expanduser().resolve().is_relative_to(source_root):
            raise SourceError("storage_source_overlap",
                              "The library storage folder is inside the selected game source.",
                              ["Choose storage outside the source folder before importing."],
                              str(storage), {"source": str(source_root)})
    destination_value = request.get("destination")
    if not destination_value:
        game_registry_dir = install_jobs.registry_game_dir(storage, game_id)
        activity_path = game_registry_dir / "activity.json"
        activity = install_jobs.read_versioned(activity_path, "activity") if activity_path.is_file() else {}
        for locator in activity.get("activeJobs", []):
            if isinstance(locator, dict) and locator.get("jobId") == request.get("jobId"):
                destination_value = locator.get("destination")
                break
    destination = Path(destination_value or installation_root(storage, game_id)).expanduser().resolve()
    request = {**request, "destination": str(destination)}
    with install_jobs.DestinationLock(storage, lock_name="registry.lock") as registry_lock:
        with install_jobs.DestinationLock(destination) as destination_lock:
            global DESTINATION_LOCKS
            DESTINATION_LOCKS = [registry_lock, destination_lock]
            try:
                _, journal = install_jobs.load_job(destination, request.get("jobId"))
                if journal.get("gameId") != game_id:
                    raise install_jobs.JobError("job_game_mismatch", "The saved job belongs to a different game.")
                if journal.get("publication"):
                    recover_publication(destination, request["jobId"])
                prepared = prepare_install(request, storage)
                if prepared["root"] != destination_lock.root:
                    raise install_jobs.JobError("destination_changed", "The registered destination changed during preflight.")
                return install(request, storage, prepared)
            finally:
                DESTINATION_LOCKS = []


def continue_assisted(request, storage):
    if request.get("continue") is not True:
        raise install_jobs.JobError("explicit_continue_required", "Continuing preparation requires an explicit user action.")
    game_id = request["game"]
    directory = KIT / "games" / game_id
    if directory.parent != KIT / "games" or game_id == "stub" or not (directory / "game.toml").is_file():
        raise install_jobs.JobError("unknown_game", "The requested game is unavailable.")
    root, _, _ = assisted_install._locate(storage, game_id, request.get("jobId"))
    with install_jobs.DestinationLock(storage, lock_name="registry.lock") as registry_lock:
        with install_jobs.DestinationLock(root) as destination_lock:
            global DESTINATION_LOCKS
            DESTINATION_LOCKS = [registry_lock, destination_lock]
            try:
                root, job_paths, journal = assisted_install._locate(storage, game_id, request.get("jobId"))
                if journal.get("stage") != "waiting_for_input":
                    raise install_jobs.JobError("assisted_not_waiting", "This preparation job is not waiting for files.")
                _, content = assisted_install._validate_assisted_paths(job_paths, journal)
                try:
                    session_id = str(uuid.UUID(request.get("sessionId")))
                except (ValueError, TypeError, AttributeError) as error:
                    raise install_jobs.JobError("invalid_watch_session", "Recheck preparation before continuing.") from error
                if session_id != request.get("sessionId"):
                    raise install_jobs.JobError("invalid_watch_session", "Recheck preparation before continuing.")
                settle = journal.get("assisted", {}).get("settle") or {}
                if settle.get("sessionId") != session_id:
                    raise install_jobs.JobError("preparation_recheck_required", "Recheck preparation in this session before continuing.")
                cfg = game_config.load(directory)
                recipe_facts = recipe(directory)
                if recipe_facts.get("sha256") != journal.get("recipeIdentity", {}).get("sha256"):
                    raise install_jobs.JobError("recipe_changed", "The build recipe changed; recheck preparation before continuing.",
                                                ["Recheck preparation", "Keep the waiting folder"])
                before = assisted_install._snapshot(content, check_cancel)
                if before["invalid"]:
                    raise install_jobs.JobError("assisted_content_unstable", "Preparation files are still changing or contain unsafe paths.",
                                                ["Wait for files to settle", "Recheck preparation"],
                                                details={"diagnostics": before["invalid"]})
                now = time.time()
                optional_paths = [item["path"] for item in cfg["setup"]["checklist"]["items"]
                                  if not item.get("required", True)]
                for item in cfg["setup"]["checklist"]["items"]:
                    if not item.get("required", True):
                        continue
                    record = settle.get("items", {}).get(item["id"], {})
                    digest = assisted_install._item_digest(before, item["path"], optional_paths)
                    if (digest is None or digest != record.get("digest")
                            or assisted_install._item_changed(before, item["path"], optional_paths)
                            or now - record.get("since", now) < assisted_install.STABLE_SECONDS):
                        raise install_jobs.JobError("preparation_not_settled", "Required files have not been stable for two seconds.",
                                                    ["Wait for the required files to settle", "Recheck preparation"],
                                                    details={"path": item["path"]})
                inspection = inspect_folder(cfg, content, check_cancel)
                selected = select_candidate(inspection, request.get("candidateId"))
                executable_relative = Path(*selected["executable"]["path"].split("/"))
                if executable_relative.parent != Path("."):
                    raise install_jobs.JobError("nested_layout_unsupported", "This game's executable is nested in a layout the installer cannot preserve yet.",
                                                ["Use a supported flat game folder"], details={"path": selected["executable"]["path"]})
                if cfg["code_map_path"] is None or not cfg["code_map_path"].exists():
                    raise install_jobs.JobError("missing_translation_metadata", "This game has no translation metadata.")
                if cfg["game"].get("developer_exe"):
                    raise install_jobs.JobError("fixed_developer_executable", "This game recipe requires a fixed developer executable.")
                assembly_mismatches = [{"role": item["role"], "expectedPath": item["path"],
                                        "actualPath": item.get("actualPath")}
                                       for item in selected.get("requiredRoles", [])
                                       if item.get("found") and item.get("actualPath") != item.get("path")]
                if assembly_mismatches:
                    raise install_jobs.JobError("assembly_pending", "Required files use path spelling the installer cannot preserve yet.",
                                                ["Choose a supported game folder"], details={"paths": assembly_mismatches})
                role_checks, role_diagnostics, _, _ = source_inspection.validate_required_roles(
                    cfg, content, artifacts=[item for item in cfg["setup"]["checklist"]["items"]
                        if item.get("required", True) or (
                            item.get("id") in settle.get("items", {})
                            and settle["items"][item["id"]].get("digest") is not None
                            and now - settle["items"][item["id"]].get("since", now)
                                >= assisted_install.STABLE_SECONDS)])
                if any(item.get("required", True) and item["status"] != "verified" for item in role_checks):
                    raise install_jobs.JobError("required_content_unverified", "Required preparation files no longer pass validation.",
                                                ["Recheck preparation"], details={"diagnostics": role_diagnostics})
                verified_optional = {item["id"] for item in role_checks
                                     if not item.get("required", True) and item["status"] == "verified"}
                unsupported_optional = {item.get("id") for item in selected.get("requiredRoles", [])
                                        if not item.get("required", True) and item.get("found")
                                        and item.get("actualPath") != item.get("path")}
                excluded_optional = [item.get("actualPath") or item["path"]
                                     for item in cfg["setup"]["checklist"]["items"]
                                     if not item.get("required", True)
                                     and (item["id"] not in verified_optional or item["id"] in unsupported_optional)]
                after = assisted_install._snapshot(content, check_cancel)
                required_changed = any(
                    assisted_install._item_changed(after, item["path"], optional_paths)
                    or assisted_install._item_digest(before, item["path"], optional_paths)
                    != assisted_install._item_digest(after, item["path"], optional_paths)
                    for item in cfg["setup"]["checklist"]["items"] if item.get("required", True))
                if after["invalid"] or required_changed:
                    raise install_jobs.JobError("assisted_content_changed", "Preparation files changed during final verification.",
                                                ["Wait for files to settle", "Recheck preparation"])
                checks = prerequisites(True, False)
                if not checks["ready"]:
                    raise install_jobs.JobError("missing_prerequisites", "Build prerequisites are missing; the waiting folder is preserved.",
                                                ["Install the listed build tools", "Continue preparation"],
                                                details={"checks": checks["checks"]})
                identity = {"kind": "folder", **install_jobs.payload_identity(
                    selected["contentRoot"], check_cancel, exclude_paths=excluded_optional),
                    "excludedOptionalPaths": excluded_optional}
                if journal.get("sourceIdentity") != identity:
                    work = job_paths["staging"] / "work"
                    if work.exists():
                        install_jobs.remove_owned(work, job_paths["staging"], journal["jobId"],
                                                  journal.get("ownedTokens", {}).get("work"))
                    journal["validatedInputs"] = {}
                journal["sourceIdentity"] = identity
                journal["candidateId"] = selected["id"]
                journal["recipeIdentity"] = recipe_facts
                journal.setdefault("assisted", {})["validatedDropBytes"] = before["bytes"]
                install_jobs.update_job(job_paths, journal, "waiting_for_input")
                prepared = {"gameId": game_id, "directory": directory, "sourcePath": content,
                            "root": root, "cfg": cfg, "sourceKind": "folder", "explicitSource": None,
                            "selected": selected, "excludePaths": excluded_optional}
                return install({**request, "mode": "assisted", "jobId": journal["jobId"]}, storage, prepared)
            finally:
                DESTINATION_LOCKS = []


def play(request, storage):
    game_id = request["game"]
    game_dir = KIT / "games" / game_id
    if game_dir.parent != KIT / "games" or game_id == "stub" or not (game_dir / "game.toml").is_file():
        raise install_jobs.JobError("unknown_game", "The requested game is unavailable.")
    root = installation_root(storage, game_id)
    cfg = game_config.load(game_dir, root)
    saved = install_jobs.read_versioned(root / ".recomp" / "installed.json", "installed_facts")
    expected_launch = {"executable": cfg["game"]["executable"], "sha256": cfg["game"]["sha256"],
                       "requiredDirs": cfg.get("setup", {}).get("required_dirs", [])}
    if (saved.get("gameId") != game_id or saved.get("launch") != expected_launch
            or not artifact(cfg, root).is_file()):
        raise ValueError("Build this game successfully before playing")
    if install_jobs.owned_tree_identity(root / "app") != saved.get("appIdentity"):
        raise install_jobs.JobError("installed_app_mismatch", "The installed app does not match its saved fingerprint.")
    image = validate_game(root / "original", expected_launch["executable"],
                          expected_launch["sha256"], expected_launch["requiredDirs"]) / expected_launch["executable"]
    for module in cfg["aux_modules"]:
        auxiliary = root / "original" / module["path"].relative_to(cfg["developer_exe_path"].parent)
        if not auxiliary.is_file() or hashlib.sha256(auxiliary.read_bytes()).hexdigest() != module["sha256"]:
            raise install_jobs.JobError("installed_auxiliary_mismatch", "An installed auxiliary module does not match its supported hash.",
                                        details={"path": str(auxiliary.relative_to(root))})
    binary = artifact(cfg, root)
    log = root / "game.log"
    with log.open("ab") as output:
        subprocess.Popen([str(binary), "--exe", str(image)],
                         cwd=root / "original", stdout=output, stderr=output,
                         start_new_session=os.name != "nt")
    return {"game": game_id, "log": str(log)}


def main():
    global CANCEL_FILE, DESTINATION_LOCKS
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    os.environ.pop("RECOMP_BUILD_ROOT", None)
    os.environ.pop("BUILDLOCK_HELD", None)
    request = json.loads(args.request)
    if request.get("action") in ("install", "inspect", "discover"):
        CANCEL_FILE = Path(request["cancelFile"]) if request.get("cancelFile") else None
        if os.name != "nt":
            signal.signal(signal.SIGTERM, cancel)
    storage = args.storage.expanduser().resolve()
    try:
        action = request["action"]
        if action == "catalog":
            result = {"games": catalog(storage), "storage": str(storage)}
        elif action == "inspect":
            result = inspect_request(request)
        elif action == "discover":
            result = discover_request(request)
        elif action == "job_log":
            result = job_log(request, storage)
        elif action == "assisted_start":
            result = assisted_install.start(request, storage, KIT, check_cancel,
                                            lambda game_dir: install_jobs.recipe_identity(KIT, game_dir),
                                            inspect_folder, emit)
        elif action == "assisted_status":
            result = assisted_install.status(request, storage, KIT, request.get("sessionId"), check_cancel)
        elif action == "assisted_path":
            result = assisted_install.path(request, storage)
        elif action == "assisted_cancel":
            result = assisted_install.cancel_job(request, storage)
        elif action == "assisted_recheck":
            result = assisted_install.recheck(request, storage, KIT,
                                              lambda game_dir: install_jobs.recipe_identity(KIT, game_dir))
        elif action == "check":
            result = prerequisites(request.get("needsRust", True), request.get("needsInnoextract", False))
        elif action == "install":
            if request.get("jobId"):
                result = resume_install(request, storage)
            else:
                prepared = prepare_install(request, storage)
                with install_jobs.DestinationLock(storage, lock_name="registry.lock") as registry_lock:
                    with install_jobs.DestinationLock(prepared["root"]) as destination_lock:
                        DESTINATION_LOCKS = [registry_lock, destination_lock]
                        try:
                            prepared = prepare_install(request, storage)
                            if prepared["root"] != destination_lock.root:
                                raise install_jobs.JobError("destination_changed", "The registered destination changed during preflight.")
                            result = install(request, storage, prepared)
                        finally:
                            DESTINATION_LOCKS = []
        elif action == "play":
            root = installation_root(storage, request["game"])
            with install_jobs.DestinationLock(root):
                result = play(request, storage)
        else:
            raise ValueError("Unknown library action")
        emit("result", result=result)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired, TimeoutError) as error:
        fields = {"message": str(error)}
        if getattr(error, "diagnostic", None):
            fields["diagnostic"] = error.diagnostic
        elif isinstance(error, InterruptedError):
            fields["diagnostic"] = {"code": "cancelled", "stage": "cancelled", "path": ".",
                                     "message": "The installation was cancelled.", "details": {},
                                     "recovery": ["resume", "choose_source"]}
        elif isinstance(error, install_jobs.JobError):
            fields["diagnostic"] = {"code": error.code, "stage": "installing", "path": ".",
                                     "message": str(error), "details": error.details,
                                     "recovery": error.recovery}
        else:
            fields["diagnostic"] = {"code": "library_operation_failed", "stage": request.get("action", "unknown"),
                                     "path": ".", "message": str(error), "details": {},
                                     "recovery": ["check_permissions", "open_log", "retry"]}
        emit("error", **fields)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
