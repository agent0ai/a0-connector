// Native host-side installer UI. Packaging must seal this launcher and the
// exact stable companion together in a signed/notarized application bundle.
import AppKit
import Foundation
import Darwin

private struct Outcome {
    let title: String
    let detail: String
    let installed: Bool
    static let unknown = Outcome(title: "Installation needs checking",
        detail: "Setup did not return a verified result. Changes may have started. Keep this installer and use Agent Zero’s browser setup diagnostics before trying again.", installed: false)
    static let unavailable = Outcome(title: "Installer cannot start",
        detail: "The bundled browser companion is missing or cannot be verified. Download a fresh official installer. Nothing was started.", installed: false)
}

private struct InstallReceipt: Decodable {
    let contract: String
    let schema_version: Int
    let companion_version: String
    let install_contract: String
    let operation: String
    let state: String
    let reason_code: String
    let mutation_allowed: Bool
    let catalog: String
    let artifact: String
    let platform_signature: String
    let platform: String
    let architecture: String
    let install_root: String
    let target_browsers: [String]
    let registration_count: Int?
    let rollback: String?
}

private func interpret(_ data: Data, exitCode: Int32) -> Outcome {
    let fields = ["contract", "schema_version", "companion_version", "install_contract",
                  "operation", "state", "reason_code", "mutation_allowed", "catalog",
                  "artifact", "platform_signature", "platform", "architecture",
                  "install_root", "target_browsers", "registration_count", "rollback"]
    guard data.count <= 16 * 1024,
          data.allSatisfy({ $0 == 9 || $0 == 10 || $0 == 13 || (32...126).contains($0) }),
          !data.contains(92), let text = String(data: data, encoding: .utf8),
          // Native install receipts contain no escaped strings. Requiring each
          // exact key once rejects duplicate/escaped-key decoder ambiguity.
          fields.allSatisfy({ text.components(separatedBy: "\"\($0)\"").count == 2 }),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          Set(object.keys) == Set(fields),
          let receipt = try? JSONDecoder().decode(InstallReceipt.self, from: data),
          receipt.contract == "a0.browser-bridge.install-plan.v1",
          receipt.schema_version == 1,
          receipt.install_contract == "a0.browser-bridge.install.v1",
          receipt.operation == "install", receipt.platform == "macos",
          receipt.companion_version == "2.12.0",
          ["aarch64", "x86_64"].contains(receipt.architecture),
          receipt.target_browsers == ["auto"] else { return .unknown }
    if exitCode == 0, receipt.state == "installed", receipt.reason_code == "INSTALL_VERIFIED",
       receipt.mutation_allowed, receipt.catalog == "verified", receipt.artifact == "verified",
       receipt.platform_signature == "verified", let count = receipt.registration_count,
       (1...6).contains(count), receipt.rollback == "not_needed", receipt.install_root == "resolved" {
        return Outcome(title: "Browser companion installed",
            detail: "Next, open the Agent Zero extension in your browser and choose Connect. Use your Agent Zero address and paste its one-time pairing code. Pairing is saved for this browser profile; browser control begins only after Agent Zero confirms the connection.", installed: true)
    }
    // Never render arbitrary native text, local paths, URLs or credentials.
    switch receipt.reason_code {
    case "RELEASE_EVIDENCE_UNAVAILABLE":
        return Outcome(title: "This release is not available yet",
            detail: "The signed release needed for installation is not available. Use a published installer when it is ready; this is not a pairing problem.", installed: false)
    case "NO_SUPPORTED_BROWSER_FOUND":
        return Outcome(title: "Install a supported browser first",
            detail: "Setup could not find a supported browser in its standard location. Install Chrome, Edge, Brave, Vivaldi, Opera or Chromium, then reopen this installer.", installed: false)
    case "INSTALL_BUSY":
        return Outcome(title: "Another setup is already running",
            detail: "Let the other installer finish, then check your browser extension. This window will not start another installation.", installed: false)
    default:
        return Outcome(title: "Setup could not finish",
            detail: "Agent Zero could not verify or complete this installation. Check your internet connection and the official release instructions. Do not remove existing browser credentials. Browser control has not been enabled by this installer.", installed: false)
    }
}

