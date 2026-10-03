//! D3D row-major matrix and vector helpers.
//!
//! D3D8 uses row vectors and row-major matrices (`v' = v * M`), with translation
//! in the last row of `D3DMATRIX`. These semantics must be preserved through
//! fixed-function emulation. The conversion to WGSL is owned by
//! [`crate::d3d8::fixed_function`], which interprets each CPU row as one WGSL
//! matrix column (i.e. uploads the transpose) so the shader's
//! `matrix * column_vector` matches the row-vector product here.

/// A 4x4 matrix stored row-major, matching `D3DMATRIX`/`D3DXMATRIX` and the
/// D3D8 row-vector convention `v' = v * M`.
///
/// `rows[r][c]` is row `r`, column `c`. Translation lives in `rows[3][0..3]`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Mat4 {
    pub rows: [[f32; 4]; 4],
}

impl Mat4 {
    /// The multiplicative identity, matching `D3DMatrixIdentity` semantics.
    pub const IDENTITY: Mat4 = Mat4 {
        rows: [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
    };

    /// Matrix product `self * rhs`.
    ///
    /// D3D8 composes transforms as `world * view * projection`, so a vector is
    /// transformed by `v * world * view * projection`.
    pub fn mul(self, rhs: Mat4) -> Mat4 {
        let mut rows = [[0.0f32; 4]; 4];
        for (r, out_row) in rows.iter_mut().enumerate() {
            for (c, out) in out_row.iter_mut().enumerate() {
                let mut sum = 0.0f32;
                for (k, lhs) in self.rows[r].iter().enumerate() {
                    sum += lhs * rhs.rows[k][c];
                }
                *out = sum;
            }
        }
        Mat4 { rows }
    }

    /// Row-vector transform `v * self`.
    ///
    /// `self` is row-major; `v[3]` is the homogeneous `w` component.
    pub fn transform(self, v: [f32; 4]) -> [f32; 4] {
        let mut out = [0.0f32; 4];
        for (c, out) in out.iter_mut().enumerate() {
            let mut sum = 0.0f32;
            for (r, component) in v.iter().enumerate() {
                sum += component * self.rows[r][c];
            }
            *out = sum;
        }
        out
    }

    /// Convenience constructor: translate by `(x, y, z)` (row-vector form,
    /// translation in the last row).
    pub fn translation(x: f32, y: f32, z: f32) -> Mat4 {
        let mut m = Mat4::IDENTITY;
        m.rows[3][0] = x;
        m.rows[3][1] = y;
        m.rows[3][2] = z;
        m
    }

    /// Convenience constructor: non-uniform scale.
    pub fn scale(x: f32, y: f32, z: f32) -> Mat4 {
        let mut m = Mat4::IDENTITY;
        m.rows[0][0] = x;
        m.rows[1][1] = y;
        m.rows[2][2] = z;
        m
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_is_neutral_for_transform_and_mul() {
        let v = [2.0, -3.0, 0.5, 1.0];
        assert_eq!(Mat4::IDENTITY.transform(v), v);
        let m = Mat4::translation(1.0, 2.0, 3.0);
        assert_eq!(m.mul(Mat4::IDENTITY), m);
        assert_eq!(Mat4::IDENTITY.mul(m), m);
    }

    #[test]
    fn translation_uses_last_row_row_vector_convention() {
        let m = Mat4::translation(10.0, 20.0, 30.0);
        assert_eq!(m.transform([1.0, 2.0, 3.0, 1.0]), [11.0, 22.0, 33.0, 1.0]);
        // w == 0 is a direction and must not be translated.
        assert_eq!(m.transform([1.0, 2.0, 3.0, 0.0]), [1.0, 2.0, 3.0, 0.0]);
    }

    #[test]
    fn mul_order_is_self_then_rhs() {
        // v * (T * S): translate (+1) then scale (x2) => x = (1+1)*2 = 4.
        let t = Mat4::translation(1.0, 0.0, 0.0);
        let s = Mat4::scale(2.0, 2.0, 2.0);
        let v = [1.0, 0.0, 0.0, 1.0];
        assert_eq!(t.mul(s).transform(v), [4.0, 0.0, 0.0, 1.0]);
        // The opposite order applies scale first: x = 1*2 + 1 = 3.
        assert_eq!(s.mul(t).transform(v), [3.0, 0.0, 0.0, 1.0]);
    }

    #[test]
    fn mul_matches_sequential_transforms() {
        let world = Mat4::translation(1.0, 2.0, 3.0);
        let view = Mat4::scale(2.0, 2.0, 2.0);
        let projection = Mat4::translation(0.5, 0.0, 0.0);
        let v = [1.0, 1.0, 1.0, 1.0];
        let stepwise = projection.transform(view.transform(world.transform(v)));
        let combined = world.mul(view).mul(projection).transform(v);
        assert_eq!(stepwise, combined);
    }

    #[test]
    fn nonidentity_row_column_placement() {
        // A matrix that puts distinct values in row 0 and column 0 catches a
        // transpose error in transform().
        let mut m = Mat4::IDENTITY;
        m.rows[0][1] = 1.0;
        m.rows[1][0] = 2.0;
        m.rows[1][1] = 0.0;
        // v = [x, y, 0, 0] => out[0] = x*1 + y*2 ; out[1] = x*1 + y*0
        assert_eq!(m.transform([3.0, 5.0, 0.0, 0.0]), [13.0, 3.0, 0.0, 0.0]);
    }
}
