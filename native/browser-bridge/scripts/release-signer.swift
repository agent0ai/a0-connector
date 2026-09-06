// Standalone macOS publisher utility; never linked into the Browser Bridge.
// Build a stable executable before initializing keys so macOS can retain its
// default application access policy. No ACL widening, migration or key export.
import Foundation
import Security
import CryptoKit
import Darwin

private let service = "io.agentzero.browser_bridge.release-signing.v1"
private let maximumInputBytes = 128 * 1024

private enum Failure: String, Error {
    case usage = "Usage: release-signer initialize|public publisher|builder; release-signer sign publisher|builder /absolute/input; release-signer --self-test"
    case keychain = "RELEASE_KEYCHAIN_UNAVAILABLE"
    case ambiguous = "RELEASE_KEY_RECORD_AMBIGUOUS"
    case corrupt = "RELEASE_KEY_RECORD_INVALID"
    case missing = "RELEASE_KEY_NOT_INITIALIZED"
    case independent = "RELEASE_KEYS_MUST_BE_INDEPENDENT"
    case input = "RELEASE_INPUT_INVALID_OR_CHANGED"
    case crypto = "RELEASE_CRYPTOGRAPHY_FAILED"
}

private enum Role: String {
    case publisher, builder
    var account: String { rawValue + "-2026" }
    var other: Role { self == .publisher ? .builder : .publisher }
}

private func digest(_ bytes: Data) -> String {
    SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
}

private func metadata(_ key: Curve25519.Signing.PrivateKey, role: Role) -> [String: Any] {
    let bytes = key.publicKey.rawRepresentation
    return ["schema": "a0.release-signing.v1", "algorithm": "Ed25519",
            "role": role.rawValue, "key_id": role.account, "service": service,
            "public_key_base64": bytes.base64EncodedString(),
            "public_key_sha256": digest(bytes)]
}

// Intentionally use the user's default file-based macOS keychain: this
// standalone, entitlement-free executable needs user-controlled application
// ACLs, not data-protection access groups. Never provide kSecAttrAccess or
// trusted-application lists (especially an all-app list). Do not use this
// utility through the Swift interpreter for real persistent keys.
private final class ReleaseKeys {
    private let keychain: SecKeychain

    init() throws {
        var result: SecKeychain?
        guard SecKeychainCopyDefault(&result) == errSecSuccess, let result else {
            throw Failure.keychain
        }
        keychain = result
    }

    private func identity(_ role: Role) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: role.account,
         kSecAttrSynchronizable as String: false]
    }

    func load(_ role: Role) throws -> Curve25519.Signing.PrivateKey? {
        var query = identity(role)
        query[kSecMatchSearchList as String] = [keychain]
        query[kSecMatchLimit as String] = kSecMatchLimitAll
        query[kSecReturnAttributes as String] = true
        query[kSecReturnPersistentRef as String] = true
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw Failure.keychain }
        guard let matches = result as? [[String: Any]], matches.count == 1 else {
            throw Failure.ambiguous
        }
        let item = matches[0]
        guard item[kSecAttrService as String] as? String == service,
              item[kSecAttrAccount as String] as? String == role.account,
              (item[kSecAttrSynchronizable as String] as? NSNumber)?.boolValue != true,
              let reference = item[kSecValuePersistentRef as String] as? Data,
              !reference.isEmpty else { throw Failure.corrupt }
        // Resolve the exact enumerated item; never silently select the first
        // member of an ambiguous account or a different keychain search list.
        let read: [String: Any] = [
            kSecValuePersistentRef as String: reference,
            kSecMatchSearchList as String: [keychain],
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne]
        var value: CFTypeRef?
        guard SecItemCopyMatching(read as CFDictionary, &value) == errSecSuccess else {
            throw Failure.keychain
        }
        guard var seed = value as? Data, seed.count == 32 else { throw Failure.corrupt }
        value = nil
        defer { seed.resetBytes(in: seed.startIndex..<seed.endIndex) }
        do { return try Curve25519.Signing.PrivateKey(rawRepresentation: seed) }
        catch { throw Failure.corrupt }
    }

    func initialize(_ role: Role) throws -> Curve25519.Signing.PrivateKey {
        let other = try load(role.other)
        if let existing = try load(role) {
            try requireIndependent(existing, other)
            return existing
        }
        // CryptoKit generates each independent seed using OS entropy.
        let key = Curve25519.Signing.PrivateKey()
        try requireIndependent(key, other)
        var seed = key.rawRepresentation
        defer { seed.resetBytes(in: seed.startIndex..<seed.endIndex) }
        var add = identity(role)
        add[kSecUseKeychain as String] = keychain
        add[kSecAttrLabel as String] = "Agent Zero release " + role.account
        // On the intentional file-based keychain, actual availability follows
        // the user's keychain lock and default application ACL. No promise of
        // data-protection-keychain semantics is made by this attribute.
        add[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        add[kSecValueData as String] = seed
        let status = SecItemAdd(add as CFDictionary, nil)
        add.removeValue(forKey: kSecValueData as String)
        guard status == errSecSuccess || status == errSecDuplicateItem else {
            throw Failure.keychain
        }
        // CREATE-ONLY. A concurrent initializer wins without overwrite. Read
        // its exact valid record back; corruption never triggers replacement.
        guard let retained = try load(role) else { throw Failure.corrupt }
        if status == errSecSuccess,
           retained.publicKey.rawRepresentation != key.publicKey.rawRepresentation {
            throw Failure.corrupt
        }
        try requireIndependent(retained, try load(role.other))
        return retained
    }
}

