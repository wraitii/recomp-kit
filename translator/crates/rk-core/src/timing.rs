use std::time::Instant;

use indexmap::IndexMap;

pub struct Phases {
    start: Instant,
    last: Instant,
    pub seconds: IndexMap<String, f64>,
    quiet: bool,
}

impl Phases {
    pub fn new(quiet: bool) -> Self {
        let now = Instant::now();
        Self {
            start: now,
            last: now,
            seconds: IndexMap::new(),
            quiet,
        }
    }

    pub fn mark(&mut self, name: &str) {
        let now = Instant::now();
        let elapsed = (now - self.last).as_secs_f64();
        self.last = now;
        if !self.quiet {
            eprintln!("  [phase] {name:<18} {elapsed:6.2}s");
        }
        *self.seconds.entry(name.to_string()).or_default() += elapsed;
    }

    pub fn total(&self) -> f64 {
        self.start.elapsed().as_secs_f64()
    }
}
