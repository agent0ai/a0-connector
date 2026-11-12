//! Windows release gates bind Authenticode and execution to sealed private bytes.
use super::provenance::{verify_local_build_provenance, VerifiedBuildProvenance};
use super::{CompositionError, EmbeddedIdentity, ExpectedRelease, ReleaseCandidateVerifier};
use crate::release_payload::VerifiedExecutablePayload;
use std::fs::File;
use std::io::Read;
use std::mem::size_of;
use std::os::windows::{ffi::OsStrExt, io::AsRawHandle, process::CommandExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::ptr::null_mut;
use std::time::{Duration, Instant};
use windows_sys::Win32::{
    Foundation::{GetLastError, ERROR_BROKEN_PIPE, INVALID_HANDLE_VALUE},
    Security::{
        Cryptography::{CertGetCertificateContextProperty, CERT_SHA256_HASH_PROP_ID},
        WinTrust::*,
    },
    System::Pipes::PeekNamedPipe,
};

pub(super) struct WindowsCandidateVerifier {
    path: PathBuf,
    provenance: VerifiedBuildProvenance,
}

impl WindowsCandidateVerifier {
    pub(super) fn new(
        payload: &mut VerifiedExecutablePayload,
        expected: &ExpectedRelease<'_>,
        statement: &[u8],
        signature: &[u8],
    ) -> Result<Self, CompositionError> {
        if crate::release::WINDOWS_SIGNER_CERTIFICATES.is_empty() {
            return Err(CompositionError::ReleaseTrustUnavailable);
        }
        Ok(Self {
            path: payload
                .windows_verification_path()
                .map_err(|_| CompositionError::PayloadChanged)?,
            provenance: verify_local_build_provenance(statement, signature, expected)?,
        })
    }
    pub(super) fn retained_provenance(&self) -> VerifiedBuildProvenance {
        self.provenance.clone()
    }
}

impl ReleaseCandidateVerifier for WindowsCandidateVerifier {
    fn platform_signature(
        &mut self,
        file: &mut File,
        _: &ExpectedRelease<'_>,
    ) -> Result<(), CompositionError> {
        verify_authenticode(
            file,
            &self.path,
            crate::release::WINDOWS_SIGNER_CERTIFICATES,
        )
    }
    fn provenance(
        &mut self,
        _: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<(), CompositionError> {
        if self.provenance.matches(expected) {
            Ok(())
        } else {
            Err(CompositionError::ProvenanceRejected)
        }
    }
    fn embedded_identity(
        &mut self,
        file: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<EmbeddedIdentity, CompositionError> {
        super::pe::embedded_identity(file, expected)
    }
    fn offline_self_test(
        &mut self,
        _: &mut File,
        _: &ExpectedRelease<'_>,
    ) -> Result<Vec<u8>, CompositionError> {
        run_self_test(&self.path)
    }
}

fn verify_authenticode(
    file: &File,
    path: &Path,
    pins: &[[u8; 32]],
) -> Result<(), CompositionError> {
    let bad = || CompositionError::PlatformSignatureRejected;
    if pins.is_empty() {
        return Err(CompositionError::ReleaseTrustUnavailable);
    }
    let mut path: Vec<u16> = path.as_os_str().encode_wide().collect();
    if path.is_empty() || path.len() > 4096 || path.contains(&0) {
        return Err(bad());
    }
    path.push(0);
    let mut info = WINTRUST_FILE_INFO {
        cbStruct: size_of::<WINTRUST_FILE_INFO>() as u32,
        pcwszFilePath: path.as_ptr(),
        hFile: file.as_raw_handle(),
        pgKnownSubject: null_mut(),
    };
    let mut data = WINTRUST_DATA {
        cbStruct: size_of::<WINTRUST_DATA>() as u32,
        dwUIChoice: WTD_UI_NONE,
        fdwRevocationChecks: WTD_REVOKE_WHOLECHAIN,
        dwUnionChoice: WTD_CHOICE_FILE,
        Anonymous: WINTRUST_DATA_0 { pFile: &mut info },
        dwStateAction: WTD_STATEACTION_VERIFY,
        dwProvFlags: WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT
            | WTD_CACHE_ONLY_URL_RETRIEVAL
            | WTD_DISABLE_MD2_MD4,
        ..Default::default()
    };
    let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    // Only exact zero succeeds. Cache-only chain/revocation checks never prompt
    // or fetch ambient URLs; missing revocation evidence remains a rejection.
    let status = unsafe {
        WinVerifyTrust(
            INVALID_HANDLE_VALUE,
            &mut action,
            (&mut data as *mut WINTRUST_DATA).cast(),
        )
    };
    let result = (|| {
        if status != 0 || data.hWVTStateData.is_null() {
            return Err(bad());
        }
        let provider = unsafe { WTHelperProvDataFromStateData(data.hWVTStateData) };
        if provider.is_null() {
            return Err(bad());
        }
        let signer = unsafe { WTHelperGetProvSignerFromChain(provider, 0, 0, 0) };
        if signer.is_null() {
            return Err(bad());
        }
        let signer = unsafe { &*signer };
        if signer.dwError != 0
            || signer.csCertChain == 0
            || signer.pasCertChain.is_null()
            || signer.csCounterSigners != 1
            || signer.pasCounterSigners.is_null()
            || unsafe { (*signer.pasCounterSigners).dwError } != 0
            || !unsafe { WTHelperGetProvSignerFromChain(provider, 1, 0, 0) }.is_null()
        {
            return Err(bad());
        }
        let certificate = unsafe { &*signer.pasCertChain };
        if certificate.pCert.is_null()
            || certificate.dwError != 0
            || certificate.fTestCert != 0
            || certificate.fSelfSigned != 0
        {
            return Err(bad());
        }
        let mut digest = [0u8; 32];
        let mut length = 32;
        if unsafe {
            CertGetCertificateContextProperty(
                certificate.pCert,
                CERT_SHA256_HASH_PROP_ID,
                digest.as_mut_ptr().cast(),
                &mut length,
            )
        } == 0
            || length != 32
            || !pins.contains(&digest)
        {
            return Err(bad());
        }
        Ok(())
    })();
    data.dwStateAction = WTD_STATEACTION_CLOSE;
    unsafe {
        WinVerifyTrust(
            INVALID_HANDLE_VALUE,
            &mut action,
            (&mut data as *mut WINTRUST_DATA).cast(),
        );
    }
    result
}

fn run_self_test(path: &Path) -> Result<Vec<u8>, CompositionError> {
    let bad = || CompositionError::SelfTestRejected;
    let local = std::env::var_os("LOCALAPPDATA").ok_or_else(bad)?;
    let system = std::env::var_os("SystemRoot").ok_or_else(bad)?;
    let mut child = Command::new(path)
        .args(["self-test", "--json"])
        .env_clear()
        .env("LOCALAPPDATA", local)
        .env("SystemRoot", system)
        .current_dir(path.parent().ok_or_else(bad)?)
        .creation_flags(0x08000000)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|_| bad())?;
    let result = (|| {
        let mut pipe = child.stdout.take().ok_or_else(bad)?;
        let deadline = Instant::now() + Duration::from_secs(8);
        let mut output = Vec::new();
        loop {
            let mut available = 0;
            if unsafe {
                PeekNamedPipe(
                    pipe.as_raw_handle(),
                    null_mut(),
                    0,
                    null_mut(),
                    &mut available,
                    null_mut(),
                )
            } == 0
            {
                if unsafe { GetLastError() } != ERROR_BROKEN_PIPE {
                    return Err(bad());
                }
                if child
                    .try_wait()
                    .map_err(|_| bad())?
                    .is_some_and(|status| status.success())
                {
                    return Ok(output);
                }
            } else if available > 0 {
                if output.len() + available as usize > 2048 {
                    return Err(bad());
                }
                let mut bytes = vec![0; available as usize];
                pipe.read_exact(&mut bytes).map_err(|_| bad())?;
                output.extend_from_slice(&bytes);
            }
            if Instant::now() >= deadline {
                return Err(bad());
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    })();
    if result.is_err() {
        let _ = child.kill();
    }
    let _ = child.wait();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn authenticode_never_accepts_unsigned_bytes_or_absent_publisher_identity() {
        let path = std::env::current_exe().unwrap();
        let file = File::open(&path).unwrap();
        assert_eq!(
            verify_authenticode(&file, &path, &[]),
            Err(CompositionError::ReleaseTrustUnavailable)
        );
        assert_eq!(
            verify_authenticode(&file, &path, &[[1; 32]]),
            Err(CompositionError::PlatformSignatureRejected)
        );
    }
}
