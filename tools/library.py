import argparse
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import shlex
import signal
import subprocess
import sys
import threading
import zipfile

import game_config
from setup import validate_game
from stage_game_files import excluded
from web_launcher import game_entry

CANCEL_FILE = None
KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "tools/recomp"))
import buildlock


def emit(kind, **fields):
    print(json.dumps({"kind": kind, **fields}), flush=True)


def cancel(signum, frame):
    raise InterruptedError("Installation cancelled")


def check_cancel():
    if CANCEL_FILE is not None and CANCEL_FILE.exists():
        raise InterruptedError("Installation cancelled")


def run(command, log_path=None):
    check_cancel()
    process = subprocess.Popen(command, cwd=KIT, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors="replace",
                               start_new_session=os.name != "nt")
    output = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    try:
        with (log_path.open("w") if log_path else nullcontext()) as log:
            while True:
                check_cancel()
                try:
                    line = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                if log:
                    log.write(line)
                    log.flush()
                emit("log", text=line.rstrip())
        while process.poll() is None:
            check_cancel()
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise ValueError(f"{Path(command[0]).name} failed with exit code {process.returncode}")
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
        process.wait()
        raise
    finally:
        reader.join()
        process.stdout.close()


def installation_root(storage, game_id):
    record = storage / game_id / "location.json"
    return Path(json.loads(record.read_text())["path"]) if record.is_file() else storage / game_id


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


def write_json(path, value):
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def recover_app(root):
    transaction = root / "installing.json"
    installed, previous = root / "app", root / "app.previous"
    if transaction.is_file():
        saved = json.loads(transaction.read_text())
        if previous.exists():
            remove(installed)
            previous.replace(installed)
        elif not saved["hadApp"]:
            remove(installed)
        elif not installed.exists():
            raise OSError("Cannot restore the previous app: its backup is missing")
        marker = root / "ready.json"
        if saved["ready"] is None:
            marker.unlink(missing_ok=True)
        else:
            temporary = root / "ready.json.part"
            temporary.write_text(saved["ready"])
            temporary.replace(marker)
        transaction.unlink()
    if previous.exists() and not installed.exists():
        previous.replace(installed)


def clean_build(cfg, root):
    recover_app(root)
    errors = []
    for name in ("cmake", "recomp", "package", "d3d8-generated", "d3d8-wgpu", "extracting", "importing",
                 "app.new", "app.previous", "ready.json.part", "installing.json.part", "imported.json.part",
                 "import-source.json.part", cfg["game"]["app_name"] + ".app"):
        try:
            remove(root / name)
        except OSError as error:
            errors.append(f"{name}: {error}")
    if errors:
        raise OSError("Temporary-file cleanup failed: " + "; ".join(errors))
    (root / "work.json").unlink(missing_ok=True)


def publish_app(root, pending, stamp):
    check_cancel()
    installed, previous = root / "app", root / "app.previous"
    marker = root / "ready.json"
    temporary = root / "ready.json.part"
    temporary.write_text(json.dumps(stamp))
    write_json(root / "installing.json", {"hadApp": installed.exists(),
                                          "ready": marker.read_text() if marker.is_file() else None})
    try:
        if installed.exists():
            installed.replace(previous)
        pending.replace(installed)
        temporary.replace(marker)
        (root / "installing.json").unlink()
    except BaseException:
        recover_app(root)
        raise


def recipe(game_dir):
    digest = hashlib.sha256((game_dir / "game.toml").read_bytes())
    revision = subprocess.run(["git", "-C", str(KIT), "rev-parse", "HEAD"],
                              capture_output=True, check=True).stdout
    digest.update(revision)
    changes = subprocess.run(["git", "-C", str(KIT), "diff", "HEAD", "--",
                              str(game_dir), "runtime", "dx", "host", "platform", "mods", "cmake",
                              "CMakeLists.txt", "tools/recomp", "tools/build.py",
                              "tools/game_config.py", "tools/gen_game_config.py"],
                             capture_output=True, check=True).stdout
    digest.update(changes)
    return digest.hexdigest()


