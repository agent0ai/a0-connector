//! Advisory names only. Windows returns blobs with enumeration; erase them
//! immediately without decoding, copying, logging, or granting deletion authority.
use std::ptr::null_mut;
use windows_sys::Win32::{
    Foundation::{GetLastError, ERROR_NOT_FOUND},
    Security::Credentials::{CredEnumerateW, CredFree, CREDENTIALW, CRED_TYPE_GENERIC},
};

const SERVICE: &str = ".io.agentzero.browser_bridge";

unsafe fn bounded_text(pointer: *const u16) -> Result<String, ()> {
    if pointer.is_null() {
        return Err(());
    }
    for length in 0..512 {
        if *pointer.add(length) == 0 {
            return String::from_utf16(std::slice::from_raw_parts(pointer, length)).map_err(|_| ());
        }
    }
    Err(())
}

fn account(target: &str, user: &str, kind: u32) -> Result<Option<String>, ()> {
    let Some(name) = target.strip_suffix(SERVICE) else {
        return Ok(None);
    };
    if kind != CRED_TYPE_GENERIC || name != user || !super::valid_account(name) {
        return Err(());
    }
    Ok(Some(name.to_owned()))
}

struct Entries {
    count: u32,
    pointer: *mut *mut CREDENTIALW,
}
impl Drop for Entries {
    fn drop(&mut self) {
        if self.pointer.is_null() {
            return;
        }
        unsafe {
            for index in 0..self.count as usize {
                let entry = *self.pointer.add(index);
                if entry.is_null() {
                    continue;
                }
                let entry = &*entry;
                if !entry.CredentialBlob.is_null() {
                    for byte in 0..entry.CredentialBlobSize as usize {
                        std::ptr::write_volatile(entry.CredentialBlob.add(byte), 0);
                    }
                }
            }
            CredFree(self.pointer.cast());
        }
    }
}

pub(super) fn accounts() -> Result<Vec<String>, ()> {
    if cfg!(feature = "local-development") {
        return Err(());
    }
    let mut names = Vec::new();
    // keyring 3.6.3 stores targets as account.service; no suffix filter exists.
    for filter in [
        "profile-credential-v2-*".to_owned(),
        format!("installation-v1{SERVICE}"),
        format!("active-credential-v1{SERVICE}"),
    ] {
        let filter: Vec<u16> = filter.encode_utf16().chain(Some(0)).collect();
        let mut entries = Entries {
            count: 0,
            pointer: null_mut(),
        };
        if unsafe { CredEnumerateW(filter.as_ptr(), 0, &mut entries.count, &mut entries.pointer) }
            == 0
        {
            if unsafe { GetLastError() } == ERROR_NOT_FOUND {
                continue;
            }
            return Err(());
        }
        if entries.count as usize > super::MAX_ACCOUNTS || entries.pointer.is_null() {
            return Err(());
        }
        for index in 0..entries.count as usize {
            let entry = unsafe { (*entries.pointer.add(index)).as_ref() }.ok_or(())?;
            let target = unsafe { bounded_text(entry.TargetName) }?;
            // Do not interpret even the account attribute of another service.
            if !target.ends_with(SERVICE) {
                continue;
            }
            let user = unsafe { bounded_text(entry.UserName) }?;
            if let Some(name) = account(&target, &user, entry.Type)? {
                names.push(name);
            }
            if names.len() > super::MAX_ACCOUNTS {
                return Err(());
            }
        }
    }
    Ok(names)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn inventory_binds_exact_service_account_and_type_without_reading_store() {
        let name = format!("profile-credential-v2-{}", "a".repeat(64));
        assert_eq!(
            account(&format!("{name}{SERVICE}"), &name, CRED_TYPE_GENERIC),
            Ok(Some(name.clone()))
        );
        assert_eq!(
            account(&format!("{name}{SERVICE}.dev"), &name, CRED_TYPE_GENERIC),
            Ok(None)
        );
        assert!(account(
            &format!("{name}{SERVICE}"),
            "another-profile",
            CRED_TYPE_GENERIC
        )
        .is_err());
        assert!(account(&format!("{name}{SERVICE}"), &name, 2).is_err());
        assert!(account(&format!("foreign{SERVICE}"), "foreign", CRED_TYPE_GENERIC).is_err());
    }
}
