//! Opt-in draw-rejection survey (`RECOMP_D3D8_SURVEY=1`).
//!
//! The bounded draw paths reject a state/FVF/texture-stage combination they
//! cannot honour with a named [`RenderError`]. By default that is a hard
//! failure, which is correct for the probe but makes the fixed-function
//! coverage of a real guest expensive to discover: each new unsupported state
//! aborts the run and needs a person to restart it.
//!
//! Survey mode records the first N distinct rejections, skips the offending
//! draw, and lets the guest continue, so a single run enumerates every
//! unsupported draw. It is off unless `RECOMP_D3D8_SURVEY=1`; unset means the
//! draw paths behave exactly as before. Only state/FVF/TSS rejections are
//! surveyable: malformed arguments (bad handles, out-of-range vertex/index
//! reads) stay hard errors.
//!
//! The registry is process-global because there is one render device per
//! guest. A summary is printed every survey interval from [`record`]'s caller
//! and once at process exit through `atexit`, so a run that later hits a hard
//! error still leaves the collected table behind.

use crate::RenderError;
use std::collections::BTreeMap;
use std::sync::{Mutex, Once, OnceLock};

/// `RECOMP_D3D8_SURVEY` is enabled by exactly `1`.
pub fn parse_enabled(raw: Option<&str>) -> bool {
    matches!(raw, Some("1"))
}

/// One de-duplicated rejection key with its count and first sighting.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rejection {
    pub operation: &'static str,
    pub cause: String,
    /// Compact dump of the draw state that produced the rejection.
    pub state: String,
    pub count: u64,
    pub first_frame: u64,
    pub first_draw: u64,
}

/// Key for the de-duplicated map: the error text plus the state dump.
fn key(error: &RenderError, state: &str) -> String {
    format!("{}|{}|{state}", error.operation, error.cause)
}

/// A de-duplicated rejection table. Kept free of global state so it can be
/// unit-tested directly.
#[derive(Default, Debug)]
pub struct Survey {
    entries: BTreeMap<String, Rejection>,
}

impl Survey {
    /// Record one rejection, incrementing the count when the same
    /// error+state pair was seen before.
    pub fn record(&mut self, error: &RenderError, state: &str, frame: u64, draw: u64) {
        let key = key(error, state);
        self.entries
            .entry(key)
            .and_modify(|entry| entry.count += 1)
            .or_insert_with(|| Rejection {
                operation: error.operation,
                cause: error.cause.clone(),
                state: state.to_string(),
                count: 1,
                first_frame: frame,
                first_draw: draw,
            });
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn distinct(&self) -> usize {
        self.entries.len()
    }

    pub fn total(&self) -> u64 {
        self.entries.values().map(|entry| entry.count).sum()
    }

    /// Compact table sorted by descending count, then by operation/cause/state
    /// for a stable order. Empty when nothing was recorded.
    pub fn summary(&self) -> String {
        if self.entries.is_empty() {
            return String::new();
        }
        let mut entries: Vec<&Rejection> = self.entries.values().collect();
        entries.sort_by(|a, b| {
            b.count
                .cmp(&a.count)
                .then_with(|| a.operation.cmp(b.operation))
                .then_with(|| a.cause.cmp(&b.cause))
                .then_with(|| a.state.cmp(&b.state))
        });
        let mut out = format!(
            "[d3d8-survey] {} unsupported draw rejection(s), {} distinct\n",
            self.total(),
            self.distinct()
        );
        out.push_str("  count  first(frame/draw)  operation | cause | state\n");
        for entry in entries {
            out.push_str(&format!(
                "  {:>5}  f={} d={}  {}: {} | {}\n",
                entry.count,
                entry.first_frame,
                entry.first_draw,
                entry.operation,
                entry.cause,
                entry.state
            ));
        }
        out
    }
}

// ---------------------------------------------------------------------------
// Process-global registry
// ---------------------------------------------------------------------------

static ENABLED: OnceLock<bool> = OnceLock::new();
static SURVEY: OnceLock<Mutex<Survey>> = OnceLock::new();
static ATEXIT: Once = Once::new();

/// True when `RECOMP_D3D8_SURVEY=1`; the value is read once.
pub fn enabled() -> bool {
    *ENABLED.get_or_init(|| parse_enabled(std::env::var("RECOMP_D3D8_SURVEY").ok().as_deref()))
}

fn registry() -> &'static Mutex<Survey> {
    SURVEY.get_or_init(|| Mutex::new(Survey::default()))
}

