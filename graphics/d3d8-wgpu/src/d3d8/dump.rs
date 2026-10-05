//! Temporary, environment-gated diagnostics for texture content and draw
//! texture coordinates.
//!
//! This module exists to separate two failure hypotheses for a wrong-looking
//! two-stage fixed-function draw:
//!
//! * **(A)** the texture the bridge uploads is stale, uniform or from the wrong
//!   mip/pool path, or
//! * **(B)** the per-vertex texture coordinate the shader samples is constant or
//!   garbage.
//!
//! It is deliberately self-contained and off by default so it never affects a
//! normal run:
//!
//! * `RECOMP_D3D8_DUMP_TEXTURES=<dir>` writes every bound texture level the
//!   bridge is asked to upload as a PNG under `<dir>` (one file per distinct
//!   `texture_id`/`level`/`generation`), and prints a one-line summary per bind.
//!   This is the CPU texel content the bridge converts to RGBA and hands to the
//!   GPU, i.e. exactly what the draw samples.
//! * The existing `RECOMP_D3D8_TRACE_DRAWS` draw trace (`device::trace_draw`)
//!   also reports the min/max/mean of texture-coordinate sets 0 and 1 over the
//!   whole draw, whether or not the texture dump is enabled.
//!
//! The PNG writer has no dependency: it emits a zlib stream made of stored
//! (uncompressed) deflate blocks, so the output is valid PNG for any viewer.

use std::path::PathBuf;
use std::sync::Mutex;

/// Set to a directory to enable the texture dump. Empty or unset disables it.
const DUMP_ENV: &str = "RECOMP_D3D8_DUMP_TEXTURES";

/// Optional `RECOMP_D3D8_DUMP_TEXTURE_LEVEL=<n>` restricts the dump to one mip
/// level, so a selected level can be inspected without writing the whole chain.
const DUMP_LEVEL_ENV: &str = "RECOMP_D3D8_DUMP_TEXTURE_LEVEL";

/// Upper bound on files per process, so a long session cannot fill the disk.
const MAX_FILES: u32 = 512;

static WRITTEN: Mutex<Vec<(u32, u32, u64)>> = Mutex::new(Vec::new());
static COUNT: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

/// True when the texture dump is enabled, so the caller can skip the RGBA
/// conversion that is otherwise only done for a real upload.
pub fn texture_enabled() -> bool {
    std::env::var_os(DUMP_ENV).is_some_and(|v| !v.is_empty())
}

/// Write one bound level as a PNG and print a summary line.
///
/// `rgba` is tightly packed `R,G,B,A` at `width * height * 4`, already decoded
/// from the source D3D8 format. The same `(texture_id, level, generation)` is
/// written once; a re-bind of unchanged content does not create another file.
pub fn texture(
    stage: u32,
    texture_id: u32,
    level: u32,
    generation: u64,
    format: u32,
    width: u32,
    height: u32,
    rgba: &[u8],
) {
    if let Ok(only) = std::env::var(DUMP_LEVEL_ENV) {
        if only.parse::<u32>() != Ok(level) {
            return;
        }
    }
    let Some(dir) = dump_dir() else {
        return;
    };
    {
        let mut written = WRITTEN.lock().unwrap();
        if written.contains(&(texture_id, level, generation)) {
            return;
        }
        if written.len() as u32 >= MAX_FILES {
            return;
        }
        written.push((texture_id, level, generation));
    }
    let n = COUNT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let name = format!(
        "tex{n:04}_s{stage}_t{texture_id:08x}_l{level}_g{generation}_{width}x{height}_fmt{format:#x}.png"
    );
    let path = dir.join(name);
    match write_png(&path, width, height, rgba) {
        Ok(()) => eprintln!(
            "[d3d8-dump] stage={stage} texture=0x{texture_id:08x} level={level} gen={generation} {width}x{height} fmt={format:#x} -> {}",
            path.display()
        ),
        Err(e) => eprintln!("[d3d8-dump] failed to write {}: {e}", path.display()),
    }
}

fn dump_dir() -> Option<PathBuf> {
    let dir = std::env::var_os(DUMP_ENV)?;
    if dir.is_empty() {
        return None;
    }
    let path = PathBuf::from(dir);
    if let Err(e) = std::fs::create_dir_all(&path) {
        eprintln!("[d3d8-dump] cannot create {}: {e}", path.display());
        return None;
    }
    Some(path)
}

