//! Exact current-user native-host keys; foreign keys and the other registry view
//! are collisions, including empty keys. No machine-wide registrations are changed.
use super::*;
use std::ptr::{null, null_mut};
use windows_sys::Win32::{
    Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS},
    Security::SECURITY_ATTRIBUTES,
    System::Registry::*,
};

struct Key(HKEY);
impl Drop for Key {
    fn drop(&mut self) {
        unsafe {
            RegCloseKey(self.0);
        }
    }
}
fn conflict<T>() -> Result<T, InstallTransactionError> {
    Err(InstallTransactionError::RegistrationConflict)
}
fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}

fn key_name(path: &Path) -> Result<&'static str, InstallTransactionError> {
    if path.file_name().and_then(|name| name.to_str()) != Some(&format!("{NATIVE_HOST_NAME}.json"))
    {
        return conflict();
    }
    let family = path
        .parent()
        .and_then(Path::file_name)
        .and_then(|name| name.to_str());
    let id = match family {
        Some("chrome") => BrowserId::Chrome,
        Some("edge") => BrowserId::Edge,
        Some("chromium") => BrowserId::Chromium,
        _ => return conflict(),
    };
    crate::registry::browser(id)
        .windows_registry_key
        .strip_prefix("HKCU\\")
        .ok_or(InstallTransactionError::RegistrationConflict)
}

fn open(name: &str, view: u32, write: bool) -> Result<Option<Key>, InstallTransactionError> {
    let mut key = null_mut();
    let access = KEY_READ | if write { KEY_WRITE | 0x00010000 } else { 0 } | view;
    match unsafe {
        RegOpenKeyExW(
            HKEY_CURRENT_USER,
            wide(name).as_ptr(),
            REG_OPTION_OPEN_LINK,
            access,
            &mut key,
        )
    } {
        ERROR_SUCCESS => Ok(Some(Key(key))),
        ERROR_FILE_NOT_FOUND => Ok(None),
        _ => conflict(),
    }
}

fn exact_value(kind: u32, data: &[u8], path: &Path) -> bool {
    if kind != REG_SZ || data.len() < 4 || data.len() > 8192 || data.len() % 2 != 0 {
        return false;
    }
    let units: Vec<u16> = data
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .collect();
    units
        == path
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>()
}

fn verify(key: &Key, path: &Path, allow_empty: bool) -> Result<(), InstallTransactionError> {
    super::files::verify_registry(key.0)
        .map_err(|_| InstallTransactionError::RegistrationConflict)?;
    let (mut subkeys, mut values) = (0, 0);
    if unsafe {
        RegQueryInfoKeyW(
            key.0,
            null_mut(),
            null_mut(),
            null(),
            &mut subkeys,
            null_mut(),
            null_mut(),
            &mut values,
            null_mut(),
            null_mut(),
            null_mut(),
            null_mut(),
        )
    } != ERROR_SUCCESS
        || subkeys != 0
        || values > 1
    {
        return conflict();
    }
    if values == 0 {
        return if allow_empty { Ok(()) } else { conflict() };
    }
    let (mut kind, mut size) = (0, 8192);
    let mut data = vec![0u8; size as usize];
    if unsafe {
        RegQueryValueExW(
            key.0,
            wide("").as_ptr(),
            null(),
            &mut kind,
            data.as_mut_ptr(),
            &mut size,
        )
    } != ERROR_SUCCESS
        || size as usize > data.len()
    {
        return conflict();
    }
    data.truncate(size as usize);
    if !exact_value(kind, &data, path) {
        return conflict();
    }
    Ok(())
}

fn present_os(path: &Path, owned: bool) -> Result<bool, InstallTransactionError> {
    let name = key_name(path)?;
    let mut found = false;
    // HKCU views can alias. Check both, allowing only the same owned manifest.
    for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
        if let Some(key) = open(name, view, false)? {
            if !owned {
                return conflict();
            }
            verify(&key, path, false)?;
            found = true;
        }
    }
    Ok(found)
}

fn publish_os(path: &Path, owned: bool) -> Result<(), InstallTransactionError> {
    if present_os(path, owned)? {
        return Ok(());
    }
    let name = wide(key_name(path)?);
    let mut raw = null_mut();
    let mut disposition = 0;
    let status = super::files::with_registry_descriptor(|descriptor| {
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor,
            bInheritHandle: 0,
        };
        unsafe {
            RegCreateKeyExW(
                HKEY_CURRENT_USER,
                name.as_ptr(),
                0,
                null(),
                0,
                KEY_ALL_ACCESS | KEY_WOW64_32KEY,
                &attributes,
                &mut raw,
                &mut disposition,
            )
        }
    })
    .map_err(|_| InstallTransactionError::RegistrationConflict)?;
    if status != ERROR_SUCCESS {
        return conflict();
    }
    let key = Key(raw);
    if disposition != REG_CREATED_NEW_KEY {
        return conflict();
    }
    super::files::verify_registry(key.0)
        .map_err(|_| InstallTransactionError::RegistrationConflict)?;
    let data: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    if data.len() > 4096
        || unsafe {
            RegSetValueExW(
                key.0,
                wide("").as_ptr(),
                0,
                REG_SZ,
                data.as_ptr().cast(),
                (data.len() * 2) as u32,
            )
        } != ERROR_SUCCESS
    {
        return conflict();
    }
    verify(&key, path, false)?;
    if unsafe { RegFlushKey(key.0) } != ERROR_SUCCESS {
        return conflict();
    }
    if !present_os(path, true)? {
        return conflict();
    }
    Ok(())
}