def catalog(storage, repair=False):
    games = []
    for directory in sorted((KIT / "games").iterdir()):
        if directory.name == "stub" or not (directory / "game.toml").is_file():
            continue
        item = {"id": directory.name, "title": directory.name, "state": "unavailable"}
        try:
            root = installation_root(storage, directory.name)
            cfg = game_config.load(directory, root)
            if repair and ((root / "work.json").is_file() or (root / "installing.json").is_file() or (root / "app.previous").exists()):
                try:
                    clean_build(cfg, root)
                except (OSError, ValueError, KeyError) as error:
                    item["maintenanceError"] = str(error)
            known = (storage / directory.name / "location.json").is_file() or (root / "imported.json").is_file() or (root / "library.json").is_file()
            destination = root if known else Path.home() / ("Applications" if platform.system() == "Darwin" else "Games") / directory.name
            item.update(game_entry(cfg), id=directory.name, installation=str(destination),
                        needsRust=True)
            item["state"] = "notInstalled"
            if (root / "imported.json").is_file():
                item["managedSource"] = str(root / "original")
                item["buildLog"] = str(root / "build.log")
                item["state"] = "needsBuild"
            marker = root / "ready.json"
            if marker.is_file() and artifact(cfg, root).is_file() and cfg["developer_exe_path"].is_file():
                item["state"] = "ready"
                try:
                    saved = json.loads(marker.read_text())
                    item["updateAvailable"] = saved.get("recipe") != recipe(directory)
                except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
                    item["updateCheckError"] = str(error)
            elif cfg["code_map_path"] is None or not cfg["code_map_path"].exists():
                item["state"] = "unavailable"
                item["detail"] = "Translation metadata is not available in this checkout."
        except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            item["state"] = "unavailable"
            item["detail"] = str(error)
        games.append(item)
    return games


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


def find_install(source, executable):
    candidates = [source]
    for _ in range(3):
        next_level = []
        for directory in candidates:
            entries = list(directory.iterdir())
            if any(p.is_file() and p.name == executable for p in entries):
                return directory
            next_level.extend(p for p in entries if p.is_dir() and not p.is_symlink())
        candidates = next_level
    raise ValueError(f"No {executable} found in the selected source")


def extract(source, dest):
    emit("stage", stage="extracting", message="Extracting game files")
    dest.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            required = sum(entry.file_size for entry in entries)
            if shutil.disk_usage(dest).free < required:
                raise ValueError("Not enough free space to extract this ZIP")
            for index, entry in enumerate(entries):
                check_cancel()
                target = dest / entry.filename.replace("\\", "/")
                if not target.resolve().is_relative_to(dest.resolve()) or ":" in entry.filename:
                    raise ValueError(f"Unsafe ZIP entry: {entry.filename}")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as src, target.open("wb") as dst:
                        copy_bytes(src, dst)
                emit("progress", current=entry.filename, fraction=(index + 1) / max(1, len(entries)))
    elif source.suffix.lower() == ".exe":
        helper = shutil.which("innoextract")
        if not helper:
            raise ValueError("GOG installer import requires innoextract. Install it or choose an installed game folder.")
        run([helper, "--gog", "--output-dir", str(dest), str(source)], dest.parent / "extract.log")
    else:
        raise ValueError("Select a game folder, ZIP, or GOG offline setup .exe")
    return dest


def copy_bytes(source, destination):
    while True:
        check_cancel()
        chunk = source.read(1024 * 1024)
        if not chunk:
            return
        destination.write(chunk)