// SAFETY: `atexit` is the C runtime hook; the callback has C linkage and does
// not unwind. Used so a normal guest exit still prints the collected table.
unsafe extern "C" {
    fn atexit(callback: extern "C" fn()) -> i32;
}

extern "C" fn report_at_exit() {
    report_to_stderr();
}

/// Register the exit report once, the first time a surveyable device is made.
pub fn register_exit_report() {
    ATEXIT.call_once(|| {
        // SAFETY: `report_at_exit` matches the C callback signature.
        unsafe {
            atexit(report_at_exit);
        }
    });
}

/// Record a rejection into the global registry. No-op when disabled.
pub fn record(error: &RenderError, state: &str, frame: u64, draw: u64) {
    if !enabled() {
        return;
    }
    register_exit_report();
    registry().lock().unwrap().record(error, state, frame, draw);
}

/// Print the accumulated table to stderr. Returns true when it was non-empty.
pub fn report_to_stderr() -> bool {
    let survey = registry().lock().unwrap();
    let summary = survey.summary();
    if summary.is_empty() {
        return false;
    }
    eprint!("{summary}");
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rejection(cause: &str) -> RenderError {
        RenderError::new("d3d8::state::validate_unlit", cause)
    }

    #[test]
    fn only_one_is_enabled() {
        assert!(parse_enabled(Some("1")));
        assert!(!parse_enabled(Some("0")));
        assert!(!parse_enabled(Some("true")));
        assert!(!parse_enabled(None));
    }

    #[test]
    fn record_dedups_and_keeps_first_sighting() {
        let mut survey = Survey::default();
        let error = rejection("D3DRS_FOGENABLE must be FALSE");
        survey.record(&error, "fog=1", 10, 3);
        survey.record(&error, "fog=1", 20, 9);
        // A different state dump is a different key.
        survey.record(&error, "fog=2", 30, 12);
        assert_eq!(survey.distinct(), 2);
        assert_eq!(survey.total(), 3);
        let summary = survey.summary();
        assert!(summary.contains("fog=1"), "{summary}");
        assert!(summary.contains("fog=2"), "{summary}");
        // The repeated key keeps the first frame/draw and count 2.
        let first = survey
            .entries
            .values()
            .find(|entry| entry.state == "fog=1")
            .unwrap();
        assert_eq!(first.count, 2);
        assert_eq!(first.first_frame, 10);
        assert_eq!(first.first_draw, 3);
    }

    #[test]
    fn summary_sorts_by_descending_count() {
        let mut survey = Survey::default();
        let rare = rejection("rare");
        let common = rejection("common");
        survey.record(&rare, "s", 1, 1);
        survey.record(&common, "s", 2, 2);
        survey.record(&common, "s", 3, 3);
        survey.record(&common, "s", 4, 4);
        let summary = survey.summary();
        let common_at = summary.find("common").unwrap();
        let rare_at = summary.find("rare").unwrap();
        assert!(common_at < rare_at, "{summary}");
        assert!(
            summary.starts_with("[d3d8-survey] 4 unsupported"),
            "{summary}"
        );
    }

    #[test]
    fn empty_summary_is_empty() {
        let survey = Survey::default();
        assert!(survey.is_empty());
        assert_eq!(survey.summary(), "");
    }
}
