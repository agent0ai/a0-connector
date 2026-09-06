use std::env;
use std::io;
use std::process;

use a0_browser_bridge::native_host::{
    is_native_host_candidate, run_native_session, validate_invocation,
};
use a0_browser_bridge::{cli, EXIT_INTEGRITY_OR_POLICY, EXIT_OK};

fn main() {
    let mut args = Vec::new();
    for argument in env::args_os().skip(1) {
        let Ok(argument) = argument.into_string() else {
            eprintln!("a0-browser-bridge: ARGUMENT_ENCODING_INVALID");
            process::exit(EXIT_INTEGRITY_OR_POLICY.into());
        };
        args.push(argument);
    }
    if is_native_host_candidate(&args) {
        let invocation = match validate_invocation(&args) {
            Ok(invocation) => invocation,
            Err(error) => {
                eprintln!("a0-browser-bridge: {}", error.reason_code());
                process::exit(EXIT_INTEGRITY_OR_POLICY.into());
            }
        };
        let mut stdout = io::stdout().lock();
        if let Err(error) = run_native_session(&invocation, io::stdin(), &mut stdout) {
            let exit_code = error.exit_code();
            if exit_code != EXIT_OK {
                eprintln!("a0-browser-bridge: {}", error.reason_code());
            }
            process::exit(exit_code.into());
        }
        process::exit(EXIT_OK.into());
    }
    process::exit(cli::run(&args).into());
}
