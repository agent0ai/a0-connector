//! Concrete Darwin release verification using a retained private staging lease.
//! Security.framework/codesign and execve are path-based public APIs on macOS.
//! The narrowly scoped lease binds those operations to one original inode and
//! digest before/after every call; an actively compromised same-UID host is not
//! claimed to be contained by this per-user installer.

use std::fs::File;
use std::io::{ErrorKind, Read};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use super::provenance::{verify_local_build_provenance, VerifiedBuildProvenance};
use super::{CompositionError, EmbeddedIdentity, ExpectedRelease, ReleaseCandidateVerifier};
use crate::release::{MacosReleaseIdentity, MACOS_RELEASE_IDENTITY};
use crate::release_payload::{MacosVerificationLease, VerifiedExecutablePayload};

pub(super) struct MacosCandidateVerifier {
    lease: MacosVerificationLease,
    identity: &'static MacosReleaseIdentity,
    provenance: VerifiedBuildProvenance,
}

impl MacosCandidateVerifier {
    pub(super) fn retained_provenance(&self) -> VerifiedBuildProvenance {
        self.provenance.clone()
    }
    pub(super) fn new(
        payload: &mut VerifiedExecutablePayload,
        expected: &ExpectedRelease<'_>,
        statement: &[u8],
        signature: &[u8],
    ) -> Result<Self, CompositionError> {
        let identity = MACOS_RELEASE_IDENTITY
            .as_ref()
            .ok_or(CompositionError::ReleaseTrustUnavailable)?;
        if identity.team_id.len() != 10
            || !identity
                .team_id
                .bytes()
                .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit())
            || identity.signing_identifier.is_empty()
            || identity.signing_identifier.len() > 128
            || !identity
                .signing_identifier
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b".-_".contains(&byte))
        {
            return Err(CompositionError::ReleaseTrustUnavailable);
        }
        let provenance = verify_local_build_provenance(statement, signature, expected)?;
        let lease = payload
            .macos_verification_lease()
            .map_err(|_| CompositionError::PayloadChanged)?;
        Ok(Self {
            lease,
            identity,
            provenance,
        })
    }

    fn bound_command(
        &mut self,
        executable: &File,
        program: Option<&'static str>,
        arguments: &[String],
        timeout: Duration,
        stdout_limit: usize,
    ) -> Result<CommandOutput, CompositionError> {
        self.lease
            .verify(executable)
            .map_err(|_| CompositionError::PayloadChanged)?;
        let path = self.lease.path().to_owned();
        let (program, args) = match program {
            Some(program) => {
                if program != "/usr/bin/codesign" {
                    return Err(CompositionError::CandidateRejected);
                }
                let mut args = arguments.to_vec();
                args.push(
                    path.to_str()
                        .ok_or(CompositionError::PayloadChanged)?
                        .to_owned(),
                );
                (PathBuf::from(program), args)
            }
            None => (path, arguments.to_vec()),
        };
        let result = run_bounded(&program, &args, timeout, stdout_limit);
        // Always perform readback even when the tool fails. A tool failure can
        // never suppress detection of a changed retained executable.
        self.lease
            .verify(executable)
            .map_err(|_| CompositionError::PayloadChanged)?;
        result
    }
}

impl ReleaseCandidateVerifier for MacosCandidateVerifier {
    fn platform_signature(
        &mut self,
        executable: &mut File,
        _: &ExpectedRelease<'_>,
    ) -> Result<(), CompositionError> {
        let requirement = format!(
            "=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = \"{}\" and identifier \"{}\" and notarized",
            self.identity.team_id, self.identity.signing_identifier,
        );
        self.bound_command(
            executable,
            Some("/usr/bin/codesign"),
            &[
                "--verify".into(),
                "--strict".into(),
                "--all-architectures".into(),
                "--check-notarization".into(),
                "--test-requirement".into(),
                requirement,
            ],
            Duration::from_secs(30),
            4096,
        )?;
        for architecture in ["x86_64", "arm64"] {
            let details = self.bound_command(
                executable,
                Some("/usr/bin/codesign"),
                &[
                    "--display".into(),
                    "--verbose=4".into(),
                    "--architecture".into(),
                    architecture.into(),
                ],
                Duration::from_secs(20),
                4096,
            )?;
            validate_signature_details(&details.stderr)?;
        }
        // spctl's execute assessment is app-specific and rejects bare Mach-O
        // tools even when notarized. The explicit codesign requirement above
        // binds Apple's online ticket to the retained executable and exact
        // Developer ID identity. No localized diagnostic string is authority.
        Ok(())
    }

