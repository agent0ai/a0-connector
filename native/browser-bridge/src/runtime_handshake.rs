//! Exact, authority-preserving Core runtime admission handshake.
//!
//! Extension and companion metadata are capability claims, not authority.
//! Authority comes from the validated native invocation, the profile-scoped
//! credential used for the signed proof, the authenticated Socket.IO namespace
//! SID, and the complete Core-owned activation attestation parsed here.

use std::collections::{BTreeMap, BTreeSet};

use crate::artifact::{ArtifactError, ArtifactRoute};
use crate::json::Value;
use crate::native_host::NativeInvocation;
use crate::pairing::CredentialRecord;
use crate::platform::{architecture, Platform};
use crate::release::catalog::MINIMUM_SECURE_COMPANION;
use crate::rpc::{self, valid_opaque_id};
use crate::COMPANION_VERSION;

pub(crate) const CORE_HELLO_ACK_ID: u64 = 1;
pub(crate) const CORE_HELLO_CORRELATION: &str = "bridge-hello";
pub(crate) const CORE_PROTOCOL: &str = "a0-connector.v1";
pub(crate) const HANDLER_ID: &str = crate::transport_profile::PRODUCTION_HANDLER_ID;
pub(crate) const MINIMUM_CHROMIUM_MAJOR: u64 = 120;

