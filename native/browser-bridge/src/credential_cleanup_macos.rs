//! Attribute-only enumeration of the same default user keychain/service as keyring.
//! No secret-return flag, unlock request, interactive prompt, deletion, or shell.
use std::ffi::{c_char, c_void, CStr};
use std::ptr;

type Ref = *const c_void;
const SERVICE: &str = "io.agentzero.browser_bridge";

#[link(name = "Security", kind = "framework")]
unsafe extern "C" {
    fn SecKeychainCopyDomainDefault(domain: u32, keychain: *mut Ref) -> i32;
    fn SecItemCopyMatching(query: Ref, result: *mut Ref) -> i32;
    static kSecClass: Ref;
    static kSecClassGenericPassword: Ref;
    static kSecAttrService: Ref;
    static kSecAttrAccount: Ref;
    static kSecMatchSearchList: Ref;
    static kSecMatchLimit: Ref;
    static kSecReturnAttributes: Ref;
    static kSecReturnData: Ref;
    static kSecUseAuthenticationUI: Ref;
    static kSecUseAuthenticationUIFail: Ref;
}

#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    fn CFRelease(value: Ref);
    fn CFGetTypeID(value: Ref) -> usize;
    fn CFArrayGetTypeID() -> usize;
    fn CFDictionaryGetTypeID() -> usize;
    fn CFStringGetTypeID() -> usize;
    fn CFStringCreateWithCString(allocator: Ref, value: *const c_char, encoding: u32) -> Ref;
    fn CFStringGetLength(value: Ref) -> isize;
    fn CFStringGetCString(value: Ref, buffer: *mut c_char, size: isize, encoding: u32) -> bool;
    fn CFNumberCreate(allocator: Ref, kind: isize, value: *const c_void) -> Ref;
    fn CFArrayCreate(allocator: Ref, values: *const Ref, count: isize, callbacks: Ref) -> Ref;
    fn CFArrayGetCount(array: Ref) -> isize;
    fn CFArrayGetValueAtIndex(array: Ref, index: isize) -> Ref;
    fn CFDictionaryCreate(
        allocator: Ref,
        keys: *const Ref,
        values: *const Ref,
        count: isize,
        key_callbacks: Ref,
        value_callbacks: Ref,
    ) -> Ref;
    fn CFDictionaryGetValue(dictionary: Ref, key: Ref) -> Ref;
    static kCFBooleanTrue: Ref;
    static kCFBooleanFalse: Ref;
}

struct Owned(Ref);
impl Owned {
    fn new(value: Ref) -> Result<Self, ()> {
        if value.is_null() {
            Err(())
        } else {
            Ok(Self(value))
        }
    }
}
impl Drop for Owned {
    fn drop(&mut self) {
        unsafe {
            CFRelease(self.0);
        }
    }
}

unsafe fn ascii(value: Ref, max: isize) -> Result<String, ()> {
    if value.is_null() || CFGetTypeID(value) != CFStringGetTypeID() {
        return Err(());
    }
    let length = CFStringGetLength(value);
    if !(1..=max).contains(&length) {
        return Err(());
    }
    let mut bytes = vec![0 as c_char; length as usize + 1];
    if !CFStringGetCString(value, bytes.as_mut_ptr(), bytes.len() as isize, 0x600) {
        return Err(());
    }
    CStr::from_ptr(bytes.as_ptr())
        .to_str()
        .map(str::to_owned)
        .map_err(|_| ())
}

pub(super) fn accounts() -> Result<Vec<String>, ()> {
    if cfg!(feature = "local-development") {
        return Err(());
    }
    unsafe {
        let mut keychain = ptr::null();
        if SecKeychainCopyDomainDefault(0, &mut keychain) != 0 {
            return Err(());
        }
        let keychain = Owned::new(keychain)?;
        let search_list = Owned::new(CFArrayCreate(ptr::null(), &keychain.0, 1, ptr::null()))?;
        let service = Owned::new(CFStringCreateWithCString(
            ptr::null(),
            b"io.agentzero.browser_bridge\0".as_ptr().cast(),
            0x600,
        ))?;
        let max = super::MAX_ACCOUNTS as i64 + 1;
        let limit = Owned::new(CFNumberCreate(ptr::null(), 4, (&max as *const i64).cast()))?;
        // All referenced values outlive the query; null callbacks do not retain.
        let keys = [
            kSecClass,
            kSecAttrService,
            kSecMatchSearchList,
            kSecMatchLimit,
            kSecReturnAttributes,
            kSecReturnData,
            kSecUseAuthenticationUI,
        ];
        let values = [
            kSecClassGenericPassword,
            service.0,
            search_list.0,
            limit.0,
            kCFBooleanTrue,
            kCFBooleanFalse,
            kSecUseAuthenticationUIFail,
        ];
        let query = Owned::new(CFDictionaryCreate(
            ptr::null(),
            keys.as_ptr(),
            values.as_ptr(),
            keys.len() as isize,
            ptr::null(),
            ptr::null(),
        ))?;
        let mut result = ptr::null();
        let status = SecItemCopyMatching(query.0, &mut result);
        // Even no items is only advisory inventory; it never authorizes cleanup.
        if status == -25300 {
            if !result.is_null() {
                CFRelease(result);
            }
            return Ok(Vec::new());
        }
        if status != 0 {
            if !result.is_null() {
                CFRelease(result);
            }
            return Err(());
        }
        let result = Owned::new(result)?;
        if CFGetTypeID(result.0) != CFArrayGetTypeID() {
            return Err(());
        }
        let count = CFArrayGetCount(result.0);
        if count < 0 || count as usize > super::MAX_ACCOUNTS {
            return Err(());
        }
        let mut accounts = Vec::with_capacity(count as usize);
        for index in 0..count {
            let entry = CFArrayGetValueAtIndex(result.0, index);
            if entry.is_null() || CFGetTypeID(entry) != CFDictionaryGetTypeID() {
                return Err(());
            }
            if ascii(CFDictionaryGetValue(entry, kSecAttrService), 64)? != SERVICE {
                return Err(());
            }
            accounts.push(ascii(CFDictionaryGetValue(entry, kSecAttrAccount), 128)?);
        }
        Ok(accounts)
    }
}
