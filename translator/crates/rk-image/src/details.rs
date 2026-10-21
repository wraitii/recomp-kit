use std::path::Path;

use rk_core::{Addr, Error, Result};

const DIR_EXPORT: usize = 0;
const DIR_IMPORT: usize = 1;
const DIR_DEBUG: usize = 6;
const DIR_TLS: usize = 9;
const DIR_DELAY_IMPORT: usize = 13;

#[derive(Clone, Debug)]
pub struct Section {
    pub virtual_address: u32,
    pub virtual_size: u32,
    pub raw_size: u32,
    pub raw_pointer: u32,
    pub characteristics: u32,
}

#[derive(Clone, Debug)]
pub struct PeDetails {
    pub preferred_base: Addr,
    pub entry_point: Addr,
    pub size: u32,
    pub delta: u32,
    pub original: Vec<u8>,
    pub sections: Vec<Section>,
    pub data_ranges: Vec<(Addr, Addr, &'static str)>,
    pub exports: Vec<Addr>,
    pub tls_callbacks: Option<Addr>,
}

fn u16_at(data: &[u8], off: usize) -> Option<u16> {
    Some(u16::from_le_bytes(data.get(off..off + 2)?.try_into().ok()?))
}

fn u32_at(data: &[u8], off: usize) -> Option<u32> {
    Some(u32::from_le_bytes(data.get(off..off + 4)?.try_into().ok()?))
}

fn c_string(data: &[u8], off: usize) -> Option<String> {
    let tail = data.get(off..)?;
    let end = tail.iter().position(|&b| b == 0)?;
    Some(String::from_utf8_lossy(&tail[..end]).into_owned())
}

pub fn load(path: &Path, base: Addr) -> Result<PeDetails> {
    let file = std::fs::read(path).map_err(|e| Error::io(path, e))?;
    parse(&file, base)
        .ok_or_else(|| Error::input(format!("{}: malformed PE image", path.display())))?
        .map_err(|message| Error::input(format!("{}: {message}", path.display())))
}

fn parse(file: &[u8], base: Addr) -> Option<std::result::Result<PeDetails, String>> {
    let pe = u32_at(file, 0x3c)? as usize;
    if file.get(pe..pe + 4)? != b"PE\0\0" {
        return None;
    }
    let coff = pe + 4;
    let section_count = u16_at(file, coff + 2)? as usize;
    let optional_size = u16_at(file, coff + 16)? as usize;
    let opt = coff + 20;
    if u16_at(file, opt)? != 0x10b {
        return None;
    }
    let entry_rva = u32_at(file, opt + 16)?;
    let preferred_base = u32_at(file, opt + 28)?;
    let size = u32_at(file, opt + 56)?;
    let headers = u32_at(file, opt + 60)? as usize;
    let directories = u32_at(file, opt + 92)? as usize;
    let directory = |index: usize| -> Option<(u32, u32)> {
        if index >= directories {
            return Some((0, 0));
        }
        Some((u32_at(file, opt + 96 + index * 8)?, u32_at(file, opt + 100 + index * 8)?))
    };
    let table = opt + optional_size;
    let mut sections = Vec::with_capacity(section_count);
    for i in 0..section_count {
        let at = table + i * 40;
        sections.push(Section {
            virtual_size: u32_at(file, at + 8)?,
            virtual_address: u32_at(file, at + 12)?,
            raw_size: u32_at(file, at + 16)?,
            raw_pointer: u32_at(file, at + 20)?,
            characteristics: u32_at(file, at + 36)?,
        });
    }

    let mut original = file.get(..headers.min(file.len()))?.to_vec();
    for s in &sections {
        if s.virtual_size == 0 && s.raw_size == 0 {
            continue;
        }
        let start = s.raw_pointer as usize;
        let end = start + s.raw_size as usize;
        if end > file.len() {
            continue;
        }
        original.resize(s.virtual_address as usize, 0);
        original.extend_from_slice(&file[start..end]);
    }
    original.resize(size as usize, 0);

    let delta = base.wrapping_sub(preferred_base);
    let mut data_ranges = Vec::new();
    for index in 0..directories.min(16) {
        if index == 4 {
            continue;
        }
        let (rva, len) = directory(index)?;
        if rva != 0 && len != 0 {
            data_ranges.push((
                base + rva,
                base + rva + len,
                "PE directory",
            ));
        }
    }
    let (debug_rva, debug_size) = directory(DIR_DEBUG)?;
    let mut off = debug_rva as usize;
    let end = off + debug_size as usize;
    while debug_rva != 0 && off + 28 <= end {
        let at = u32_at(&original, off + 20)?;
        let len = u32_at(&original, off + 16)?;
        if at != 0 && len != 0 {
            data_ranges.push((base + at, base + at + len, "PE debug data"));
        }
        off += 28;
    }

    let exec_ranges: Vec<(Addr, Addr)> = sections
        .iter()
        .filter(|s| s.characteristics & 0x2000_0000 != 0)
        .map(|s| {
            let start = base + s.virtual_address;
            (start, start + s.virtual_size.max(s.raw_size))
        })
        .collect();
    let mut import_ranges = Vec::new();
    let mut add_import = |rva: u32, len: u32| {
        if rva == 0 || len == 0 {
            return;
        }
        let low = base + rva;
        let high = low + len;
        for &(start, end) in &exec_ranges {
            if low.max(start) < high.min(end) {
                import_ranges.push((low.max(start), high.min(end), "PE import data"));
            }
        }
    };
    let (import_rva, _) = directory(DIR_IMPORT)?;
    let mut descriptor = import_rva as usize;
    while import_rva != 0 && descriptor + 20 <= original.len() {
        let original_thunk = u32_at(&original, descriptor)?;
        let name = u32_at(&original, descriptor + 12)?;
        let first_thunk = u32_at(&original, descriptor + 16)?;
        if original_thunk == 0 && name == 0 && first_thunk == 0 {
            break;
        }
        if let Some(dll) = c_string(&original, name as usize) {
            add_import(name, dll.len() as u32 + 1);
        }
        let lookup = if original_thunk != 0 { original_thunk } else { first_thunk };
        let mut count = 0u32;
        loop {
            let Some(thunk) = u32_at(&original, lookup as usize + count as usize * 4) else {
                break;
            };
            if thunk == 0 {
                break;
            }
            if thunk & 0x8000_0000 == 0 {
                if let Some(symbol) = c_string(&original, thunk as usize + 2) {
                    add_import(thunk + 2, symbol.len() as u32 + 1);
                }
            }
            count += 1;
        }
        add_import(original_thunk, 4 * (count + 1));
        add_import(first_thunk, 4 * (count + 1));
        descriptor += 20;
    }
    let (delay_rva, _) = directory(DIR_DELAY_IMPORT)?;
    let mut descriptor = delay_rva as usize;
    while delay_rva != 0 && descriptor + 32 <= original.len() {
        let name = u32_at(&original, descriptor + 4)?;
        let handle = u32_at(&original, descriptor + 8)?;
        let iat = u32_at(&original, descriptor + 12)?;
        let int = u32_at(&original, descriptor + 16)?;
        let bound = u32_at(&original, descriptor + 20)?;
        let unload = u32_at(&original, descriptor + 24)?;
        if name == 0 && iat == 0 && int == 0 {
            break;
        }
        if let Some(dll) = c_string(&original, name as usize) {
            add_import(name, dll.len() as u32 + 1);
        }
        let lookup = if int != 0 { int } else { iat };
        let mut count = 0u32;
        loop {
            let Some(thunk) = u32_at(&original, lookup as usize + count as usize * 4) else {
                break;
            };
            if thunk == 0 {
                break;
            }
            if thunk & 0x8000_0000 == 0 {
                if let Some(symbol) = c_string(&original, thunk as usize + 2) {
                    add_import(thunk + 2, symbol.len() as u32 + 1);
                }
            }
            count += 1;
        }
        for array in [int, iat, bound, unload] {
            add_import(array, 4 * (count + 1));
        }
        add_import(handle, 4);
        descriptor += 32;
    }
    data_ranges.extend(import_ranges);

    let mut exports = Vec::new();
    let (export_rva, _) = directory(DIR_EXPORT)?;
    if export_rva != 0 {
        let base_at = export_rva as usize;
        let count = u32_at(&original, base_at + 20)?;
        let functions = u32_at(&original, base_at + 28)?;
        for i in 0..count {
            let rva = u32_at(&original, functions as usize + i as usize * 4)?;
            if rva != 0 {
                exports.push(base + rva);
            }
        }
    }

    let (tls_rva, _) = directory(DIR_TLS)?;
    let tls_callbacks = if tls_rva != 0 {
        u32_at(&original, tls_rva as usize + 12).map(|at| at.wrapping_add(delta))
    } else {
        None
    };

    Some(Ok(PeDetails {
        preferred_base,
        entry_point: base + entry_rva,
        size,
        delta,
        original,
        sections,
        data_ranges,
        exports,
        tls_callbacks,
    }))
}
