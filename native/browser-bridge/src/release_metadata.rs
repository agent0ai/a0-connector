//! Release identity embedded in every executable slice, readable without execution.

macro_rules! metadata {
    ($platform:literal, $channel:literal, $host:literal) => {
        concat!(
            "{\"channel\":\"",
            $channel,
            "\",\"companion_version\":\"",
            env!("CARGO_PKG_VERSION"),
            "\",\"contract\":\"a0.browser-bridge.release-metadata.v1",
            "\",\"install_contract\":\"a0.browser-bridge.install.v1",
            "\",\"native_host\":\"",
            $host,
            "\",\"platform\":\"",
            $platform,
            "\",\"protocol_version\":1,\"schema_version\":1,",
            "\"self_test_contract\":\"a0.browser-bridge.self-test.v1\"}"
        )
    };
}

#[cfg(all(target_os = "macos", not(feature = "local-development")))]
const JSON: &str = metadata!("macos", "stable", "io.agentzero.browser_bridge");
#[cfg(all(target_os = "macos", feature = "local-development"))]
const JSON: &str = metadata!(
    "macos",
    "local-development",
    "io.agentzero.browser_bridge.dev"
);
#[cfg(all(target_os = "linux", not(feature = "local-development")))]
const JSON: &str = metadata!("linux", "stable", "io.agentzero.browser_bridge");
#[cfg(all(target_os = "linux", feature = "local-development"))]
const JSON: &str = metadata!(
    "linux",
    "local-development",
    "io.agentzero.browser_bridge.dev"
);
#[cfg(all(target_os = "windows", not(feature = "local-development")))]
const JSON: &str = metadata!("windows", "stable", "io.agentzero.browser_bridge");
#[cfg(all(target_os = "windows", feature = "local-development"))]
const JSON: &str = metadata!(
    "windows",
    "local-development",
    "io.agentzero.browser_bridge.dev"
);
#[cfg(not(any(target_os = "macos", target_os = "linux", target_os = "windows")))]
const JSON: &str = metadata!("unsupported", "unavailable", "unavailable");

const fn literal_bytes<const N: usize>(text: &str) -> [u8; N] {
    let mut bytes = [0; N];
    let mut index = 0;
    while index < N {
        bytes[index] = text.as_bytes()[index];
        index += 1;
    }
    bytes
}

#[used]
#[cfg_attr(target_os = "macos", link_section = "__TEXT,__a0_release")]
#[cfg_attr(target_os = "linux", link_section = ".a0_release")]
static EMBEDDED_METADATA: [u8; JSON.len()] = literal_bytes(JSON);

pub fn as_json() -> &'static str {
    std::str::from_utf8(&EMBEDDED_METADATA).expect("embedded metadata is a UTF-8 literal")
}
