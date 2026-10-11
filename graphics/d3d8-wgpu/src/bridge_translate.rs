use crate::bridge::{
    D3d7RenderStateTranslation, D3d7TextureStageTranslation, D3d7TransformTranslation,
    D3d7TranslationKind,
};

pub(crate) fn translate_render_state(state: u32, value: u32) -> D3d7RenderStateTranslation {
    let kind = match state {
        4 => D3d7TranslationKind::Ignore,
        41 if value == 0 => D3d7TranslationKind::Ignore,
        41 => D3d7TranslationKind::Invalid,
        7 | 8 | 9 | 10 | 14 | 15 | 16 | 19 | 20 | 22 | 23 | 24 | 25 | 26 | 27 | 28 | 29 | 30
        | 34 | 35 | 36 | 37 | 38 | 40 | 47 | 48 | 52 | 53 | 54 | 55 | 56 | 57 | 58 | 59 | 60
        | 128 | 129 | 130 | 131 | 132 | 133 | 134 | 135 | 136 | 137 | 139 | 140 | 141 | 142
        | 143 | 145 | 146 | 147 | 148 | 151 | 152 | 153 | 154 | 155 | 156 | 157 | 158 | 159
        | 160 | 161 | 162 | 163 | 164 | 165 | 166 | 167 | 168 | 170 | 171 | 172 | 173 => {
            D3d7TranslationKind::Forward
        }
        _ => D3d7TranslationKind::Invalid,
    };
    D3d7RenderStateTranslation { kind, state }
}

pub(crate) fn translate_transform(state: u32) -> D3d7TransformTranslation {
    let translated = match state {
        1 | 256 => Some(256),
        2 | 3 | 16..=23 => Some(state),
        _ => None,
    };
    D3d7TransformTranslation {
        valid: translated.is_some(),
        state: translated.unwrap_or(0),
    }
}

pub(crate) fn translate_texture_stage_state(state: u32) -> D3d7TextureStageTranslation {
    match state {
        0 => D3d7TextureStageTranslation {
            count: 0,
            first: 0,
            second: 0,
        },
        12 => D3d7TextureStageTranslation {
            count: 2,
            first: 13,
            second: 14,
        },
        1..=11 | 13..=28 => D3d7TextureStageTranslation {
            count: 1,
            first: state,
            second: 0,
        },
        _ => D3d7TextureStageTranslation {
            count: -1,
            first: 0,
            second: 0,
        },
    }
}
