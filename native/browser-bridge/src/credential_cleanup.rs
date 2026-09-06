//! Advisory, retryable cleanup inventory; never a revocation/deletion permit.
//!
//! CLI has neither a validated Chrome profile nor its admitted Core route. Even
//! an empty OS namespace cannot prove that previously disconnected server keys
//! were revoked. Keep registrations and credentials until the existing exact
//! interactive credential.revoke path has received authoritative Core receipts.
use super::*;

#[cfg(target_os = "macos")]
#[path = "credential_cleanup_macos.rs"]
mod macos;

const MAX_ACCOUNTS: usize = 258; // 128 profiles + rotations + installation + legacy.
const PREFIX: &str = "profile-credential-v2-";
const CONTRACT: &str = "a0.browser-bridge.credential-cleanup-pending.v1";

struct Inventory {
    source: &'static str,
    accounts: Vec<String>,
}

impl Inventory {
    fn unavailable() -> Self {
        Self {
            source: "unavailable",
            accounts: Vec::new(),
        }
    }

    fn from_accounts(accounts: Vec<String>) -> Result<Self, ()> {
        if accounts.len() > MAX_ACCOUNTS {
            return Err(());
        }
        let mut unique = BTreeSet::new();
        for account in accounts {
            if !valid_account(&account) || !unique.insert(account) {
                return Err(());
            }
        }
        Ok(Self {
            source: "macos_keychain_attributes",
            accounts: unique.into_iter().collect(),
        })
    }
}

fn valid_account(account: &str) -> bool {
    if matches!(account, "installation-v1" | "active-credential-v1") {
        return true;
    }
    let Some(digest) = account.strip_prefix(PREFIX) else {
        return false;
    };
    valid_sha256(digest.strip_suffix("-rotation").unwrap_or(digest))
}

pub(super) fn prepare(root: &Path, state_bytes: &[u8]) -> Result<(), InstallTransactionError> {
    // This branch is also unreachable from development lifecycle CLI, but keep
    // compile-profile isolation local to the OS-store boundary as well.
    if cfg!(feature = "local-development") {
        return Err(InstallTransactionError::ReleaseEvidenceUnavailable);
    }
    #[cfg(target_os = "macos")]
    let inventory = macos::accounts()
        .and_then(Inventory::from_accounts)
        .unwrap_or_else(|_| Inventory::unavailable());
    #[cfg(not(target_os = "macos"))]
    let inventory = Inventory::unavailable();
    persist(root, state_bytes, inventory)
}

fn persist(
    root: &Path,
    state_bytes: &[u8],
    inventory: Inventory,
) -> Result<(), InstallTransactionError> {
    verify_private_directory(root)?;
    let path = root.join("credential-cleanup-pending.json");
    if !matches!(fs::symlink_metadata(&path), Err(error) if error.kind() == io::ErrorKind::NotFound)
    {
        let previous = read_owned_evidence(&path, 64 * 1024)?;
        validate_pending(&previous)?;
    }
    let snapshot = json::object(&[
        ("contract", json::quote(CONTRACT)),
        ("schema_version", "1".into()),
        ("install_state_sha256", json::quote(&sha256(state_bytes))),
        ("inventory_source", json::quote(inventory.source)),
        (
            "accounts",
            json::string_array(inventory.accounts.iter().map(String::as_str)),
        ),
        (
            "revocation",
            json::quote("requires_interactive_profile_receipt"),
        ),
        ("credential_deletion_authorized", "false".into()),
        ("registration_retirement_authorized", "false".into()),
    ]);
    validate_pending(snapshot.as_bytes())?;
    atomic_write(&path, snapshot.as_bytes(), 0o600)?;
    if read_owned_evidence(&path, 64 * 1024)? != snapshot.as_bytes() {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    Ok(())
}

fn validate_pending(bytes: &[u8]) -> Result<(), InstallTransactionError> {
    let value = json::parse(bytes).map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    let object = exact_object(
        &value,
        &[
            "contract",
            "schema_version",
            "install_state_sha256",
            "inventory_source",
            "accounts",
            "revocation",
            "credential_deletion_authorized",
            "registration_retirement_authorized",
        ],
        InstallTransactionError::OwnedStateInvalid,
    )?;
    let source = text(object, "inventory_source")?;
    let Some(Value::Array(accounts)) = object.get("accounts") else {
        return Err(InstallTransactionError::OwnedStateInvalid);
    };
    if text(object, "contract")? != CONTRACT
        || number(object, "schema_version")? != 1
        || !valid_sha256(text(object, "install_state_sha256")?)
        || !matches!(source, "unavailable" | "macos_keychain_attributes")
        || text(object, "revocation")? != "requires_interactive_profile_receipt"
        || object.get("credential_deletion_authorized") != Some(&Value::Bool(false))
        || object.get("registration_retirement_authorized") != Some(&Value::Bool(false))
        || (source == "unavailable" && !accounts.is_empty())
    {
        return Err(InstallTransactionError::OwnedStateInvalid);
    }
    let names = accounts
        .iter()
        .map(|value| match value {
            Value::String(name) => Ok(name.clone()),
            _ => Err(()),
        })
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    Inventory::from_accounts(names).map_err(|_| InstallTransactionError::OwnedStateInvalid)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cleanup_inventory_rejects_foreign_duplicate_and_unbounded_slots() {
        let profile = format!("{PREFIX}{}", "a".repeat(64));
        assert!(Inventory::from_accounts(vec![
            profile.clone(),
            format!("{profile}-rotation"),
            "installation-v1".into()
        ])
        .is_ok());
        for values in [
            vec![profile.clone(), profile.clone()],
            vec!["development-installation-v1".into()],
            vec!["arbitrary-service-account".into()],
            vec![profile; MAX_ACCOUNTS + 1],
        ] {
            assert!(Inventory::from_accounts(values).is_err());
        }
    }

    #[test]
    fn cleanup_pending_retry_never_promotes_empty_inventory_to_deletion_authority() {
        let root = std::env::temp_dir().join(format!("a0-cleanup-test-{}", random_id().unwrap()));
        ensure_new_private_dir(&root).unwrap();
        persist(&root, b"state", Inventory::unavailable()).unwrap();
        persist(
            &root,
            b"state",
            Inventory::from_accounts(Vec::new()).unwrap(),
        )
        .unwrap();
        let bytes = fs::read(root.join("credential-cleanup-pending.json")).unwrap();
        validate_pending(&bytes).unwrap();
        let text = String::from_utf8(bytes).unwrap();
        assert!(text.contains("\"credential_deletion_authorized\":false"));
        assert!(text.contains("\"registration_retirement_authorized\":false"));
        assert!(text.contains("requires_interactive_profile_receipt"));
        assert!(validate_pending(
            text.replace(
                "\"credential_deletion_authorized\":false",
                "\"credential_deletion_authorized\":true"
            )
            .as_bytes()
        )
        .is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
