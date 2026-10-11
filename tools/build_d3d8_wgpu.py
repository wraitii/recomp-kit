import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cargo", required=True)
    parser.add_argument("--crate", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--header", type=Path, required=True)
    args = parser.parse_args()
    process = subprocess.Popen(
        [args.cargo, "build", "--release", "--lib", "--locked", "--message-format=json",
         "--manifest-path", str(args.crate / "Cargo.toml")],
        env=dict(os.environ, CARGO_TARGET_DIR=str(args.target_dir)),
        stdout=subprocess.PIPE, text=True)
    outputs = {}
    package = None
    for line in process.stdout:
        message = json.loads(line)
        reason = message["reason"]
        if reason == "build-script-executed":
            outputs[message["package_id"]] = Path(message["out_dir"])
        elif reason == "compiler-artifact" and message["target"]["name"] == "d3d8_wgpu":
            package = message["package_id"]
        elif reason == "compiler-message":
            print(message["message"].get("rendered") or message["message"]["message"], end="", flush=True)
    status = process.wait()
    if status:
        parser.exit(status, "D3D8 renderer Cargo build failed\n")
    if package not in outputs:
        parser.exit(1, "Cargo did not report the D3D8 ABI header output directory\n")
    source = outputs[package] / "d3d8_abi.h"
    if not source.is_file():
        parser.exit(1, f"Cargo did not generate the D3D8 ABI header: {source}\n")
    args.header.parent.mkdir(parents=True, exist_ok=True)
    if not args.header.is_file() or args.header.read_bytes() != source.read_bytes():
        shutil.copyfile(source, args.header)


if __name__ == "__main__":
    main()
