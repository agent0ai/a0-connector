//! Bounded PE32+ metadata reader; this never executes or authenticates a file.
use super::{CompositionError, EmbeddedIdentity, ExpectedRelease};
use crate::{json, platform::Platform};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};

fn bad() -> CompositionError {
    CompositionError::EmbeddedIdentityRejected
}
fn word(bytes: &[u8]) -> u16 {
    u16::from_le_bytes(bytes.try_into().expect("bounded PE field"))
}
fn dword(bytes: &[u8]) -> u64 {
    u32::from_le_bytes(bytes.try_into().expect("bounded PE field")) as u64
}
fn read(
    input: &mut (impl Read + Seek),
    offset: u64,
    length: usize,
    size: u64,
) -> Result<Vec<u8>, CompositionError> {
    if length > 4096
        || offset
            .checked_add(length as u64)
            .is_none_or(|end| end > size)
    {
        return Err(bad());
    }
    let mut bytes = vec![0; length];
    input.seek(SeekFrom::Start(offset)).map_err(|_| bad())?;
    input.read_exact(&mut bytes).map_err(|_| bad())?;
    Ok(bytes)
}

pub(super) fn embedded_identity(
    file: &mut File,
    expected: &ExpectedRelease<'_>,
) -> Result<EmbeddedIdentity, CompositionError> {
    let size = file.metadata().map_err(|_| bad())?.len();
    parse(file, size, expected)
}