def copy_game(source, dest, cfg):
    emit("stage", stage="importing", message="Importing your game copy")
    dest.mkdir(parents=True, exist_ok=True)
    keep = {cfg["game"]["executable"], *[m["name"] for m in cfg["aux_modules"]]}
    entries = []
    for path in source.rglob("*"):
        check_cancel()
        if path.is_symlink():
            raise ValueError(f"Game import does not support symbolic links: {path}")
        relative = path.relative_to(source)
        if path.is_file() and not excluded(relative, cfg["bundle"]["exclude"], keep):
            entries.append((path, relative))
    required = sum(path.stat().st_size for path, _ in entries)
    if shutil.disk_usage(dest).free < required:
        raise ValueError("Not enough free space to import this game")
    for index, (path, relative) in enumerate(entries):
        check_cancel()
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with path.open("rb") as src, partial.open("wb") as dst:
            copy_bytes(src, dst)
        shutil.copystat(path, partial)
        check_cancel()
        partial.replace(target)
        emit("progress", current=str(relative), fraction=(index + 1) / max(1, len(entries)))


def install(request, storage):
    game_id = request["game"]
    directory = KIT / "games" / game_id
    if directory.parent != KIT / "games" or not (directory / "game.toml").is_file() or game_id == "stub":
        raise ValueError("Unknown game")
    previous_root = installation_root(storage, game_id).resolve()
    root = Path(request.get("destination") or previous_root).expanduser().resolve()
    cfg = game_config.load(directory, root)
    emit("stage", stage="checking", message="Checking build prerequisites")
    checks = prerequisites(True,
                           Path(request["source"]).suffix.lower() == ".exe")
    if not checks["ready"]:
        raise ValueError("Missing prerequisites: " + ", ".join(c["label"] for c in checks["checks"] if not c["ok"]))
    if cfg["code_map_path"] is None or not cfg["code_map_path"].exists():
        raise ValueError("This game has no translation metadata")
    if cfg["game"].get("developer_exe"):
        raise ValueError("Library installation requires a recipe without a fixed developer_exe")
    check_cancel()
    owner = root / "library.json"
    if root.exists() and any(root.iterdir()):
        owned = owner.is_file() and json.loads(owner.read_text()).get("game") == game_id
        legacy = root == previous_root and (root / "imported.json").is_file()
        if not owned and not legacy:
            raise ValueError("The install folder is not empty. Choose another location; existing files will not be overwritten.")
    if root != previous_root and (previous_root / "imported.json").is_file():
        raise ValueError("Rebuild this game in its existing install folder.")
    root.mkdir(parents=True, exist_ok=True)
    write_json(owner, {"game": game_id})
    registry = storage / game_id
    registry.mkdir(parents=True, exist_ok=True)
    write_json(registry / "location.json", {"path": str(root)})
    clean_build(cfg, root)
    write_json(root / "work.json", {"game": game_id})
    try:
        source = Path(request["source"]).expanduser().resolve()
        original_source = str(source)
        data = root / "original"
        staging = root / "extracting"
        if not source.is_dir():
            source = extract(source, staging)
        source = find_install(source, cfg["game"]["executable"])
        if source != data.resolve() and data.resolve().is_relative_to(source):
            raise ValueError("The import source cannot contain the library's game storage")
        emit("stage", stage="validating", message="Verifying the supported game version")
        required = cfg.get("setup", {}).get("required_dirs", [])
        validate_game(source, cfg["game"]["executable"], cfg["game"]["sha256"], required)
        for module in cfg["aux_modules"]:
            image = source / module["path"].relative_to(cfg["developer_exe_path"].parent)
            if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != module["sha256"]:
                raise ValueError(f"Missing or unsupported auxiliary module: {module['name']}")
        check_cancel()
        import_record = root / "import-source.json"
        imported = root / "imported.json"
        if source != data.resolve():
            if imported.is_file() or (root / "ready.json").is_file():
                raise ValueError("This game already has a complete library copy. Choose that folder to rebuild without replacing its files or mods.")
            if data.exists() and not import_record.is_file():
                raise ValueError("Game storage already exists without an import record; existing files will not be overwritten.")
            write_json(import_record, {"source": original_source})
            pending_data = root / "importing"
            copy_game(source, pending_data, cfg)
            validate_game(pending_data, cfg["game"]["executable"], cfg["game"]["sha256"], required)
            check_cancel()
            remove(data)
            pending_data.replace(data)
        validate_game(data, cfg["game"]["executable"], cfg["game"]["sha256"], required)
        write_json(imported, {"source": str(data)})
        remove(staging)
        emit("stage", stage="building", message="Building the game app")
        run([sys.executable, str(KIT / "tools/build.py"), "--game", game_id,
             "--build-root", str(root), "--regenerate", "--target", "app"], root / "build.log")
        binary = artifact(cfg, root, installed=False)
        if not binary.is_file():
            raise ValueError("Build finished without the expected game application")
        check_cancel()
        emit("stage", stage="installing", message="Installing the game app")
        built = root / (cfg["game"]["app_name"] + ".app") if platform.system() == "Darwin" else binary.parent
        pending = root / "app.new"
        if platform.system() == "Darwin":
            pending.mkdir()
            shutil.move(str(built), str(pending / built.name))
        else:
            shutil.move(str(built), str(pending))
        relative_binary = artifact(cfg, root).relative_to(root / "app")
        if not (pending / relative_binary).is_file():
            raise ValueError("Installation staging is missing the game application")
        try:
            fingerprint = recipe(directory)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            fingerprint = None
            emit("log", text=f"Update check unavailable: {error}")
        stamp = {"recipe": fingerprint, "launch": {"executable": cfg["game"]["executable"],
                 "sha256": cfg["game"]["sha256"], "requiredDirs": required}}
        publish_app(root, pending, stamp)
        emit("stage", stage="cleaning", message="Removing temporary build files")
        return {"game": game_id, "state": "ready"}
    finally:
        failure = sys.exc_info()[0] is not None
        try:
            clean_build(cfg, root)
        except OSError as error:
            if not failure:
                raise
            emit("log", text=str(error))