fn remove_os(path: &Path, allow_empty: bool) -> Result<(), InstallTransactionError> {
    let name = key_name(path)?;
    // Preflight both before the first mutation.
    for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
        if let Some(key) = open(name, view, true)? {
            verify(&key, path, allow_empty)?;
        }
    }
    for view in [KEY_WOW64_32KEY, KEY_WOW64_64KEY] {
        let Some(key) = open(name, view, true)? else {
            continue;
        };
        verify(&key, path, allow_empty)?;
        if unsafe { RegDeleteKeyExW(HKEY_CURRENT_USER, wide(name).as_ptr(), view, 0) }
            != ERROR_SUCCESS
        {
            return conflict();
        }
    }
    if present_os(path, true)? {
        return conflict();
    }
    Ok(())
}

#[cfg(not(test))]
pub(in super::super) fn present(path: &Path, owned: bool) -> Result<bool, InstallTransactionError> {
    present_os(path, owned)
}
#[cfg(not(test))]
pub(in super::super) fn publish(path: &Path, owned: bool) -> Result<(), InstallTransactionError> {
    publish_os(path, owned)
}
#[cfg(not(test))]
pub(in super::super) fn remove(
    path: &Path,
    allow_empty: bool,
) -> Result<(), InstallTransactionError> {
    remove_os(path, allow_empty)
}

#[cfg(test)]
pub(in super::super) use mock::{present, publish, remove};
#[cfg(test)]
mod mock {
    use super::*;
    use std::cell::RefCell;
    thread_local! { static KEYS: RefCell<BTreeSet<PathBuf>> = RefCell::new(BTreeSet::new()); }
    pub(in super::super::super) fn present(
        path: &Path,
        owned: bool,
    ) -> Result<bool, InstallTransactionError> {
        KEYS.with(|keys| {
            let present = keys.borrow().contains(path);
            if present && !owned {
                conflict()
            } else {
                Ok(present)
            }
        })
    }
    pub(in super::super::super) fn publish(
        path: &Path,
        owned: bool,
    ) -> Result<(), InstallTransactionError> {
        present(path, owned)?;
        KEYS.with(|keys| {
            keys.borrow_mut().insert(path.to_owned());
        });
        Ok(())
    }
    pub(in super::super::super) fn remove(
        path: &Path,
        _: bool,
    ) -> Result<(), InstallTransactionError> {
        KEYS.with(|keys| {
            keys.borrow_mut().remove(path);
        });
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn registration_requires_exact_unquoted_local_path_and_default_string() {
        let path = Path::new(r"C:\A0 Test\manifests\chrome\io.agentzero.browser_bridge.json");
        let bytes = path
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>();
        assert!(exact_value(REG_SZ, &bytes, path));
        assert!(!exact_value(REG_EXPAND_SZ, &bytes, path));
        assert!(!exact_value(REG_SZ, &bytes[..bytes.len() - 2], path));
        assert!(!exact_value(REG_SZ, &bytes, Path::new(r"C:\foreign.json")));
        assert!(key_name(Path::new(r"C:\A0\manifests\foreign\host.json")).is_err());
    }

    #[cfg(feature = "local-development")]
    #[test]
    #[ignore = "requires explicit approval: temporarily registers the absent development native host in HKCU"]
    fn approved_windows_registry_roundtrip() {
        let root = std::env::temp_dir().join(format!("a0-registry-test-{}", random_id().unwrap()));
        let path = root.join("chrome").join(format!("{NATIVE_HOST_NAME}.json"));
        assert_eq!(NATIVE_HOST_NAME, "io.agentzero.browser_bridge.dev");
        assert!(!present_os(&path, false).expect("Refuse any pre-existing key in either view"));
        publish_os(&path, false).unwrap();
        assert!(present_os(&path, true).unwrap());
        assert!(publish_os(&path, false).is_err());
        publish_os(&path, true).unwrap();
        let foreign = root
            .join("another/chrome")
            .join(format!("{NATIVE_HOST_NAME}.json"));
        assert!(present_os(&foreign, true).is_err());
        assert!(remove_os(&foreign, false).is_err());
        assert!(present_os(&path, true).unwrap());
        remove_os(&path, false).unwrap();
        assert!(!present_os(&path, false).unwrap());
    }
}
