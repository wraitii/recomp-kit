import os
import json
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
from app_icons import set_bundle_icon, svg_image, write_desktop_icons

ROOT = Path(__file__).resolve().parents[1]


def build_extractors(build_root, config, innoextract=False):
    from build import cmake_tool
    environment = dict(os.environ, CMAKE_POLICY_VERSION_MINIMUM="3.5")
    output = Path(build_root) / "toolkit/extractors"
    command = [cmake_tool("cmake"), "-S", str(ROOT / "tools/extractors"), "-B", str(output),
               "-G", "Ninja", "-DCMAKE_BUILD_TYPE=" + config,
               "-DCMAKE_MAKE_PROGRAM=" + cmake_tool("ninja"),
               "-DRECOMP_BUILD_INNOEXTRACT=" + ("ON" if innoextract else "OFF")]
    if platform.system() == "Darwin":
        command += ["-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0"]
    subprocess.run(command, check=True, env=environment)
    targets = ["libunshield", *(["innoextract_external"] if innoextract else [])]
    subprocess.run([cmake_tool("cmake"), "--build", str(output), "--config", config,
                    "--parallel", str(min(os.cpu_count() or 2, 8)), "--target", *targets], check=True, env=environment)
    return output


def build(build_root, config, portable=False, python_root=None):
    if portable and (platform.system() != "Darwin" or platform.machine() != "arm64" or not python_root):
        raise ValueError("--package-toolkit requires arm64 macOS and --toolkit-python-root")
    if portable:
        os.environ["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
    source = ROOT / "desktop/library"
    output = Path(build_root) / "toolkit"
    output.mkdir(parents=True, exist_ok=True)
    build_extractors(build_root, config, innoextract=portable)
    app_icon = output / "icon.png"
    write_desktop_icons(svg_image("icon"), output)
    windows_icon = output / "icon.ico"
    env = dict(os.environ, CARGO_TARGET_DIR=str(output / "rust"),
               RECOMP_TOOLKIT_KIT=str(ROOT), RECOMP_TOOLKIT_PYTHON=sys.executable,
               RECOMP_TOOLKIT_UNSHIELD=str(output / "extractors/unshield/lib" /
                   ("unshield.dll" if os.name == "nt" else "libunshield.dylib" if sys.platform == "darwin" else "libunshield.so")),
               TAURI_CONFIG=json.dumps({"bundle": {"icon": [str(app_icon), str(windows_icon)]}}))
    command = ["cargo", "build", "--manifest-path", str(source / "Cargo.toml")]
    if config == "Release":
        command.append("--release")
    if (source / "Cargo.lock").is_file():
        command.append("--locked")
    subprocess.run(command, cwd=source, env=env, check=True)
    binary = output / "rust" / ("release" if config == "Release" else "debug") / (
        "recomp-toolkit.exe" if os.name == "nt" else "recomp-toolkit")
    if platform.system() == "Darwin":
        bundle = output / "Recomp Toolkit.app"
        contents = bundle / "Contents"
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, contents / "MacOS/recomp-toolkit")
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleExecutable": "recomp-toolkit", "CFBundleIdentifier": "dev.recomptoolkit.app",
                          "CFBundleName": "Recomp Toolkit", "CFBundlePackageType": "APPL",
                          "CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "0.1.0",
                          "NSHighResolutionCapable": True,
                          **({"LSMinimumSystemVersion": "14.0"} if portable else {})}, stream)
        set_bundle_icon(bundle, output / "icon.icns")
        subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
        if portable:
            from package_library import package
            package(bundle, build_root, python_root)
        print(f"Recomp Toolkit app: {bundle}")
    else:
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, output / binary.name)
        print(f"Recomp Toolkit app: {output / binary.name}")
