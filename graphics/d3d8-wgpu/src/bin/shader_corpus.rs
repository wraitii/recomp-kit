use std::process::ExitCode;

fn main() -> ExitCode {
    let mut paths: Vec<String> = std::env::args().skip(1).collect();
    if paths.is_empty() {
        eprintln!("usage: shader-corpus <shader.bin>...");
        return ExitCode::FAILURE;
    }
    paths.sort();
    let mut ok = 0usize;
    let mut failed = 0usize;
    for path in &paths {
        match inspect(path) {
            Ok(version) => {
                println!("ok {version} {path}");
                ok += 1;
            }
            Err(error) => {
                eprintln!("FAIL {path}: {error}");
                failed += 1;
            }
        }
    }
    println!("{ok} ok, {failed} failed");
    if failed == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}

fn inspect(path: &str) -> Result<String, Box<dyn std::error::Error>> {
    let bytes = std::fs::read(path)?;
    if bytes.is_empty() || bytes.len() % 4 != 0 {
        return Err(format!(
            "byte length {} is not a non-empty multiple of four",
            bytes.len()
        )
        .into());
    }
    let words: Vec<u32> = bytes
        .chunks_exact(4)
        .map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect();
    let pixel = words[0] >> 16 == 0xffff;
    let program = d3d8_wgpu::d3d9::wgsl::Program::decode(&words, pixel)?;
    let wgsl = d3d8_wgpu::d3d9::wgsl::translate(&program, &Default::default())?;
    d3d8_wgpu::d3d9::wgsl::validate(&wgsl)?;
    Ok(program.version())
}
