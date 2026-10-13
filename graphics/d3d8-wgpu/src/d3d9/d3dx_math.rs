use crate::bridge::{D3dxMatrixInverseResult, D3dxMatrixResult, StatusCode, status};

const WORDS: usize = 16;

fn invalid(message: &str) -> D3dxMatrixResult {
    D3dxMatrixResult {
        status: status(StatusCode::InvalidArgument, message),
        values: Vec::new(),
    }
}

pub fn d3dx_matrix_multiply(a: &[f32], b: &[f32]) -> D3dxMatrixResult {
    if a.len() != WORDS || b.len() != WORDS {
        return invalid("D3DXMatrixMultiply expects two 4x4 matrices");
    }
    let mut out = vec![0.0f32; WORDS];
    for row in 0..4 {
        for column in 0..4 {
            let mut sum = 0.0f32;
            for k in 0..4 {
                sum += a[row * 4 + k] * b[k * 4 + column];
            }
            out[row * 4 + column] = sum;
        }
    }
    D3dxMatrixResult {
        status: status(StatusCode::Ok, ""),
        values: out,
    }
}

pub fn d3dx_matrix_transpose(a: &[f32]) -> D3dxMatrixResult {
    if a.len() != WORDS {
        return invalid("D3DXMatrixTranspose expects a 4x4 matrix");
    }
    let mut out = vec![0.0f32; WORDS];
    for row in 0..4 {
        for column in 0..4 {
            out[row * 4 + column] = a[column * 4 + row];
        }
    }
    D3dxMatrixResult {
        status: status(StatusCode::Ok, ""),
        values: out,
    }
}

fn cofactor(m: &[f32]) -> [f32; 16] {
    let mut inv = [0.0f32; 16];
    inv[0] = m[5] * m[10] * m[15] - m[5] * m[11] * m[14] - m[9] * m[6] * m[15]
        + m[9] * m[7] * m[14]
        + m[13] * m[6] * m[11]
        - m[13] * m[7] * m[10];
    inv[4] = -m[4] * m[10] * m[15] + m[4] * m[11] * m[14] + m[8] * m[6] * m[15]
        - m[8] * m[7] * m[14]
        - m[12] * m[6] * m[11]
        + m[12] * m[7] * m[10];
    inv[8] = m[4] * m[9] * m[15] - m[4] * m[11] * m[13] - m[8] * m[5] * m[15]
        + m[8] * m[7] * m[13]
        + m[12] * m[5] * m[11]
        - m[12] * m[7] * m[9];
    inv[12] = -m[4] * m[9] * m[14] + m[4] * m[10] * m[13] + m[8] * m[5] * m[14]
        - m[8] * m[6] * m[13]
        - m[12] * m[5] * m[10]
        + m[12] * m[6] * m[9];
    inv[1] = -m[1] * m[10] * m[15] + m[1] * m[11] * m[14] + m[9] * m[2] * m[15]
        - m[9] * m[3] * m[14]
        - m[13] * m[2] * m[11]
        + m[13] * m[3] * m[10];
    inv[5] = m[0] * m[10] * m[15] - m[0] * m[11] * m[14] - m[8] * m[2] * m[15]
        + m[8] * m[3] * m[14]
        + m[12] * m[2] * m[11]
        - m[12] * m[3] * m[10];
    inv[9] = -m[0] * m[9] * m[15] + m[0] * m[11] * m[13] + m[8] * m[1] * m[15]
        - m[8] * m[3] * m[13]
        - m[12] * m[1] * m[11]
        + m[12] * m[3] * m[9];
    inv[13] = m[0] * m[9] * m[14] - m[0] * m[10] * m[13] - m[8] * m[1] * m[14]
        + m[8] * m[2] * m[13]
        + m[12] * m[1] * m[10]
        - m[12] * m[2] * m[9];
    inv[2] = m[1] * m[6] * m[15] - m[1] * m[7] * m[14] - m[5] * m[2] * m[15]
        + m[5] * m[3] * m[14]
        + m[13] * m[2] * m[7]
        - m[13] * m[3] * m[6];
    inv[6] = -m[0] * m[6] * m[15] + m[0] * m[7] * m[14] + m[4] * m[2] * m[15]
        - m[4] * m[3] * m[14]
        - m[12] * m[2] * m[7]
        + m[12] * m[3] * m[6];
    inv[10] = m[0] * m[5] * m[15] - m[0] * m[7] * m[13] - m[4] * m[1] * m[15]
        + m[4] * m[3] * m[13]
        + m[12] * m[1] * m[7]
        - m[12] * m[3] * m[5];
    inv[14] = -m[0] * m[5] * m[14] + m[0] * m[6] * m[13] + m[4] * m[1] * m[14]
        - m[4] * m[2] * m[13]
        - m[12] * m[1] * m[6]
        + m[12] * m[2] * m[5];
    inv[3] = -m[1] * m[6] * m[11] + m[1] * m[7] * m[10] + m[5] * m[2] * m[11]
        - m[5] * m[3] * m[10]
        - m[9] * m[2] * m[7]
        + m[9] * m[3] * m[6];
    inv[7] = m[0] * m[6] * m[11] - m[0] * m[7] * m[10] - m[4] * m[2] * m[11]
        + m[4] * m[3] * m[10]
        + m[8] * m[2] * m[7]
        - m[8] * m[3] * m[6];
    inv[11] = -m[0] * m[5] * m[11] + m[0] * m[7] * m[9] + m[4] * m[1] * m[11]
        - m[4] * m[3] * m[9]
        - m[8] * m[1] * m[7]
        + m[8] * m[3] * m[5];
    inv[15] = m[0] * m[5] * m[10] - m[0] * m[6] * m[9] - m[4] * m[1] * m[10]
        + m[4] * m[2] * m[9]
        + m[8] * m[1] * m[6]
        - m[8] * m[2] * m[5];
    inv
}

pub fn d3dx_matrix_inverse(a: &[f32]) -> D3dxMatrixInverseResult {
    if a.len() != WORDS {
        return D3dxMatrixInverseResult {
            status: status(
                StatusCode::InvalidArgument,
                "D3DXMatrixInverse expects a 4x4 matrix",
            ),
            valid: false,
            determinant: 0.0,
            values: Vec::new(),
        };
    }
    let mut inv = cofactor(a);
    let determinant = a[0] * inv[0] + a[1] * inv[4] + a[2] * inv[8] + a[3] * inv[12];
    if determinant == 0.0 {
        return D3dxMatrixInverseResult {
            status: status(StatusCode::Ok, ""),
            valid: false,
            determinant,
            values: Vec::new(),
        };
    }
    let scale = 1.0f32 / determinant;
    for value in &mut inv {
        *value *= scale;
    }
    D3dxMatrixInverseResult {
        status: status(StatusCode::Ok, ""),
        valid: true,
        determinant,
        values: inv.to_vec(),
    }
}