pub(crate) const OUTER_FEATURES: &[&str] = &[
    "browser_extension_bridge_v1",
    "connector_browser_artifact_chunks",
    "connector_browser_control",
    "connector_browser_event",
];
pub(crate) const PROVEN_ACTIONS: &[&str] = &[
    "click",
    "content",
    "ensure",
    "hover",
    "list",
    "navigate",
    "open",
    "screenshot",
    "scroll",
    "state",
    "status",
    "type",
    "upload_file",
];
pub(crate) const PROVEN_FEATURES: &[&str] = &[
    "artifacts_v1",
    "cursor_v1",
    "screenshots_v1",
    "semantic_dom_v1",
    "tab_groups_v1",
    "tab_leases_v1",
    "trusted_input_v1",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum HandshakeError {
    InvalidExtensionHello,
    UnsupportedVersion,
    UnsupportedCapability,
    CredentialMismatch,
    InvalidCorePacket,
    CoreDenied,
    CoreBindingMismatch,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ExtensionRuntimeHello {
    extension_id: String,
    extension_version: String,
    install_instance_id: String,
    load_generation_id: String,
    browser_family: String,
    browser_version: String,
    browser_label: String,
    actions: Vec<String>,
    features: Vec<String>,
}

impl ExtensionRuntimeHello {
    pub(crate) fn from_validated_invocation(
        invocation: &NativeInvocation,
        params: &Value,
    ) -> Result<Self, HandshakeError> {
        let root = exact_object(
            params,
            &[
                "browser",
                "capabilities",
                "contract",
                "extension",
                "protocol",
                "resume",
            ],
        )?;
        if string(root, "protocol", 64)? != rpc::BRIDGE_PROTOCOL {
            return Err(HandshakeError::InvalidExtensionHello);
        }
        let contract = exact_object_value(root, "contract", &["max", "min"])?;
        if integer(contract, "min")? != rpc::CONTRACT_VERSION
            || integer(contract, "max")? != rpc::CONTRACT_VERSION
        {
            return Err(HandshakeError::UnsupportedVersion);
        }

        let extension = exact_object_value(
            root,
            "extension",
            &[
                "id",
                "install_instance_id",
                "load_generation_id",
                "manifest_version",
                "version",
            ],
        )?;
        let extension_id = string(extension, "id", 32)?;
        let caller_extension_id = rpc::extension_id_from_origin(invocation.caller_origin())
            .ok_or(HandshakeError::InvalidExtensionHello)?;
        if extension_id != caller_extension_id || integer(extension, "manifest_version")? != 3 {
            return Err(HandshakeError::CredentialMismatch);
        }
        let extension_version = string(extension, "version", 64)?;
        if !valid_semver(extension_version) {
            return Err(HandshakeError::UnsupportedVersion);
        }
        let install_instance_id = opaque(extension, "install_instance_id")?;
        let load_generation_id = opaque(extension, "load_generation_id")?;

        let browser = exact_object_value(root, "browser", &["family", "version"])?;
        let browser_family = string(browser, "family", 64)?;
        let browser_label = match browser_family {
            "brave" => "Brave — Agent Zero Extension",
            "chrome" => "Chrome — Agent Zero Extension",
            "chromium" => "Chromium — Agent Zero Extension",
            "edge" => "Edge — Agent Zero Extension",
            "opera" => "Opera — Agent Zero Extension",
            "vivaldi" => "Vivaldi — Agent Zero Extension",
            _ => return Err(HandshakeError::UnsupportedVersion),
        };
        let browser_version = string(browser, "version", 64)?;
        if browser_major(browser_version).is_none_or(|major| major < MINIMUM_CHROMIUM_MAJOR) {
            return Err(HandshakeError::UnsupportedVersion);
        }

        let capabilities = exact_object_value(
            root,
            "capabilities",
            &["actions", "cdp_domains", "features"],
        )?;
        let actions = capability_set(capabilities, "actions", PROVEN_ACTIONS)?;
        let features = capability_set(capabilities, "features", PROVEN_FEATURES)?;
        let cdp_domains = array(capabilities, "cdp_domains")?;
        if !cdp_domains.is_empty() {
            return Err(HandshakeError::UnsupportedCapability);
        }

        validate_resume(exact_object_value(
            root,
            "resume",
            &["event_cursors", "inflight_op_ids", "lease_digest"],
        )?)?;
        Ok(Self {
            extension_id: extension_id.to_owned(),
            extension_version: extension_version.to_owned(),
            install_instance_id: install_instance_id.to_owned(),
            load_generation_id: load_generation_id.to_owned(),
            browser_family: browser_family.to_owned(),
            browser_version: browser_version.to_owned(),
            browser_label: browser_label.to_owned(),
            actions,
            features,
        })
    }

    pub(crate) fn install_instance_id(&self) -> &str {
        &self.install_instance_id
    }

    pub(crate) fn extension_id(&self) -> &str {
        &self.extension_id
    }

    pub(crate) fn extension_version(&self) -> &str {
        &self.extension_version
    }

    pub(crate) fn load_generation_id(&self) -> &str {
        &self.load_generation_id
    }

    pub(crate) fn browser_family(&self) -> &str {
        &self.browser_family
    }

    pub(crate) fn browser_version(&self) -> &str {
        &self.browser_version
    }

    pub(crate) fn browser_label(&self) -> &str {
        &self.browser_label
    }

    pub(crate) fn supports_actions(&self, required: &[&str]) -> bool {
        required.iter().all(|action| {
            self.actions
                .binary_search_by(|value| value.as_str().cmp(action))
                .is_ok()
        })
    }

    pub(crate) fn supports_features(&self, required: &[&str]) -> bool {
        required.iter().all(|feature| {
            self.features
                .binary_search_by(|value| value.as_str().cmp(feature))
                .is_ok()
        })
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct CredentialRuntimeBinding {
    bridge_id: String,
    server_instance_id: String,
    extension_id: String,
    install_instance_id: String,
    companion_instance_id: String,
    key_generation: u32,
}

impl CredentialRuntimeBinding {
    pub(crate) fn from_credential(
        credential: &CredentialRecord,
        hello: &ExtensionRuntimeHello,
    ) -> Result<Self, HandshakeError> {
        if credential.extension_id != hello.extension_id
            || credential.install_instance_id != hello.install_instance_id
            || !valid_opaque_id(&credential.bridge_id)
            || !valid_opaque_id(&credential.server_instance_id)
            || !valid_opaque_id(credential.companion_instance_id())
            || credential.key_generation() == 0
            || !semver_at_least(COMPANION_VERSION, MINIMUM_SECURE_COMPANION)
        {
            return Err(HandshakeError::CredentialMismatch);
        }
        Ok(Self {
            bridge_id: credential.bridge_id.clone(),
            server_instance_id: credential.server_instance_id.clone(),
            extension_id: credential.extension_id.clone(),
            install_instance_id: credential.install_instance_id.clone(),
            companion_instance_id: credential.companion_instance_id().to_owned(),
            key_generation: credential.key_generation(),
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct CoreHelloExpectation {
    hello: ExtensionRuntimeHello,
    credential: CredentialRuntimeBinding,
    data: Value,
    correlation: String,
}

impl CoreHelloExpectation {
    pub(crate) fn new(hello: ExtensionRuntimeHello, credential: CredentialRuntimeBinding) -> Self {
        let data = core_hello_data(&hello, &credential);
        Self {
            hello,
            credential,
            data,
            correlation: CORE_HELLO_CORRELATION.to_owned(),
        }
    }

    pub(crate) fn packet(&self) -> String {
        format!(
            "42/ws,{CORE_HELLO_ACK_ID}{}",
            Value::Array(vec![
                text("connector_hello"),
                object(&[
                    ("correlationId", text(&self.correlation)),
                    ("data", self.data.clone()),
                ]),
            ])
            .encode()
        )
    }

    pub(crate) fn renew_correlation(&mut self, sequence: u64) {
        self.correlation = format!("bridge-refresh-{sequence}");
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct RuntimeActivationAttestation {
    rollout: String,
    server_features: Vec<String>,
}

impl RuntimeActivationAttestation {
    pub(crate) fn native_value(&self, route: &AdmittedRuntimeRoute) -> Value {
        object(&[
            ("principal", text("browser_bridge")),
            ("bridge_id", text(&route.bridge_id)),
            (
                "key_generation",
                Value::Number(route.key_generation.to_string()),
            ),
            ("extension_id", text(&route.extension_id)),
            ("install_instance_id", text(&route.install_instance_id)),
            (
                "server_features",
                strings(self.server_features.iter().map(String::as_str)),
            ),
            ("rollout", text(&self.rollout)),
            ("selected_bridge", Value::Bool(true)),
            ("heartbeat_fresh", Value::Bool(true)),
            ("subject_profile_bound", Value::Bool(true)),
            ("legacy_control_plane_inactive", Value::Bool(true)),
        ])
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct AdmittedRuntimeRoute {
    server_instance_id: String,
    bridge_id: String,
    connector_sid: String,
    key_generation: u32,
    extension_id: String,
    install_instance_id: String,
    load_generation_id: String,
    companion_instance_id: String,
    extension_version: String,
    browser_family: String,
    browser_version: String,
    browser_label: String,
    actions: Vec<String>,
    features: Vec<String>,
    activation: RuntimeActivationAttestation,
}

impl AdmittedRuntimeRoute {
    pub(crate) fn bridge_id(&self) -> &str {
        &self.bridge_id
    }

    pub(crate) fn server_instance_id(&self) -> &str {
        &self.server_instance_id
    }

    #[cfg(test)]
    pub(crate) fn connector_sid(&self) -> &str {
        &self.connector_sid
    }

    pub(crate) const fn key_generation(&self) -> u32 {
        self.key_generation
    }

    pub(crate) fn install_instance_id(&self) -> &str {
        &self.install_instance_id
    }

    pub(crate) fn companion_instance_id(&self) -> &str {
        &self.companion_instance_id
    }

    pub(crate) fn load_generation_id(&self) -> &str {
        &self.load_generation_id
    }

    pub(crate) fn native_activation(&self) -> Value {
        self.activation.native_value(self)
    }

    pub(crate) fn native_negotiated(&self) -> Value {
        object(&[
            ("actions", strings(self.actions.iter().map(String::as_str))),
            (
                "features",
                strings(self.features.iter().map(String::as_str)),
            ),
        ])
    }

    pub(crate) fn artifact_route(
        &self,
        invocation: &NativeInvocation,
    ) -> Result<ArtifactRoute, ArtifactError> {
        let caller = rpc::extension_id_from_origin(invocation.caller_origin());
        if caller != Some(self.extension_id.as_str()) {
            return Err(ArtifactError::InvalidBinding);
        }
        ArtifactRoute::from_validated_invocation(
            invocation,
            &self.install_instance_id,
            &self.load_generation_id,
            &self.server_instance_id,
            &self.bridge_id,
            self.key_generation,
            &self.connector_sid,
        )
    }
}

pub(crate) fn parse_core_hello_ack(
    value: &Value,
    expected: &CoreHelloExpectation,
    namespace_sid: &str,
) -> Result<AdmittedRuntimeRoute, HandshakeError> {
    if !valid_opaque_id(namespace_sid) {
        return Err(HandshakeError::InvalidCorePacket);
    }
    let args = value
        .as_array()
        .filter(|values| values.len() == 1)
        .ok_or(HandshakeError::InvalidCorePacket)?;
    let envelope = exact_object(&args[0], &["correlationId", "results"])?;
    if string(envelope, "correlationId", 128)? != expected.correlation {
        return Err(HandshakeError::InvalidCorePacket);
    }
    let results = array(envelope, "results")?;
    if results.len() != 1 {
        return Err(HandshakeError::InvalidCorePacket);
    }
    let fields = results[0]
        .as_object()
        .ok_or(HandshakeError::InvalidCorePacket)?;
    let keys: &[&str] = if fields.contains_key("durationMs") {
        &["correlationId", "data", "durationMs", "handlerId", "ok"]
    } else {
        &["correlationId", "data", "handlerId", "ok"]
    };
    let result = exact_object(&results[0], keys)?;
    if string(result, "correlationId", 128)? != expected.correlation
        || result.get("durationMs").is_some_and(|value| match value {
            Value::Number(number) => !number
                .parse::<f64>()
                .is_ok_and(|duration| duration.is_finite() && (0.0..=60_000.0).contains(&duration)),
            _ => true,
        })
    {
        return Err(HandshakeError::InvalidCorePacket);
    }
    if string(result, "handlerId", 128)? != HANDLER_ID {
        return Err(HandshakeError::InvalidCorePacket);
    }
    if result.get("ok") != Some(&Value::Bool(true)) {
        return Err(HandshakeError::CoreDenied);
    }
    let data = exact_object_value(
        result,
        "data",
        &[
            "activation",
            "browser_control_ready",
            "connector_binding",
            "connector_session_ready",
            "features",
            "host_browser",
            "principal_type",
            "protocol",
        ],
    )?;
    if string(data, "protocol", 64)? != CORE_PROTOCOL
        || string(data, "principal_type", 64)? != "browser_bridge"
        || data.get("connector_session_ready") != Some(&Value::Bool(true))
        || data.get("browser_control_ready") != Some(&Value::Bool(true))
    {
        return Err(HandshakeError::CoreDenied);
    }
    exact_string_array(data, "features", OUTER_FEATURES)?;
    validate_redacted_host(data, expected)?;
    validate_connector_binding(data, expected, namespace_sid)?;
    let activation = validate_activation(data, expected)?;

    Ok(AdmittedRuntimeRoute {
        server_instance_id: expected.credential.server_instance_id.clone(),
        bridge_id: expected.credential.bridge_id.clone(),
        connector_sid: namespace_sid.to_owned(),
        key_generation: expected.credential.key_generation,
        extension_id: expected.credential.extension_id.clone(),
        install_instance_id: expected.credential.install_instance_id.clone(),
        load_generation_id: expected.hello.load_generation_id.clone(),
        companion_instance_id: expected.credential.companion_instance_id.clone(),
        extension_version: expected.hello.extension_version.clone(),
        browser_family: expected.hello.browser_family.clone(),
        browser_version: expected.hello.browser_version.clone(),
        browser_label: expected.hello.browser_label.clone(),
        actions: expected.hello.actions.clone(),
        features: expected.hello.features.clone(),
        activation,
    })
}

fn core_hello_data(hello: &ExtensionRuntimeHello, credential: &CredentialRuntimeBinding) -> Value {
    object(&[
        ("protocol", text(CORE_PROTOCOL)),
        ("features", strings(OUTER_FEATURES.iter().copied())),
        (
            "host_browser",
            object(&[
                ("supported", Value::Bool(true)),
                ("enabled", Value::Bool(true)),
                ("status", text("ready")),
                ("backend_id", text("chrome_extension")),
                (
                    "browser_id",
                    text(&format!("extension:{}", credential.bridge_id)),
                ),
                ("browser_label", text(&hello.browser_label)),
                (
                    "contract_version",
                    Value::Number(rpc::CONTRACT_VERSION.to_string()),
                ),
                (
                    "features",
                    strings(["browser_extension_bridge_v1"].into_iter()),
                ),
                (
                    "capabilities",
                    object(&[
                        ("actions", strings(hello.actions.iter().map(String::as_str))),
                        (
                            "features",
                            strings(hello.features.iter().map(String::as_str)),
                        ),
                        ("limits", limits()),
                    ]),
                ),
                (
                    "extension",
                    object(&[
                        ("id", text(&hello.extension_id)),
                        ("version", text(&hello.extension_version)),
                        ("manifest_version", Value::Number("3".into())),
                        ("install_instance_id", text(&hello.install_instance_id)),
                        ("load_generation_id", text(&hello.load_generation_id)),
                    ]),
                ),
                (
                    "companion",
                    object(&[
                        ("instance_id", text(&credential.companion_instance_id)),
                        ("version", text(COMPANION_VERSION)),
                        ("platform", text(runtime_platform())),
                        ("arch", text(architecture())),
                    ]),
                ),
            ]),
        ),
    ])
}

fn validate_redacted_host(
    data: &BTreeMap<String, Value>,
    expected: &CoreHelloExpectation,
) -> Result<(), HandshakeError> {
    let host = exact_object_value(
        data,
        "host_browser",
        &[
            "backend_id",
            "browser_id",
            "browser_label",
            "capabilities",
            "companion",
            "contract_version",
            "enabled",
            "extension",
            "features",
            "status",
            "supported",
        ],
    )?;
    if host.get("supported") != Some(&Value::Bool(true))
        || host.get("enabled") != Some(&Value::Bool(true))
        || string(host, "status", 32)? != "ready"
        || string(host, "backend_id", 64)? != "chrome_extension"
        || string(host, "browser_id", 300)?
            != format!("extension:{}", expected.credential.bridge_id)
        || string(host, "browser_label", 192)? != expected.hello.browser_label
        || integer(host, "contract_version")? != rpc::CONTRACT_VERSION
    {
        return Err(HandshakeError::CoreBindingMismatch);
    }
    exact_string_array(host, "features", &["browser_extension_bridge_v1"])?;
    let capabilities =
        exact_object_value(host, "capabilities", &["actions", "features", "limits"])?;
    exact_owned_string_array(capabilities, "actions", &expected.hello.actions)?;
    exact_owned_string_array(capabilities, "features", &expected.hello.features)?;
    if capabilities.get("limits") != Some(&limits()) {
        return Err(HandshakeError::CoreBindingMismatch);
    }
    let extension = exact_object_value(
        host,
        "extension",
        &["load_generation_id", "manifest_version", "version"],
    )?;
    if string(extension, "version", 64)? != expected.hello.extension_version
        || integer(extension, "manifest_version")? != 3
        || opaque(extension, "load_generation_id")? != expected.hello.load_generation_id
    {
        return Err(HandshakeError::CoreBindingMismatch);
    }
    let companion = exact_object_value(host, "companion", &["arch", "platform", "version"])?;
    if string(companion, "version", 64)? != COMPANION_VERSION
        || !semver_at_least(string(companion, "version", 64)?, MINIMUM_SECURE_COMPANION)
        || string(companion, "platform", 16)? != runtime_platform()
        || string(companion, "arch", 16)? != architecture()
    {
        return Err(HandshakeError::CoreBindingMismatch);
    }
    Ok(())
}

fn validate_connector_binding(
    data: &BTreeMap<String, Value>,
    expected: &CoreHelloExpectation,
    namespace_sid: &str,
) -> Result<(), HandshakeError> {
    let binding = exact_object_value(
        data,
        "connector_binding",
        &[
            "bridge_id",
            "connector_sid",
            "key_generation",
            "load_generation_id",
            "server_instance_id",
        ],
    )?;
    if opaque(binding, "server_instance_id")? != expected.credential.server_instance_id
        || opaque(binding, "bridge_id")? != expected.credential.bridge_id
        || opaque(binding, "connector_sid")? != namespace_sid
        || integer(binding, "key_generation")? != u64::from(expected.credential.key_generation)
        || opaque(binding, "load_generation_id")? != expected.hello.load_generation_id
    {
        return Err(HandshakeError::CoreBindingMismatch);
    }
    Ok(())
}

fn validate_activation(
    data: &BTreeMap<String, Value>,
    expected: &CoreHelloExpectation,
) -> Result<RuntimeActivationAttestation, HandshakeError> {
    let activation = exact_object_value(
        data,
        "activation",
        &[
            "bridge_id",
            "extension_id",
            "heartbeat_fresh",
            "install_instance_id",
            "key_generation",
            "legacy_control_plane_inactive",
            "principal",
            "rollout",
            "selected_bridge",
            "server_features",
            "subject_profile_bound",
        ],
    )?;
    let rollout = string(activation, "rollout", 32)?;
    if !matches!(rollout, "available" | "preview_authorized")
        || string(activation, "principal", 64)? != "browser_bridge"
        || opaque(activation, "bridge_id")? != expected.credential.bridge_id
        || integer(activation, "key_generation")? != u64::from(expected.credential.key_generation)
        || string(activation, "extension_id", 32)? != expected.credential.extension_id
        || opaque(activation, "install_instance_id")? != expected.credential.install_instance_id
        || activation.get("selected_bridge") != Some(&Value::Bool(true))
        || activation.get("heartbeat_fresh") != Some(&Value::Bool(true))
        || activation.get("subject_profile_bound") != Some(&Value::Bool(true))
        || activation.get("legacy_control_plane_inactive") != Some(&Value::Bool(true))
    {
        return Err(HandshakeError::CoreDenied);
    }
    exact_string_array(activation, "server_features", OUTER_FEATURES)?;
    Ok(RuntimeActivationAttestation {
        rollout: rollout.to_owned(),
        server_features: OUTER_FEATURES
            .iter()
            .map(|value| (*value).to_owned())
            .collect(),
    })
}

fn validate_resume(fields: &BTreeMap<String, Value>) -> Result<(), HandshakeError> {
    let event_cursors = array(fields, "event_cursors")?;
    if event_cursors.len() > 8 {
        return Err(HandshakeError::InvalidExtensionHello);
    }
    let mut generations = BTreeSet::new();
    for cursor in event_cursors {
        let cursor = exact_object(cursor, &["last_acked_event_sequence", "load_generation_id"])?;
        let generation = opaque(cursor, "load_generation_id")?;
        integer(cursor, "last_acked_event_sequence")?;
        if !generations.insert(generation) {
            return Err(HandshakeError::InvalidExtensionHello);
        }
    }
    let inflight = array(fields, "inflight_op_ids")?;
    if inflight.len() > 256 {
        return Err(HandshakeError::InvalidExtensionHello);
    }
    let mut ids = BTreeSet::new();
    for value in inflight {
        let id = value
            .as_str()
            .filter(|value| valid_opaque_id(value))
            .ok_or(HandshakeError::InvalidExtensionHello)?;
        if !ids.insert(id) {
            return Err(HandshakeError::InvalidExtensionHello);
        }
    }
    let digest = string(fields, "lease_digest", 71)?;
    if !digest.strip_prefix("sha256:").is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    }) {
        return Err(HandshakeError::InvalidExtensionHello);
    }
    Ok(())
}

fn capability_set(
    fields: &BTreeMap<String, Value>,
    key: &str,
    allowed: &[&str],
) -> Result<Vec<String>, HandshakeError> {
    let values = array(fields, key)?;
    if values.len() > 64 {
        return Err(HandshakeError::UnsupportedCapability);
    }
    let allowed = allowed.iter().copied().collect::<BTreeSet<_>>();
    let mut result = BTreeSet::new();
    for value in values {
        let value = value
            .as_str()
            .filter(|value| valid_opaque_id(value))
            .ok_or(HandshakeError::UnsupportedCapability)?;
        if !allowed.contains(value) || !result.insert(value.to_owned()) {
            return Err(HandshakeError::UnsupportedCapability);
        }
    }
    Ok(result.into_iter().collect())
}

fn limits() -> Value {
    object(&[
        (
            "artifact_chunk_bytes",
            Value::Number(rpc::MAX_ARTIFACT_CHUNK_RAW_BYTES.to_string()),
        ),
        (
            "max_artifact_bytes",
            Value::Number(rpc::MAX_ARTIFACT_BYTES.to_string()),
        ),
        (
            "max_json_frame_bytes",
            Value::Number(rpc::MAX_NATIVE_FRAME_BYTES.to_string()),
        ),
    ])
}

pub(crate) fn runtime_platform() -> &'static str {
    match Platform::current() {
        Platform::Macos => "darwin",
        Platform::Windows => "windows",
        Platform::Linux => "linux",
        Platform::Unsupported => "unsupported",
    }
}

fn exact_object<'a>(
    value: &'a Value,
    expected: &[&str],
) -> Result<&'a BTreeMap<String, Value>, HandshakeError> {
    let fields = value.as_object().ok_or(HandshakeError::InvalidCorePacket)?;
    if fields.len() != expected.len() || expected.iter().any(|key| !fields.contains_key(*key)) {
        return Err(HandshakeError::InvalidCorePacket);
    }
    Ok(fields)
}

fn exact_object_value<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    expected: &[&str],
) -> Result<&'a BTreeMap<String, Value>, HandshakeError> {
    exact_object(
        fields.get(key).ok_or(HandshakeError::InvalidCorePacket)?,
        expected,
    )
}

fn array<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a [Value], HandshakeError> {
    fields
        .get(key)
        .and_then(Value::as_array)
        .ok_or(HandshakeError::InvalidCorePacket)
}

fn string<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    maximum: usize,
) -> Result<&'a str, HandshakeError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= maximum)
        .ok_or(HandshakeError::InvalidCorePacket)
}

fn opaque<'a>(fields: &'a BTreeMap<String, Value>, key: &str) -> Result<&'a str, HandshakeError> {
    let value = string(fields, key, 256)?;
    valid_opaque_id(value)
        .then_some(value)
        .ok_or(HandshakeError::InvalidCorePacket)
}

fn integer(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, HandshakeError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(HandshakeError::InvalidCorePacket)
}

fn exact_string_array(
    fields: &BTreeMap<String, Value>,
    key: &str,
    expected: &[&str],
) -> Result<(), HandshakeError> {
    let expected = expected.iter().copied().collect::<Vec<_>>();
    let actual = array(fields, key)?
        .iter()
        .map(|value| value.as_str())
        .collect::<Option<Vec<_>>>()
        .ok_or(HandshakeError::InvalidCorePacket)?;
    if actual != expected {
        return Err(HandshakeError::CoreBindingMismatch);
    }
    Ok(())
}

fn exact_owned_string_array(
    fields: &BTreeMap<String, Value>,
    key: &str,
    expected: &[String],
) -> Result<(), HandshakeError> {
    let actual = array(fields, key)?
        .iter()
        .map(|value| value.as_str())
        .collect::<Option<Vec<_>>>()
        .ok_or(HandshakeError::InvalidCorePacket)?;
    if actual != expected.iter().map(String::as_str).collect::<Vec<_>>() {
        return Err(HandshakeError::CoreBindingMismatch);
    }
    Ok(())
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn strings<'a>(values: impl IntoIterator<Item = &'a str>) -> Value {
    Value::Array(values.into_iter().map(text).collect())
}

fn object(fields: &[(&str, Value)]) -> Value {
    Value::Object(
        fields
            .iter()
            .map(|(key, value)| ((*key).to_owned(), value.clone()))
            .collect(),
    )
}

fn browser_major(value: &str) -> Option<u64> {
    let mut parts = value.split('.');
    let major = parts.next()?.parse::<u64>().ok()?;
    let mut count = 1;
    for part in parts {
        if part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        part.parse::<u64>().ok()?;
        count += 1;
        if count > 4 {
            return None;
        }
    }
    Some(major)
}

fn valid_semver(value: &str) -> bool {
    let (without_build, build) = value
        .split_once('+')
        .map_or((value, None), |(core, value)| (core, Some(value)));
    if build.is_some_and(|value| !valid_semver_identifiers(value, false)) {
        return false;
    }
    let (core, prerelease) = without_build
        .split_once('-')
        .map_or((without_build, None), |(core, value)| (core, Some(value)));
    if prerelease.is_some_and(|value| !valid_semver_identifiers(value, true)) {
        return false;
    }
    let parts = core.split('.').collect::<Vec<_>>();
    parts.len() == 3
        && parts.iter().all(|part| {
            !part.is_empty()
                && part.bytes().all(|byte| byte.is_ascii_digit())
                && (part == &"0" || !part.starts_with('0'))
                && part.parse::<u64>().is_ok()
        })
}

fn valid_semver_identifiers(value: &str, reject_numeric_leading_zero: bool) -> bool {
    !value.is_empty()
        && value.split('.').all(|identifier| {
            !identifier.is_empty()
                && identifier
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                && (!reject_numeric_leading_zero
                    || !identifier.bytes().all(|byte| byte.is_ascii_digit())
                    || identifier == "0"
                    || !identifier.starts_with('0'))
        })
}

fn semver_at_least(value: &str, minimum: &str) -> bool {
    let parse = |value: &str| -> Option<((u64, u64, u64), bool)> {
        if !valid_semver(value) {
            return None;
        }
        let without_build = value.split_once('+').map_or(value, |(core, _)| core);
        let (core, prerelease) = without_build
            .split_once('-')
            .map_or((without_build, false), |(core, _)| (core, true));
        let mut parts = core.split('.').map(|part| part.parse::<u64>().ok());
        Some(((parts.next()??, parts.next()??, parts.next()??), prerelease))
    };
    parse(value).zip(parse(minimum)).is_some_and(
        |((value, value_prerelease), (minimum, minimum_prerelease))| {
            !minimum_prerelease && (value > minimum || (value == minimum && !value_prerelease))
        },
    )
}

#[cfg(test)]
pub(crate) fn fixture_expectation() -> CoreHelloExpectation {
    let invocation =
        NativeInvocation::fixture("chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/");
    let params = crate::json::parse(
        br#"{
          "protocol":"a0.browser-bridge",
          "contract":{"min":1,"max":1},
          "extension":{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","version":"0.1.0","manifest_version":3,"install_instance_id":"install-fixture","load_generation_id":"generation-fixture"},
          "browser":{"family":"chrome","version":"146.0.0.0"},
          "capabilities":{"actions":["open","list"],"features":["tab_leases_v1","cursor_v1"],"cdp_domains":[]},
          "resume":{"event_cursors":[],"inflight_op_ids":[],"lease_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}
        }"#,
    )
    .expect("valid fixture hello");
    let hello = ExtensionRuntimeHello::from_validated_invocation(&invocation, &params)
        .expect("valid fixture runtime hello");
    let credential = CredentialRecord::fixture("https://agent.example/a0");
    let binding = CredentialRuntimeBinding::from_credential(&credential, &hello)
        .expect("valid fixture credential");
    CoreHelloExpectation::new(hello, binding)
}

#[cfg(test)]
pub(crate) fn fixture_admitted_ack(expected: &CoreHelloExpectation, sid: &str) -> Value {
    let request_host = expected.data.as_object().expect("fixture host root")["host_browser"]
        .as_object()
        .expect("fixture host");
    let request_extension = request_host["extension"]
        .as_object()
        .expect("fixture extension");
    let request_companion = request_host["companion"]
        .as_object()
        .expect("fixture companion");
    let host = object(&[
        ("supported", Value::Bool(true)),
        ("enabled", Value::Bool(true)),
        ("status", text("ready")),
        ("backend_id", text("chrome_extension")),
        ("browser_id", request_host["browser_id"].clone()),
        ("browser_label", request_host["browser_label"].clone()),
        ("contract_version", Value::Number("1".into())),
        (
            "features",
            strings(["browser_extension_bridge_v1"].into_iter()),
        ),
        ("capabilities", request_host["capabilities"].clone()),
        (
            "extension",
            object(&[
                ("version", request_extension["version"].clone()),
                ("manifest_version", Value::Number("3".into())),
                (
                    "load_generation_id",
                    request_extension["load_generation_id"].clone(),
                ),
            ]),
        ),
        (
            "companion",
            object(&[
                ("version", request_companion["version"].clone()),
                ("platform", request_companion["platform"].clone()),
                ("arch", request_companion["arch"].clone()),
            ]),
        ),
    ]);
    let data = object(&[
        ("protocol", text(CORE_PROTOCOL)),
        ("principal_type", text("browser_bridge")),
        ("features", strings(OUTER_FEATURES.iter().copied())),
        ("connector_session_ready", Value::Bool(true)),
        ("browser_control_ready", Value::Bool(true)),
        (
            "connector_binding",
            object(&[
                ("server_instance_id", text("server-fixture")),
                ("bridge_id", text("bridge-fixture")),
                ("connector_sid", text(sid)),
                ("key_generation", Value::Number("1".into())),
                ("load_generation_id", text("generation-fixture")),
            ]),
        ),
        ("host_browser", host),
        (
            "activation",
            object(&[
                ("principal", text("browser_bridge")),
                ("bridge_id", text("bridge-fixture")),
                ("key_generation", Value::Number("1".into())),
                ("extension_id", text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")),
                ("install_instance_id", text("install-fixture")),
                ("server_features", strings(OUTER_FEATURES.iter().copied())),
                ("rollout", text("available")),
                ("selected_bridge", Value::Bool(true)),
                ("heartbeat_fresh", Value::Bool(true)),
                ("subject_profile_bound", Value::Bool(true)),
                ("legacy_control_plane_inactive", Value::Bool(true)),
            ]),
        ),
    ]);
    Value::Array(vec![object(&[
        ("correlationId", text(&expected.correlation)),
        (
            "results",
            Value::Array(vec![object(&[
                ("correlationId", text(&expected.correlation)),
                ("handlerId", text(HANDLER_ID)),
                ("ok", Value::Bool(true)),
                ("data", data),
            ])]),
        ),
    ])])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json;

    const ORIGIN: &str = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/";

    fn extension_hello() -> Value {
        json::parse(
            br#"{
              "protocol":"a0.browser-bridge",
              "contract":{"min":1,"max":1},
              "extension":{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","version":"0.1.0","manifest_version":3,"install_instance_id":"install-fixture","load_generation_id":"generation-fixture"},
              "browser":{"family":"chrome","version":"146.0.0.0"},
              "capabilities":{"actions":["open","list"],"features":["tab_leases_v1","cursor_v1"],"cdp_domains":[]},
              "resume":{"event_cursors":[],"inflight_op_ids":[],"lease_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}
            }"#,
        )
        .unwrap()
    }

    fn expectation() -> CoreHelloExpectation {
        let invocation = NativeInvocation::fixture(ORIGIN);
        let hello =
            ExtensionRuntimeHello::from_validated_invocation(&invocation, &extension_hello())
                .unwrap();
        let credential = CredentialRecord::fixture("https://agent.example/a0");
        let binding = CredentialRuntimeBinding::from_credential(&credential, &hello).unwrap();
        CoreHelloExpectation::new(hello, binding)
    }

    fn admitted_data(expected: &CoreHelloExpectation, sid: &str) -> Value {
        let request_host = expected
            .data
            .as_object()
            .unwrap()
            .get("host_browser")
            .unwrap()
            .as_object()
            .unwrap();
        let request_extension = request_host["extension"].as_object().unwrap();
        let request_companion = request_host["companion"].as_object().unwrap();
        let host = object(&[
            ("supported", Value::Bool(true)),
            ("enabled", Value::Bool(true)),
            ("status", text("ready")),
            ("backend_id", text("chrome_extension")),
            ("browser_id", request_host["browser_id"].clone()),
            ("browser_label", request_host["browser_label"].clone()),
            ("contract_version", Value::Number("1".into())),
            ("features", strings(["browser_extension_bridge_v1"])),
            ("capabilities", request_host["capabilities"].clone()),
            (
                "extension",
                object(&[
                    ("version", request_extension["version"].clone()),
                    ("manifest_version", Value::Number("3".into())),
                    (
                        "load_generation_id",
                        request_extension["load_generation_id"].clone(),
                    ),
                ]),
            ),
            (
                "companion",
                object(&[
                    ("version", request_companion["version"].clone()),
                    ("platform", request_companion["platform"].clone()),
                    ("arch", request_companion["arch"].clone()),
                ]),
            ),
        ]);
        object(&[
            ("protocol", text(CORE_PROTOCOL)),
            ("principal_type", text("browser_bridge")),
            ("features", strings(OUTER_FEATURES.iter().copied())),
            ("connector_session_ready", Value::Bool(true)),
            ("browser_control_ready", Value::Bool(true)),
            (
                "connector_binding",
                object(&[
                    ("server_instance_id", text("server-fixture")),
                    ("bridge_id", text("bridge-fixture")),
                    ("connector_sid", text(sid)),
                    ("key_generation", Value::Number("1".into())),
                    ("load_generation_id", text("generation-fixture")),
                ]),
            ),
            ("host_browser", host),
            (
                "activation",
                object(&[
                    ("principal", text("browser_bridge")),
                    ("bridge_id", text("bridge-fixture")),
                    ("key_generation", Value::Number("1".into())),
                    ("extension_id", text("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")),
                    ("install_instance_id", text("install-fixture")),
                    ("server_features", strings(OUTER_FEATURES.iter().copied())),
                    ("rollout", text("available")),
                    ("selected_bridge", Value::Bool(true)),
                    ("heartbeat_fresh", Value::Bool(true)),
                    ("subject_profile_bound", Value::Bool(true)),
                    ("legacy_control_plane_inactive", Value::Bool(true)),
                ]),
            ),
        ])
    }

    fn ack(expected: &CoreHelloExpectation, sid: &str) -> Value {
        Value::Array(vec![object(&[
            ("correlationId", text(CORE_HELLO_CORRELATION)),
            (
                "results",
                Value::Array(vec![object(&[
                    ("correlationId", text(&expected.correlation)),
                    ("handlerId", text(HANDLER_ID)),
                    ("ok", Value::Bool(true)),
                    ("data", admitted_data(expected, sid)),
                ])]),
            ),
        ])])
    }

    #[test]
    fn exact_request_uses_profile_credential_and_proven_capabilities() {
        let expected = expectation();
        let packet = expected.packet();
        assert!(packet.starts_with("42/ws,1[\"connector_hello\","));
        assert!(packet.contains("\"browser_id\":\"extension:bridge-fixture\""));
        assert!(packet.contains("\"install_instance_id\":\"install-fixture\""));
        assert!(packet.contains("\"instance_id\":\"companion-fixture\""));
        assert!(packet.contains("\"version\":\"2.12.0\""));
        assert!(!packet.contains("private_seed"));
        assert_eq!(expected.hello.actions, vec!["list", "open"]);
        assert_eq!(expected.hello.features, vec!["cursor_v1", "tab_leases_v1"]);
    }

    #[test]
    fn complete_attestation_binds_namespace_credential_generation_and_artifact_route() {
        let expected = expectation();
        let route =
            parse_core_hello_ack(&ack(&expected, "sid-fixture"), &expected, "sid-fixture").unwrap();
        assert_eq!(route.bridge_id(), "bridge-fixture");
        assert_eq!(route.server_instance_id(), "server-fixture");
        assert_eq!(route.connector_sid(), "sid-fixture");
        assert_eq!(route.load_generation_id(), "generation-fixture");
        assert_eq!(route.key_generation(), 1);
        let artifact = route
            .artifact_route(&NativeInvocation::fixture(ORIGIN))
            .unwrap();
        assert_eq!(artifact.connector_sid(), "sid-fixture");
        assert_eq!(artifact.server_instance_id(), "server-fixture");
        assert_eq!(artifact.key_generation(), 1);
    }

    #[test]
    fn bool_only_partial_or_mismatched_attestation_never_admits() {
        let expected = expectation();
        let mut partial = admitted_data(&expected, "sid-fixture");
        let Value::Object(partial_fields) = &mut partial else {
            panic!()
        };
        partial_fields.remove("activation");
        let partial = Value::Array(vec![object(&[
            ("correlationId", text(CORE_HELLO_CORRELATION)),
            (
                "results",
                Value::Array(vec![object(&[
                    ("handlerId", text(HANDLER_ID)),
                    ("ok", Value::Bool(true)),
                    ("data", partial),
                ])]),
            ),
        ])]);
        assert!(parse_core_hello_ack(&partial, &expected, "sid-fixture").is_err());

        let mismatched = json::parse(
            ack(&expected, "sid-fixture")
                .encode()
                .replacen(
                    "\"connector_sid\":\"sid-fixture\"",
                    "\"connector_sid\":\"sid-other\"",
                    1,
                )
                .as_bytes(),
        )
        .unwrap();
        assert_eq!(
            parse_core_hello_ack(&mismatched, &expected, "sid-fixture"),
            Err(HandshakeError::CoreBindingMismatch)
        );

        let stale_companion = json::parse(
            ack(&expected, "sid-fixture")
                .encode()
                .replacen("\"version\":\"2.12.0\"", "\"version\":\"2.11.0\"", 1)
                .as_bytes(),
        )
        .unwrap();
        assert_eq!(
            parse_core_hello_ack(&stale_companion, &expected, "sid-fixture"),
            Err(HandshakeError::CoreBindingMismatch)
        );

        let incomplete_features = json::parse(
            ack(&expected, "sid-fixture")
                .encode()
                .replacen(",\"connector_browser_event\"", "", 1)
                .as_bytes(),
        )
        .unwrap();
        assert_eq!(
            parse_core_hello_ack(&incomplete_features, &expected, "sid-fixture"),
            Err(HandshakeError::CoreBindingMismatch)
        );
    }

    #[test]
    fn runtime_claims_reject_extra_fields_unproven_caps_and_old_browser() {
        let invocation = NativeInvocation::fixture(ORIGIN);
        let mut extra = extension_hello();
        let Value::Object(extra_fields) = &mut extra else {
            panic!()
        };
        extra_fields.insert("authority".into(), Value::Bool(true));
        assert!(ExtensionRuntimeHello::from_validated_invocation(&invocation, &extra).is_err());

        let mut unproven = extension_hello();
        let Value::Object(unproven_fields) = &mut unproven else {
            panic!()
        };
        let Value::Object(capabilities) = unproven_fields.get_mut("capabilities").unwrap() else {
            panic!()
        };
        let Value::Array(actions) = capabilities.get_mut("actions").unwrap() else {
            panic!()
        };
        actions.push(text("submit"));
        assert_eq!(
            ExtensionRuntimeHello::from_validated_invocation(&invocation, &unproven),
            Err(HandshakeError::UnsupportedCapability)
        );

        let mut old = extension_hello();
        let Value::Object(old_fields) = &mut old else {
            panic!()
        };
        let Value::Object(browser) = old_fields.get_mut("browser").unwrap() else {
            panic!()
        };
        browser.insert("version".into(), text("119.0.0.0"));
        assert_eq!(
            ExtensionRuntimeHello::from_validated_invocation(&invocation, &old),
            Err(HandshakeError::UnsupportedVersion)
        );

        let mut complete = extension_hello();
        let Value::Object(complete_fields) = &mut complete else {
            panic!()
        };
        let Value::Object(capabilities) = complete_fields.get_mut("capabilities").unwrap() else {
            panic!()
        };
        capabilities.insert("actions".into(), strings(PROVEN_ACTIONS.iter().copied()));
        capabilities.insert("features".into(), strings(PROVEN_FEATURES.iter().copied()));
        let accepted =
            ExtensionRuntimeHello::from_validated_invocation(&invocation, &complete).unwrap();
        assert!(accepted.actions.iter().any(|value| value == "screenshot"));
        assert!(accepted.actions.iter().any(|value| value == "hover"));
        assert!(accepted
            .features
            .iter()
            .any(|value| value == "screenshots_v1"));
        assert!(accepted
            .features
            .iter()
            .any(|value| value == "artifacts_v1"));
        assert!(accepted
            .features
            .iter()
            .any(|value| value == "trusted_input_v1"));

        assert!(!valid_semver("2.12.0-"));
        assert!(!valid_semver("2.12.0+"));
        assert!(!valid_semver("2.12.0-01"));
        assert!(valid_semver("2.12.0-rc.1+build.7"));
        assert!(!semver_at_least("2.12.0-rc.1", "2.12.0"));
        assert!(semver_at_least(COMPANION_VERSION, MINIMUM_SECURE_COMPANION));
    }
}
