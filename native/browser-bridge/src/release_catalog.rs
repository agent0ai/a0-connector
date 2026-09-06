//! Detached catalog verification, separate from platform signature/install authority.
//! Production entry points use compiled public roots only. Tests cannot add roots
//! to a production process, and verified catalog/artifact bytes alone never mint
//! a fully verified install authorization.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{Read, Write};
use std::time::Duration;

use ed25519_dalek::{Signature, VerifyingKey};
use sha2::{Digest, Sha256};

use super::{
    is_exact_release_extension_origin, TrustedReleasePublicKey, PINNED_RELEASE_CATALOGS,
    PRODUCTION_EXTENSION_ORIGINS, TRUSTED_RELEASE_PUBLIC_KEYS,
};
use crate::json::{self, Value};

const MAX_CATALOG_BYTES: usize = 128 * 1024;
const MAX_ARTIFACT_BYTES: u64 = 512 * 1024 * 1024;
pub const MINIMUM_SECURE_COMPANION: &str = "2.12.0";
// A declared platform is always its complete shipping group, not an arbitrary
// architecture subset. v1 requires this entire matrix; v2 signs its platform union.
const DELIVERY_MATRIX: [(&str, &str, &str); 9] = [
    ("macos", "universal2", "installer"),
    ("macos", "universal2", "payload"),
    ("windows", "x86_64", "installer"),
    ("windows", "x86_64", "payload"),
    ("windows", "arm64", "installer"),
    ("windows", "arm64", "payload"),
    ("linux", "any", "bootstrap"),
    ("linux", "x86_64", "payload"),
    ("linux", "aarch64", "payload"),
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CatalogError {
    Unavailable,
    InvalidCatalog,
    InvalidSignature,
    Incompatible,
    InvalidArtifact,
    DownloadFailed,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CatalogArtifact {
    pub name: String,
    pub platform: String,
    pub arch: String,
    pub kind: String,
    pub download_url: String,
    pub sha256: String,
    pub size: u64,
}

#[derive(Debug)]
pub struct VerifiedCatalog {
    release: String,
    key_id: String,
    digest: String,
    artifacts: Vec<CatalogArtifact>,
    signed_bytes: Vec<u8>,
    signature: Vec<u8>,
}

#[derive(Debug)]
pub struct VerifiedArtifactBytes {
    catalog_digest: String,
    artifact: CatalogArtifact,
}

impl VerifiedCatalog {
    pub(crate) fn signed_evidence(&self) -> (&[u8], &[u8]) {
        (&self.signed_bytes, &self.signature)
    }
    pub fn release(&self) -> &str {
        &self.release
    }
    pub fn key_id(&self) -> &str {
        &self.key_id
    }
    pub fn digest(&self) -> &str {
        &self.digest
    }
    pub fn artifacts(&self) -> &[CatalogArtifact] {
        &self.artifacts
    }

    /// Hash the bounded exact stream. This does not execute, extract, install,
    /// certify platform signatures, or trust a caller-supplied path.
    pub fn verify_artifact<R: Read>(
        &self,
        name: &str,
        reader: &mut R,
    ) -> Result<VerifiedArtifactBytes, CatalogError> {
        self.copy_verified_artifact(name, reader, &mut std::io::sink())
    }

    /// Copy only bounded bytes to caller-owned private staging. An error does
    /// not authorize the partial output: its owner must discard it. Success
    /// still proves neither executable/platform provenance nor install policy.
    pub fn copy_verified_artifact<R: Read, W: Write>(
        &self,
        name: &str,
        reader: &mut R,
        staging: &mut W,
    ) -> Result<VerifiedArtifactBytes, CatalogError> {
        let artifact = self
            .artifacts
            .iter()
            .find(|item| item.name == name)
            .ok_or(CatalogError::InvalidArtifact)?;
        let mut hash = Sha256::new();
        let mut total = 0u64;
        let mut buffer = [0u8; 32 * 1024];
        loop {
            let read = reader
                .read(&mut buffer)
                .map_err(|_| CatalogError::InvalidArtifact)?;
            if read == 0 {
                break;
            }
            total = total
                .checked_add(read as u64)
                .ok_or(CatalogError::InvalidArtifact)?;
            if total > artifact.size {
                return Err(CatalogError::InvalidArtifact);
            }
            hash.update(&buffer[..read]);
            staging
                .write_all(&buffer[..read])
                .map_err(|_| CatalogError::InvalidArtifact)?;
        }
        if total != artifact.size || hex(&hash.finalize()) != artifact.sha256 {
            return Err(CatalogError::InvalidArtifact);
        }
        Ok(VerifiedArtifactBytes {
            catalog_digest: self.digest.clone(),
            artifact: artifact.clone(),
        })
    }

    /// Fetch only the URL already authenticated by this catalog. No cookies,
    /// caller headers, redirects, ambient proxy, or unverified URL are accepted.
    pub fn download_artifact<W: Write>(
        &self,
        name: &str,
        staging: &mut W,
    ) -> Result<VerifiedArtifactBytes, CatalogError> {
        let artifact = self
            .artifacts
            .iter()
            .find(|entry| entry.name == name)
            .ok_or(CatalogError::InvalidArtifact)?;
        let tls = ureq::tls::TlsConfig::builder()
            .root_certs(ureq::tls::RootCerts::PlatformVerifier)
            .build();
        let config = ureq::Agent::config_builder()
            .tls_config(tls)
            .max_redirects(0)
            .proxy(None)
            .timeout_global(Some(Duration::from_secs(120)))
            .user_agent(format!("a0-browser-bridge/{}", crate::COMPANION_VERSION))
            .build();
        let agent = ureq::Agent::new_with_config(config);
        let mut response = agent
            .get(&artifact.download_url)
            .header("Accept-Encoding", "identity")
            .call()
            .map_err(|_| CatalogError::DownloadFailed)?;
        if response.status().as_u16() != 200 {
            return Err(CatalogError::DownloadFailed);
        }
        if let Some(length) = response.headers().get("Content-Length") {
            if length
                .to_str()
                .ok()
                .and_then(|value| value.parse::<u64>().ok())
                != Some(artifact.size)
            {
                return Err(CatalogError::InvalidArtifact);
            }
        }
        if let Some(encoding) = response.headers().get("Content-Encoding") {
            if encoding.to_str().ok() != Some("identity") {
                return Err(CatalogError::InvalidArtifact);
            }
        }
        self.copy_verified_artifact(name, &mut response.body_mut().as_reader(), staging)
    }
}

impl VerifiedArtifactBytes {
    pub fn artifact(&self) -> &CatalogArtifact {
        &self.artifact
    }
    pub fn catalog_digest(&self) -> &str {
        &self.catalog_digest
    }

    #[cfg(test)]
    pub(crate) fn payload_fixture(name: &str, platform: &str, arch: &str, bytes: &[u8]) -> Self {
        Self {
            catalog_digest: "1".repeat(64),
            artifact: CatalogArtifact {
                name: name.to_owned(),
                platform: platform.to_owned(),
                arch: arch.to_owned(),
                kind: "payload".to_owned(),
                download_url: "https://release.example.invalid/payload".to_owned(),
                sha256: hex(&Sha256::digest(bytes)),
                size: bytes.len() as u64,
            },
        }
    }
}

/// `known_floor` is supplied by owned local/server policy, never catalog data.
/// The compiled non-lowerable security floor applies even if that value is low.
pub fn verify_catalog(
    bytes: &[u8],
    signature: &[u8],
    known_floor: &str,
) -> Result<VerifiedCatalog, CatalogError> {
    verify_with_policy(
        bytes,
        signature,
        known_floor,
        TRUSTED_RELEASE_PUBLIC_KEYS,
        PRODUCTION_EXTENSION_ORIGINS,
    )
}

/// Acquire one reviewed immutable release; no URL/path override and no
/// unsigned latest-release lookup. This is catalog proof, not install proof.
pub fn fetch_catalog(release: &str, known_floor: &str) -> Result<VerifiedCatalog, CatalogError> {
    if TRUSTED_RELEASE_PUBLIC_KEYS.is_empty() || PRODUCTION_EXTENSION_ORIGINS.is_empty() {
        return Err(CatalogError::Unavailable);
    }
    let matches: Vec<_> = PINNED_RELEASE_CATALOGS
        .iter()
        .filter(|entry| entry.release == release)
        .collect();
    if matches.len() != 1 {
        return Err(CatalogError::Unavailable);
    }
    let source = matches[0];
    version(source.release)?;
    if source.catalog_url == source.signature_url
        || [source.catalog_url, source.signature_url]
            .iter()
            .any(|url| !url.starts_with("https://") || !crate::rpc::valid_server_base_origin(url))
    {
        return Err(CatalogError::Unavailable);
    }
    let bytes = download_metadata(source.catalog_url, MAX_CATALOG_BYTES)?;
    let signature = download_metadata(source.signature_url, 64)?;
    let catalog = verify_catalog(&bytes, &signature, known_floor)?;
    if catalog.release() != source.release {
        return Err(CatalogError::Incompatible);
    }
    Ok(catalog)
}

pub(crate) fn download_metadata(url: &str, limit: usize) -> Result<Vec<u8>, CatalogError> {
    let tls = ureq::tls::TlsConfig::builder()
        .root_certs(ureq::tls::RootCerts::PlatformVerifier)
        .build();
    let agent = ureq::Agent::new_with_config(
        ureq::Agent::config_builder()
            .tls_config(tls)
            .max_redirects(0)
            .proxy(None)
            .timeout_global(Some(Duration::from_secs(30)))
            .user_agent(format!("a0-browser-bridge/{}", crate::COMPANION_VERSION))
            .build(),
    );
    let mut response = agent
        .get(url)
        .header("Accept-Encoding", "identity")
        .call()
        .map_err(|_| CatalogError::DownloadFailed)?;
    if response.status().as_u16() != 200 {
        return Err(CatalogError::DownloadFailed);
    }
    if let Some(encoding) = response.headers().get("Content-Encoding") {
        if encoding.to_str().ok() != Some("identity") {
            return Err(CatalogError::InvalidCatalog);
        }
    }
    let length = response
        .headers()
        .get("Content-Length")
        .map(|header| {
            header
                .to_str()
                .ok()
                .and_then(|text| text.parse::<usize>().ok())
                .filter(|size| *size > 0 && *size <= limit)
                .ok_or(CatalogError::InvalidCatalog)
        })
        .transpose()?;
    read_metadata(&mut response.body_mut().as_reader(), limit, length)
}

fn read_metadata<R: Read>(
    reader: &mut R,
    limit: usize,
    expected: Option<usize>,
) -> Result<Vec<u8>, CatalogError> {
    let mut bytes = Vec::new();
    reader
        .take((limit + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| CatalogError::DownloadFailed)?;
    if bytes.is_empty() || bytes.len() > limit || expected.is_some_and(|size| size != bytes.len()) {
        return Err(CatalogError::InvalidCatalog);
    }
    Ok(bytes)
}

fn verify_with_policy(
    bytes: &[u8],
    signature: &[u8],
    known_floor: &str,
    roots: &[TrustedReleasePublicKey],
    expected_origins: &[&str],
) -> Result<VerifiedCatalog, CatalogError> {
    if roots.is_empty() || expected_origins.is_empty() {
        return Err(CatalogError::Unavailable);
    }
    let root_ids: BTreeSet<_> = roots.iter().map(|key| key.key_id).collect();
    let expected: BTreeSet<_> = expected_origins.iter().copied().collect();
    if root_ids.len() != roots.len()
        || expected.len() != expected_origins.len()
        || expected
            .iter()
            .any(|origin| !is_exact_release_extension_origin(origin))
    {
        return Err(CatalogError::Unavailable);
    }
    if bytes.is_empty() || bytes.len() > MAX_CATALOG_BYTES || signature.len() != 64 {
        return Err(CatalogError::InvalidCatalog);
    }
    let value = json::parse(bytes).map_err(|_| CatalogError::InvalidCatalog)?;
    let root = object(&value)?;
    let schema_version = number(root, "schema_version")?;
    let mut required = vec![
        "schema_version",
        "release",
        "channel",
        "published_at",
        "protocol",
        "trust",
        "minimum_secure_companion",
        "extension_origins",
        "artifacts",
        "release_key_id",
    ];
    match schema_version {
        1 => {}
        2 => required.push("platforms"),
        _ => return Err(CatalogError::Incompatible),
    }
    exact(root, &required, &[])?;
    let key_id = text(root, "release_key_id", 128)?;
    let key = roots
        .iter()
        .find(|key| key.key_id == key_id)
        .ok_or(CatalogError::InvalidSignature)?;
    let public_key = VerifyingKey::from_bytes(&key.ed25519_public_key)
        .map_err(|_| CatalogError::InvalidSignature)?;
    public_key
        .verify_strict(
            bytes,
            &Signature::from_slice(signature).map_err(|_| CatalogError::InvalidSignature)?,
        )
        .map_err(|_| CatalogError::InvalidSignature)?;
    if text(root, "channel", 32)? != "stable" {
        return Err(CatalogError::Incompatible);
    }
    compatible_range(root.get("protocol").ok_or(CatalogError::InvalidCatalog)?)?;
    compatible_range(root.get("trust").ok_or(CatalogError::InvalidCatalog)?)?;
    let release = text(root, "release", 64)?;
    let release_version = version(release)?;
    let floor = version(text(root, "minimum_secure_companion", 64)?)?
        .max(version(known_floor)?)
        .max(version(MINIMUM_SECURE_COMPANION)?);
    if release_version < floor {
        return Err(CatalogError::Incompatible);
    }
    if !valid_date(text(root, "published_at", 32)?) {
        return Err(CatalogError::InvalidCatalog);
    }
    let origins = root
        .get("extension_origins")
        .and_then(Value::as_array)
        .ok_or(CatalogError::InvalidCatalog)?;
    let origins: Vec<_> = origins
        .iter()
        .map(|value| value.as_str().ok_or(CatalogError::InvalidCatalog))
        .collect::<Result<_, _>>()?;
    let supplied: BTreeSet<_> = origins.iter().copied().collect();
    if origins.len() != supplied.len() || supplied != expected {
        return Err(CatalogError::Incompatible);
    }
    let declared_platforms = if schema_version == 1 {
        vec!["linux", "macos", "windows"]
    } else {
        let platforms = root
            .get("platforms")
            .and_then(Value::as_array)
            .filter(|values| (1..=3).contains(&values.len()))
            .ok_or(CatalogError::InvalidCatalog)?;
        let mut declared = Vec::new();
        for platform in platforms {
            let platform = platform.as_str().ok_or(CatalogError::InvalidCatalog)?;
            if !matches!(platform, "linux" | "macos" | "windows")
                || declared
                    .last()
                    .is_some_and(|previous| *previous >= platform)
            {
                return Err(CatalogError::InvalidCatalog);
            }
            declared.push(platform);
        }
        declared
    };
    let required_matrix: BTreeSet<_> = DELIVERY_MATRIX
        .iter()
        .copied()
        .filter(|(platform, _, _)| declared_platforms.contains(platform))
        .collect();
    let artifacts = root
        .get("artifacts")
        .and_then(Value::as_array)
        .filter(|items| items.len() == required_matrix.len())
        .ok_or(CatalogError::InvalidCatalog)?;
    let mut parsed = Vec::new();
    let mut matrix = BTreeSet::new();
    let mut names = BTreeSet::new();
    let mut urls = BTreeSet::new();
    for artifact in artifacts {
        let entry = object(artifact)?;
        exact(
            entry,
            &[
                "name",
                "platform",
                "arch",
                "kind",
                "download_url",
                "sha256",
                "size",
            ],
            &["minimum_os"],
        )?;
        let name = text(entry, "name", 128)?;
        if !name.as_bytes()[0].is_ascii_alphanumeric()
            || !name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
        {
            return Err(CatalogError::InvalidCatalog);
        }
        let platform = text(entry, "platform", 16)?;
        let arch = text(entry, "arch", 16)?;
        let kind = text(entry, "kind", 16)?;
        if !required_matrix.contains(&(platform, arch, kind))
            || !matrix.insert((platform, arch, kind))
            || !names.insert(name)
        {
            return Err(CatalogError::InvalidCatalog);
        }
        let url = text(entry, "download_url", 2048)?;
        if !url.starts_with("https://")
            || !crate::rpc::valid_server_base_origin(url)
            || !urls.insert(url)
        {
            return Err(CatalogError::InvalidCatalog);
        }
        let sha256 = text(entry, "sha256", 64)?;
        if sha256.len() != 64
            || !sha256
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        {
            return Err(CatalogError::InvalidCatalog);
        }
        let size = number(entry, "size")?;
        if size == 0 || size > MAX_ARTIFACT_BYTES {
            return Err(CatalogError::InvalidCatalog);
        }
        if entry.contains_key("minimum_os") {
            text(entry, "minimum_os", 128)?;
        }
        parsed.push(CatalogArtifact {
            name: name.into(),
            platform: platform.into(),
            arch: arch.into(),
            kind: kind.into(),
            download_url: url.into(),
            sha256: sha256.into(),
            size,
        });
    }
    if matrix != required_matrix {
        return Err(CatalogError::InvalidCatalog);
    }
    // Every accepted key/string is ASCII, all numbers bounded integers, and
    // object keys are fixed ASCII. Sorted compact encoding is therefore JCS.
    if value.encode().as_bytes() != bytes {
        return Err(CatalogError::InvalidCatalog);
    }
    Ok(VerifiedCatalog {
        release: release.into(),
        key_id: key_id.into(),
        digest: hex(&Sha256::digest(bytes)),
        artifacts: parsed,
        signed_bytes: bytes.to_vec(),
        signature: signature.to_vec(),
    })
}

fn object(value: &Value) -> Result<&BTreeMap<String, Value>, CatalogError> {
    value.as_object().ok_or(CatalogError::InvalidCatalog)
}
fn exact(
    value: &BTreeMap<String, Value>,
    required: &[&str],
    optional: &[&str],
) -> Result<(), CatalogError> {
    if required.iter().any(|key| !value.contains_key(*key))
        || value
            .keys()
            .any(|key| !required.contains(&key.as_str()) && !optional.contains(&key.as_str()))
    {
        return Err(CatalogError::InvalidCatalog);
    }
    Ok(())
}
fn text<'a>(
    value: &'a BTreeMap<String, Value>,
    name: &str,
    limit: usize,
) -> Result<&'a str, CatalogError> {
    value
        .get(name)
        .and_then(Value::as_str)
        .filter(|value| {
            !value.is_empty()
                && value.len() <= limit
                && value.bytes().all(|c| (0x20..=0x7e).contains(&c))
        })
        .ok_or(CatalogError::InvalidCatalog)
}
fn number(value: &BTreeMap<String, Value>, name: &str) -> Result<u64, CatalogError> {
    value
        .get(name)
        .and_then(Value::as_u64)
        .ok_or(CatalogError::InvalidCatalog)
}
fn compatible_range(value: &Value) -> Result<(), CatalogError> {
    let value = object(value)?;
    exact(value, &["min", "max"], &[])?;
    if number(value, "min")? != 1 || number(value, "max")? < 1 || number(value, "max")? > 65535 {
        return Err(CatalogError::Incompatible);
    }
    Ok(())
}
fn version(value: &str) -> Result<(u32, u32, u32), CatalogError> {
    if value.len() > 64 {
        return Err(CatalogError::Incompatible);
    }
    let base = if let Some((base, build)) = value.split_once('+') {
        if build.is_empty()
            || build.split('.').any(|id| {
                id.is_empty() || !id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
            })
        {
            return Err(CatalogError::Incompatible);
        }
        base
    } else {
        value
    };
    let parts: Vec<_> = base.split('.').collect();
    if parts.len() != 3
        || parts.iter().any(|part| {
            part.is_empty()
                || part.len() > 9
                || (part.len() > 1 && part.starts_with('0'))
                || !part.bytes().all(|c| c.is_ascii_digit())
        })
    {
        return Err(CatalogError::Incompatible);
    }
    Ok((
        parts[0].parse().map_err(|_| CatalogError::Incompatible)?,
        parts[1].parse().map_err(|_| CatalogError::Incompatible)?,
        parts[2].parse().map_err(|_| CatalogError::Incompatible)?,
    ))
}
fn valid_date(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() != 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'Z'
        || bytes
            .iter()
            .enumerate()
            .any(|(i, c)| ![4, 7, 10, 13, 16, 19].contains(&i) && !c.is_ascii_digit())
    {
        return false;
    }
    let part = |start, end| {
        value
            .get(start..end)
            .and_then(|s| s.parse::<u32>().ok())
            .unwrap_or(0)
    };
    let (year, month, day) = (part(0, 4), part(5, 7), part(8, 10));
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let max_day = match month {
        4 | 6 | 9 | 11 => 30,
        2 if leap => 29,
        2 => 28,
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        _ => 0,
    };
    year >= 2020
        && day > 0
        && day <= max_day
        && part(11, 13) < 24
        && part(14, 16) < 60
        && part(17, 19) < 60
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    fn fixture() -> (
        Value,
        SigningKey,
        [TrustedReleasePublicKey; 1],
        [&'static str; 1],
    ) {
        let signing = SigningKey::from_bytes(&[7; 32]);
        let roots = [TrustedReleasePublicKey {
            key_id: "fixture",
            ed25519_public_key: signing.verifying_key().to_bytes(),
        }];
        let origins = ["chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"];
        let mut value = json::parse(br#"{"schema_version":1,"release":"2.12.0","channel":"stable","published_at":"2026-09-04T00:00:00Z","protocol":{"min":1,"max":1},"trust":{"min":1,"max":1},"minimum_secure_companion":"2.12.0","extension_origins":["chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"],"artifacts":[],"release_key_id":"fixture"}"#).unwrap();
        let matrix = [
            ("macos", "universal2", "installer"),
            ("macos", "universal2", "payload"),
            ("windows", "x86_64", "installer"),
            ("windows", "x86_64", "payload"),
            ("windows", "arm64", "installer"),
            ("windows", "arm64", "payload"),
            ("linux", "any", "bootstrap"),
            ("linux", "x86_64", "payload"),
            ("linux", "aarch64", "payload"),
        ];
        let entries = matrix.iter().map(|(platform, arch, kind)| {
            let name = format!("bridge-{platform}-{arch}-{kind}");
            let entry = format!(r#"{{"name":"{name}","platform":"{platform}","arch":"{arch}","kind":"{kind}","download_url":"https://example.com/v2.12.0/{name}","sha256":"{}","size":3}}"#, hex(&Sha256::digest(b"abc")));
            json::parse(entry.as_bytes()).unwrap()
        }).collect();
        if let Value::Object(root) = &mut value {
            root.insert("artifacts".into(), Value::Array(entries));
        }
        (value, signing, roots, origins)
    }

    #[test]
    fn exact_signature_complete_matrix_and_bounded_artifact_bytes() {
        let (value, signing, roots, origins) = fixture();
        let bytes = value.encode();
        let signature = signing.sign(bytes.as_bytes()).to_bytes();
        let catalog =
            verify_with_policy(bytes.as_bytes(), &signature, "2.11.0", &roots, &origins).unwrap();
        assert_eq!(catalog.release(), "2.12.0");
        let name = &catalog.artifacts()[0].name;
        assert!(catalog.verify_artifact(name, &mut &b"abc"[..]).is_ok());
        let mut staging = Vec::new();
        let proof = catalog
            .copy_verified_artifact(name, &mut &b"abc"[..], &mut staging)
            .unwrap();
        assert_eq!(staging, b"abc");
        assert_eq!(proof.catalog_digest(), catalog.digest());
        staging.clear();
        assert!(catalog
            .copy_verified_artifact(name, &mut &b"abcd"[..], &mut staging)
            .is_err());
        assert!(staging.is_empty());
        for data in [&b"ab"[..], &b"abcd"[..], &b"abd"[..]] {
            assert_eq!(
                catalog.verify_artifact(name, &mut &*data).unwrap_err(),
                CatalogError::InvalidArtifact
            );
        }
        assert_eq!(
            verify_catalog(bytes.as_bytes(), &signature, "2.12.0").unwrap_err(),
            CatalogError::Unavailable
        );
        assert_eq!(
            verify_with_policy(bytes.as_bytes(), &[0; 64], "2.12.0", &roots, &origins).unwrap_err(),
            CatalogError::InvalidSignature
        );
        assert_eq!(
            verify_with_policy(bytes.as_bytes(), &signature, "2.13.0", &roots, &origins)
                .unwrap_err(),
            CatalogError::Incompatible
        );
    }

    #[test]
    fn signed_noncanonical_duplicate_or_incomplete_catalog_cannot_authorize() {
        let (mut value, signing, roots, origins) = fixture();
        let spaced = format!("{}\n", value.encode());
        assert_eq!(
            verify_with_policy(
                spaced.as_bytes(),
                &signing.sign(spaced.as_bytes()).to_bytes(),
                "2.12.0",
                &roots,
                &origins
            )
            .unwrap_err(),
            CatalogError::InvalidCatalog
        );
        if let Value::Object(root) = &mut value {
            if let Some(Value::Array(artifacts)) = root.get_mut("artifacts") {
                artifacts[8] = artifacts[7].clone();
            }
        }
        let bytes = value.encode();
        assert_eq!(
            verify_with_policy(
                bytes.as_bytes(),
                &signing.sign(bytes.as_bytes()).to_bytes(),
                "2.12.0",
                &roots,
                &origins
            )
            .unwrap_err(),
            CatalogError::InvalidCatalog
        );
        assert!(!valid_date("2026-02-30T00:00:00Z"));
        assert!(valid_date("2028-02-29T00:00:00Z"));
    }

    #[test]
    fn catalog_acquisition_is_pinned_and_bounds_streams_without_network() {
        assert_eq!(
            fetch_catalog("2.12.0", "2.12.0").unwrap_err(),
            CatalogError::Unavailable
        );
        assert!(read_metadata(&mut &b"abc"[..], 3, Some(3)).is_ok());
        assert!(read_metadata(&mut &b"abcd"[..], 3, None).is_err());
        assert!(read_metadata(&mut &b"ab"[..], 3, Some(3)).is_err());
        assert!(read_metadata(&mut &b""[..], 3, None).is_err());
    }

    fn scoped(mut value: Value, platforms: &[&str]) -> Value {
        let Value::Object(root) = &mut value else {
            unreachable!();
        };
        root.insert("schema_version".into(), Value::Number("2".into()));
        root.insert(
            "platforms".into(),
            Value::Array(
                platforms
                    .iter()
                    .map(|value| Value::String((*value).into()))
                    .collect(),
            ),
        );
        let Some(Value::Array(artifacts)) = root.get_mut("artifacts") else {
            unreachable!();
        };
        artifacts.retain(|artifact| {
            artifact
                .as_object()
                .and_then(|entry| entry.get("platform"))
                .and_then(Value::as_str)
                .is_some_and(|platform| platforms.contains(&platform))
        });
        value
    }

    #[test]
    fn v2_signed_platform_unions_include_mac_only_without_undeclared_artifacts() {
        let (value, signing, roots, origins) = fixture();
        for (platforms, count) in [
            (vec!["macos"], 2),
            (vec!["linux"], 3),
            (vec!["windows"], 4),
            (vec!["linux", "macos"], 5),
            (vec!["macos", "windows"], 6),
            (vec!["linux", "windows"], 7),
            (vec!["linux", "macos", "windows"], 9),
        ] {
            let bytes = scoped(value.clone(), &platforms).encode();
            let catalog = verify_with_policy(
                bytes.as_bytes(),
                &signing.sign(bytes.as_bytes()).to_bytes(),
                "2.12.0",
                &roots,
                &origins,
            )
            .unwrap();
            assert_eq!(catalog.artifacts().len(), count);
            assert!(catalog
                .artifacts()
                .iter()
                .all(|artifact| platforms.contains(&artifact.platform.as_str())));
            if platforms == ["macos"] {
                assert!(catalog
                    .verify_artifact("bridge-macos-universal2-payload", &mut &b"abc"[..])
                    .is_ok());
                assert!(catalog
                    .verify_artifact("bridge-windows-x86_64-payload", &mut &b"abc"[..])
                    .is_err());
                assert!(catalog
                    .verify_artifact("bridge-macos-universal2-payload", &mut &b"abd"[..])
                    .is_err());
            }
        }
    }

    #[test]
    fn v2_rejects_partial_groups_wrong_declarations_and_v1_shape_changes() {
        let (value, signing, roots, origins) = fixture();
        let mac = scoped(value.clone(), &["macos"]);
        let mut cases = vec![
            scoped(value.clone(), &[]),
            scoped(value.clone(), &["macos", "macos"]),
            scoped(value.clone(), &["windows", "macos"]),
            scoped(value.clone(), &["unknown"]),
        ];
        for (key, replacement) in [
            ("platforms", Value::String("macos".into())),
            ("platforms", Value::Array(vec![Value::Number("1".into())])),
            (
                "platforms",
                Value::Array(vec![Value::String("windows".into())]),
            ),
            ("schema_version", Value::Number("1".into())),
        ] {
            let mut invalid = mac.clone();
            let Value::Object(root) = &mut invalid else {
                unreachable!();
            };
            root.insert(key.into(), replacement);
            cases.push(invalid);
        }
        for platforms in [vec!["macos"], vec!["windows"], vec!["linux"]] {
            let mut invalid = scoped(value.clone(), &platforms);
            let Value::Object(root) = &mut invalid else {
                unreachable!();
            };
            let Some(Value::Array(artifacts)) = root.get_mut("artifacts") else {
                unreachable!();
            };
            artifacts.pop();
            cases.push(invalid);
        }
        for mutation in 0..4 {
            let mut invalid = mac.clone();
            let Value::Object(root) = &mut invalid else {
                unreachable!();
            };
            match mutation {
                0 => {
                    root.remove("platforms");
                }
                1 => {
                    root.insert("unexpected".into(), Value::Bool(true));
                }
                2 => {
                    let Some(Value::Array(artifacts)) = root.get_mut("artifacts") else {
                        unreachable!();
                    };
                    artifacts[1] = artifacts[0].clone();
                }
                _ => {
                    root.insert("schema_version".into(), Value::Number("1".into()));
                    root.remove("platforms");
                }
            }
            cases.push(invalid);
        }
        for invalid in cases {
            let bytes = invalid.encode();
            assert!(
                verify_with_policy(
                    bytes.as_bytes(),
                    &signing.sign(bytes.as_bytes()).to_bytes(),
                    "2.12.0",
                    &roots,
                    &origins
                )
                .is_err(),
                "accepted {}",
                bytes
            );
        }
    }

    #[test]
    fn v2_scope_is_signed_and_does_not_relax_origins_or_security_floor() {
        let (value, signing, roots, origins) = fixture();
        let bytes = scoped(value, &["macos"]).encode();
        let signature = signing.sign(bytes.as_bytes()).to_bytes();
        for changed in [
            bytes.replace("\"platforms\":[\"macos\"]", "\"platforms\":[\"windows\"]"),
            bytes.replace("\"size\":3", "\"size\":4"),
        ] {
            assert_eq!(
                verify_with_policy(changed.as_bytes(), &signature, "2.12.0", &roots, &origins)
                    .unwrap_err(),
                CatalogError::InvalidSignature
            );
        }
        assert_eq!(
            verify_with_policy(bytes.as_bytes(), &[0; 64], "2.12.0", &roots, &origins).unwrap_err(),
            CatalogError::InvalidSignature
        );
        assert_eq!(
            verify_with_policy(bytes.as_bytes(), &signature, "2.13.0", &roots, &origins)
                .unwrap_err(),
            CatalogError::Incompatible
        );
        assert_eq!(
            verify_with_policy(
                bytes.as_bytes(),
                &signature,
                "2.12.0",
                &roots,
                &["chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/"]
            )
            .unwrap_err(),
            CatalogError::Incompatible
        );
    }
}
