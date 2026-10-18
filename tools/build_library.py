import os
import json
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import struct
import zlib

ROOT = Path(__file__).resolve().parents[1]


def icon(path):
    size = 64
    glyph = ["11110", "10001", "10001", "11110", "10100", "10010", "10001"]
    pixels = bytearray()
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            gx, gy = (x - 17) // 6, (y - 11) // 6
            ink = 0 <= gx < 5 and 0 <= gy < 7 and glyph[gy][gx] == "1"
            pixels.extend((183, 229, 154, 255) if ink else (26, 32, 36, 255))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def build(build_root, config, portable=False, python_root=None):
    if portable and (platform.system() != "Darwin" or platform.machine() != "arm64" or not python_root):
        raise ValueError("--package-library requires arm64 macOS and --library-python-root")
    source = ROOT / "desktop/library"
    output = Path(build_root) / "library"
    output.mkdir(parents=True, exist_ok=True)
    app_icon = output / "icon.png"
    icon(app_icon)
    windows_icon = output / "icon.ico"
    png = app_icon.read_bytes()
    windows_icon.write_bytes(struct.pack("<HHH", 0, 1, 1)
                             + struct.pack("<BBBBHHII", 64, 64, 0, 0, 1, 32, len(png), 22) + png)
    env = dict(os.environ, CARGO_TARGET_DIR=str(output / "rust"),
               RECOMP_LIBRARY_KIT=str(ROOT), RECOMP_LIBRARY_PYTHON=sys.executable,
               TAURI_CONFIG=json.dumps({"bundle": {"icon": [str(app_icon), str(windows_icon)]}}))
    command = ["cargo", "build", "--manifest-path", str(source / "Cargo.toml")]
    if config == "Release":
        command.append("--release")
    if (source / "Cargo.lock").is_file():
        command.append("--locked")
    subprocess.run(command, cwd=source, env=env, check=True)
    binary = output / "rust" / ("release" if config == "Release" else "debug") / (
        "recomp-library.exe" if os.name == "nt" else "recomp-library")
    if platform.system() == "Darwin":
        bundle = output / "Recomp Library.app"
        contents = bundle / "Contents"
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, contents / "MacOS/recomp-library")
        with (contents / "Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleExecutable": "recomp-library", "CFBundleIdentifier": "dev.recompkit.library",
                          "CFBundleName": "Recomp Library", "CFBundlePackageType": "APPL",
                          "CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "0.1.0",
                          "NSHighResolutionCapable": True}, stream)
        subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
        if portable:
            from package_library import package
            package(bundle, build_root, python_root)
        print(f"Library app: {bundle}")
    else:
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, output / binary.name)
        print(f"Library app: {output / binary.name}")
