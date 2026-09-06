pub mod artifact;
mod artifact_codec;
pub mod cli;
mod connector_codec;
mod context_codec;
mod core_connector;
pub mod development;
#[cfg(feature = "local-development")]
mod development_session;
pub mod diagnostics;
mod event_codec;
pub mod install;
pub mod json;
pub mod manifest;
pub mod native_host;
pub mod pairing;
pub mod platform;
pub mod registry;
pub mod release;
pub mod release_metadata;
pub mod release_payload;
pub mod rpc;
mod runtime_handshake;
pub mod session;
mod transport_profile;

pub const COMPANION_VERSION: &str = env!("CARGO_PKG_VERSION");
#[cfg(not(feature = "local-development"))]
pub const NATIVE_HOST_NAME: &str = "io.agentzero.browser_bridge";
#[cfg(feature = "local-development")]
pub const NATIVE_HOST_NAME: &str = DEVELOPMENT_NATIVE_HOST_NAME;

pub const DEVELOPMENT_CHANNEL: &str = "local-development";
pub const DEVELOPMENT_NATIVE_HOST_NAME: &str = "io.agentzero.browser_bridge.dev";
pub const DEVELOPMENT_EXTENSION_ID: &str = "paoagmddepkmonpeboobaijlenlcokpc";
pub const DEVELOPMENT_EXTENSION_ORIGIN: &str =
    "chrome-extension://paoagmddepkmonpeboobaijlenlcokpc/";
pub const DEVELOPMENT_TRUST_CONTRACT: &str = "a0.browser-bridge.development-trust.v1";
pub const DEVELOPMENT_RESULT_CONTRACT: &str = "a0.browser-bridge.development.v1";

pub const STATUS_CONTRACT: &str = "a0.browser-bridge.status.v1";
pub const SELF_TEST_CONTRACT: &str = "a0.browser-bridge.self-test.v1";
pub const INSTALL_CONTRACT: &str = "a0.browser-bridge.install.v1";
pub const INSTALL_PLAN_CONTRACT: &str = "a0.browser-bridge.install-plan.v1";

pub const EXIT_OK: u8 = 0;
pub const EXIT_USAGE: u8 = 2;
pub const EXIT_NOT_INSTALLED: u8 = 3;
pub const EXIT_INTEGRITY_OR_POLICY: u8 = 5;
pub const EXIT_PARTIAL: u8 = 6;
pub const EXIT_RELEASE_UNAVAILABLE: u8 = 7;
