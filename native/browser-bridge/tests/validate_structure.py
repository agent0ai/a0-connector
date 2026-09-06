#!/usr/bin/env python3
"""Dependency-free structural checks for the browser bridge foundation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def toml_string(path: Path, section: str, key: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^\[{re.escape(section)}\]\s*"
        rf"(?:(?!^\[).)*?^{re.escape(key)}\s*=\s*\"([^\"]+)\"\s*$",
        text,
    )
    if match is None:
        raise AssertionError(f"missing [{section}] {key} in {path}")
    return match.group(1)


class BrowserBridgeStructureTests(unittest.TestCase):
    def test_scoped_catalog_schema_preserves_v1_and_declares_complete_groups(self) -> None:
        v1 = load_json("schemas/release-catalog-v1.schema.json")
        v2 = load_json("schemas/release-catalog-v2.schema.json")
        self.assertEqual(v1["properties"]["schema_version"], {"const": 1})
        self.assertNotIn("platforms", v1["properties"])
        self.assertEqual(v1["properties"]["artifacts"]["minItems"], 9)
        self.assertEqual(v1["properties"]["artifacts"]["maxItems"], 9)
        self.assertEqual(v2["properties"]["schema_version"], {"const": 2})
        self.assertEqual(set(v2["required"]), set(v1["required"]) | {"platforms"})
        self.assertFalse(v2["additionalProperties"])
        scopes = v2["properties"]["platforms"]["enum"]
        self.assertEqual(len(scopes), 7)
        for scope in scopes:
            self.assertEqual(scope, sorted(set(scope)))
            self.assertTrue(set(scope) <= {"linux", "macos", "windows"})
            self.assertTrue(scope)
        expected = {"macos": 2, "windows": 4, "linux": 3}
        for branch in v2["allOf"]:
            platform = branch["if"]["properties"]["platforms"]["contains"]["const"]
            references = branch["then"]["properties"]["artifacts"]["allOf"]
            self.assertEqual(len(references), expected.pop(platform))
            for reference in references:
                definition = reference["$ref"].removeprefix("release-catalog-v1.schema.json#/$defs/")
                self.assertEqual(v1["$defs"][definition]["minContains"], 1)
                self.assertEqual(v1["$defs"][definition]["maxContains"], 1)
            self.assertEqual(branch["else"]["properties"]["artifacts"]["not"]["contains"]["properties"]["platform"]["const"], platform)
        self.assertEqual(expected, {})

    def test_companion_version_meets_secure_floor_and_matches_own_lock_entry(self) -> None:
        companion_version = toml_string(ROOT / "Cargo.toml", "package", "version")
        catalog = (ROOT / "src/release_catalog.rs").read_text(encoding="utf-8")
        minimum = re.search(
            r'pub const MINIMUM_SECURE_COMPANION: &str = "([0-9]+\.[0-9]+\.[0-9]+)";',
            catalog,
        )
        self.assertIsNotNone(minimum)

        def version(value: str) -> tuple[int, int, int]:
            match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value)
            if match is None:
                raise AssertionError(f"invalid stable companion version: {value}")
            return tuple(int(part) for part in match.groups())

        assert minimum is not None
        self.assertGreaterEqual(version(companion_version), version(minimum.group(1)))
        lock = (ROOT / "Cargo.lock").read_text(encoding="utf-8")
        package = re.search(
            r'(?ms)^\[\[package\]\]\s*^name = "a0-browser-bridge"\s*^version = "([^"]+)"$',
            lock,
        )
        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(package.group(1), companion_version)

    def test_provisioned_release_roots_match_policy_without_waiving_evidence(self) -> None:
        policy = load_json("release-policy-v1.json")
        publisher, = policy["trusted_release_keys"]
        builder, = policy["trusted_build_provenance_roots"]
        self.assertEqual(publisher["key_id"], "publisher-2026")
        self.assertEqual(builder["key_id"], "builder-2026")
        self.assertEqual(publisher["ed25519_public_key_base64"], "GEOygP0rBYlVYZEx+bgDhUZ3sVpfVyedoI9Jo+bcYII=")
        self.assertEqual(builder["ed25519_public_key_base64"], "bgVolIrNPT7ijoUZHxBGYEDfE5eA6M5+VOojUjDIt1g=")
        self.assertNotEqual(publisher["ed25519_public_key_base64"], builder["ed25519_public_key_base64"])
        origin = "chrome-extension://nhliclifilepdkoolioacpjpijomfplj/"
        self.assertEqual(policy["production_extension_origins"], [origin])
        self.assertEqual(policy["macos_release_identity"], {
            "team_id": "R2KNNFH5FC",
            "signing_identifier": "io.agentzero.browser_bridge",
        })
        self.assertEqual(policy["signature_verifier"], "detached_ed25519_strict")
        self.assertEqual(
            policy["activation"],
            "requires_verified_artifact_platform_provenance_install_and_core_runtime_evidence",
        )
        release_source = (ROOT / "src/release.rs").read_text(encoding="utf-8")
        self.assertIn("RELEASE_SIGNATURE_VERIFIER_READY: bool = true", release_source)
        for key in (publisher, builder):
            public_key = base64.b64decode(key["ed25519_public_key_base64"], validate=True)
            self.assertEqual(len(public_key), 32)
            encoded = re.search(r'key_id: "' + re.escape(key["key_id"]) + r'",\s*ed25519_public_key: \[([^]]+)\]', release_source)
            self.assertIsNotNone(encoded)
            assert encoded is not None
            self.assertEqual(bytes(int(value, 16) for value in re.findall(r"0x([0-9a-f]{2})", encoded.group(1))), public_key)
        self.assertEqual(builder["recipe_sha256"], hashlib.sha256((ROOT / "scripts/build-macos-candidate.mjs").read_bytes()).hexdigest())
        self.assertEqual(builder["rust_toolchain"], "rustc 1.85.1 (4eb161250 2025-03-15)")
        for field in ("builder_id", "source_repository", "recipe_sha256", "rust_toolchain"):
            self.assertIn(f'{field}: "{builder[field]}"', release_source)
        self.assertIn("ed25519_public_key: [u8; 32]", release_source)
        self.assertIn(f'&["{origin}"]', release_source)
        self.assertIn('team_id: "R2KNNFH5FC"', release_source)
        self.assertIn('signing_identifier: "io.agentzero.browser_bridge"', release_source)
        catalog, = policy["pinned_release_catalogs"]
        provenance, = policy["pinned_build_provenance"]
        base = "https://raw.githubusercontent.com/TerminallyLazy/agent-zero-browser-releases/native-v2.12.0-macos-r1/"
        self.assertEqual(catalog, {"release": "2.12.0", "catalog_url": base + "catalog.json", "signature_url": base + "catalog.sig"})
        self.assertEqual(provenance, {"release": "2.12.0", "platform": "macos", "artifact_arch": "universal2", "statement_url": base + "provenance-macos-universal2.json", "signature_url": base + "provenance-macos-universal2.sig"})
        for source in (catalog, provenance):
            for field, value in source.items():
                self.assertIn(f'{field}: "{value}"', release_source)
        for verifier in ("release_catalog.rs", "release_provenance.rs"):
            self.assertIn("verify_strict(", (ROOT / "src" / verifier).read_text(encoding="utf-8"))
        self.assertIn("RELEASE_SIGNATURE_VERIFIER_READY", release_source)
        self.assertIn("release_trust_configured()", release_source)
        self.assertIn("is_exact_release_extension_origin(origin)", release_source)

    def test_install_and_update_have_no_mutating_fallback(self) -> None:
        install = (ROOT / "src/install.rs").read_text(encoding="utf-8")
        transaction = (ROOT / "src/install_transaction.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn("INSTALL_CONTRACT", install)
        self.assertIn('reason_code: "RELEASE_EVIDENCE_UNAVAILABLE"', install)
        self.assertIn('reason_code: "VERIFIED_INSTALL_CANDIDATE_REQUIRED"', install)
        self.assertIn("mutation_allowed: false", install)
        self.assertIn("registration_count: 0", install)
        self.assertIn("pub struct FullyVerifiedInstallCandidate", transaction)
        self.assertIn("fn test_fixture(", transaction)
        self.assertIn("#[cfg(test)]", transaction)
        self.assertIn("release_trust_configured()", transaction)
        self.assertIn("verify_payload_identity", transaction)
        self.assertIn("try_flock_exclusive", transaction)
        self.assertIn("WINDOWS_INSTALL_ADAPTER_UNAVAILABLE", transaction)
        all_source = "\n".join(
            path.read_text(encoding="utf-8") for path in (ROOT / "src").glob("*.rs")
        )
        for forbidden in (
            "std::process::Command",
            "Command::new",
            "/bin/sh",
            "powershell.exe",
        ):
            self.assertNotIn(forbidden, all_source)

    def test_native_framing_is_bounded_native_endian_and_stdout_isolated(self) -> None:
        native = (ROOT / "src/native_host.rs").read_text(encoding="utf-8")
        main = (ROOT / "src/main.rs").read_text(encoding="utf-8")
        self.assertIn("MAX_INBOUND_FRAME_BYTES: usize = 768 * 1024", native)
        self.assertIn("MAX_OUTBOUND_FRAME_BYTES: usize = 768 * 1024", native)
        self.assertIn("u32::from_ne_bytes", native)
        self.assertIn("to_ne_bytes", native)
        self.assertIsNone(re.search(r"(?<!e)println!", native))
        self.assertIsNone(re.search(r"(?<!e)println!", main))
        self.assertIn("Self::Closed => crate::EXIT_OK", native)
        self.assertIn("crate::EXIT_INTEGRITY_OR_POLICY", native)
        self.assertNotIn("EXIT_RELEASE_UNAVAILABLE", main)
        self.assertLess(
            main.index("is_native_host_candidate(&args)"),
            main.index("cli::run(&args)"),
        )

    def test_native_rpc_contract_is_directional_bounded_and_fixture_backed(self) -> None:
        schema = load_json("schemas/native-rpc-v1.schema.json")
        limits = schema["x-a0-limits"]
        self.assertEqual(limits["native_frame_bytes_each_direction"], 768 * 1024)
        self.assertEqual(limits["non_artifact_bytes"], 512 * 1024)
        self.assertEqual(limits["artifact_chunk_raw_bytes"], 192 * 1024)
        self.assertEqual(limits["artifact_bytes"], 25 * 1024 * 1024)
        self.assertEqual(limits["request_timeout_ms"], 120_000)
        self.assertEqual(limits["hello_timeout_ms"], 10_000)

        directions = schema["x-a0-method-directions"]
        all_methods = set(schema["properties"]["method"]["enum"])
        directional_methods = set().union(*map(set, directions.values()))
        self.assertEqual(all_methods, directional_methods)
        self.assertEqual(directions["extension_notifications"], ["browser.event"])
        self.assertEqual(
            directions["server_notifications"],
            ["context.snapshot", "context.event", "context.complete"],
        )
        for forbidden in ("shell.run", "file.read", "exec", "browser.execute"):
            self.assertNotIn(forbidden, all_methods)

        rpc = (ROOT / "src/rpc.rs").read_text(encoding="utf-8")
        session = (ROOT / "src/session.rs").read_text(encoding="utf-8")
        library = (ROOT / "src/lib.rs").read_text(encoding="utf-8")
        for expected in (
            "MAX_NATIVE_FRAME_BYTES: usize = 768 * 1024",
            "MAX_NON_ARTIFACT_BYTES: usize = 512 * 1024",
            "MAX_ARTIFACT_CHUNK_RAW_BYTES: usize = 192 * 1024",
            "MAX_ARTIFACT_BYTES: u64 = 25 * 1024 * 1024",
            "MAX_REQUEST_TIMEOUT_MS: u64 = 120_000",
            "HELLO_TIMEOUT_MS: u64 = 10_000",
        ):
            self.assertIn(expected, rpc)
        self.assertIn("MAX_PENDING_CORRELATIONS: usize = 512", session)
        self.assertIn("MAX_COMPLETED_CORRELATIONS: usize = 2_048", session)
        self.assertIn("PairingOnly", session)
        # Every credential operation is scoped to the validated extension AND
        # profile install instance. Ignore Rust formatting, not this binding.
        compact_session = "".join(session.split())
        self.assertIn(".status(&self.caller_extension_id,&install_instance_id)", compact_session)
        self.assertIn(".exchange(&request.params,&self.caller_extension_id,&install_instance_id", compact_session)
        self.assertIn(".disconnect(&self.caller_extension_id,&install_instance_id)", compact_session)
        self.assertIn("NOT_PAIRED", session)
        self.assertIn("pub mod pairing;", library)
        self.assertIn("pub mod rpc;", library)
        self.assertIn("pub mod session;", library)
        self.assertEqual(
            schema["properties"]["id"]["pattern"],
            r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        )
        pairing = schema["$defs"]["pairingExchangeParams"]
        self.assertEqual(
            set(pairing["required"]),
            {"contract_version", "pairing_code", "server_base_origin"},
        )
        self.assertFalse(pairing["additionalProperties"])
        self.assertIn("fn bounded_rpc_id", rpc)
        self.assertIn("fn valid_server_base_origin", rpc)
        self.assertIn("IpAddr", rpc)

    def test_native_pairing_is_host_owned_transactional_and_secure_store_only(self) -> None:
        cargo = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
        pairing = (ROOT / "src/pairing.rs").read_text(encoding="utf-8")
        for dependency in (
            'base64 = { version = "=0.23.1"',
            'ed25519-dalek = { version = "=3.0.0"',
            'getrandom = "=0.4.3"',
            'keyring = { version = "=3.6.3"',
            'ureq = { version = "=3.4.0"',
            'zeroize = { version = "=1.9.0"',
        ):
            self.assertIn(dependency, cargo)
        for feature in (
            '"apple-native"',
            '"windows-native"',
            '"sync-secret-service"',
            '"crypto-rust"',
            '"platform-verifier"',
            '"rustls"',
        ):
            self.assertIn(feature, cargo)
        for expected in (
            "getrandom::fill",
            "SigningKey::from_bytes",
            "URL_SAFE_NO_PAD",
            "Zeroizing",
            "set_secret",
            "get_secret",
            "delete_credential",
            'max_redirects(0)',
            'proxy(None)',
            'RootCerts::PlatformVerifier',
            '"/api/plugins/_a0_connector/browser_bridge_exchange"',
            'PairingFailure::CredentialCommitFailed',
            'PairingFailure::ExtensionBindingMismatch',
            'PairingFailure::AlreadyPaired',
            '"PAIRING_EXCHANGE_OUTCOME_UNKNOWN"',
            '"SERVER_REVOCATION_REQUIRED"',
        ):
            self.assertIn(expected, pairing)
        self.assertLess(
            pairing.index(".exchange(&endpoint, request.encode().as_bytes())"),
            pairing.index(".save_credential(&credential)"),
        )
        self.assertNotIn("std::fs", pairing)
        self.assertNotIn("File::", pairing)
        self.assertNotIn("plaintext", pairing.split("//!", 1)[-1].split("use ", 1)[-1])

    def test_native_runtime_requires_release_trust_before_stdin(self) -> None:
        native = (ROOT / "src/native_host.rs").read_text(encoding="utf-8")
        main = (ROOT / "src/main.rs").read_text(encoding="utf-8")
        self.assertIn("if !release_trust_configured()", native)
        self.assertIn("PRODUCTION_EXTENSION_ORIGINS.contains", native)
        self.assertLess(
            main.index("validate_invocation(&args)"),
            main.index("run_native_session(&invocation, io::stdin(), &mut stdout)"),
        )
        self.assertRegex(
            native,
            r"#\[cfg\(test\)\]\s+pub\(crate\) fn fixture\(caller_origin: &str\)",
        )
        self.assertRegex(
            (ROOT / "src/session.rs").read_text(encoding="utf-8"),
            r"#\[cfg\(test\)\]\s+pub\(crate\) fn fixture\(",
        )
        invocation = native.split("pub struct NativeInvocation", 1)[1].split("}", 1)[0]
        self.assertNotIn("pub caller_origin", invocation)
        self.assertNotIn("pub parent_window", invocation)

    def test_native_rpc_fixtures_cover_valid_and_fail_closed_envelopes(self) -> None:
        fixture_root = ROOT / "tests/fixtures/native-rpc-v1"
        hello = json.loads((fixture_root / "hello.valid.json").read_text(encoding="utf-8"))
        perform = json.loads(
            (fixture_root / "browser-perform.valid.json").read_text(encoding="utf-8")
        )
        unknown = json.loads(
            (fixture_root / "unknown-method.invalid.json").read_text(encoding="utf-8")
        )
        batch = json.loads((fixture_root / "batch.invalid.json").read_text(encoding="utf-8"))
        pairing = json.loads(
            (fixture_root / "pairing-exchange.valid.json").read_text(encoding="utf-8")
        )
        pairing_extra = json.loads(
            (fixture_root / "pairing-exchange-extra.invalid.json").read_text(
                encoding="utf-8"
            )
        )
        directions = load_json("schemas/native-rpc-v1.schema.json")[
            "x-a0-method-directions"
        ]
        self.assertIn(hello["method"], directions["extension_requests"])
        self.assertEqual(
            hello["params"]["extension"]["id"],
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.assertIn(perform["method"], directions["server_requests"])
        self.assertEqual(perform["params"]["timeout_ms"], 120_000)
        self.assertNotIn(
            unknown["method"],
            set().union(*map(set, directions.values())),
        )
        self.assertIsInstance(batch, list)
        self.assertEqual(
            set(pairing["params"]),
            {"contract_version", "pairing_code", "server_base_origin"},
        )
        self.assertEqual(pairing["params"]["server_base_origin"], "http://localhost:50080")
        self.assertEqual(set(pairing_extra["params"]) - set(pairing["params"]), {"credential"})

    def test_native_protocol_sources_do_not_emit_unframed_stdout(self) -> None:
        for relative in ("src/native_host.rs", "src/rpc.rs", "src/session.rs"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?<!e)println!", source), relative)

    def test_manifest_requires_absolute_path_and_exact_nonwildcard_origins(self) -> None:
        manifest = (ROOT / "src/manifest.rs").read_text(encoding="utf-8")
        release = (ROOT / "src/release.rs").read_text(encoding="utf-8")
        self.assertIn("binary_path.is_absolute()", manifest)
        self.assertIn("extension_id.len() == 32", release)
        self.assertIn("matches!(byte, b'a'..=b'p')", release)
        self.assertIn("!origin.contains('*')", release)
        self.assertIn("is_exact_release_extension_origin(origin)", manifest)
        self.assertIn("release.is_fully_verified()", manifest)

    def test_browser_registry_is_canonical_and_per_user(self) -> None:
        registry = load_json("browser-registry-v1.json")
        registry_source = (ROOT / "src/registry.rs").read_text(encoding="utf-8")
        self.assertEqual(registry["schema_version"], 1)
        self.assertEqual(
            [entry["id"] for entry in registry["browsers"]],
            ["chrome", "edge", "brave", "vivaldi", "opera", "chromium"],
        )
        for entry in registry["browsers"]:
            self.assertIn(entry["id"], registry_source)
            self.assertIn(entry["display_name"], registry_source)
            self.assertIn(entry["macos_manifest_suffix"], registry_source)
            self.assertIn(entry["linux_manifest_suffix"], registry_source)
            self.assertIn(entry["windows_registry_key"], registry_source)
            self.assertTrue(entry["windows_registry_key"].startswith("HKCU\\"))
            self.assertNotIn("HKLM", entry["windows_registry_key"])
            self.assertFalse(entry["macos_manifest_suffix"].startswith("/"))
            self.assertFalse(entry["linux_manifest_suffix"].startswith("/"))
            self.assertNotIn("..", entry["macos_manifest_suffix"])
            self.assertNotIn("..", entry["linux_manifest_suffix"])
            if compatibility := entry.get("linux_compatibility_suffix"):
                self.assertIn(compatibility, registry_source)
        self.assertIn('include_str!("../browser-registry-v1.json")', registry_source)

    def test_release_schema_requires_exact_extension_origin_shape(self) -> None:
        schema = load_json("schemas/release-catalog-v1.schema.json")
        origin_pattern = schema["properties"]["extension_origins"]["items"][
            "pattern"
        ]
        self.assertEqual(origin_pattern, r"^chrome-extension://[a-p]{32}/$")
        self.assertIsNotNone(
            re.fullmatch(
                origin_pattern,
                "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
            )
        )
        self.assertIsNone(re.fullmatch(origin_pattern, "chrome-extension://*/"))

    def test_release_schema_covers_complete_delivery_matrix(self) -> None:
        schema = load_json("schemas/release-catalog-v1.schema.json")
        artifacts = schema["properties"]["artifacts"]
        artifact = schema["$defs"]["artifact"]
        self.assertEqual(artifacts["minItems"], 9)
        self.assertEqual(artifacts["maxItems"], 9)
        self.assertTrue(artifacts["uniqueItems"])
        self.assertIn("download_url", artifact["required"])
        self.assertEqual(
            set(artifact["properties"]["kind"]["enum"]),
            {"installer", "bootstrap", "payload"},
        )
        self.assertEqual(
            set(artifact["properties"]["arch"]["enum"]),
            {"universal2", "x86_64", "arm64", "aarch64", "any"},
        )
        required_matrix = {
            "requiresMacosInstaller",
            "requiresMacosPayload",
            "requiresWindowsX64Installer",
            "requiresWindowsX64Payload",
            "requiresWindowsArm64Installer",
            "requiresWindowsArm64Payload",
            "requiresLinuxBootstrap",
            "requiresLinuxX64Payload",
            "requiresLinuxArm64Payload",
        }
        matrix_refs = {
            item["$ref"].rsplit("/", 1)[-1] for item in artifacts["allOf"]
        }
        self.assertEqual(matrix_refs, required_matrix)

    def test_all_public_output_schemas_are_versioned(self) -> None:
        expected = {
            "schemas/status-v1.schema.json": "a0.browser-bridge.status.v1",
            "schemas/self-test-v1.schema.json": "a0.browser-bridge.self-test.v1",
            "schemas/install-plan-v1.schema.json": "a0.browser-bridge.install-plan.v1",
        }
        for relative, contract in expected.items():
            schema = load_json(relative)
            self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
            self.assertEqual(schema["properties"]["contract"]["const"], contract)


if __name__ == "__main__":
    unittest.main(verbosity=2)
