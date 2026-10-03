//! Offscreen probe: backend init, clear/readback and triangle-colour readback
//! with no window. See `docs/d3d8-inventory.md` for the milestone list.
//!
//! Uses the same renderer and assertions as the windowed probe.

fn main() {
    env_logger::init();

    let options = d3d8_wgpu::probe::ProbeOptions {
        headless: true,
        ..Default::default()
    };

    match d3d8_wgpu::probe::run(&options) {
        Ok(report) => {
            d3d8_wgpu::probe::print_report(&report);
            if !report.all_passed() {
                std::process::exit(1);
            }
        }
        Err(err) => {
            eprintln!("probe-headless unavailable: {err}");
            std::process::exit(2);
        }
    }
}
