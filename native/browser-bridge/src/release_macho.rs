//! Bounded universal Mach-O embedded-release identity parsing without execution.

use std::collections::BTreeSet;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};

use super::{CompositionError, EmbeddedIdentity, ExpectedRelease};
use crate::json::{self, Value};
use crate::platform::Platform;

const ARM64: u32 = 0x0100_000c;
const X86_64: u32 = 0x0100_0007;
const MAX_COMMANDS: usize = 1024 * 1024;

fn invalid() -> CompositionError {
    CompositionError::EmbeddedIdentityRejected
}
fn be(bytes: &[u8]) -> u32 {
    u32::from_be_bytes(bytes.try_into().expect("bounded four-byte field"))
}
fn le(bytes: &[u8]) -> u32 {
    u32::from_le_bytes(bytes.try_into().expect("bounded four-byte field"))
}
fn le64(bytes: &[u8]) -> u64 {
    u64::from_le_bytes(bytes.try_into().expect("bounded eight-byte field"))
}

fn read(
    file: &mut File,
    offset: u64,
    size: usize,
    limit: u64,
) -> Result<Vec<u8>, CompositionError> {
    if size > MAX_COMMANDS
        || offset
            .checked_add(size as u64)
            .filter(|end| *end <= limit)
            .is_none()
    {
        return Err(invalid());
    }
    let mut bytes = vec![0; size];
    file.seek(SeekFrom::Start(offset)).map_err(|_| invalid())?;
    file.read_exact(&mut bytes).map_err(|_| invalid())?;
    Ok(bytes)
}

pub(super) fn embedded_identity(
    file: &mut File,
    expected: &ExpectedRelease<'_>,
) -> Result<EmbeddedIdentity, CompositionError> {
    let limit = file.metadata().map_err(|_| invalid())?.len();
    if expected.platform != Platform::Macos
        || expected.artifact_arch != "universal2"
        || limit != expected.executable_size
    {
        return Err(invalid());
    }
    let header = read(file, 0, 48, limit)?;
    // The v1 <=512MiB archive profile uses ordinary big-endian FAT_MAGIC with
    // precisely the Intel and Apple Silicon executable slices. No thin binary
    // may self-report that it is a universal release.
    if be(&header[0..4]) != 0xcafe_babe || be(&header[4..8]) != 2 {
        return Err(invalid());
    }
    let mut architectures = BTreeSet::new();
    let mut ranges = Vec::new();
    let mut metadata = None;
    for record in header[8..48].chunks_exact(20) {
        let cpu = be(&record[0..4]);
        let offset = be(&record[8..12]) as u64;
        let size = be(&record[12..16]) as u64;
        let alignment = be(&record[16..20]);
        if ![ARM64, X86_64].contains(&cpu)
            || !architectures.insert(cpu)
            || alignment > 31
            || offset < 48
            || offset % (1u64 << alignment) != 0
            || offset
                .checked_add(size)
                .filter(|end| *end <= limit)
                .is_none()
            || size < 32
        {
            return Err(invalid());
        }
        ranges.push((offset, offset + size));
        let slice_metadata = slice_metadata(file, offset, size, cpu)?;
        if metadata
            .as_ref()
            .is_some_and(|previous| previous != &slice_metadata)
        {
            return Err(invalid());
        }
        metadata = Some(slice_metadata);
    }
    ranges.sort_unstable();
    if ranges[0].1 > ranges[1].0 {
        return Err(invalid());
    }
    validate_metadata(&metadata.ok_or_else(invalid)?, expected)
}

pub(super) fn validate_metadata(
    bytes: &[u8],
    expected: &ExpectedRelease<'_>,
) -> Result<EmbeddedIdentity, CompositionError> {
    if bytes.len() > 2048 || !bytes.is_ascii() {
        return Err(invalid());
    }
    let value = json::parse(bytes).map_err(|_| invalid())?;
    let object = value.as_object().ok_or_else(invalid)?;
    let fields = [
        ("contract", "a0.browser-bridge.release-metadata.v1"),
        ("channel", "stable"),
        ("companion_version", expected.version),
        ("native_host", "io.agentzero.browser_bridge"),
        ("platform", "macos"),
        ("self_test_contract", crate::SELF_TEST_CONTRACT),
        ("install_contract", crate::INSTALL_CONTRACT),
    ];
    if object.len() != 9
        || fields
            .iter()
            .any(|(key, expected)| object.get(*key).and_then(Value::as_str) != Some(*expected))
        || object.get("schema_version").and_then(Value::as_u64) != Some(1)
        || object.get("protocol_version").and_then(Value::as_u64)
            != Some(crate::rpc::CONTRACT_VERSION)
    {
        return Err(invalid());
    }
    Ok(EmbeddedIdentity {
        version: expected.version.to_owned(),
        platform: Platform::Macos,
        artifact_arch: "universal2".to_owned(),
        native_host: "io.agentzero.browser_bridge".to_owned(),
        protocol_version: crate::rpc::CONTRACT_VERSION,
        self_test_contract: crate::SELF_TEST_CONTRACT.to_owned(),
        install_contract: crate::INSTALL_CONTRACT.to_owned(),
    })
}