/// A minimal 8-bit RGBA PNG writer (color type 6), no compression.
pub fn write_png(
    path: &std::path::Path,
    width: u32,
    height: u32,
    rgba: &[u8],
) -> std::io::Result<()> {
    let expected = (width as usize) * (height as usize) * 4;
    if rgba.len() < expected {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!(
                "rgba has {} bytes but {width}x{height} needs {expected}",
                rgba.len()
            ),
        ));
    }
    // Raw scanlines: one filter byte (0) then the row's RGBA bytes.
    let mut raw = Vec::with_capacity((width as usize * 4 + 1) * height as usize);
    for y in 0..height as usize {
        raw.push(0);
        let row = &rgba[y * width as usize * 4..(y + 1) * width as usize * 4];
        raw.extend_from_slice(row);
    }
    let zlib = zlib_store(&raw);

    let mut out = Vec::with_capacity(zlib.len() + 128);
    out.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
    let mut ihdr = Vec::with_capacity(13);
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 6, 0, 0, 0]); // depth 8, RGBA, deflate, no filter, no interlace
    chunk(&mut out, b"IHDR", &ihdr);
    chunk(&mut out, b"IDAT", &zlib);
    chunk(&mut out, b"IEND", &[]);
    std::fs::write(path, out)
}

fn chunk(out: &mut Vec<u8>, kind: &[u8; 4], data: &[u8]) {
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(kind);
    out.extend_from_slice(data);
    let mut crc = Crc32::new();
    crc.update(kind);
    crc.update(data);
    out.extend_from_slice(&crc.finish().to_be_bytes());
}

/// zlib wrapper around stored deflate blocks.
fn zlib_store(raw: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(raw.len() + raw.len() / 65535 * 5 + 16);
    out.extend_from_slice(&[0x78, 0x01]); // CMF/FLG: deflate, 32K window, no dict
    if raw.is_empty() {
        out.extend_from_slice(&[0x01, 0x00, 0x00, 0xff, 0xff]); // final empty stored block
    } else {
        for (i, block) in raw.chunks(65535).enumerate() {
            let final_block = (i + 1) * 65535 >= raw.len();
            out.push(u8::from(final_block));
            let len = block.len() as u16;
            out.extend_from_slice(&len.to_le_bytes());
            out.extend_from_slice(&(!len).to_le_bytes());
            out.extend_from_slice(block);
        }
    }
    out.extend_from_slice(&adler32(raw).to_be_bytes());
    out
}

fn adler32(data: &[u8]) -> u32 {
    let mut a: u32 = 1;
    let mut b: u32 = 0;
    for &byte in data {
        a = (a + byte as u32) % 65521;
        b = (b + a) % 65521;
    }
    (b << 16) | a
}

struct Crc32 {
    value: u32,
}

impl Crc32 {
    fn new() -> Self {
        Self { value: 0xffff_ffff }
    }

    fn update(&mut self, data: &[u8]) {
        for &byte in data {
            self.value ^= byte as u32;
            for _ in 0..8 {
                let mask = (self.value & 1).wrapping_neg();
                self.value = (self.value >> 1) ^ (0xedb8_8320 & mask);
            }
        }
    }

    fn finish(self) -> u32 {
        !self.value
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn png_is_well_formed_and_self_consistent() {
        // 2x1 image: red, green.
        let rgba = [255, 0, 0, 255, 0, 255, 0, 255];
        let mut out = Vec::new();
        let zlib = zlib_store(&[0, 255, 0, 0, 255, 0, 255, 0, 255]);
        // Build the same container the writer builds, then check the pieces.
        out.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        let mut ihdr = Vec::new();
        ihdr.extend_from_slice(&2u32.to_be_bytes());
        ihdr.extend_from_slice(&1u32.to_be_bytes());
        ihdr.extend_from_slice(&[8, 6, 0, 0, 0]);
        chunk(&mut out, b"IHDR", &ihdr);
        chunk(&mut out, b"IDAT", &zlib);
        chunk(&mut out, b"IEND", &[]);
        assert_eq!(out[..8], [0x89, b'P', b'N', b'G', 0x0d, 0x0a, 0x1a, 0x0a]);
        assert_eq!(&out[12..16], b"IHDR");
        assert_eq!(u32::from_be_bytes(out[8..12].try_into().unwrap()), 13);
        assert_eq!(rgba.len(), 8);
        // Adler-32 of the empty string is 1; of "abc" is 0x024d0127.
        assert_eq!(adler32(b"abc"), 0x024d_0127);
        // CRC-32 of "IEND" is the well-known constant.
        let mut crc = Crc32::new();
        crc.update(b"IEND");
        assert_eq!(crc.finish(), 0xae42_6082);
    }

    #[test]
    fn empty_zlib_payload_is_a_valid_final_stored_block() {
        let z = zlib_store(&[]);
        assert_eq!(&z[..2], &[0x78, 0x01]);
        assert_eq!(&z[2..7], &[0x01, 0x00, 0x00, 0xff, 0xff]);
        assert_eq!(
            u32::from_be_bytes(z[7..11].try_into().unwrap()),
            adler32(&[])
        );
    }
}
