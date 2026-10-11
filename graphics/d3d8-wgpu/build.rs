use std::{env, path::PathBuf};

fn main() {
    let crate_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("d3d8_abi.h");
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=cbindgen.toml");
    let config = cbindgen::Config::from_file(crate_dir.join("cbindgen.toml"))
        .expect("Cannot read D3D8 ABI generator configuration");
    cbindgen::Builder::new()
        .with_crate(crate_dir)
        .with_config(config)
        .generate()
        .expect("Cannot generate D3D8 ABI header")
        .write_to_file(output);
}
