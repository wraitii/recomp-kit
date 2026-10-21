use std::collections::BTreeMap;
use std::path::Path;

use rk_core::program::{Image, Import, Relocation};
use rk_core::{Addr, Error, Result};

const SCN_MEM_EXECUTE: u32 = 0x2000_0000;
const DIR_IMPORT: usize = 1;
const DIR_BASERELOC: usize = 5;

struct Section {
    virtual_address: u32,
    virtual_size: u32,
    raw_size: u32,
    raw_pointer: u32,
    characteristics: u32,
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

pub fn load(path: &Path, base: Option<Addr>) -> Result<Image> {
    let file = std::fs::read(path).map_err(|e| Error::io(path, e))?;
    parse(&file, base).ok_or_else(|| Error::input(format!("{}: malformed PE image", path.display())))?
        .map_err(|message| Error::input(format!("{}: {message}", path.display())))
}

fn parse(file: &[u8], base: Option<Addr>) -> Option<std::result::Result<Image, String>> {
    let pe = u32_at(file, 0x3c)? as usize;
    if file.get(pe..pe + 4)? != b"PE\0\0" {
        return None;
    }
    let coff = pe + 4;
    let sections_count = u16_at(file, coff + 2)? as usize;
    let optional_size = u16_at(file, coff + 16)? as usize;
    let opt = coff + 20;
    if u16_at(file, opt)? != 0x10b {
        return None;
    }
    let preferred = u32_at(file, opt + 28)?;
    let size = u32_at(file, opt + 56)?;
    let headers = u32_at(file, opt + 60)? as usize;
    let directories = u32_at(file, opt + 92)? as usize;
    let directory = |index: usize| -> Option<(u32, u32)> {
        if index >= directories {
            return Some((0, 0));
        }
        Some((u32_at(file, opt + 96 + index * 8)?, u32_at(file, opt + 100 + index * 8)?))
    };
    let mut sections = Vec::with_capacity(sections_count);
    let table = opt + optional_size;
    for i in 0..sections_count {
        let at = table + i * 40;
        sections.push(Section {
            virtual_size: u32_at(file, at + 8)?,
            virtual_address: u32_at(file, at + 12)?,
            raw_size: u32_at(file, at + 16)?,
            raw_pointer: u32_at(file, at + 20)?,
            characteristics: u32_at(file, at + 36)?,
        });
    }

    let mut data = file.get(..headers.min(file.len()))?.to_vec();
    for s in &sections {
        if s.virtual_size == 0 && s.raw_size == 0 {
            continue;
        }
        let start = s.raw_pointer as usize;
        let end = start + s.raw_size as usize;
        if end > file.len() {
            continue;
        }
        data.resize(s.virtual_address as usize, 0);
        data.extend_from_slice(&file[start..end]);
    }
    data.resize(size as usize, 0);

    let base = base.unwrap_or(preferred);
    let delta = base.wrapping_sub(preferred);
    let (reloc_rva, reloc_size) = directory(DIR_BASERELOC)?;
    let mut relocations = Vec::new();
    let mut off = reloc_rva as usize;
    let reloc_end = off + reloc_size as usize;
    while reloc_size != 0 && off + 8 <= reloc_end {
        let page = u32_at(&data, off)? as usize;
        let block = u32_at(&data, off + 4)? as usize;
        if block < 8 || off + block > reloc_end {
            if delta != 0 {
                return Some(Err(format!("malformed relocation block at {off:x}")));
            }
            break;
        }
        for slot in (off + 8..off + block).step_by(2) {
            let entry = u16_at(&data, slot)?;
            if entry >> 12 != 3 {
                continue;
            }
            let site = page + (entry & 0xfff) as usize;
            if site + 4 > size as usize {
                if delta != 0 {
                    return Some(Err(format!("relocation outside image at {site:x}")));
                }
                continue;
            }
            let value = u32_at(&data, site)?.wrapping_add(delta);
            data[site..site + 4].copy_from_slice(&value.to_le_bytes());
            relocations.push(Relocation {
                target: value,
                site: base + site as u32,
            });
        }
        off += block;
    }
    if delta != 0 && reloc_size == 0 {
        return Some(Err("rebased image has no relocation directory".into()));
    }

    let mut imports = BTreeMap::new();
    let (import_rva, _) = directory(DIR_IMPORT)?;
    let mut descriptor = import_rva as usize;
    while import_rva != 0 && descriptor + 20 <= data.len() {
        let original = u32_at(&data, descriptor)?;
        let name = u32_at(&data, descriptor + 12)?;
        let first = u32_at(&data, descriptor + 16)?;
        if original == 0 && name == 0 && first == 0 {
            break;
        }
        let dll = c_string(&data, name as usize)?.to_lowercase();
        let lookup = if original != 0 { original } else { first };
        for i in 0.. {
            let thunk = u32_at(&data, lookup as usize + i * 4)?;
            if thunk == 0 {
                break;
            }
            let symbol = if thunk & 0x8000_0000 != 0 {
                format!("ord{}", thunk & 0xffff)
            } else {
                c_string(&data, thunk as usize + 2)?
            };
            imports.insert(
                base + first + (i as u32) * 4,
                Import { dll: dll.clone(), name: symbol },
            );
        }
        descriptor += 20;
    }

    let exec_ranges = sections
        .iter()
        .filter(|s| s.characteristics & SCN_MEM_EXECUTE != 0)
        .map(|s| {
            let start = base + s.virtual_address;
            (start, start + s.virtual_size.max(s.raw_size))
        })
        .collect();
    Some(Ok(Image {
        base,
        size,
        data,
        exec_ranges,
        imports,
        relocations,
    }))
}