private func requireIndependent(_ key: Curve25519.Signing.PrivateKey,
                                _ other: Curve25519.Signing.PrivateKey?) throws {
    if let other, key.publicKey.rawRepresentation == other.publicKey.rawRepresentation {
        throw Failure.independent
    }
}

private func sameFile(_ left: stat, _ right: stat) -> Bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino &&
    left.st_mode == right.st_mode && left.st_nlink == 1 && right.st_nlink == 1 &&
    left.st_size == right.st_size && left.st_uid == right.st_uid &&
    left.st_mtimespec.tv_sec == right.st_mtimespec.tv_sec &&
    left.st_mtimespec.tv_nsec == right.st_mtimespec.tv_nsec &&
    left.st_ctimespec.tv_sec == right.st_ctimespec.tv_sec &&
    left.st_ctimespec.tv_nsec == right.st_ctimespec.tv_nsec
}

private func readInput(_ path: String) throws -> Data {
    guard path.hasPrefix("/"), path.utf8.count <= 4096,
          !path.utf8.contains(where: { $0 < 32 || $0 == 127 }) else { throw Failure.input }
    let components = path.split(separator: "/", omittingEmptySubsequences: false)
    guard components.count >= 2, components.dropFirst().allSatisfy({
        !$0.isEmpty && $0 != "." && $0 != ".."
    }) else { throw Failure.input }
    var descriptors = [Int32]()
    defer { for descriptor in descriptors.reversed() { close(descriptor) } }
    let root = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    guard root >= 0 else { throw Failure.input }
    descriptors.append(root)
    for component in components.dropFirst().dropLast() {
        let descriptor = openat(descriptors.last!, String(component),
                                O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw Failure.input }
        descriptors.append(descriptor)
    }
    let parent = descriptors.last!
    let leaf = String(components.last!)
    let file = openat(parent, leaf, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
    guard file >= 0 else { throw Failure.input }
    descriptors.append(file)
    var before = stat()
    guard fstat(file, &before) == 0, before.st_mode & S_IFMT == S_IFREG,
          before.st_nlink == 1, before.st_size >= 0,
          before.st_size <= maximumInputBytes else { throw Failure.input }
    var bytes = Data(count: Int(before.st_size))
    var offset = 0
    while offset < bytes.count {
        let count = bytes.withUnsafeMutableBytes {
            Darwin.read(file, $0.baseAddress!.advanced(by: offset), $0.count - offset)
        }
        if count < 0 && errno == EINTR { continue }
        guard count > 0 else { throw Failure.input }
        offset += count
    }
    var extra: UInt8 = 0
    var after = stat()
    var named = stat()
    guard Darwin.read(file, &extra, 1) == 0,
          fstat(file, &after) == 0,
          fstatat(parent, leaf, &named, AT_SYMLINK_NOFOLLOW) == 0,
          sameFile(before, after), sameFile(before, named) else { throw Failure.input }
    return bytes
}

private func selfTest() throws -> [String: Any] {
    // This branch creates no ReleaseKeys object and performs no Keychain call.
    let first = Curve25519.Signing.PrivateKey()
    let second = Curve25519.Signing.PrivateKey()
    try requireIndependent(first, second)
    let message = Data("Agent Zero ephemeral signer self-test".utf8)
    let signature = try first.signature(for: message)
    let restored = try Curve25519.Signing.PublicKey(rawRepresentation: first.publicKey.rawRepresentation)
    guard signature.count == 64, restored.isValidSignature(signature, for: message),
          !restored.isValidSignature(signature, for: message + Data([0])),
          !second.publicKey.isValidSignature(signature, for: message),
          digest(Data()) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" else {
        throw Failure.crypto
    }
    return ["schema": "a0.release-signing.self-test.v1", "passed": true,
            "keychain_accessed": false, "persistent_keys_created": false]
}

private func run(_ arguments: [String]) throws -> [String: Any] {
    if arguments == ["--self-test"] { return try selfTest() }
    guard arguments.count >= 2, let role = Role(rawValue: arguments[1]) else { throw Failure.usage }
    let operation = arguments[0]
    guard (operation == "initialize" || operation == "public") && arguments.count == 2 ||
          operation == "sign" && arguments.count == 3 else { throw Failure.usage }
    // Validate/read the exact explicit input before any Keychain access.
    let input = operation == "sign" ? try readInput(arguments[2]) : nil
    let keys = try ReleaseKeys()
    let key: Curve25519.Signing.PrivateKey
    if operation == "initialize" { key = try keys.initialize(role) }
    else {
        guard let existing = try keys.load(role) else { throw Failure.missing }
        key = existing
    }
    var output = metadata(key, role: role)
    if let input {
        let signature = try key.signature(for: input)
        guard signature.count == 64, key.publicKey.isValidSignature(signature, for: input) else {
            throw Failure.crypto
        }
        output["input_sha256"] = digest(input)
        output["input_size"] = input.count
        output["signature_base64"] = signature.base64EncodedString()
    }
    return output
}

do {
    let output = try run(Array(CommandLine.arguments.dropFirst()))
    let bytes = try JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
    FileHandle.standardOutput.write(bytes + Data([10]))
} catch {
    // Fixed diagnostics only: no system errors, filenames, seed data, or query
    // dictionaries can be interpolated into output.
    let message = (error as? Failure)?.rawValue ?? Failure.crypto.rawValue
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}
