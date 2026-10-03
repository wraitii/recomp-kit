use d3d8_wgpu::probe::{CheckResult, Outcome, ProbeReport};

#[test]
fn success_requires_every_requested_check_to_pass() {
    for (outcomes, expected) in [
        (vec![], false),
        (vec![Outcome::Unsupported], false),
        (vec![Outcome::Passed, Outcome::Unsupported], false),
        (vec![Outcome::Passed, Outcome::Failed], false),
        (vec![Outcome::Passed, Outcome::Passed], true),
    ] {
        let report = ProbeReport {
            backend: String::new(),
            adapter: String::new(),
            max_texture_dimension_2d: 0,
            requested_checks: (0..outcomes.len())
                .map(|i| if i == 0 { "first" } else { "second" })
                .collect(),
            checks: outcomes
                .iter()
                .enumerate()
                .map(|(i, &outcome)| CheckResult {
                    name: if i == 0 { "first" } else { "second" },
                    outcome,
                    detail: String::new(),
                })
                .collect(),
        };
        assert_eq!(report.all_passed(), expected, "{outcomes:?}");
    }
}

#[test]
fn missing_duplicate_or_unrequested_checks_fail() {
    for names in [
        vec![],
        vec!["clear"],
        vec!["clear", "clear"],
        vec!["clear", "other"],
    ] {
        let report = ProbeReport {
            backend: String::new(),
            adapter: String::new(),
            max_texture_dimension_2d: 0,
            requested_checks: vec!["clear", "triangle"],
            checks: names
                .into_iter()
                .map(|name| CheckResult {
                    name,
                    outcome: Outcome::Passed,
                    detail: String::new(),
                })
                .collect(),
        };
        assert!(!report.all_passed());
    }
}