def play(request, storage):
    item = next((g for g in catalog(storage) if g["id"] == request["game"]), None)
    if not item or item["state"] != "ready":
        raise ValueError("Build this game successfully before playing")
    root = installation_root(storage, item["id"])
    cfg = game_config.load(KIT / "games" / item["id"], root)
    try:
        saved = json.loads((root / "ready.json").read_text())
    except (OSError, ValueError):
        saved = {}
    launch = saved.get("launch", {"executable": cfg["game"]["executable"],
                                 "sha256": cfg["game"]["sha256"], "requiredDirs": cfg["setup"]["required_dirs"]})
    image = validate_game(root / "original", launch["executable"], launch["sha256"], launch["requiredDirs"]) / launch["executable"]
    binary = artifact(cfg, root)
    log = root / "game.log"
    with log.open("ab") as output:
        subprocess.Popen([str(binary), "--exe", str(image)],
                         cwd=root / "original", stdout=output, stderr=output,
                         start_new_session=os.name != "nt")
    return {"game": item["id"], "log": str(log)}


def main():
    global CANCEL_FILE
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    os.environ.pop("RECOMP_BUILD_ROOT", None)
    os.environ.pop("BUILDLOCK_HELD", None)
    request = json.loads(args.request)
    if request.get("action") == "install":
        CANCEL_FILE = Path(request["cancelFile"]) if request.get("cancelFile") else None
        if os.name != "nt":
            signal.signal(signal.SIGTERM, cancel)
    storage = args.storage.expanduser().resolve()
    try:
        action = request["action"]
        if action == "catalog":
            with buildlock.BuildLock(storage, "library maintenance"):
                result = {"games": catalog(storage, repair=True), "storage": str(storage)}
        elif action == "check":
            result = prerequisites(request.get("needsRust", True), request.get("needsInnoextract", False))
        elif action == "install":
            with buildlock.BuildLock(storage, "library installation"):
                result = install(request, storage)
        elif action == "play":
            with buildlock.BuildLock(storage, "library launch"):
                result = play(request, storage)
        else:
            raise ValueError("Unknown library action")
        emit("result", result=result)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError, subprocess.TimeoutExpired, TimeoutError) as error:
        emit("error", message=str(error))
        return 1
    finally:
        if CANCEL_FILE is not None:
            CANCEL_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
