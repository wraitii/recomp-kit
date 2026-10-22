//! x87 translation. `Recipes` re-decodes each x87 instruction into typed recipes for `rk-ir`.
//! `prepare` turns the SSA's x87 effects into a per-clone event plan: it tracks the stack as
//! scalars (`scalar`), carries unpublished state across blocks (`carry`), and uses known modes to
//! choose between fast and dynamic control-word clones. `printer` renders events as the C fragments
//! that `rk-emit` inserts. Unrecognized forms fail with `UnsupportedX87`.

mod carry;
mod plan;
mod printer;
mod recipes;
mod scalar;
mod types;

pub use plan::{prepare, CloneProgram, ModeCondition, ModeForce, ModeInput, Options, Plan};
pub use printer::{External, Printer};
pub use recipes::Recipes;
pub use types::{Event, Expr};
