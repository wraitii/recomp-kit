use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[repr(u8)]
pub enum Field {
    Eax,
    Ecx,
    Edx,
    Ebx,
    Esp,
    Ebp,
    Esi,
    Edi,
    Cf,
    Pf,
    Af,
    Zf,
    Sf,
    Of,
}

impl Field {
    pub const ALL: [Field; 14] = [
        Field::Eax,
        Field::Ecx,
        Field::Edx,
        Field::Ebx,
        Field::Esp,
        Field::Ebp,
        Field::Esi,
        Field::Edi,
        Field::Cf,
        Field::Pf,
        Field::Af,
        Field::Zf,
        Field::Sf,
        Field::Of,
    ];

    pub fn bit(self) -> u16 {
        1 << self as u8
    }

    pub fn name(self) -> &'static str {
        ["EAX", "ECX", "EDX", "EBX", "ESP", "EBP", "ESI", "EDI", "CF", "PF", "AF", "ZF", "SF", "OF"]
            [self as usize]
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Fields(pub u16);

impl Fields {
    pub const ALL: Fields = Fields(0x3fff);
    pub const GPRS: Fields = Fields(0x00ff);
    pub const FLAGS: Fields = Fields(0x3f00);
    pub const NONE: Fields = Fields(0);

    pub fn contains(self, field: Field) -> bool {
        self.0 & field.bit() != 0
    }

    pub fn is_empty(self) -> bool {
        self.0 == 0
    }

    pub fn intersection(self, other: Fields) -> Fields {
        Fields(self.0 & other.0)
    }

    pub fn difference(self, other: Fields) -> Fields {
        Fields(self.0 & !other.0 & Fields::ALL.0)
    }

    pub fn insert(&mut self, field: Field) {
        self.0 |= field.bit();
    }

    pub fn iter(self) -> impl Iterator<Item = Field> {
        Field::ALL.into_iter().filter(move |f| self.contains(*f))
    }
}

impl FromIterator<Field> for Fields {
    fn from_iter<I: IntoIterator<Item = Field>>(iter: I) -> Self {
        iter.into_iter().fold(Fields::NONE, |fields, field| fields | Fields(field.bit()))
    }
}

impl std::ops::BitOr for Fields {
    type Output = Fields;
    fn bitor(self, rhs: Fields) -> Fields {
        Fields(self.0 | rhs.0)
    }
}

impl std::ops::BitAnd for Fields {
    type Output = Fields;
    fn bitand(self, rhs: Fields) -> Fields {
        Fields(self.0 & rhs.0)
    }
}

impl std::ops::Not for Fields {
    type Output = Fields;
    fn not(self) -> Fields {
        Fields(!self.0 & Fields::ALL.0)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Contract {
    pub reads: Fields,
    pub kills: Fields,
    pub writes: Fields,
    pub preserves: Fields,
}

impl Contract {
    pub const CONSERVATIVE: Contract = Contract {
        reads: Fields::ALL,
        kills: Fields::NONE,
        writes: Fields::ALL,
        preserves: Fields::NONE,
    };
}
