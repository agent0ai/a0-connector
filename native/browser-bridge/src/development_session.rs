//! Isolated signed session for the local-development channel.
//!
//! This module proves possession of the separately stored development key and
//! binds one Socket.IO SID to the validated extension profile/generation. A
//! separately tagged admission may enable only the frozen limited transport;
//! it can never construct a production runtime route or activation claim.

use std::collections::BTreeMap;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use ed25519_dalek::{Signer, SigningKey};
use zeroize::Zeroizing;

use crate::json::Value;
use crate::pairing::CredentialRecord;
use crate::rpc::{valid_development_server_base_origin, valid_opaque_id};
use crate::runtime_handshake::{runtime_platform, ExtensionRuntimeHello};
use crate::transport_profile::{
    DEVELOPMENT_HANDLER_ID as PROFILE_HANDLER_ID, DEVELOPMENT_HANDLER_PATH as PROFILE_HANDLER_PATH,
    DEVELOPMENT_PRINCIPAL_TYPE as PROFILE_PRINCIPAL_TYPE,
};
use crate::{COMPANION_VERSION, DEVELOPMENT_CHANNEL, DEVELOPMENT_EXTENSION_ID};

pub(crate) const DEVELOPMENT_SESSION_CONTRACT: &str = "a0.browser-bridge.development-session.v1";
pub(crate) const DEVELOPMENT_CHALLENGE_PATH: &str =
    "/api/plugins/_a0_connector/browser_bridge_development_challenge";
pub(crate) const DEVELOPMENT_HANDLER_PATH: &str = PROFILE_HANDLER_PATH;
pub(crate) const DEVELOPMENT_HANDLER_ID: &str = PROFILE_HANDLER_ID;
pub(crate) const DEVELOPMENT_PRINCIPAL_TYPE: &str = PROFILE_PRINCIPAL_TYPE;
pub(crate) const DEVELOPMENT_HELLO_FALLBACK_MS: u64 = 8_000;
pub(crate) const DEVELOPMENT_RUNTIME_CONTRACT: &str = "a0.browser-bridge.development-runtime.v1";
pub(crate) const LIMITED_ACTIONS: &[&str] = &[
    "content", "ensure", "list", "navigate", "open", "scroll", "state", "status",
];
pub(crate) const LIMITED_FEATURES: &[&str] = &[
    "cursor_v1",
    "semantic_dom_v1",
    "tab_groups_v1",
    "tab_leases_v1",
];
pub(crate) const LIMITED_OUTER_FEATURES: &[&str] = &[
    "browser_extension_bridge_v1",
    "connector_browser_control",
    "connector_browser_event",
];
pub(crate) const LIMITED_TRANSPORTS: &[&str] = &["control", "critical_event", "operation"];

