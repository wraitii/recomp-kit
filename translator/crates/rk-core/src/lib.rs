pub mod contract;
pub mod diag;
pub mod ir;
pub mod lookup;
pub mod program;
pub mod settings;
pub mod timing;

pub use diag::{Diagnostic, Error, Reason, Result};
pub use lookup::{Lookup, LookupSet};

pub type Addr = u32;

pub struct Hex(pub Addr);

impl std::fmt::Display for Hex {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:08x}", self.0)
    }
}

pub fn parse_hex(text: &str) -> Option<Addr> {
    let digits = text.strip_prefix("0x").unwrap_or(text);
    if digits.is_empty() || digits.len() > 8 {
        return None;
    }
    Addr::from_str_radix(digits, 16).ok()
}
