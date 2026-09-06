//! Concrete Linux policy: publisher-signed catalog plus independently signed
//! exact executable provenance, static ELF identity, then exact-FD self-test.
//! Linux has no Developer ID equivalent; no unsigned "platform passed" shortcut.
use super::provenance::{verify_local_build_provenance, VerifiedBuildProvenance};
use super::{CompositionError, EmbeddedIdentity, ExpectedRelease, ReleaseCandidateVerifier};
use crate::release::catalog::VerifiedCatalog;
use std::fs::File;

pub(super) struct LinuxCandidateVerifier {
    catalog: Vec<u8>,
    signature: Vec<u8>,
    provenance: VerifiedBuildProvenance,
}
impl LinuxCandidateVerifier {
    pub(super) fn new(
        catalog: &VerifiedCatalog,
        expected: &ExpectedRelease<'_>,
        statement: &[u8],
        signature: &[u8],
    ) -> Result<Self, CompositionError> {
        let provenance = verify_local_build_provenance(statement, signature, expected)?;
        let (catalog, signature) = catalog.signed_evidence();
        Ok(Self {
            catalog: catalog.to_vec(),
            signature: signature.to_vec(),
            provenance,
        })
    }
    pub(super) fn retained_provenance(&self) -> VerifiedBuildProvenance {
        self.provenance.clone()
    }
}
impl ReleaseCandidateVerifier for LinuxCandidateVerifier {
    fn platform_signature(
        &mut self,
        _: &mut File,
        expected: &ExpectedRelease<'_>,
    ) -> Result<(), CompositionError> {
        let catalog = crate::release::catalog::verify_catalog(
            &self.catalog,
            &self.signature,
            crate::release::catalog::MINIMUM_SECURE_COMPANION,
        )
        .map_err(|_| CompositionError::PlatformSignatureRejected)?;
        if expected.platform != crate::platform::Platform::Linux
            || catalog.release() != expected.version
            || catalog.key_id() != expected.catalog_key_id
            || catalog.digest() != expected.catalog_sha256
            || catalog
                .artifacts()
                .iter()
                .filter(|artifact| {
                    artifact.platform == "linux"
                        && artifact.arch == expected.artifact_arch
                        && artifact.kind == "payload"
                        && artifact.sha256 == expected.archive_sha256
                })
                .count()
                != 1
            || !self.provenance.matches(expected)
        {
            return Err(CompositionError::PlatformSignatureRejected);
        }
        Ok(())
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
        super::elf::embedded_identity(file, expected)
    }
    fn offline_self_test(
        &mut self,
        file: &mut File,
        _: &ExpectedRelease<'_>,
    ) -> Result<Vec<u8>, CompositionError> {
        #[cfg(target_os = "linux")]
        {
            super::run_linux_offline_self_test(file)
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = file;
            Err(CompositionError::PlatformMismatch)
        }
    }
}
