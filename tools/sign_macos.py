import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess


def macho_type(path):
    with path.open("rb") as stream:
        header = stream.read(32)
        magic = header[:4]
        if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            offset = struct.unpack_from(">Q" if magic[-1] == 0xbf else ">I", header, 16)[0]
            stream.seek(offset)
            header = stream.read(16)
            magic = header[:4]
        endian = {b"\xcf\xfa\xed\xfe": "<", b"\xce\xfa\xed\xfe": "<",
                  b"\xfe\xed\xfa\xcf": ">", b"\xfe\xed\xfa\xce": ">"}.get(magic)
        return struct.unpack_from(endian + "I", header, 12)[0] if endian else None


def sign_bundle(bundle, identity="-", keychain=None):
    bundle = Path(bundle).resolve(strict=True)
    command = ["codesign", "--force", "--sign", identity]
    if identity != "-":
        command.extend(["--options", "runtime", "--timestamp"])
    if keychain:
        command.extend(["--keychain", str(keychain)])
    paths = sorted(bundle.rglob("*"))
    binaries = [path for path in paths
                if not path.is_symlink() and path.is_file() and macho_type(path) in (2, 6, 8)]
    for path in binaries:
        subprocess.run([*command, str(path)], check=True)
    nested = [path for path in paths if not path.is_symlink() and path.is_dir()
              and path.suffix in (".app", ".framework", ".xpc", ".bundle")]
    for path in sorted(nested, key=lambda path: len(path.parts), reverse=True):
        subprocess.run([*command, str(path)], check=True)
    dependencies = bundle / "Contents/Resources/dependencies"
    manifest_path = dependencies / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        manifest["files"] = {
            path.relative_to(dependencies).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(dependencies.rglob("*")) if path.is_file() and path != manifest_path}
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    subprocess.run([*command, str(bundle)], check=True)
    for path in binaries:
        subprocess.run(["codesign", "--verify", "--strict", str(path)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sign nested Mach-O code before its enclosing app bundle")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--keychain", type=Path)
    args = parser.parse_args()
    sign_bundle(args.bundle, args.identity, args.keychain)
