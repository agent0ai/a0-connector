use std::io::{self, Write};

use crate::diagnostics::{redacted_human_status, LocalStatus, SelfTestReport};
use crate::install::{parse_browser_targets, plan_install_or_update, InstallOperation};
use crate::release::verified_release_evidence_unavailable;
use crate::{COMPANION_VERSION, EXIT_OK, EXIT_USAGE};

pub fn run(args: &[String]) -> u8 {
    let Some(command) = args.first().map(String::as_str) else {
        write_human(help_text());
        return EXIT_USAGE;
    };
    if command == "--help" || command == "-h" || command == "help" {
        write_human(help_text());
        return EXIT_OK;
    }
    if command == "--version" || command == "-V" {
        write_human(COMPANION_VERSION);
        return EXIT_OK;
    }

    let json_mode = args[1..].iter().any(|argument| argument == "--json");
    match command {
        "metadata" => {
            if args[1..].iter().any(|argument| argument != "--json") {
                return usage_error(json_mode, "UNEXPECTED_ARGUMENT");
            }
            write_json(crate::release_metadata::as_json());
            EXIT_OK
        }
        "status" | "doctor" => {
            if args[1..].iter().any(|argument| argument != "--json") {
                return usage_error(json_mode, "UNEXPECTED_ARGUMENT");
            }
            let status = LocalStatus::collect();
            if json_mode {
                write_json(&status.to_json());
            } else {
                write_human(&redacted_human_status(&status));
            }
            status.exit_code()
        }
        "self-test" => {
            if args[1..].iter().any(|argument| argument != "--json") {
                return usage_error(json_mode, "UNEXPECTED_ARGUMENT");
            }
            let report = SelfTestReport::run();
            if json_mode {
                write_json(&report.to_json());
            } else {
                write_human(&format!(
                    "Self-test: {} ({})\nInstall ready: {}",
                    report.state, report.reason_code, report.install_ready
                ));
            }
            if report.state == "passed" {
                EXIT_OK
            } else {
                crate::EXIT_INTEGRITY_OR_POLICY
            }
        }
        "install" => run_install_command(&args[1..], json_mode, InstallOperation::Install),
        "update" => run_install_command(&args[1..], json_mode, InstallOperation::Update),
        "repair" | "uninstall" => run_lifecycle_command(command, &args[1..], json_mode),
        "development" => crate::development::run(&args[1..]),
        _ => usage_error(json_mode, "UNKNOWN_COMMAND"),
    }
}

fn run_lifecycle_command(command: &str, args: &[String], json_mode: bool) -> u8 {
    use crate::install::lifecycle::{self, Operation};
    let mut yes = false;
    let mut force_local = false;
    let mut browsers = Vec::new();
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--json" => {}
            "--yes" if command == "uninstall" && !yes => yes = true,
            "--force-local" if command == "uninstall" && !force_local => force_local = true,
            "--keep-logs" if command == "uninstall" => {} // All logs and credentials are retained.
            "--browser" if command == "repair" => {
                index += 1;
                let Some(browser) = args.get(index) else {
                    return usage_error(json_mode, "BROWSER_VALUE_REQUIRED");
                };
                browsers.push(browser.clone());
            }
            _ => return usage_error(json_mode, "UNEXPECTED_ARGUMENT"),
        }
        index += 1;
    }
    let targets = match parse_browser_targets(&browsers) {
        Ok(targets) => targets,
        Err(reason) => return usage_error(json_mode, reason),
    };
    let (state, reason, count, cleanup, disposition, exit_code) = if command == "uninstall" && !yes
    {
        (
            "action_required",
            "LOCAL_RETIREMENT_CONFIRMATION_REQUIRED",
            0,
            "not_attempted",
            "unchanged",
            4,
        )
    } else {
        let operation = if command == "repair" {
            Operation::Repair
        } else if !force_local {
            Operation::PrepareCleanup
        } else {
            Operation::Uninstall
        };
        match lifecycle::run(operation, &targets) {
            Ok(count) if operation == Operation::PrepareCleanup => (
                "cleanup_pending",
                "PROFILE_REVOCATION_REQUIRED",
                count,
                "pending",
                "unchanged",
                6,
            ),
            Ok(count) if command == "repair" => (
                "repaired",
                "REPAIR_VERIFIED",
                count,
                "not_attempted",
                "repaired",
                0,
            ),
            Ok(count) => (
                "cleanup_pending",
                "CREDENTIAL_CLEANUP_PENDING",
                count,
                "pending",
                "registrations_retired_recoverable",
                6,
            ),
            Err(crate::install::InstallTransactionError::LocalRetirementRecoveryRequired) => (
                "cleanup_pending",
                "LOCAL_RETIREMENT_RECOVERY_REQUIRED",
                0,
                "pending",
                "recovery_required",
                6,
            ),
            Err(crate::install::InstallTransactionError::RepairRecoveryRequired) => (
                "blocked",
                "REPAIR_RECOVERY_REQUIRED",
                0,
                "not_attempted",
                "recovery_required",
                6,
            ),
            Err(error) => (
                "blocked",
                error.reason_code(),
                0,
                "not_attempted",
                "unchanged",
                error.exit_code(),
            ),
        }
    };
    if json_mode {
        write_json(&crate::json::object(&[
            (
                "contract",
                crate::json::quote("a0.browser-bridge.lifecycle.v1"),
            ),
            ("schema_version", "1".into()),
            ("companion_version", crate::json::quote(COMPANION_VERSION)),
            ("operation", crate::json::quote(command)),
            ("state", crate::json::quote(state)),
            ("reason_code", crate::json::quote(reason)),
            ("registration_count", count.to_string()),
            ("credential_cleanup", crate::json::quote(cleanup)),
            ("disposition", crate::json::quote(disposition)),
        ]));
    } else {
        write_human(&format!("Browser Bridge {command}: {state} ({reason})"));
        if command == "uninstall" {
            write_human("Use --yes to prepare cleanup, then revoke each paired profile in the extension while connected to Agent Zero and retry. This command cannot claim server revocation from local inventory. --yes --force-local separately retires registrations to recoverable private backups while preserving credentials, logs and executable releases.");
        }
    }
    exit_code
}

