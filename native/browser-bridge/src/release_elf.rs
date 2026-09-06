//! Bounded ELF64 little-endian release metadata reader. No execution or scanning.
//! ELF gABI section/program headers bind one allocated .a0_release section to
//! the signed executable's exact x86-64/AArch64 machine and read-only load map.
use super::{CompositionError, EmbeddedIdentity, ExpectedRelease};
use crate::{
    json::{self, Value},
    platform::Platform,
};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};

fn bad() -> CompositionError {
    CompositionError::EmbeddedIdentityRejected
}
fn u16le(bytes: &[u8]) -> u16 {
    u16::from_le_bytes(bytes.try_into().expect("bounded field"))
}
fn u32le(bytes: &[u8]) -> u32 {
    u32::from_le_bytes(bytes.try_into().expect("bounded field"))
}
fn u64le(bytes: &[u8]) -> u64 {
    u64::from_le_bytes(bytes.try_into().expect("bounded field"))
}
fn read(
    input: &mut (impl Read + Seek),
    offset: u64,
    size: usize,
    limit: u64,
) -> Result<Vec<u8>, CompositionError> {
    if size > 256 * 1024
        || offset
            .checked_add(size as u64)
            .is_none_or(|end| end > limit)
    {
        return Err(bad());
    }
    let mut bytes = vec![0; size];
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
        "x86_64" => 62,
        "aarch64" => 183,
        _ => return Err(bad()),
    };
    if expected.platform != Platform::Linux || size != expected.executable_size {
        return Err(bad());
    }
    let header = read(input, 0, 64, size)?;
    if &header[..7] != b"\x7fELF\x02\x01\x01"
        || ![0, 3].contains(&header[7])
        || header[8..16].iter().any(|byte| *byte != 0)
        || ![2, 3].contains(&u16le(&header[16..18]))
        || u16le(&header[18..20]) != machine
        || u32le(&header[20..24]) != 1
        || u32le(&header[48..52]) != 0
        || u16le(&header[52..54]) != 64
        || u16le(&header[54..56]) != 56
        || u16le(&header[58..60]) != 64
    {
        return Err(bad());
    }
    let phnum = u16le(&header[56..58]) as usize;
    let shnum = u16le(&header[60..62]) as usize;
    let strings_index = u16le(&header[62..64]) as usize;
    if !(1..=128).contains(&phnum)
        || !(3..=4096).contains(&shnum)
        || strings_index == 0
        || strings_index >= shnum
    {
        return Err(bad());
    }
    let programs = read(input, u64le(&header[32..40]), phnum * 56, size)?;
    let sections = read(input, u64le(&header[40..48]), shnum * 64, size)?;
    let strings_header = &sections[strings_index * 64..(strings_index + 1) * 64];
    let strings_size = u64le(&strings_header[32..40]);
    if u32le(&strings_header[4..8]) != 3 || strings_size == 0 || strings_size > 64 * 1024 {
        return Err(bad());
    }
    let strings = read(
        input,
        u64le(&strings_header[24..32]),
        strings_size as usize,
        size,
    )?;
    let mut metadata = None;
    for section in sections.chunks_exact(64) {
        let start = u32le(&section[..4]) as usize;
        let name = strings.get(start..).ok_or_else(bad)?;
        let end = name.iter().position(|byte| *byte == 0).ok_or_else(bad)?;
        if &name[..end] != b".a0_release" {
            continue;
        }
        let offset = u64le(&section[24..32]);
        let length = u64le(&section[32..40]);
        let address = u64le(&section[16..24]);
        if metadata.is_some()
            || u32le(&section[4..8]) != 1
            || u64le(&section[8..16]) != 2
            || length == 0
            || length > 2048
        {
            return Err(bad());
        }
        let mut mapped = 0;
        for segment in programs.chunks_exact(56) {
            if u32le(&segment[..4]) != 1 {
                continue;
            }
            let flags = u32le(&segment[4..8]);
            let file_offset = u64le(&segment[8..16]);
            let virtual_address = u64le(&segment[16..24]);
            let file_size = u64le(&segment[32..40]);
            let memory_size = u64le(&segment[40..48]);
            let file_end = file_offset
                .checked_add(file_size)
                .filter(|end| *end <= size)
                .ok_or_else(bad)?;
            if file_size > memory_size {
                return Err(bad());
            }
            if offset >= file_offset
                && offset
                    .checked_add(length)
                    .is_some_and(|end| end <= file_end)
            {
                if flags & 6 != 4
                    || virtual_address.checked_add(offset - file_offset) != Some(address)
                {
                    return Err(bad());
                }
                mapped += 1;
            }
        }
        if mapped != 1 {
            return Err(bad());
        }
        metadata = Some(read(input, offset, length as usize, size)?);
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
        ("platform", "linux"),
        ("self_test_contract", crate::SELF_TEST_CONTRACT),
        ("install_contract", crate::INSTALL_CONTRACT),
    ];
    if fields.len() != 9
        || exact
            .iter()
            .any(|(key, value)| fields.get(*key).and_then(Value::as_str) != Some(*value))
        || fields.get("schema_version").and_then(Value::as_u64) != Some(1)
        || fields.get("protocol_version").and_then(Value::as_u64)
            != Some(crate::rpc::CONTRACT_VERSION)
    {
        return Err(bad());
    }
    Ok(EmbeddedIdentity {
        version: expected.version.into(),
        platform: Platform::Linux,
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
    fn fixture(machine: u16) -> Vec<u8> {
        let metadata = crate::release_metadata::as_json()
            .replace("\"platform\":\"macos\"", "\"platform\":\"linux\"")
            .replace("\"platform\":\"windows\"", "\"platform\":\"linux\"");
        let mut bytes = vec![0; 2048];
        bytes[..7].copy_from_slice(b"\x7fELF\x02\x01\x01");
        for (offset, value) in [
            (16, 3u16),
            (18, machine),
            (52, 64),
            (54, 56),
            (56, 1),
            (58, 64),
            (60, 3),
            (62, 1),
        ] {
            bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
        }
        bytes[20..24].copy_from_slice(&1u32.to_le_bytes());
        for (offset, value) in [
            (32, 64u64),
            (40, 256),
            (72, 0),
            (80, 0x400000),
            (96, 2048),
            (104, 2048),
            (344, 512),
            (352, 27),
            (392, 2),
            (400, 0x400300),
            (408, 768),
            (416, metadata.len() as u64),
        ] {
            bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
        }
        for (offset, value) in [(64, 1u32), (68, 4), (320, 1), (324, 3), (384, 11), (388, 1)] {
            bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        }
        bytes[512..539].copy_from_slice(b"\0.shstrtab\0.a0_release\0xxx\0");
        bytes[768..768 + metadata.len()].copy_from_slice(metadata.as_bytes());
        bytes
    }
    fn expected(arch: &str) -> ExpectedRelease<'_> {
        ExpectedRelease {
            version: crate::COMPANION_VERSION,
            catalog_key_id: "fixture",
            catalog_sha256: "",
            archive_sha256: "",
            executable_sha256: "",
            executable_size: 2048,
            platform: Platform::Linux,
            artifact_arch: arch,
        }
    }
    #[test]
    fn elf_requires_exact_machine_and_one_readonly_mapped_metadata_section() {
        for (machine, arch) in [(62, "x86_64"), (183, "aarch64")] {
            let bytes = fixture(machine);
            assert!(parse(
                &mut std::io::Cursor::new(bytes.clone()),
                2048,
                &expected(arch)
            )
            .is_ok());
            for (offset, value) in [
                (4, 1),
                (5, 2),
                (18, 0),
                (68, 6),
                (388, 8),
                (392, 3),
                (408, 1),
            ] {
                let mut bad_bytes = bytes.clone();
                bad_bytes[offset] = value;
                assert!(
                    parse(&mut std::io::Cursor::new(bad_bytes), 2048, &expected(arch)).is_err(),
                    "offset {offset}"
                );
            }
        }
    }
    #[test]
    fn elf_rejects_missing_section_truncation_and_unbounded_tables() {
        let mut bytes = fixture(62);
        bytes[60..62].copy_from_slice(&u16::MAX.to_le_bytes());
        assert!(parse(&mut std::io::Cursor::new(bytes), 2048, &expected("x86_64")).is_err());
        assert!(parse(
            &mut std::io::Cursor::new(vec![0; 8]),
            2048,
            &expected("x86_64")
        )
        .is_err());
        let mut bytes = fixture(62);
        bytes[384] = 1;
        assert!(parse(&mut std::io::Cursor::new(bytes), 2048, &expected("x86_64")).is_err());
    }
}