private func sameIdentity(_ first: stat, _ second: stat) -> Bool {
    first.st_dev == second.st_dev && first.st_ino == second.st_ino &&
    first.st_size == second.st_size && first.st_mtimespec.tv_sec == second.st_mtimespec.tv_sec &&
    first.st_mtimespec.tv_nsec == second.st_mtimespec.tv_nsec &&
    first.st_ctimespec.tv_sec == second.st_ctimespec.tv_sec &&
    first.st_ctimespec.tv_nsec == second.st_ctimespec.tv_nsec &&
    second.st_mode & S_IFMT == S_IFREG && second.st_nlink == 1
}

private func installBundledCompanion() -> Outcome {
    let bundle = Bundle.main.bundleURL
    guard bundle.pathExtension == "app", bundle.path.hasPrefix("/") else { return .unavailable }
    let executable = bundle.appendingPathComponent("Contents/Resources/a0-browser-bridge")
    // No caller-selected executable, PATH search, shell, elevation or developer
    // fallback. Retain all traversed directory descriptors and the exact file.
    var handles = [Int32]()
    defer { for handle in handles.reversed() { close(handle) } }
    var parent = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
    guard parent >= 0 else { return .unavailable }
    handles.append(parent)
    let parts = executable.path.split(separator: "/")
    for part in parts.dropLast() {
        guard part != ".", part != ".." else { return .unavailable }
        parent = openat(parent, String(part), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard parent >= 0 else { return .unavailable }
        handles.append(parent)
    }
    let file = openat(parent, String(parts.last!), O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
    guard file >= 0 else { return .unavailable }
    handles.append(file)
    var identity = stat()
    guard fstat(file, &identity) == 0, identity.st_mode & S_IFMT == S_IFREG,
          identity.st_nlink == 1, identity.st_mode & 0o111 != 0,
          identity.st_mode & 0o022 == 0, identity.st_size > 0,
          identity.st_size <= 512 * 1024 * 1024 else { return .unavailable }
    let process = Process()
    let pipe = Pipe()
    process.executableURL = executable
    process.arguments = ["install", "--browser", "auto", "--json"]
    process.environment = ["HOME": NSHomeDirectory(), "LANG": "en_US.UTF-8"]
    process.currentDirectoryURL = bundle
    process.standardInput = FileHandle.nullDevice
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    let reader = pipe.fileHandleForReading.fileDescriptor
    guard fcntl(reader, F_SETFL, O_NONBLOCK) == 0 else { return .unavailable }
    var named = stat()
    guard lstat(executable.path, &named) == 0, sameIdentity(identity, named) else { return .unavailable }
    do { try process.run() } catch { return .unavailable }
    pipe.fileHandleForWriting.closeFile()
    defer { pipe.fileHandleForReading.closeFile() }
    let deadline = ProcessInfo.processInfo.systemUptime + 600
    var bytes = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    var eof = false
    var failed = false
    while !eof || process.isRunning {
        let count = Darwin.read(reader, &buffer, buffer.count)
        if count > 0 {
            if bytes.count + count > 16 * 1024 { failed = true; break }
            bytes.append(contentsOf: buffer.prefix(count))
        } else if count == 0 { eof = true }
        else if errno != EAGAIN && errno != EINTR { failed = true; break }
        if ProcessInfo.processInfo.systemUptime >= deadline { failed = true; break }
        if count <= 0 { usleep(20_000) } // Dedicated worker, never the AppKit thread.
    }
    if failed {
        if process.isRunning {
            process.terminate()
            let stopDeadline = ProcessInfo.processInfo.systemUptime + 2
            while process.isRunning && ProcessInfo.processInfo.systemUptime < stopDeadline { usleep(20_000) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
        return .unknown // Timeout/termination never implies rollback or no effects.
    }
    var retained = stat()
    guard fstat(file, &retained) == 0, lstat(executable.path, &named) == 0,
          sameIdentity(identity, retained), sameIdentity(identity, named),
          process.terminationReason == .exit else { return .unknown }
    return interpret(bytes, exitCode: process.terminationStatus)
}

@MainActor
private final class Installer: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private let status = NSTextField(wrappingLabelWithString: "Ready to install on this Mac")
    private let detail = NSTextField(wrappingLabelWithString: "Setup installs a small companion for your browsers. It runs on this Mac even when Agent Zero runs in Docker or on another computer. No administrator password is needed.")
    private let button = NSButton(title: "Install browser companion", target: nil, action: nil)
    private var running = false
    private var attempted = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.appearance = NSAppearance(named: .darkAqua)
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 540),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "Agent Zero Browser Setup"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(calibratedWhite: 0.075, alpha: 1)
        let body = NSStackView()
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = 18
        body.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(body)
        NSLayoutConstraint.activate([
            body.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 32),
            body.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -32),
            body.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 30),
            body.bottomAnchor.constraint(lessThanOrEqualTo: window.contentView!.bottomAnchor, constant: -24)])
        let eyebrow = NSTextField(labelWithString: "AGENT ZERO  /  BROWSER COMPANION")
        eyebrow.font = .systemFont(ofSize: 11, weight: .semibold)
        eyebrow.textColor = .secondaryLabelColor
        let heading = NSTextField(labelWithString: "Connect your browser, once.")
        heading.font = .systemFont(ofSize: 27, weight: .semibold)
        let subtitle = NSTextField(wrappingLabelWithString: "1. Install on this Mac    2. Pair in the extension    3. Start in Agent Zero")
        subtitle.font = .systemFont(ofSize: 13)
        subtitle.textColor = .secondaryLabelColor
        status.font = .systemFont(ofSize: 16, weight: .semibold)
        detail.font = .systemFont(ofSize: 14)
        detail.textColor = .secondaryLabelColor
        detail.maximumNumberOfLines = 0
        let divider = NSBox()
        divider.boxType = .separator
        for view in [eyebrow, heading, subtitle, divider, status, detail] { body.addArrangedSubview(view) }
        for view in [subtitle, divider, detail] { view.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true }
        button.target = self
        button.action = #selector(install)
        button.bezelStyle = .rounded
        button.controlSize = .large
        button.contentTintColor = .controlAccentColor
        button.keyEquivalent = "\r"
        button.setAccessibilityHelp("Installs only after you activate this button. Pairing happens separately in your browser extension.")
        body.addArrangedSubview(button)
        let privacy = NSTextField(wrappingLabelWithString: "Your passwords and pairing codes are not requested by this installer.")
        privacy.font = .systemFont(ofSize: 12)
        privacy.textColor = .secondaryLabelColor
        body.addArrangedSubview(privacy)
        privacy.widthAnchor.constraint(equalTo: body.widthAnchor).isActive = true
        // Native controls, stable layout and text status; no motion to disable.
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func install() {
        guard !running, !attempted else { return }
        running = true
        attempted = true
        button.isEnabled = false
        button.title = "Installing…"
        status.stringValue = "Verifying and installing the signed companion"
        detail.stringValue = "This may take a few minutes. Keep this window open. Setup checks the official release and registers only supported browsers on this Mac."
        DispatchQueue.global(qos: .userInitiated).async {
            let outcome = installBundledCompanion()
            DispatchQueue.main.async {
                self.running = false
                self.status.stringValue = outcome.title
                self.detail.stringValue = outcome.detail
                self.button.title = outcome.installed ? "Installed" : "Setup finished"
                NSAccessibility.post(element: self.status, notification: .valueChanged)
            }
        }
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { !running }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        running ? .terminateCancel : .terminateNow
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

if CommandLine.arguments.dropFirst().isEmpty {
    MainActor.assumeIsolated {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let installer = Installer()
        app.delegate = installer
        app.run()
    }
} else if Array(CommandLine.arguments.dropFirst()) == ["--self-test"] {
    // Receipt interpretation only. No NSApplication, Process, filesystem or
    // credential call can occur in this developer check.
    let success = Data(#"{"contract":"a0.browser-bridge.install-plan.v1","schema_version":1,"companion_version":"2.12.0","install_contract":"a0.browser-bridge.install.v1","operation":"install","state":"installed","reason_code":"INSTALL_VERIFIED","mutation_allowed":true,"catalog":"verified","artifact":"verified","platform_signature":"verified","platform":"macos","architecture":"aarch64","install_root":"resolved","target_browsers":["auto"],"registration_count":1,"rollback":"not_needed"}"#.utf8)
    guard interpret(success, exitCode: 0).installed,
          !interpret(success, exitCode: 1).installed,
          !interpret(Data("{}".utf8), exitCode: 0).installed,
          !interpret(Data(repeating: 0, count: 16 * 1024 + 1), exitCode: 0).installed else { exit(1) }
    print("{\"receipt_self_test\":\"passed\",\"native_process_started\":false}")
} else {
    FileHandle.standardError.write(Data("Unsupported installer arguments\n".utf8))
    exit(1)
}
