use clap::Parser;
use rk_analysis::Args;

fn main() {
    if let Err(error) = rk_analysis::run(Args::parse()) {
        eprintln!("rk-analysis: {error}");
        std::process::exit(1);
    }
}