const CONNECTOR_PROTOCOL: &str = "a0-connector.v1";
const CORE_HELLO_ACK_ID: u64 = 1;
const CORE_HELLO_CORRELATION: &str = "bridge-hello";
const MAX_CHALLENGE_BYTES: usize = 8 * 1024;
const CHALLENGE_TTL_MS: u64 = 60_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum DevelopmentSessionError {
    InvalidBinding,
    InvalidChallenge,
    Expired,
    InvalidCorePacket,
    CoreDenied,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DevelopmentHelloExpectation {
    server_instance_id: String,
    bridge_id: String,
    key_generation: u32,
    extension_id: String,
    extension_version: String,
    install_instance_id: String,
    load_generation_id: String,
    companion_instance_id: String,
    browser_family: String,
    browser_version: String,
    browser_label: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DevelopmentSessionBinding {
    server_instance_id: String,
    bridge_id: String,
    connector_sid: String,
    key_generation: u32,
    load_generation_id: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DevelopmentRuntimeRoute {
    server_instance_id: String,
    bridge_id: String,
    connector_sid: String,
    key_generation: u32,
    extension_id: String,
    extension_version: String,
    install_instance_id: String,
    load_generation_id: String,
    companion_instance_id: String,
    browser_family: String,
    browser_version: String,
    browser_label: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum DevelopmentHelloOutcome {
    PairingOnly(DevelopmentSessionBinding),
    LimitedRuntime(DevelopmentRuntimeRoute),
}

impl DevelopmentRuntimeRoute {
    pub(crate) fn server_instance_id(&self) -> &str {
        &self.server_instance_id
    }

    pub(crate) fn bridge_id(&self) -> &str {
        &self.bridge_id
    }

    pub(crate) fn connector_sid(&self) -> &str {
        &self.connector_sid
    }

    pub(crate) const fn key_generation(&self) -> u32 {
        self.key_generation
    }

    pub(crate) fn extension_id(&self) -> &str {
        &self.extension_id
    }

    pub(crate) fn install_instance_id(&self) -> &str {
        &self.install_instance_id
    }

    pub(crate) fn load_generation_id(&self) -> &str {
        &self.load_generation_id
    }

    pub(crate) fn companion_instance_id(&self) -> &str {
        &self.companion_instance_id
    }

    pub(crate) fn native_negotiated(&self) -> Value {
        object(&[
            ("actions", strings(LIMITED_ACTIONS)),
            ("features", strings(LIMITED_FEATURES)),
        ])
    }

    pub(crate) fn native_admission(&self) -> Value {
        object(&[
            ("contract", text(DEVELOPMENT_RUNTIME_CONTRACT)),
            ("channel", text(DEVELOPMENT_CHANNEL)),
            ("mode", text("limited_runtime")),
            ("transports", strings(LIMITED_TRANSPORTS)),
            ("selection_scope", text("explicit_context_bridge")),
            ("server_instance_id", text(&self.server_instance_id)),
            ("bridge_id", text(&self.bridge_id)),
            (
                "key_generation",
                Value::Number(self.key_generation.to_string()),
            ),
            ("extension_id", text(&self.extension_id)),
            ("install_instance_id", text(&self.install_instance_id)),
            ("load_generation_id", text(&self.load_generation_id)),
        ])
    }

    #[cfg(test)]
    pub(crate) fn browser_identity(&self) -> (&str, &str, &str) {
        (
            &self.browser_family,
            &self.browser_version,
            &self.browser_label,
        )
    }
}

impl DevelopmentHelloExpectation {
    pub(crate) fn from_credential(
        hello: &ExtensionRuntimeHello,
        credential: &CredentialRecord,
    ) -> Result<Self, DevelopmentSessionError> {
        if hello.extension_id() != DEVELOPMENT_EXTENSION_ID
            || credential.extension_id != DEVELOPMENT_EXTENSION_ID
            || hello.extension_id() != credential.extension_id
            || hello.install_instance_id() != credential.install_instance_id
            || !valid_opaque_id(&credential.server_instance_id)
            || !valid_opaque_id(&credential.bridge_id)
            || !valid_opaque_id(credential.companion_instance_id())
            || credential.key_generation() == 0
            || !valid_development_server_base_origin(&credential.server_base_origin)
            || !hello.supports_actions(LIMITED_ACTIONS)
            || !hello.supports_features(LIMITED_FEATURES)
        {
            return Err(DevelopmentSessionError::InvalidBinding);
        }
        Ok(Self {
            server_instance_id: credential.server_instance_id.clone(),
            bridge_id: credential.bridge_id.clone(),
            key_generation: credential.key_generation(),
            extension_id: credential.extension_id.clone(),
            extension_version: hello.extension_version().to_owned(),
            install_instance_id: credential.install_instance_id.clone(),
            load_generation_id: hello.load_generation_id().to_owned(),
            companion_instance_id: credential.companion_instance_id().to_owned(),
            browser_family: hello.browser_family().to_owned(),
            browser_version: hello.browser_version().to_owned(),
            browser_label: hello.browser_label().to_owned(),
        })
    }

    pub(crate) fn challenge_request(&self, client_nonce: &str) -> Value {
        object(&[
            ("contract", text(DEVELOPMENT_SESSION_CONTRACT)),
            ("trust_version", Value::Number("1".into())),
            ("channel", text(DEVELOPMENT_CHANNEL)),
            ("bridge_id", text(&self.bridge_id)),
            ("client_nonce", text(client_nonce)),
            ("extension_id", text(&self.extension_id)),
            ("install_instance_id", text(&self.install_instance_id)),
            ("load_generation_id", text(&self.load_generation_id)),
            ("companion_instance_id", text(&self.companion_instance_id)),
        ])
    }

    pub(crate) fn packet(&self) -> String {
        format!(
            "42/ws,{CORE_HELLO_ACK_ID}{}",
            Value::Array(vec![
                text("connector_hello"),
                object(&[
                    ("correlationId", text(CORE_HELLO_CORRELATION)),
                    ("data", self.hello_data()),
                ]),
            ])
            .encode()
        )
    }

    /// Admit no application authority before the signed Core hello ACK. This
    /// narrow validator only permits retaining the one reconciliation control
    /// that Core may emit immediately after its hello handler returns. The
    /// normal connector codec performs the complete schema validation after
    /// the exact limited-runtime ACK and namespace SID have been accepted.
    pub(crate) fn validate_provisional_reconcile(
        &self,
        packet: &str,
    ) -> Result<(), DevelopmentSessionError> {
        let value = crate::json::parse(
            packet
                .strip_prefix("42/ws,")
                .ok_or(DevelopmentSessionError::InvalidCorePacket)?
                .as_bytes(),
        )
        .map_err(|_| DevelopmentSessionError::InvalidCorePacket)?;
        let values = value
            .as_array()
            .filter(|values| values.len() == 2)
            .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
        if values[0].as_str() != Some("connector_browser_control") {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        let envelope = values[1]
            .as_object()
            .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
        validate_development_core_event_envelope(envelope)?;
        let data = envelope
            .get("data")
            .and_then(Value::as_object)
            .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
        if string(data, "method", 64)? != "browser.reconcile"
            || opaque(data, "bridge_id")? != self.bridge_id
            || opaque(data, "load_generation_id")? != self.load_generation_id
        {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        Ok(())
    }

    pub(crate) fn signed_auth(
        &self,
        credential: &CredentialRecord,
        client_nonce: &str,
        bytes: &[u8],
        received_at_ms: u64,
    ) -> Result<(Zeroizing<String>, u64), DevelopmentSessionError> {
        if bytes.len() > MAX_CHALLENGE_BYTES
            || !valid_nonce(client_nonce)
            || credential.bridge_id != self.bridge_id
            || credential.server_instance_id != self.server_instance_id
            || credential.extension_id != self.extension_id
            || credential.install_instance_id != self.install_instance_id
            || credential.companion_instance_id() != self.companion_instance_id
            || credential.key_generation() != self.key_generation
        {
            return Err(DevelopmentSessionError::InvalidChallenge);
        }
        let value =
            crate::json::parse(bytes).map_err(|_| DevelopmentSessionError::InvalidChallenge)?;
        let fields = exact_object(
            &value,
            &[
                "challenge_id",
                "channel",
                "contract",
                "expires_at_ms",
                "server_base_url",
                "server_instance_id",
                "server_nonce",
                "trust_version",
            ],
        )?;
        if string(fields, "contract", 128)? != DEVELOPMENT_SESSION_CONTRACT
            || integer(fields, "trust_version")? != 1
            || string(fields, "channel", 64)? != DEVELOPMENT_CHANNEL
            || string(fields, "server_instance_id", 256)? != self.server_instance_id
            || string(fields, "server_base_url", 2_048)? != credential.server_base_origin
        {
            return Err(DevelopmentSessionError::InvalidChallenge);
        }
        let challenge_id = opaque(fields, "challenge_id")?;
        let server_nonce = string(fields, "server_nonce", 64)?;
        if !valid_nonce(server_nonce) {
            return Err(DevelopmentSessionError::InvalidChallenge);
        }
        let expires_at_ms = integer(fields, "expires_at_ms")?;
        if expires_at_ms <= received_at_ms
            || expires_at_ms.saturating_sub(received_at_ms) > CHALLENGE_TTL_MS
        {
            return Err(DevelopmentSessionError::Expired);
        }
        let proof = object(&[
            ("aud", text(&self.server_instance_id)),
            ("bridge_id", text(&self.bridge_id)),
            ("challenge_id", text(challenge_id)),
            ("client_nonce", text(client_nonce)),
            ("companion_instance_id", text(&self.companion_instance_id)),
            ("contract", text(DEVELOPMENT_SESSION_CONTRACT)),
            ("channel", text(DEVELOPMENT_CHANNEL)),
            ("extension_id", text(&self.extension_id)),
            ("handler", text(DEVELOPMENT_HANDLER_PATH)),
            ("install_instance_id", text(&self.install_instance_id)),
            ("load_generation_id", text(&self.load_generation_id)),
            ("protocol", text(CONNECTOR_PROTOCOL)),
            ("server_base_url", text(&credential.server_base_origin)),
            ("server_nonce", text(server_nonce)),
            ("trust_version", Value::Number("1".into())),
        ]);
        let signing_key = SigningKey::from_bytes(&credential.private_seed);
        let signature =
            URL_SAFE_NO_PAD.encode(signing_key.sign(proof.encode().as_bytes()).to_bytes());
        let auth = object(&[
            (
                "handlers",
                Value::Array(vec![text(DEVELOPMENT_HANDLER_PATH)]),
            ),
            (
                "principal",
                object(&[
                    ("type", text(DEVELOPMENT_PRINCIPAL_TYPE)),
                    ("proof", proof),
                    ("signature", text(&signature)),
                ]),
            ),
        ]);
        Ok((
            Zeroizing::new(format!("40/ws,{}", auth.encode())),
            expires_at_ms,
        ))
    }

    pub(crate) fn parse_ack(
        &self,
        value: &Value,
        namespace_sid: &str,
    ) -> Result<DevelopmentHelloOutcome, DevelopmentSessionError> {
        if !valid_opaque_id(namespace_sid) {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        let args = value
            .as_array()
            .filter(|values| values.len() == 1)
            .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
        let envelope = exact_object(&args[0], &["correlationId", "results"])?;
        if string(envelope, "correlationId", 128)? != CORE_HELLO_CORRELATION {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        let results = array(envelope, "results")?;
        if results.len() != 1 {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        let result = results[0]
            .as_object()
            .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
        let required_result_keys = ["correlationId", "data", "handlerId", "ok"];
        if !(has_exact_keys(result, &required_result_keys)
            || has_exact_keys(
                result,
                &["correlationId", "data", "durationMs", "handlerId", "ok"],
            ))
            || string(result, "correlationId", 128)? != CORE_HELLO_CORRELATION
            || result
                .get("durationMs")
                .is_some_and(|value| !valid_duration_ms(value))
        {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        if string(result, "handlerId", 128)? != DEVELOPMENT_HANDLER_ID {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        if result.get("ok") != Some(&Value::Bool(true)) {
            return Err(DevelopmentSessionError::CoreDenied);
        }
        let data = result
            .get("data")
            .and_then(Value::as_object)
            .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
        let pairing_only_keys = [
            "browser_control_ready",
            "connector_binding",
            "connector_session_ready",
            "development",
            "features",
            "principal_type",
            "protocol",
            "reason_code",
        ];
        let limited_keys = [
            "browser_control_ready",
            "connector_binding",
            "connector_session_ready",
            "development_admission",
            "features",
            "host_browser",
            "principal_type",
            "protocol",
        ];
        let pairing_only = has_exact_keys(data, &pairing_only_keys);
        let limited = has_exact_keys(data, &limited_keys);
        if !pairing_only && !limited {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        if string(data, "protocol", 64)? != CONNECTOR_PROTOCOL
            || string(data, "principal_type", 64)? != DEVELOPMENT_PRINCIPAL_TYPE
        {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        let binding = self.validate_connector_binding(data, namespace_sid)?;
        if pairing_only {
            if data.get("connector_session_ready") != Some(&Value::Bool(false))
                || data.get("browser_control_ready") != Some(&Value::Bool(false))
                || string(data, "reason_code", 96)? != "development_runtime_not_available"
                || !array(data, "features")?.is_empty()
            {
                return Err(DevelopmentSessionError::CoreDenied);
            }
            validate_pairing_only_development(data)?;
            return Ok(DevelopmentHelloOutcome::PairingOnly(binding));
        }
        if data.get("connector_session_ready") != Some(&Value::Bool(true))
            || data.get("browser_control_ready") != Some(&Value::Bool(true))
        {
            return Err(DevelopmentSessionError::CoreDenied);
        }
        exact_string_array(data, "features", LIMITED_OUTER_FEATURES)?;
        self.validate_development_admission(data)?;
        if data.get("host_browser") != Some(&self.host_browser()) {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        Ok(DevelopmentHelloOutcome::LimitedRuntime(
            DevelopmentRuntimeRoute {
                server_instance_id: binding.server_instance_id,
                bridge_id: binding.bridge_id,
                connector_sid: binding.connector_sid,
                key_generation: binding.key_generation,
                extension_id: self.extension_id.clone(),
                extension_version: self.extension_version.clone(),
                install_instance_id: self.install_instance_id.clone(),
                load_generation_id: binding.load_generation_id,
                companion_instance_id: self.companion_instance_id.clone(),
                browser_family: self.browser_family.clone(),
                browser_version: self.browser_version.clone(),
                browser_label: self.browser_label.clone(),
            },
        ))
    }

    fn hello_data(&self) -> Value {
        object(&[
            ("protocol", text(CONNECTOR_PROTOCOL)),
            ("features", strings(LIMITED_OUTER_FEATURES)),
            (
                "development",
                object(&[
                    ("contract", text(DEVELOPMENT_RUNTIME_CONTRACT)),
                    ("channel", text(DEVELOPMENT_CHANNEL)),
                    ("mode", text("limited_runtime")),
                ]),
            ),
            ("host_browser", self.host_browser()),
        ])
    }

    fn host_browser(&self) -> Value {
        object(&[
            ("supported", Value::Bool(true)),
            ("enabled", Value::Bool(true)),
            ("status", text("ready")),
            ("backend_id", text("chrome_extension")),
            (
                "browser_id",
                text(&format!("development-extension:{}", self.bridge_id)),
            ),
            ("browser_label", text(&self.browser_label)),
            ("contract_version", Value::Number("1".into())),
            ("features", strings(&["browser_extension_bridge_v1"])),
            (
                "capabilities",
                object(&[
                    ("actions", strings(LIMITED_ACTIONS)),
                    ("features", strings(LIMITED_FEATURES)),
                    (
                        "limits",
                        object(&[(
                            "max_json_frame_bytes",
                            Value::Number(crate::rpc::MAX_NATIVE_FRAME_BYTES.to_string()),
                        )]),
                    ),
                ]),
            ),
            (
                "extension",
                object(&[
                    ("id", text(&self.extension_id)),
                    ("version", text(&self.extension_version)),
                    ("manifest_version", Value::Number("3".into())),
                    ("install_instance_id", text(&self.install_instance_id)),
                    ("load_generation_id", text(&self.load_generation_id)),
                ]),
            ),
            (
                "companion",
                object(&[
                    ("instance_id", text(&self.companion_instance_id)),
                    ("version", text(COMPANION_VERSION)),
                    ("platform", text(runtime_platform())),
                    ("arch", text(crate::platform::architecture())),
                ]),
            ),
        ])
    }

    fn validate_connector_binding(
        &self,
        data: &BTreeMap<String, Value>,
        namespace_sid: &str,
    ) -> Result<DevelopmentSessionBinding, DevelopmentSessionError> {
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
        if opaque(binding, "server_instance_id")? != self.server_instance_id
            || opaque(binding, "bridge_id")? != self.bridge_id
            || opaque(binding, "connector_sid")? != namespace_sid
            || integer(binding, "key_generation")? != u64::from(self.key_generation)
            || opaque(binding, "load_generation_id")? != self.load_generation_id
        {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        Ok(DevelopmentSessionBinding {
            server_instance_id: self.server_instance_id.clone(),
            bridge_id: self.bridge_id.clone(),
            connector_sid: namespace_sid.to_owned(),
            key_generation: self.key_generation,
            load_generation_id: self.load_generation_id.clone(),
        })
    }

    fn validate_development_admission(
        &self,
        data: &BTreeMap<String, Value>,
    ) -> Result<(), DevelopmentSessionError> {
        let admission = exact_object_value(
            data,
            "development_admission",
            &[
                "bridge_id",
                "channel",
                "contract",
                "extension_id",
                "install_instance_id",
                "key_generation",
                "load_generation_id",
                "mode",
                "selection_scope",
                "server_instance_id",
                "transports",
            ],
        )?;
        if string(admission, "contract", 128)? != DEVELOPMENT_RUNTIME_CONTRACT
            || string(admission, "channel", 64)? != DEVELOPMENT_CHANNEL
            || string(admission, "mode", 32)? != "limited_runtime"
            || string(admission, "selection_scope", 64)? != "explicit_context_bridge"
            || opaque(admission, "server_instance_id")? != self.server_instance_id
            || opaque(admission, "bridge_id")? != self.bridge_id
            || integer(admission, "key_generation")? != u64::from(self.key_generation)
            || string(admission, "extension_id", 32)? != self.extension_id
            || opaque(admission, "install_instance_id")? != self.install_instance_id
            || opaque(admission, "load_generation_id")? != self.load_generation_id
        {
            return Err(DevelopmentSessionError::InvalidCorePacket);
        }
        exact_string_array(admission, "transports", LIMITED_TRANSPORTS)
    }
}

fn validate_pairing_only_development(
    fields: &BTreeMap<String, Value>,
) -> Result<(), DevelopmentSessionError> {
    let development = exact_object_value(fields, "development", &["channel", "contract", "mode"])?;
    if string(development, "contract", 128)? != DEVELOPMENT_SESSION_CONTRACT
        || string(development, "channel", 64)? != DEVELOPMENT_CHANNEL
        || string(development, "mode", 32)? != "pairing_only"
    {
        return Err(DevelopmentSessionError::InvalidCorePacket);
    }
    Ok(())
}

fn object(fields: &[(&str, Value)]) -> Value {
    Value::Object(
        fields
            .iter()
            .map(|(key, value)| ((*key).to_owned(), value.clone()))
            .collect(),
    )
}

fn text(value: &str) -> Value {
    Value::String(value.to_owned())
}

fn strings(values: &[&str]) -> Value {
    Value::Array(values.iter().map(|value| text(value)).collect())
}

fn has_exact_keys(fields: &BTreeMap<String, Value>, expected: &[&str]) -> bool {
    fields.len() == expected.len() && expected.iter().all(|key| fields.contains_key(*key))
}

fn exact_string_array(
    fields: &BTreeMap<String, Value>,
    key: &str,
    expected: &[&str],
) -> Result<(), DevelopmentSessionError> {
    let actual = array(fields, key)?
        .iter()
        .map(Value::as_str)
        .collect::<Option<Vec<_>>>()
        .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
    if actual != expected {
        return Err(DevelopmentSessionError::InvalidCorePacket);
    }
    Ok(())
}

fn valid_nonce(value: &str) -> bool {
    value.len() == 43
        && URL_SAFE_NO_PAD
            .decode(value)
            .is_ok_and(|bytes| bytes.len() == 32 && URL_SAFE_NO_PAD.encode(bytes) == value)
}

fn valid_core_correlation(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

pub(crate) fn validate_development_core_event_envelope(
    fields: &BTreeMap<String, Value>,
) -> Result<(), DevelopmentSessionError> {
    if !has_exact_keys(
        fields,
        &["correlationId", "data", "eventId", "handlerId", "ts"],
    ) || string(fields, "handlerId", 128)? != DEVELOPMENT_HANDLER_ID
        || !valid_core_correlation(string(fields, "correlationId", 128)?)
        || !fields
            .get("eventId")
            .and_then(Value::as_str)
            .is_some_and(crate::rpc::valid_opaque_id)
        || !fields
            .get("ts")
            .and_then(Value::as_str)
            .is_some_and(|value| {
                !value.is_empty() && value.len() <= 64 && !value.chars().any(char::is_control)
            })
        || !fields
            .get("data")
            .is_some_and(|value| value.as_object().is_some())
    {
        return Err(DevelopmentSessionError::InvalidCorePacket);
    }
    Ok(())
}

fn valid_duration_ms(value: &Value) -> bool {
    let Value::Number(value) = value else {
        return false;
    };
    value
        .parse::<f64>()
        .is_ok_and(|duration| duration.is_finite() && (0.0..=60_000.0).contains(&duration))
}

fn exact_object<'a>(
    value: &'a Value,
    expected: &[&str],
) -> Result<&'a BTreeMap<String, Value>, DevelopmentSessionError> {
    let fields = value
        .as_object()
        .ok_or(DevelopmentSessionError::InvalidCorePacket)?;
    if fields.len() != expected.len() || expected.iter().any(|key| !fields.contains_key(*key)) {
        return Err(DevelopmentSessionError::InvalidCorePacket);
    }
    Ok(fields)
}

fn exact_object_value<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    expected: &[&str],
) -> Result<&'a BTreeMap<String, Value>, DevelopmentSessionError> {
    exact_object(
        fields
            .get(key)
            .ok_or(DevelopmentSessionError::InvalidCorePacket)?,
        expected,
    )
}

fn string<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
    max: usize,
) -> Result<&'a str, DevelopmentSessionError> {
    fields
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.len() <= max)
        .ok_or(DevelopmentSessionError::InvalidCorePacket)
}

fn opaque<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a str, DevelopmentSessionError> {
    string(fields, key, 256).and_then(|value| {
        valid_opaque_id(value)
            .then_some(value)
            .ok_or(DevelopmentSessionError::InvalidCorePacket)
    })
}

fn integer(fields: &BTreeMap<String, Value>, key: &str) -> Result<u64, DevelopmentSessionError> {
    fields
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(DevelopmentSessionError::InvalidCorePacket)
}

fn array<'a>(
    fields: &'a BTreeMap<String, Value>,
    key: &str,
) -> Result<&'a [Value], DevelopmentSessionError> {
    fields
        .get(key)
        .and_then(Value::as_array)
        .ok_or(DevelopmentSessionError::InvalidCorePacket)
}

#[cfg(test)]
pub(crate) fn fixture_runtime_hello() -> ExtensionRuntimeHello {
    let invocation =
        crate::native_host::NativeInvocation::fixture(crate::DEVELOPMENT_EXTENSION_ORIGIN);
    let params = crate::json::parse(
        br#"{
          "protocol":"a0.browser-bridge",
          "contract":{"min":1,"max":1},
          "extension":{"id":"paoagmddepkmonpeboobaijlenlcokpc","version":"0.1.0","manifest_version":3,"install_instance_id":"install-dev-fixture","load_generation_id":"load-dev-fixture"},
          "browser":{"family":"chrome","version":"146.0.0.0"},
          "capabilities":{"actions":["content","ensure","list","navigate","open","scroll","state","status"],"features":["cursor_v1","semantic_dom_v1","tab_groups_v1","tab_leases_v1"],"cdp_domains":[]},
          "resume":{"event_cursors":[],"inflight_op_ids":[],"lease_digest":"sha256:0000000000000000000000000000000000000000000000000000000000000000"}
        }"#,
    )
    .unwrap();
    ExtensionRuntimeHello::from_validated_invocation(&invocation, &params).unwrap()
}

#[cfg(test)]
pub(crate) fn fixture_expectation() -> DevelopmentHelloExpectation {
    let hello = fixture_runtime_hello();
    DevelopmentHelloExpectation::from_credential(&hello, &CredentialRecord::fixture_development())
        .unwrap()
}

#[cfg(test)]
pub(crate) fn fixture_runtime_route() -> DevelopmentRuntimeRoute {
    DevelopmentRuntimeRoute {
        server_instance_id: "server-dev-fixture".into(),
        bridge_id: "bridge-dev-fixture".into(),
        connector_sid: "sid-dev-fixture".into(),
        key_generation: 1,
        extension_id: crate::DEVELOPMENT_EXTENSION_ID.into(),
        extension_version: "0.1.0".into(),
        install_instance_id: "install-dev-fixture".into(),
        load_generation_id: "load-dev-fixture".into(),
        companion_instance_id: "companion-dev-fixture".into(),
        browser_family: "chrome".into(),
        browser_version: "146.0.0.0".into(),
        browser_label: "Chrome".into(),
    }
}

#[cfg(test)]
pub(crate) fn fixture_limited_ack_data(namespace_sid: &str) -> Value {
    let expectation = fixture_expectation();
    object(&[
        ("protocol", text(CONNECTOR_PROTOCOL)),
        ("principal_type", text(DEVELOPMENT_PRINCIPAL_TYPE)),
        ("features", strings(LIMITED_OUTER_FEATURES)),
        ("connector_session_ready", Value::Bool(true)),
        ("browser_control_ready", Value::Bool(true)),
        (
            "development_admission",
            object(&[
                ("contract", text(DEVELOPMENT_RUNTIME_CONTRACT)),
                ("channel", text(DEVELOPMENT_CHANNEL)),
                ("mode", text("limited_runtime")),
                ("transports", strings(LIMITED_TRANSPORTS)),
                ("selection_scope", text("explicit_context_bridge")),
                ("server_instance_id", text(&expectation.server_instance_id)),
                ("bridge_id", text(&expectation.bridge_id)),
                (
                    "key_generation",
                    Value::Number(expectation.key_generation.to_string()),
                ),
                ("extension_id", text(&expectation.extension_id)),
                (
                    "install_instance_id",
                    text(&expectation.install_instance_id),
                ),
                ("load_generation_id", text(&expectation.load_generation_id)),
            ]),
        ),
        (
            "connector_binding",
            object(&[
                ("server_instance_id", text(&expectation.server_instance_id)),
                ("bridge_id", text(&expectation.bridge_id)),
                ("connector_sid", text(namespace_sid)),
                (
                    "key_generation",
                    Value::Number(expectation.key_generation.to_string()),
                ),
                ("load_generation_id", text(&expectation.load_generation_id)),
            ]),
        ),
        ("host_browser", expectation.host_browser()),
    ])
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signature, VerifyingKey};

    const CLIENT_NONCE: &str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    const PUBLIC_KEY: [u8; 32] = [
        0xd7, 0x5a, 0x98, 0x01, 0x82, 0xb1, 0x0a, 0xb7, 0xd5, 0x4b, 0xfe, 0xd3, 0xc9, 0x64, 0x07,
        0x3a, 0x0e, 0xe1, 0x72, 0xf3, 0xda, 0xa6, 0x23, 0x25, 0xaf, 0x02, 0x1a, 0x68, 0xf7, 0x07,
        0x51, 0x1a,
    ];

    fn fixture() -> Value {
        crate::json::parse(include_bytes!(
            "../tests/fixtures/development-session-v1.json"
        ))
        .unwrap()
    }

    fn limited_runtime_fixture() -> Value {
        crate::json::parse(include_bytes!(
            "../tests/fixtures/limited-development-runtime-v1.json"
        ))
        .unwrap()
    }

    fn fixture_field<'a>(fixture: &'a Value, key: &str) -> &'a Value {
        fixture.as_object().unwrap().get(key).unwrap()
    }

    fn object_mut(value: &mut Value) -> &mut BTreeMap<String, Value> {
        let Value::Object(fields) = value else {
            panic!("fixture value must be an object")
        };
        fields
    }

    fn ack(data: Value) -> Value {
        Value::Array(vec![object(&[
            ("correlationId", text(CORE_HELLO_CORRELATION)),
            (
                "results",
                Value::Array(vec![object(&[
                    ("handlerId", text(DEVELOPMENT_HANDLER_ID)),
                    ("ok", Value::Bool(true)),
                    ("correlationId", text(CORE_HELLO_CORRELATION)),
                    ("data", data),
                ])]),
            ),
        ])])
    }

    fn ack_result_mut(value: &mut Value) -> &mut BTreeMap<String, Value> {
        let Value::Array(arguments) = value else {
            panic!("ACK must be an argument array")
        };
        let Value::Object(envelope) = &mut arguments[0] else {
            panic!("ACK argument must be an object")
        };
        let Value::Array(results) = envelope.get_mut("results").unwrap() else {
            panic!("ACK results must be an array")
        };
        let Value::Object(result) = &mut results[0] else {
            panic!("ACK result must be an object")
        };
        result
    }

    #[test]
    fn development_ack_requires_actual_core_result_correlation_and_bounded_duration() {
        let expectation = fixture_expectation();
        let data = fixture_field(&fixture(), "hello_ack_data").clone();
        assert!(expectation
            .parse_ack(&ack(data.clone()), "sid-dev-fixture")
            .is_ok());

        let mut timed = ack(data.clone());
        ack_result_mut(&mut timed).insert("durationMs".into(), Value::Number("1.25".into()));
        assert!(expectation.parse_ack(&timed, "sid-dev-fixture").is_ok());

        let mut wrong_correlation = ack(data.clone());
        ack_result_mut(&mut wrong_correlation)
            .insert("correlationId".into(), text("other-correlation"));
        let mut excessive_duration = ack(data.clone());
        ack_result_mut(&mut excessive_duration)
            .insert("durationMs".into(), Value::Number("60000.01".into()));
        let mut unknown_field = ack(data);
        ack_result_mut(&mut unknown_field).insert("unexpected".into(), Value::Bool(true));
        for invalid in [wrong_correlation, excessive_duration, unknown_field] {
            assert_eq!(
                expectation.parse_ack(&invalid, "sid-dev-fixture"),
                Err(DevelopmentSessionError::InvalidCorePacket)
            );
        }
    }

    fn limited_ack_data(expectation: &DevelopmentHelloExpectation, sid: &str) -> Value {
        object(&[
            ("protocol", text(CONNECTOR_PROTOCOL)),
            ("principal_type", text(DEVELOPMENT_PRINCIPAL_TYPE)),
            ("features", strings(LIMITED_OUTER_FEATURES)),
            ("connector_session_ready", Value::Bool(true)),
            ("browser_control_ready", Value::Bool(true)),
            (
                "development_admission",
                object(&[
                    ("contract", text(DEVELOPMENT_RUNTIME_CONTRACT)),
                    ("channel", text(DEVELOPMENT_CHANNEL)),
                    ("mode", text("limited_runtime")),
                    ("transports", strings(LIMITED_TRANSPORTS)),
                    ("selection_scope", text("explicit_context_bridge")),
                    ("server_instance_id", text(&expectation.server_instance_id)),
                    ("bridge_id", text(&expectation.bridge_id)),
                    (
                        "key_generation",
                        Value::Number(expectation.key_generation.to_string()),
                    ),
                    ("extension_id", text(&expectation.extension_id)),
                    (
                        "install_instance_id",
                        text(&expectation.install_instance_id),
                    ),
                    ("load_generation_id", text(&expectation.load_generation_id)),
                ]),
            ),
            (
                "connector_binding",
                object(&[
                    ("server_instance_id", text(&expectation.server_instance_id)),
                    ("bridge_id", text(&expectation.bridge_id)),
                    ("connector_sid", text(sid)),
                    (
                        "key_generation",
                        Value::Number(expectation.key_generation.to_string()),
                    ),
                    ("load_generation_id", text(&expectation.load_generation_id)),
                ]),
            ),
            ("host_browser", expectation.host_browser()),
        ])
    }

    #[test]
    fn development_fixture_proof_and_ack_are_exact_and_signed() {
        let fixture = fixture();
        let expectation = fixture_expectation();
        let credential = CredentialRecord::fixture_development();
        assert_eq!(
            expectation.challenge_request(CLIENT_NONCE),
            fixture_field(&fixture, "challenge_request").clone()
        );

        let (auth, expires_at_ms) = expectation
            .signed_auth(
                &credential,
                CLIENT_NONCE,
                fixture_field(&fixture, "challenge_response")
                    .encode()
                    .as_bytes(),
                fixture_field(&fixture, "now_ms").as_u64().unwrap(),
            )
            .unwrap();
        assert_eq!(expires_at_ms, 1_700_000_060_000);
        let auth = crate::json::parse(auth.strip_prefix("40/ws,").unwrap().as_bytes()).unwrap();
        let auth = exact_object(&auth, &["handlers", "principal"]).unwrap();
        assert_eq!(
            auth.get("handlers"),
            Some(&Value::Array(vec![text(DEVELOPMENT_HANDLER_PATH)]))
        );
        let principal =
            exact_object_value(auth, "principal", &["proof", "signature", "type"]).unwrap();
        assert_eq!(
            principal.get("type"),
            Some(&text(DEVELOPMENT_PRINCIPAL_TYPE))
        );
        let proof = principal.get("proof").unwrap();
        assert_eq!(proof, fixture_field(&fixture, "proof"));
        assert_eq!(
            proof.encode(),
            fixture_field(&fixture, "canonical_proof").as_str().unwrap()
        );
        let signature_text = principal.get("signature").unwrap().as_str().unwrap();
        assert_eq!(
            signature_text,
            fixture_field(&fixture, "signature").as_str().unwrap()
        );
        let signature_bytes = URL_SAFE_NO_PAD.decode(signature_text).unwrap();
        let signature = Signature::from_slice(&signature_bytes).unwrap();
        VerifyingKey::from_bytes(&PUBLIC_KEY)
            .unwrap()
            .verify_strict(proof.encode().as_bytes(), &signature)
            .unwrap();

        let hello_packet = expectation.packet();
        let hello_packet =
            crate::json::parse(hello_packet.strip_prefix("42/ws,1").unwrap().as_bytes()).unwrap();
        let hello_packet = hello_packet.as_array().unwrap();
        assert_eq!(hello_packet.len(), 2);
        assert_eq!(hello_packet[0], text("connector_hello"));
        let hello_envelope = exact_object(&hello_packet[1], &["correlationId", "data"]).unwrap();
        assert_eq!(
            hello_envelope.get("correlationId"),
            Some(&text(CORE_HELLO_CORRELATION))
        );
        let candidate = hello_envelope.get("data").unwrap().as_object().unwrap();
        assert!(has_exact_keys(
            candidate,
            &["development", "features", "host_browser", "protocol"]
        ));
        assert_eq!(
            candidate.get("features"),
            Some(&strings(LIMITED_OUTER_FEATURES))
        );
        assert_eq!(
            candidate.get("host_browser"),
            Some(&expectation.host_browser())
        );

        let outcome = expectation
            .parse_ack(
                &ack(fixture_field(&fixture, "hello_ack_data").clone()),
                "sid-dev-fixture",
            )
            .unwrap();
        let DevelopmentHelloOutcome::PairingOnly(binding) = outcome else {
            panic!("fixture must remain pairing-only")
        };
        assert_eq!(binding.server_instance_id, "server-dev-fixture");
        assert_eq!(binding.bridge_id, "bridge-dev-fixture");
        assert_eq!(binding.connector_sid, "sid-dev-fixture");
        assert_eq!(binding.key_generation, 1);
        assert_eq!(binding.load_generation_id, "load-dev-fixture");
    }

    #[test]
    fn development_schema_rejects_wrong_profile_generation_and_readiness() {
        let fixture = fixture();
        let expectation = fixture_expectation();
        let base = fixture_field(&fixture, "hello_ack_data").clone();

        let mut ready = base.clone();
        object_mut(&mut ready).insert("browser_control_ready".into(), Value::Bool(true));
        assert_eq!(
            expectation.parse_ack(&ack(ready), "sid-dev-fixture"),
            Err(DevelopmentSessionError::CoreDenied)
        );

        let mut wrong_generation = base.clone();
        let binding = object_mut(&mut wrong_generation)
            .get_mut("connector_binding")
            .unwrap();
        object_mut(binding).insert("load_generation_id".into(), text("load-other"));
        assert_eq!(
            expectation.parse_ack(&ack(wrong_generation), "sid-dev-fixture"),
            Err(DevelopmentSessionError::InvalidCorePacket)
        );
        assert_eq!(
            expectation.parse_ack(&ack(base.clone()), "sid-other"),
            Err(DevelopmentSessionError::InvalidCorePacket)
        );

        let mut extra = base;
        object_mut(&mut extra).insert("activation".into(), Value::Null);
        assert_eq!(
            expectation.parse_ack(&ack(extra), "sid-dev-fixture"),
            Err(DevelopmentSessionError::InvalidCorePacket)
        );

        let credential = CredentialRecord::fixture_development();
        let mut wrong_channel = fixture_field(&fixture, "challenge_response").clone();
        object_mut(&mut wrong_channel).insert("channel".into(), text("production"));
        assert!(expectation
            .signed_auth(
                &credential,
                CLIENT_NONCE,
                wrong_channel.encode().as_bytes(),
                fixture_field(&fixture, "now_ms").as_u64().unwrap(),
            )
            .is_err());

        let mut wrong_profile = CredentialRecord::fixture_development();
        wrong_profile.install_instance_id = "install-other".into();
        assert_eq!(
            DevelopmentHelloExpectation::from_credential(&fixture_runtime_hello(), &wrong_profile,),
            Err(DevelopmentSessionError::InvalidBinding)
        );
    }

    #[test]
    fn limited_runtime_ack_is_exact_identity_bound_and_pathless() {
        let expectation = fixture_expectation();
        let outcome = expectation
            .parse_ack(
                &ack(limited_ack_data(&expectation, "sid-dev-fixture")),
                "sid-dev-fixture",
            )
            .unwrap();
        let DevelopmentHelloOutcome::LimitedRuntime(route) = outcome else {
            panic!("limited ACK must construct the limited route")
        };
        assert_eq!(route.server_instance_id(), "server-dev-fixture");
        assert_eq!(route.bridge_id(), "bridge-dev-fixture");
        assert_eq!(route.connector_sid(), "sid-dev-fixture");
        assert_eq!(route.key_generation(), 1);
        assert_eq!(route.extension_id(), DEVELOPMENT_EXTENSION_ID);
        assert_eq!(route.install_instance_id(), "install-dev-fixture");
        assert_eq!(route.load_generation_id(), "load-dev-fixture");
        assert_eq!(route.browser_identity().0, "chrome");
        assert_eq!(
            route.native_negotiated(),
            object(&[
                ("actions", strings(LIMITED_ACTIONS)),
                ("features", strings(LIMITED_FEATURES)),
            ])
        );
        let admission = route.native_admission();
        assert!(has_exact_keys(
            admission.as_object().unwrap(),
            &[
                "bridge_id",
                "channel",
                "contract",
                "extension_id",
                "install_instance_id",
                "key_generation",
                "load_generation_id",
                "mode",
                "selection_scope",
                "server_instance_id",
                "transports",
            ]
        ));
        assert_eq!(
            admission.as_object().unwrap().get("transports"),
            Some(&strings(LIMITED_TRANSPORTS))
        );
        assert!(!admission.encode().contains("connector_sid"));

        let mut mixed = limited_ack_data(&expectation, "sid-dev-fixture");
        object_mut(&mut mixed).insert("activation".into(), Value::Null);
        assert_eq!(
            expectation.parse_ack(&ack(mixed), "sid-dev-fixture"),
            Err(DevelopmentSessionError::InvalidCorePacket)
        );
    }

    #[test]
    fn shared_limited_runtime_fixture_matches_candidate_ack_and_native_admission() {
        let fixture = limited_runtime_fixture();
        let fixture = fixture.as_object().unwrap();
        let expectation = fixture_expectation();

        let packet = expectation.packet();
        let packet =
            crate::json::parse(packet.strip_prefix("42/ws,1").unwrap().as_bytes()).unwrap();
        let packet = packet.as_array().unwrap();
        let envelope = exact_object(&packet[1], &["correlationId", "data"]).unwrap();
        assert_eq!(envelope.get("data"), fixture.get("candidate"));

        let ack_data = fixture.get("ack").unwrap().clone();
        let outcome = expectation
            .parse_ack(&ack(ack_data.clone()), "sid-dev-fixture")
            .unwrap();
        let DevelopmentHelloOutcome::LimitedRuntime(route) = outcome else {
            panic!("shared fixture must produce a limited development route")
        };
        assert_eq!(
            route.native_admission(),
            ack_data
                .as_object()
                .unwrap()
                .get("development_admission")
                .unwrap()
                .clone()
        );
        assert!(!route.native_admission().encode().contains("connector_sid"));
    }
}
