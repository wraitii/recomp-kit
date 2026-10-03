//! Isolated D3D8-semantics renderer prototype on [`wgpu`].
//!
//! The crate is layered so the D3D8 model stays independent of windowing and of
//! any game integration:
//!
//! - [`d3d8`] — reusable D3D8 state, resources, fixed-function emulation
//!   and device emulation. No Ghost Recon addresses, no guest CPU
//!   types, no recomp-kit types.
//! - [`backend`] — wgpu instance/adapter/device/surface and readback plumbing.
//! - [`abi`] — a small C ABI exposing opaque host handles. Guest COM dispatch,
//!   32-bit pointer/layout conversion and lock-copy bridging are out of scope.
//! - [`probe`] — the shared probe harness and its assertions.
//!
//! The isolated Metal probes have run;
//! this does not establish game rendering or original-D3D8 equivalence.

#![forbid(unsafe_op_in_unsafe_fn)]

pub mod abi;
pub mod backend;
pub mod d3d8;
pub mod probe;

/// Named failure at the renderer boundary; unsupported behavior is never success.
#[derive(Clone, Debug)]
pub struct RenderError {
    pub kind: RenderErrorKind,
    pub operation: &'static str,
    pub cause: String,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RenderErrorKind {
    Unsupported,
    InvalidArgument,
    OutOfMemory,
}

impl RenderError {
    pub fn new(operation: &'static str, cause: impl Into<String>) -> Self {
        Self {
            kind: RenderErrorKind::Unsupported,
            operation,
            cause: cause.into(),
        }
    }
    pub fn invalid(operation: &'static str, cause: impl Into<String>) -> Self {
        Self {
            kind: RenderErrorKind::InvalidArgument,
            operation,
            cause: cause.into(),
        }
    }
    pub fn out_of_memory(operation: &'static str, cause: impl Into<String>) -> Self {
        Self {
            kind: RenderErrorKind::OutOfMemory,
            operation,
            cause: cause.into(),
        }
    }
}

impl core::fmt::Display for RenderError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(f, "{}: {}", self.operation, self.cause)
    }
}

impl std::error::Error for RenderError {}
