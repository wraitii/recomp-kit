//! Reusable D3D8 model.
//!
//! These modules describe Direct3D 8 state, resources, fixed-function
//! translation and device behaviour without depending on windowing or
//! any game/recomp types. GPU objects live in [`crate::backend`]; format mapping and WGSL generation
//! may use wgpu types.
//!
//! The implemented slice is full color clear and unlit XYZ+DIFFUSE triangle
//! lists. Other reached states are rejected; this is not a complete D3D8 model.

pub mod device;
pub mod dump;
pub mod enums;
pub mod fixed_function;
pub mod format;
pub mod math;
pub mod resource;
pub mod state;
pub mod stats;
pub mod survey;
pub mod texture_cache;