fn slice_metadata(
    file: &mut File,
    offset: u64,
    size: u64,
    cpu: u32,
) -> Result<Vec<u8>, CompositionError> {
    let limit = offset + size;
    let header = read(file, offset, 32, limit)?;
    let commands_size = le(&header[20..24]) as usize;
    let command_count = le(&header[16..20]) as usize;
    if le(&header[0..4]) != 0xfeed_facf
        || le(&header[4..8]) != cpu
        || le(&header[12..16]) != 2
        || command_count == 0
        || command_count > 4096
    {
        return Err(invalid());
    }
    let commands = read(file, offset + 32, commands_size, limit)?;
    let mut position = 0usize;
    let mut result = None;
    for _ in 0..command_count {
        let command_header = commands.get(position..position + 8).ok_or_else(invalid)?;
        let length = le(&command_header[4..8]) as usize;
        if length < 8 || length % 8 != 0 {
            return Err(invalid());
        }
        let command = commands
            .get(position..position.checked_add(length).ok_or_else(invalid)?)
            .ok_or_else(invalid)?;
        if le(&command[0..4]) == 0x19 {
            if command.len() < 72 {
                return Err(invalid());
            }
            let sections = le(&command[64..68]) as usize;
            if 72 + sections.checked_mul(80).ok_or_else(invalid)? != command.len() {
                return Err(invalid());
            }
            let segment_start = le64(&command[40..48]);
            let segment_end = segment_start
                .checked_add(le64(&command[48..56]))
                .ok_or_else(invalid)?;
            for section in command[72..].chunks_exact(80) {
                if fixed_name(&section[0..16]) == Some("__a0_release") {
                    let section_offset = le(&section[48..52]) as u64;
                    let section_size = le64(&section[40..48]);
                    if result.is_some()
                        || fixed_name(&section[16..32]) != Some("__TEXT")
                        || fixed_name(&command[8..24]) != Some("__TEXT")
                        || section_size == 0
                        || section_size > 2048
                        || section_offset < segment_start
                        || section_offset
                            .checked_add(section_size)
                            .filter(|end| *end <= segment_end && *end <= size)
                            .is_none()
                    {
                        return Err(invalid());
                    }
                    result = Some(read(
                        file,
                        offset + section_offset,
                        section_size as usize,
                        limit,
                    )?);
                }
            }
        }
        position += length;
    }
    if position != commands.len() {
        return Err(invalid());
    }
    result.ok_or_else(invalid)
}

fn fixed_name(bytes: &[u8]) -> Option<&str> {
    let end = bytes
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(bytes.len());
    if bytes[end..].iter().any(|byte| *byte != 0) {
        return None;
    }
    std::str::from_utf8(&bytes[..end]).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn universal_metadata_reads_both_slices_and_rejects_a_forged_target() {
        let metadata = crate::release_metadata::as_json()
            .replace("local-development", "stable")
            .replace("browser_bridge.dev", "browser_bridge")
            .replace("\"platform\":\"linux\"", "\"platform\":\"macos\"")
            .replace("\"platform\":\"windows\"", "\"platform\":\"macos\"");
        let mut bytes = vec![0; 8192];
        bytes[0..4].copy_from_slice(&0xcafe_babe_u32.to_be_bytes());
        bytes[4..8].copy_from_slice(&2u32.to_be_bytes());
        for (index, cpu) in [X86_64, ARM64].into_iter().enumerate() {
            let record = 8 + index * 20;
            let start = 4096 * (index + 1);
            bytes.resize(start + 4096, 0);
            bytes[record..record + 4].copy_from_slice(&cpu.to_be_bytes());
            bytes[record + 8..record + 12].copy_from_slice(&(start as u32).to_be_bytes());
            bytes[record + 12..record + 16].copy_from_slice(&4096u32.to_be_bytes());
            bytes[record + 16..record + 20].copy_from_slice(&12u32.to_be_bytes());
            bytes[start..start + 4].copy_from_slice(&0xfeed_facf_u32.to_le_bytes());
            bytes[start + 4..start + 8].copy_from_slice(&cpu.to_le_bytes());
            bytes[start + 12..start + 16].copy_from_slice(&2u32.to_le_bytes());
            bytes[start + 16..start + 20].copy_from_slice(&1u32.to_le_bytes());
            bytes[start + 20..start + 24].copy_from_slice(&152u32.to_le_bytes());
            let command = start + 32;
            bytes[command..command + 4].copy_from_slice(&0x19u32.to_le_bytes());
            bytes[command + 4..command + 8].copy_from_slice(&152u32.to_le_bytes());
            bytes[command + 8..command + 14].copy_from_slice(b"__TEXT");
            bytes[command + 48..command + 56].copy_from_slice(&4096u64.to_le_bytes());
            bytes[command + 64..command + 68].copy_from_slice(&1u32.to_le_bytes());
            let section = command + 72;
            bytes[section..section + 12].copy_from_slice(b"__a0_release");
            bytes[section + 16..section + 22].copy_from_slice(b"__TEXT");
            bytes[section + 40..section + 48]
                .copy_from_slice(&(metadata.len() as u64).to_le_bytes());
            bytes[section + 48..section + 52].copy_from_slice(&256u32.to_le_bytes());
            bytes[start + 256..start + 256 + metadata.len()].copy_from_slice(metadata.as_bytes());
        }
        let path = std::env::temp_dir().join(format!("a0-macho-fixture-{}", std::process::id()));
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        file.write_all(&bytes).unwrap();
        let expected = ExpectedRelease {
            version: crate::COMPANION_VERSION,
            catalog_key_id: "fixture",
            catalog_sha256: "",
            archive_sha256: "",
            executable_sha256: "",
            executable_size: bytes.len() as u64,
            platform: Platform::Macos,
            artifact_arch: "universal2",
        };
        assert!(embedded_identity(&mut file, &expected).is_ok());
        file.seek(SeekFrom::Start(8)).unwrap();
        file.write_all(&ARM64.to_be_bytes()).unwrap();
        assert!(embedded_identity(&mut file, &expected).is_err());
        drop(file);
        std::fs::remove_file(path).unwrap();
    }
}
