use std::sync::OnceLock;

use rk_core::contract::{Field, Fields};

pub fn helper(raw: &[u8]) -> Option<&'static str> {
    static TABLE: OnceLock<Vec<(Vec<u8>, &'static str)>> = OnceLock::new();
    let table = TABLE.get_or_init(|| {
        let mut out = Vec::new();
        for (opcode, base) in [
            (0xa4u8, "movs"),
            (0xa5, "movs"),
            (0xaa, "stos"),
            (0xab, "stos"),
            (0xa6, "cmps"),
            (0xa7, "cmps"),
            (0xac, "lods"),
            (0xad, "lods"),
            (0xae, "scas"),
            (0xaf, "scas"),
        ] {
            for (prefix, wide) in [(&b""[..], "d"), (&b"\x66"[..], "w")] {
                let suffix = if opcode & 1 == 0 { "b" } else { wide };
                let mut single = prefix.to_vec();
                single.push(opcode);
                out.push((single.clone(), leak(&format!("{base}{suffix}"))));
                let repeats = if matches!(base, "movs" | "stos" | "lods") {
                    [(0xf3u8, "rep"), (0xf2, "rep")]
                } else {
                    [(0xf3u8, "repe"), (0xf2, "repne")]
                };
                for (byte, name) in repeats {
                    let name = leak(&format!("{name}_{base}{suffix}"));
                    let mut rep = prefix.to_vec();
                    rep.extend_from_slice(&[byte, opcode]);
                    out.push((rep, name));
                    let mut rep = vec![byte];
                    rep.extend_from_slice(&single);
                    out.push((rep, name));
                }
            }
        }
        out
    });
    table.iter().find(|(bytes, _)| bytes.as_slice() == raw).map(|(_, name)| *name)
}

fn leak(text: &str) -> &'static str {
    Box::leak(text.to_string().into_boxed_str())
}

fn fields(names: &[&str]) -> Fields {
    let mut out = Fields::NONE;
    for name in names {
        let field = match *name {
            "EAX" => Field::Eax,
            "ECX" => Field::Ecx,
            "EDX" => Field::Edx,
            "EBX" => Field::Ebx,
            "ESP" => Field::Esp,
            "EBP" => Field::Ebp,
            "ESI" => Field::Esi,
            "EDI" => Field::Edi,
            "CF" => Field::Cf,
            "PF" => Field::Pf,
            "AF" => Field::Af,
            "ZF" => Field::Zf,
            "SF" => Field::Sf,
            "OF" => Field::Of,
            _ => continue,
        };
        out |= Fields(field.bit());
    }
    out
}

pub fn effect(name: &str) -> (Fields, Fields, bool) {
    let base = name.rsplit('_').next().unwrap_or(name);
    let base = base.get(..4).unwrap_or(base);
    let pointers: &[&str] = match base {
        "movs" | "cmps" => &["ESI", "EDI"],
        "stos" | "scas" => &["EDI"],
        "lods" => &["ESI"],
        _ => &[],
    };
    if pointers.is_empty() {
        return (Fields::NONE, Fields::NONE, false);
    }
    let rep = name.contains('_');
    let mut reads: Vec<&str> = pointers.to_vec();
    if rep {
        reads.push("ECX");
    }
    if base == "stos" || base == "scas" {
        reads.push("EAX");
    }
    let mut writes: Vec<&str> = pointers.to_vec();
    if rep {
        writes.push("ECX");
    }
    if base == "lods" {
        writes.push("EAX");
    }
    let flags = base == "cmps" || base == "scas";
    (fields(&reads), fields(&writes), flags)
}
