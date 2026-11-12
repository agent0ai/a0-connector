//! Secret Service access without an unbounded interactive unlock prompt.

use dbus_secret_service::{EncryptionType, Item, SecretService};
use keyring::secret_service::{decode_error as error, get_collection, SsCredential};
use std::collections::HashMap;

pub(super) struct Entry {
    metadata: SsCredential,
}

impl Entry {
    pub(super) fn new(service: &str, name: &str) -> keyring::Result<Self> {
        Ok(Self {
            metadata: SsCredential::new_with_target(None, service, name)?,
        })
    }

    fn session() -> keyring::Result<SecretService> {
        SecretService::connect_with_max_prompt_timeout(EncryptionType::Dh, 0).map_err(error)
    }

    fn item<'a>(&self, service: &'a SecretService) -> keyring::Result<Option<Item<'a>>> {
        let mut attributes = HashMap::from([
            ("service", self.metadata.attributes["service"].as_str()),
            ("username", self.metadata.attributes["username"].as_str()),
            ("target", self.metadata.attributes["target"].as_str()),
        ]);
        let search = service.search_items(attributes.clone()).map_err(error)?;
        if !search.locked.is_empty() {
            return Err(error(dbus_secret_service::Error::Locked));
        }
        let mut items = search.unlocked;
        if items.is_empty() {
            // Preserve keyring's default-collection lookup for older attribute sets.
            attributes.remove("target");
            let collection = get_collection(service, "default")?;
            items = collection
                .search_items(attributes)
                .map_err(error)?
                .into_iter()
                .map(|item| Item::new(service, item.path))
                .collect();
        }
        if items.len() > 1 {
            return Err(error(dbus_secret_service::Error::Parse));
        }
        if let Some(item) = items.first() {
            item.ensure_unlocked().map_err(error)?;
        }
        Ok(items.pop())
    }

    pub(super) fn get_secret(&self) -> keyring::Result<Vec<u8>> {
        let service = Self::session()?;
        self.item(&service)?
            .ok_or(keyring::Error::NoEntry)?
            .get_secret()
            .map_err(error)
    }

    pub(super) fn set_secret(&self, secret: &[u8]) -> keyring::Result<()> {
        let service = Self::session()?;
        if let Some(item) = self.item(&service)? {
            return item.set_secret(secret, "text/plain").map_err(error);
        }
        let collection = get_collection(&service, "default")?;
        collection
            .create_item(
                &self.metadata.label,
                self.metadata
                    .attributes
                    .iter()
                    .map(|(key, value)| (key.as_str(), value.as_str()))
                    .collect(),
                secret,
                true,
                "text/plain",
            )
            .map_err(error)?;
        Ok(())
    }

    pub(super) fn delete_credential(&self) -> keyring::Result<()> {
        let service = Self::session()?;
        self.item(&service)?
            .ok_or(keyring::Error::NoEntry)?
            .delete()
            .map_err(error)
    }
}