fn run_install_command(args: &[String], json_mode: bool, operation: InstallOperation) -> u8 {
    let mut browsers = Vec::new();
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--json" => index += 1,
            "--browser" if operation == InstallOperation::Install => {
                let Some(value) = args.get(index + 1) else {
                    return usage_error(json_mode, "BROWSER_VALUE_REQUIRED");
                };
                browsers.push(value.clone());
                index += 2;
            }
            _ => return usage_error(json_mode, "UNEXPECTED_ARGUMENT"),
        }
    }
    let targets = match parse_browser_targets(&browsers) {
        Ok(targets) => targets,
        Err(reason) => return usage_error(json_mode, reason),
    };
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    if !cfg!(feature = "local-development") && crate::release::release_trust_configured() {
        let result = crate::install::acquire_and_install(operation, targets.clone());
        let (state, reason, count, rollback, exit_code) = match &result {
            Ok(result) => (
                "installed",
                "INSTALL_VERIFIED",
                result.registration_count,
                result.rollback,
                result.exit_code,
            ),
            Err(error) => (
                "blocked",
                error.reason_code(),
                0,
                "not_completed",
                error.exit_code(),
            ),
        };
        if json_mode {
            write_json(&crate::json::object(&[
                ("contract", crate::json::quote(crate::INSTALL_PLAN_CONTRACT)),
                ("schema_version", "1".into()),
                ("companion_version", crate::json::quote(COMPANION_VERSION)),
                (
                    "install_contract",
                    crate::json::quote(crate::INSTALL_CONTRACT),
                ),
                ("operation", crate::json::quote(operation.as_str())),
                ("state", crate::json::quote(state)),
                ("reason_code", crate::json::quote(reason)),
                ("mutation_allowed", result.is_ok().to_string()),
                (
                    "catalog",
                    crate::json::quote(if result.is_ok() {
                        "verified"
                    } else {
                        "not_verified"
                    }),
                ),
                (
                    "artifact",
                    crate::json::quote(if result.is_ok() {
                        "verified"
                    } else {
                        "not_verified"
                    }),
                ),
                (
                    "platform_signature",
                    crate::json::quote(if result.is_ok() {
                        "verified"
                    } else {
                        "not_verified"
                    }),
                ),
                (
                    "platform",
                    crate::json::quote(crate::platform::Platform::current().as_str()),
                ),
                (
                    "architecture",
                    crate::json::quote(crate::platform::architecture()),
                ),
                ("install_root", crate::json::quote("resolved")),
                (
                    "target_browsers",
                    crate::json::string_array(
                        targets.iter().map(crate::install::BrowserTarget::as_str),
                    ),
                ),
                ("registration_count", count.to_string()),
                ("rollback", crate::json::quote(rollback)),
            ]));
        } else {
            write_human(&format!(
                "Browser Bridge {}: {state} ({reason})",
                operation.as_str()
            ));
        }
        return exit_code;
    }
    let evidence = verified_release_evidence_unavailable();
    let plan = plan_install_or_update(operation, targets, evidence.as_ref());
    if json_mode {
        write_json(&plan.to_json());
    } else {
        write_human(&format!(
            "Browser Bridge {}: {} ({})\nNo files or registrations were changed.",
            operation.as_str(),
            plan.state,
            plan.reason_code,
        ));
    }
    plan.exit_code
}

fn usage_error(json_mode: bool, reason_code: &str) -> u8 {
    if json_mode {
        write_json(&crate::json::object(&[
            (
                "contract",
                crate::json::quote("a0.browser-bridge.cli-error.v1"),
            ),
            ("schema_version", "1".to_owned()),
            ("state", crate::json::quote("error")),
            ("reason_code", crate::json::quote(reason_code)),
        ]));
    } else {
        write_human(&format!("{reason_code}\n\n{}", help_text()));
    }
    EXIT_USAGE
}

fn write_json(value: &str) {
    let mut stdout = io::stdout().lock();
    let _ = writeln!(stdout, "{value}");
}

fn write_human(value: &str) {
    let mut stdout = io::stdout().lock();
    let _ = writeln!(stdout, "{value}");
}

fn help_text() -> &'static str {
    "Usage:\n  a0-browser-bridge status [--json]\n  a0-browser-bridge doctor [--json]\n  a0-browser-bridge self-test [--json]\n  a0-browser-bridge metadata --json\n  a0-browser-bridge install [--browser NAME ...] [--json]\n  a0-browser-bridge update [--json]\n  a0-browser-bridge repair [--browser auto|OWNED_NAME ...] [--json]\n  a0-browser-bridge uninstall --yes [--force-local] [--keep-logs] [--json]\n  a0-browser-bridge development install --yes [--browser NAME ...] [--json]\n  a0-browser-bridge development status [--json]\n  a0-browser-bridge development uninstall --yes [--json]"
}
