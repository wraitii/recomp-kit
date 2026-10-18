import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def package(bundle, build_root, python_root):
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise ValueError("Library packaging currently requires native arm64 macOS")
    python_root = Path(python_root).resolve()
    if not (python_root / "bin/python3").is_file():
        raise ValueError("Provide a standalone Python installation with --library-python-root")
    dependency_build = Path(build_root) / "library-dependencies"
    subprocess.run([sys.executable, str(ROOT / "tools/build.py"), "--game", "stub", "--stub",
                    "--target", "app", "--build-root", str(dependency_build)], check=True,
                   env=dict(os.environ, MACOSX_DEPLOYMENT_TARGET="14.0"))
    resources = bundle / "Contents/Resources"
    if resources.exists():
        shutil.rmtree(resources)
    resources.mkdir(parents=True)
    kit = resources / "kit"
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    for name in filter(None, tracked):
        source = ROOT / name
        if source.is_symlink():
            raise ValueError(f"Unexpected source symlink: {name}")
        destination = kit / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    python = resources / "python"
    shutil.copytree(python_root, python, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    subprocess.run([str(python / "bin/python3"), "-m", "pip", "install", "--only-binary=:all:",
                    "cmake==4.1.2", "ninja==1.13.0"], check=True)
    subprocess.run([str(python / "bin/python3"), "-m", "pip", "install", "--only-binary=:all:",
                    *[line for line in (ROOT / "tools/requirements.txt").read_text().splitlines()
                      if line.strip() and not line.startswith("unicorn")]], check=True)
    packages = next((python / "lib").glob("python*/site-packages"))
    for name, source in (("cmake", packages / "cmake/data/bin/cmake"),
                         ("ninja", packages / "ninja/data/bin/ninja")):
        if name == "ninja" and not source.exists():
            source = python / "bin/ninja"
        destination = python / "bin" / name
        if source != destination:
            destination.unlink(missing_ok=True)
            shutil.copy2(source, destination)
        thin = destination.with_suffix(".arm64")
        subprocess.run(["lipo", str(destination), "-thin", "arm64", "-output", str(thin)], check=True)
        thin.replace(destination)
    cmake_data = packages / "cmake/data"
    (python / "share").mkdir(exist_ok=True)
    for path in (cmake_data / "share").glob("cmake-*"):
        shutil.move(str(path), python / "share" / path.name)
    shutil.rmtree(cmake_data)
    for name in ("ctest", "cpack"):
        (python / "bin" / name).unlink(missing_ok=True)
    dependencies = resources / "dependencies"
    dependencies.mkdir()
    cmake_build = dependency_build / "cmake/macos-stub"
    subprocess.run([str(python / "bin/cmake"), "--install", str(cmake_build / "_deps/sdl3-build"),
                    "--prefix", str(dependencies / "sdl")], check=True)
    if not (dependencies / "sdl/lib/cmake/SDL3/SDL3Config.cmake").is_file():
        raise ValueError("SDL did not install its development package")
    shutil.copy2(cmake_build / "_deps/sdl3-src/LICENSE.txt", dependencies / "sdl/LICENSE.txt")
    shutil.copytree(cmake_build / "ffmpeg", dependencies / "ffmpeg", symlinks=True)
    shutil.copy2(ROOT / "third_party/ffmpeg/NOTICE.md", dependencies / "ffmpeg/NOTICE.md")
    sources = resources / "sources"
    sources.mkdir()
    shutil.copy2(cmake_build / "ffmpeg-prefix/src/ffmpeg-7.1.1.tar.xz", sources)
    registry = Path(os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))) / "registry/src"
    for lock in (ROOT / "desktop/library/Cargo.lock", ROOT / "graphics/d3d8-wgpu/Cargo.lock"):
        for entry in tomllib.loads(lock.read_text())["package"]:
            name = f"{entry['name']}-{entry['version']}"
            for crate in registry.glob(f"*/{name}"):
                for path in crate.rglob("*"):
                    if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")):
                        destination = resources / "licenses/cargo" / name / path.relative_to(crate)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)
    bridge = dependencies / "d3d8"
    bridge.mkdir()
    for name in ("d3d_bridge.h", "native-libs.rsp"):
        shutil.copy2(dependency_build / "d3d8-generated" / name, bridge / name)
    shutil.copytree(dependency_build / "d3d8-generated/rust", bridge / "rust")
    shutil.copy2(dependency_build / "d3d8-wgpu/rust/release/libd3d8_wgpu.a", bridge)
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest = {"source": source_sha, "architecture": "arm64", "macosMinimum": "14.0",
                "files": {path.relative_to(dependencies).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(dependencies.rglob("*")) if path.is_file()}}
    (dependencies / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for path in python.rglob("__pycache__"):
        shutil.rmtree(path)
    mach_magic = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
    for path in sorted(resources.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                magic = stream.read(4)
            if magic in mach_magic:
                subprocess.run(["codesign", "--force", "--sign", "-", str(path)], check=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    archive = bundle.parent / "recomp-library-macos-arm64.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(bundle), str(archive)], check=True)
    print(f"Portable library: {archive} ({archive.stat().st_size / 1048576:.1f} MiB)")