    fn provenance(
        &mut self,
        executable: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<(), CompositionError> {
        self.lease
            .verify(executable)
            .map_err(|_| CompositionError::PayloadChanged)?;
        if self.provenance.matches(expected) {
            Ok(())
        } else {
            Err(CompositionError::ProvenanceRejected)
        }
    }

    fn embedded_identity(
        &mut self,
        executable: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<EmbeddedIdentity, CompositionError> {
        self.lease
            .verify(executable)
            .map_err(|_| CompositionError::PayloadChanged)?;
        let identity = super::macho::embedded_identity(executable, expected)?;
        self.lease
            .verify(executable)
            .map_err(|_| CompositionError::PayloadChanged)?;
        let output = self.bound_command(
            executable,
            None,
            &["metadata".into(), "--json".into()],
            Duration::from_secs(4),
            2048,
        )?;
        super::macho::validate_metadata(&output.stdout, expected)?;
        Ok(identity)
    }

    fn offline_self_test(
        &mut self,
        executable: &mut File,
        _: &ExpectedRelease<'_>,
    ) -> Result<Vec<u8>, CompositionError> {
        self.bound_command(
            executable,
            None,
            &["self-test".into(), "--json".into()],
            Duration::from_secs(8),
            2048,
        )
        .map(|output| output.stdout)
    }
}

fn validate_signature_details(bytes: &[u8]) -> Result<(), CompositionError> {
    let text =
        std::str::from_utf8(bytes).map_err(|_| CompositionError::PlatformSignatureRejected)?;
    // codesign also emits "Executable Segment flags=...". Only the single
    // CodeDirectory's flags describe hardened runtime and ad-hoc signing.
    let directories: Vec<_> = text
        .lines()
        .filter(|line| line.starts_with("CodeDirectory "))
        .collect();
    if directories.len() != 1 {
        return Err(CompositionError::PlatformSignatureRejected);
    }
    let flags: Vec<_> = directories[0]
        .split_whitespace()
        .filter_map(|word| word.strip_prefix("flags=0x"))
        .filter_map(|word| word.split('(').next())
        .filter_map(|value| u32::from_str_radix(value, 16).ok())
        .collect();
    if flags.len() != 1
        || flags[0] & 0x10000 == 0
        || flags[0] & 2 != 0
        || !text.lines().any(|line| {
            line.strip_prefix("Timestamp=")
                .is_some_and(|value| !value.is_empty() && value != "none")
        })
    {
        return Err(CompositionError::PlatformSignatureRejected);
    }
    Ok(())
}

struct CommandOutput {
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

fn run_bounded(
    program: &Path,
    args: &[String],
    timeout: Duration,
    stdout_limit: usize,
) -> Result<CommandOutput, CompositionError> {
    let rejected = CompositionError::CandidateRejected;
    let home = crate::platform::discover_user_paths()
        .map_err(|_| rejected)?
        .home_root
        .ok_or(rejected)?;
    let mut child = Command::new(program)
        .args(args)
        .env_clear()
        .env("HOME", home)
        .env("LC_ALL", "C")
        .current_dir("/")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| rejected)?;
    let result = (|| {
        let mut stdout = child.stdout.take().ok_or(rejected)?;
        let mut stderr = child.stderr.take().ok_or(rejected)?;
        set_nonblocking(stdout.as_raw_fd())?;
        set_nonblocking(stderr.as_raw_fd())?;
        let mut output = CommandOutput {
            stdout: Vec::new(),
            stderr: Vec::new(),
        };
        let deadline = Instant::now() + timeout;
        loop {
            drain(&mut stdout, &mut output.stdout, stdout_limit)?;
            drain(&mut stderr, &mut output.stderr, 16 * 1024)?;
            if let Some(status) = child.try_wait().map_err(|_| rejected)? {
                drain(&mut stdout, &mut output.stdout, stdout_limit)?;
                drain(&mut stderr, &mut output.stderr, 16 * 1024)?;
                return if status.success() {
                    Ok(output)
                } else {
                    Err(rejected)
                };
            }
            if Instant::now() >= deadline {
                return Err(rejected);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    })();
    if result.is_err() {
        let _ = child.kill();
        let _ = child.wait();
    }
    result
}

fn set_nonblocking(descriptor: i32) -> Result<(), CompositionError> {
    unsafe extern "C" {
        fn fcntl(fd: i32, command: i32, ...) -> i32;
    }
    // Darwin F_GETFL=3, F_SETFL=4, O_NONBLOCK=4. Only owned pipe read ends change.
    let flags = unsafe { fcntl(descriptor, 3) };
    if flags < 0 || unsafe { fcntl(descriptor, 4, flags | 4) } < 0 {
        return Err(CompositionError::CandidateRejected);
    }
    Ok(())
}

fn drain(reader: &mut impl Read, output: &mut Vec<u8>, cap: usize) -> Result<(), CompositionError> {
    let mut buffer = [0; 4096];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(count) if output.len() + count <= cap => output.extend_from_slice(&buffer[..count]),
            Ok(_) => return Err(CompositionError::CandidateRejected),
            Err(error) if error.kind() == ErrorKind::WouldBlock => return Ok(()),
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            Err(_) => return Err(CompositionError::CandidateRejected),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signature_flags_belong_only_to_one_code_directory_not_executable_segments() {
        assert!(validate_signature_details(
            b"CodeDirectory v=20500 size=42151 flags=0x10000(runtime) hashes=1311+2 location=embedded\nExecutable Segment flags=0x1\nTimestamp=Sep 5, 2026 at 22:16:45\n"
        ).is_ok());
        for details in [
            b"Executable Segment flags=0x10000\nTimestamp=now\n".as_slice(),
            b"CodeDirectory flags=0x10000(runtime)\nCodeDirectory flags=0x10000(runtime)\nTimestamp=now\n",
            b"CodeDirectory flags=0x10002(adhoc,runtime)\nExecutable Segment flags=0x1\nTimestamp=now\n",
            b"CodeDirectory flags=0x0(none)\nExecutable Segment flags=0x10000\nTimestamp=now\n",
            b"CodeDirectory flags=0x10000(runtime)\nExecutable Segment flags=0x1\nSigned Time=now\n",
        ] {
            assert!(validate_signature_details(details).is_err());
        }
    }

    #[test]
    fn actual_system_codesign_runs_bounded_and_runtime_timestamp_checks_fail_closed() {
        let output = run_bounded(
            Path::new("/usr/bin/codesign"),
            &["--verify".into(), "--strict".into(), "/usr/bin/true".into()],
            Duration::from_secs(5),
            4096,
        )
        .unwrap();
        assert!(output.stdout.is_empty());
        assert!(validate_signature_details(
            b"CodeDirectory flags=0x10000(runtime)\nTimestamp=Sep 5, 2026\n"
        )
        .is_ok());
        for details in [
            b"flags=0x10000(runtime)\nSigned Time=now\n".as_slice(),
            b"flags=0x10002(adhoc,runtime)\nTimestamp=now\n",
            b"flags=0x0(none)\nTimestamp=now\n",
        ] {
            assert!(validate_signature_details(details).is_err());
        }
    }
}
