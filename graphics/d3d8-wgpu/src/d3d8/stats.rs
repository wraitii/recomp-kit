//! Opt-in texture bind/upload counters (`RECOMP_D3D8_TEXTURE_STATS=1`).
//!
//! One line at process exit reports how many texture binds hit the resident
//! upload cache versus how many actually converted and uploaded. A healthy
//! static scene reports a hit rate near 1.0; a rate near 0.0 means the kit is
//! treating unchanged content as dirty (or the guest really is rewriting the
//! texture every draw).
//!
//! Off by default: when the environment variable is not exactly `1`, the
//! counters are not incremented and nothing is printed.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Once, OnceLock};

/// `RECOMP_D3D8_TEXTURE_STATS` is enabled by exactly `1`.
pub fn parse_enabled(raw: Option<&str>) -> bool {
    matches!(raw, Some("1"))
}

/// Plain counter pair, kept free of globals so it can be unit-tested.
#[derive(Default, Debug, PartialEq, Eq)]
pub struct Stats {
    pub binds: u64,
    pub uploads: u64,
}

impl Stats {
    pub fn record_bind(&mut self) {
        self.binds += 1;
    }

    pub fn record_upload(&mut self) {
        self.uploads += 1;
    }

    pub fn hits(&self) -> u64 {
        self.binds.saturating_sub(self.uploads)
    }

    /// `hits / binds`; `None` before the first bind.
    pub fn hit_rate(&self) -> Option<f64> {
        (self.binds > 0).then(|| self.hits() as f64 / self.binds as f64)
    }

    pub fn summary(&self) -> String {
        format!(
            "[d3d8-texture] binds={} uploads={} hits={} hit_rate={:.3}\n",
            self.binds,
            self.uploads,
            self.hits(),
            self.hit_rate().unwrap_or(0.0)
        )
    }
}

// ---------------------------------------------------------------------------
// Process-global counters
// ---------------------------------------------------------------------------

static ENABLED: OnceLock<bool> = OnceLock::new();
static BINDS: AtomicU64 = AtomicU64::new(0);
static UPLOADS: AtomicU64 = AtomicU64::new(0);
static ATEXIT: Once = Once::new();

/// True when `RECOMP_D3D8_TEXTURE_STATS=1`; read once.
pub fn enabled() -> bool {
    *ENABLED
        .get_or_init(|| parse_enabled(std::env::var("RECOMP_D3D8_TEXTURE_STATS").ok().as_deref()))
}

pub fn record_bind() {
    if enabled() {
        BINDS.fetch_add(1, Ordering::Relaxed);
    }
}

pub fn record_upload() {
    if enabled() {
        UPLOADS.fetch_add(1, Ordering::Relaxed);
    }
}

/// Snapshot the process counters. Used by tests and the report.
pub fn snapshot() -> Stats {
    Stats {
        binds: BINDS.load(Ordering::Relaxed),
        uploads: UPLOADS.load(Ordering::Relaxed),
    }
}

/// Reset the process counters. Test-only helper.
#[cfg(test)]
pub fn reset() {
    BINDS.store(0, Ordering::Relaxed);
    UPLOADS.store(0, Ordering::Relaxed);
}

unsafe extern "C" {
    fn atexit(callback: extern "C" fn()) -> i32;
}

extern "C" fn report_at_exit() {
    report_to_stderr();
}

/// Register the exit report once. No-op when the report is disabled.
pub fn register_exit_report() {
    if !enabled() {
        return;
    }
    ATEXIT.call_once(|| {
        // SAFETY: `report_at_exit` matches the C callback signature.
        unsafe {
            atexit(report_at_exit);
        }
    });
}

/// Print the accumulated counters to stderr. Returns true when enabled and a
/// bind was seen.
pub fn report_to_stderr() -> bool {
    let stats = snapshot();
    if stats.binds == 0 {
        return false;
    }
    eprint!("{}", stats.summary());
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_one_is_enabled() {
        assert!(parse_enabled(Some("1")));
        assert!(!parse_enabled(Some("0")));
        assert!(!parse_enabled(Some("true")));
        assert!(!parse_enabled(None));
    }

    #[test]
    fn counts_and_hit_rate() {
        let mut stats = Stats::default();
        assert_eq!(stats.hit_rate(), None);
        for _ in 0..10 {
            stats.record_bind();
        }
        for _ in 0..2 {
            stats.record_upload();
        }
        assert_eq!(stats.hits(), 8);
        assert_eq!(stats.hit_rate(), Some(0.8));
        let summary = stats.summary();
        assert!(summary.contains("binds=10"), "{summary}");
        assert!(summary.contains("uploads=2"), "{summary}");
        assert!(summary.contains("hit_rate=0.800"), "{summary}");
    }
}