fn parse(
    input: &mut (impl Read + Seek),
    size: u64,
    expected: &ExpectedRelease<'_>,
) -> Result<EmbeddedIdentity, CompositionError> {
    let machine = match expected.artifact_arch {
        "x86_64" => 0x8664,
        "arm64" => 0xaa64,
        _ => return Err(bad()),
    };
    if expected.platform != Platform::Windows || size != expected.executable_size {
        return Err(bad());
    }
    let dos = read(input, 0, 64, size)?;
    let pe = dword(&dos[60..64]);
    if &dos[..2] != b"MZ" || !(64..=1024 * 1024).contains(&pe) {
        return Err(bad());
    }
    let header = read(input, pe, 24, size)?;
    let count = word(&header[6..8]) as usize;
    let flags = word(&header[22..24]);
    if &header[..4] != b"PE\0\0"
        || word(&header[4..6]) != machine
        || !(1..=96).contains(&count)
        || word(&header[20..22]) != 240
        || flags & 0x2002 != 2
    {
        return Err(bad());
    }
    let optional = read(input, pe + 24, 240, size)?;
    let headers_size = dword(&optional[60..64]);
    let image_size = dword(&optional[56..60]);
    let alignment = dword(&optional[32..36]);
    let file_alignment = dword(&optional[36..40]);
    if word(&optional[..2]) != 0x20b
        || word(&optional[68..70]) != 3
        || dword(&optional[108..112]) != 16
        || alignment < 4096
        || !alignment.is_power_of_two()
        || !file_alignment.is_power_of_two()
        || !(512..=65536).contains(&file_alignment)
        || alignment < file_alignment
        || headers_size < pe + 264 + count as u64 * 40
        || headers_size > size
        || headers_size % file_alignment != 0
        || image_size % alignment != 0
    {
        return Err(bad());
    }
    let sections = read(input, pe + 264, count * 40, size)?;
    let mut raw_ranges = Vec::new();
    let mut virtual_ranges = Vec::new();
    let mut metadata = None;
    for section in sections.chunks_exact(40) {
        let virtual_size = dword(&section[8..12]);
        let address = dword(&section[12..16]);
        let length = dword(&section[16..20]);
        let offset = dword(&section[20..24]);
        let flags = dword(&section[36..40]);
        let end = offset
            .checked_add(length)
            .filter(|end| *end <= size)
            .ok_or_else(bad)?;
        let virtual_end = address
            .checked_add(virtual_size.max(length))
            .filter(|end| *end <= image_size)
            .ok_or_else(bad)?;
        if address < headers_size
            || address % alignment != 0
            || virtual_size == 0
            || virtual_ranges
                .iter()
                .any(|&(start, end)| address < end && virtual_end > start)
        {
            return Err(bad());
        }
        virtual_ranges.push((address, virtual_end));
        if length > 0 {
            if offset < headers_size
                || offset % file_alignment != 0
                || length % file_alignment != 0
                || raw_ranges
                    .iter()
                    .any(|&(start, end)| offset < end && offset + length > start)
            {
                return Err(bad());
            }
            raw_ranges.push((offset, end));
        }
        if &section[..8] != b".a0meta\0" {
            continue;
        }
        // One readable initialized-data section, never writable/executable/discardable.
        if metadata.is_some()
            || flags & 0xe20000e0 != 0x40000040
            || !(1..=2048).contains(&virtual_size)
            || length < virtual_size
            || length > 4096
        {
            return Err(bad());
        }
        let bytes = read(input, offset, length as usize, size)?;
        if bytes[virtual_size as usize..].iter().any(|byte| *byte != 0) {
            return Err(bad());
        }
        metadata = Some(bytes[..virtual_size as usize].to_vec());
    }
    let certificate_offset = dword(&optional[144..148]);
    let certificate_size = dword(&optional[148..152]);
    if certificate_offset != 0 || certificate_size != 0 {
        let end = certificate_offset
            .checked_add(certificate_size)
            .ok_or_else(bad)?;
        if certificate_offset % 8 != 0
            || certificate_size < 8
            || end != size
            || certificate_offset < headers_size
            || raw_ranges.iter().any(|&(_, end)| end > certificate_offset)
        {
            return Err(bad());
        }
    }
    let bytes = metadata.ok_or_else(bad)?;
    if !bytes.is_ascii() {
        return Err(bad());
    }
    let value = json::parse(&bytes).map_err(|_| bad())?;
    let fields = value.as_object().ok_or_else(bad)?;
    let exact = [
        ("contract", "a0.browser-bridge.release-metadata.v1"),
        ("channel", "stable"),
        ("companion_version", expected.version),
        ("native_host", "io.agentzero.browser_bridge"),
        ("platform", "windows"),
        ("self_test_contract", crate::SELF_TEST_CONTRACT),
        ("install_contract", crate::INSTALL_CONTRACT),
    ];
    if fields.len() != 9
        || exact
            .iter()
            .any(|(key, value)| fields.get(*key).and_then(json::Value::as_str) != Some(*value))
        || fields.get("schema_version").and_then(json::Value::as_u64) != Some(1)
        || fields.get("protocol_version").and_then(json::Value::as_u64)
            != Some(crate::rpc::CONTRACT_VERSION)
    {
        return Err(bad());
    }
    Ok(EmbeddedIdentity {
        version: expected.version.into(),
        platform: Platform::Windows,
        artifact_arch: expected.artifact_arch.into(),
        native_host: "io.agentzero.browser_bridge".into(),
        protocol_version: crate::rpc::CONTRACT_VERSION,
        self_test_contract: crate::SELF_TEST_CONTRACT.into(),
        install_contract: crate::INSTALL_CONTRACT.into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    fn fixture(machine: u16) -> Vec<u8> {
        let metadata = crate::release_metadata::as_json()
            .replace("local-development", "stable")
            .replace("browser_bridge.dev", "browser_bridge")
            .replace("\"linux\"", "\"windows\"")
            .replace("\"macos\"", "\"windows\"");
        let mut bytes = vec![0; 1024];
        bytes[..2].copy_from_slice(b"MZ");
        bytes[128..132].copy_from_slice(b"PE\0\0");
        for (offset, value) in [
            (132, machine),
            (134, 1),
            (148, 240),
            (150, 2),
            (152, 0x20b),
            (220, 3),
        ] {
            bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
        }
        for (offset, value) in [
            (60, 128u32),
            (184, 4096),
            (188, 512),
            (208, 8192),
            (212, 512),
            (260, 16),
            (400, metadata.len() as u32),
            (404, 4096),
            (408, 512),
            (412, 512),
            (428, 0x40000040),
        ] {
            bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        }
        bytes[392..400].copy_from_slice(b".a0meta\0");
        bytes[512..512 + metadata.len()].copy_from_slice(metadata.as_bytes());
        bytes
    }
    fn expected(arch: &str) -> ExpectedRelease<'_> {
        ExpectedRelease {
            version: crate::COMPANION_VERSION,
            catalog_key_id: "fixture",
            catalog_sha256: "",
            archive_sha256: "",
            executable_sha256: "",
            executable_size: 1024,
            platform: Platform::Windows,
            artifact_arch: arch,
        }
    }
    #[test]
    fn pe_binds_machine_readonly_section_and_exact_metadata() {
        for (machine, arch) in [(0x8664, "x86_64"), (0xaa64, "arm64")] {
            let bytes = fixture(machine);
            assert!(parse(&mut Cursor::new(&bytes), 1024, &expected(arch)).is_ok());
            for (offset, value) in [
                (132, 0u8),
                (152, 0),
                (220, 2),
                (392, b'x'),
                (431, 0xc0),
                (400, 0),
                (412, 1),
                (188, 1),
                (260, 0),
                (1023, 1),
                (512, b'['),
            ] {
                let mut bad = bytes.clone();
                bad[offset] = value;
                assert!(
                    parse(&mut Cursor::new(bad), 1024, &expected(arch)).is_err(),
                    "offset {offset}"
                );
            }
            assert!(parse(&mut Cursor::new(&bytes), 1023, &expected(arch)).is_err());
            let other = if arch == "arm64" { "x86_64" } else { "arm64" };
            assert!(parse(&mut Cursor::new(&bytes), 1024, &expected(other)).is_err());
        }
    }

    #[cfg(all(target_os = "windows", not(feature = "local-development")))]
    #[test]
    fn native_linker_preserves_the_exact_readonly_release_section() {
        let mut file = File::open(std::env::current_exe().unwrap()).unwrap();
        let size = file.metadata().unwrap().len();
        let arch = if cfg!(target_arch = "aarch64") {
            "arm64"
        } else {
            "x86_64"
        };
        let mut expected = expected(arch);
        expected.executable_size = size;
        let identity = embedded_identity(&mut file, &expected).unwrap();
        assert_eq!(identity.version, crate::COMPANION_VERSION);
    }
}
