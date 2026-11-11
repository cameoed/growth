import Cocoa
import Darwin
import WebKit

private let projectPath = ProcessInfo.processInfo.environment["BEINGS_GROWTH_PROJECT_DIR"]
    ?? Bundle.main.url(forResource: "checkout-path", withExtension: nil).flatMap {
        try? String(contentsOf: $0, encoding: .utf8).trimmingCharacters(in: .newlines)
    }
    ?? Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().path
private let buildID = "20261108-bundled-dashboard"
// Match the dashboard canvas through the native-to-WebKit handoff.
private let dashboardBackground = NSColor(srgbRed: 16 / 255, green: 18 / 255, blue: 22 / 255, alpha: 1)
private let remoteConfigPath = "\(projectPath)/data/oracle-remote.json"
private let nextStoragePath = "\(projectPath)/data/oracle-storage-next.json"

private let savedVMPath = "\(projectPath)/data/oracle-vm-profile.json"

private func storageProfile(at path: String) -> [String: Any]? {
    guard let data = FileManager.default.contents(atPath: path) else { return nil }
    return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
}

private func remoteConfiguration() -> [String: Any]? {
    guard let value = storageProfile(at: remoteConfigPath),
          value["enabled"] as? Bool == true,
          let port = value["local_port"] as? Int,
          (1024...65535).contains(port), port != 8787,
          value["remote_database"] as? String == "/opt/beings-growth/data/leads.db"
    else { return nil }
    return value
}

private func configuredPort() -> String {
    if FileManager.default.fileExists(atPath: remoteConfigPath) {
        return String(remoteConfiguration()?["local_port"] as? Int ?? 18787)
    }
    return ProcessInfo.processInfo.environment["BEINGS_LEADS_PORT"] ?? "8787"
}

private func localURL(path: String = "/") -> URL {
    URL(string: "http://127.0.0.1:\(configuredPort())\(path)")!
}

private func databaseMaintenanceActive(project: String = projectPath) -> Bool {
    // A configured remote app must never fall back to the local database.
    if FileManager.default.fileExists(atPath: "\(project)/data/oracle-remote.json") {
        return false
    }
    let receipt = "\(project)/data/oracle/backup-operation.json"
    if let data = FileManager.default.contents(atPath: receipt),
       let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       value["resume_on_release"] as? Bool == true,
       let holder = value["holder_pid"] as? Int, holder > 0, holder <= Int(Int32.max),
       kill(pid_t(holder), 0) == 0 || errno == EPERM {
        // The holder announces its pause before draining the backend and taking its lock.
        return true
    }
    let descriptor = Darwin.open("\(project)/data", O_RDONLY)
    guard descriptor >= 0 else { return false }
    defer { Darwin.close(descriptor) }
    if flock(descriptor, LOCK_SH | LOCK_NB) == 0 {
        return false // Closing the descriptor releases only our shared probe.
    }
    return errno == EWOULDBLOCK || errno == EAGAIN
}

private func publicWebsiteURL(_ url: URL) -> Bool {
    guard let scheme = url.scheme?.lowercased(), ["https", "http"].contains(scheme),
          url.user == nil, url.password == nil, let rawHost = url.host?.lowercased()
    else { return false }
    let host = rawHost.trimmingCharacters(in: CharacterSet(charactersIn: "."))
    var ipv4 = in_addr()
    guard inet_aton(host, &ipv4) != 1 else { return false }
    guard host.contains("."), !host.contains(":"), !host.contains("["),
          host.range(of: "^[0-9.]+$", options: .regularExpression) == nil
    else { return false }
    return !["localhost", "local", "internal", "lan", "home", "invalid"].contains {
        host == $0 || host.hasSuffix(".\($0)")
    }
}

private final class DashboardWindow: NSWindow {
    weak var websitePane: NSView?
    private var allowsWebsiteFocus = false

    override func sendEvent(_ event: NSEvent) {
        withWebsiteFocusPermission(for: event) { super.sendEvent(event) }
    }

    override func keyDown(with event: NSEvent) {
        // Focused controls get Escape first. Ignore it if it reaches the window.
        if event.keyCode == 48,
           event.modifierFlags.intersection([.command, .control, .option, .shift]).isEmpty {
            return
        }
        super.keyDown(with: event)
    }

    func withWebsiteFocusPermission(for event: NSEvent, dispatch: () -> Void) {
        let previous = allowsWebsiteFocus
        defer { allowsWebsiteFocus = previous }
        allowsWebsiteFocus = false
        if let pane = websitePane, !pane.isHiddenOrHasHiddenAncestor {
            switch event.type {
            case .leftMouseDown, .rightMouseDown:
                allowsWebsiteFocus = pane.bounds.contains(pane.convert(event.locationInWindow, from: nil))
            case .keyDown:
                allowsWebsiteFocus = event.keyCode == 48
                    && event.modifierFlags.intersection([.command, .control, .option]).isEmpty
            default: break
            }
        }
        dispatch()
    }

    override func makeFirstResponder(_ responder: NSResponder?) -> Bool {
        if let pane = websitePane, let view = responder as? NSView, view.isDescendant(of: pane) {
            let alreadyFocused = (firstResponder as? NSView)?.isDescendant(of: pane) == true
            guard !pane.isHiddenOrHasHiddenAncestor, alreadyFocused || allowsWebsiteFocus else {
                return false
            }
        }
        return super.makeFirstResponder(responder)
    }
}

// The website is a separate top-level browsing context with no dashboard message handlers.
private final class CompanyWebsite: NSObject, WKNavigationDelegate, WKUIDelegate {
    let view: WKWebView
    let container = NSView(frame: .zero)
    let token: Int
    var update: (([String: Any]) -> Void)?
    private let loadingBackdrop = NSView(frame: .zero)
    private var observations: [NSKeyValueObservation] = []

    init(token: Int) {
        self.token = token
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.addUserScript(WKUserScript(source: """
            (() => {
                // Cosmetic rules stay active when a consent manager inserts its UI later.
                const css = `
                    :is(
                        #onetrust-banner-sdk, #onetrust-pc-sdk, .onetrust-pc-dark-filter,
                        #CybotCookiebotDialog, #CybotCookiebotDialogBodyUnderlay,
                        #didomi-host, #didomi-popup, #didomi-notice, .didomi-popup-backdrop,
                        #usercentrics-root, #uc-banner-modal, #qc-cmp2-container,
                        #cmplz-cookiebanner-container, .cmplz-cookiebanner,
                        .cky-consent-container, .cky-modal, .cky-overlay, .cky-btn-revisit-wrapper,
                        #cookie-law-info-bar, #cookie-law-info-again, #cliModal, .cli-modal-backdrop,
                        #hs-eu-cookie-confirmation, #truste-consent-track,
                        #iubenda-cs-banner, #cookiescript_injected,
                        .osano-cm-window, .osano-cm-dialog, .osano-cm-widget, .cc-window.cc-banner,
                        #shopify-pc__banner, #shopify-pc__prefs,
                        #cookie-banner, #cookie-notice, #cookie-consent-banner, #cookie-consent-modal,
                        .cookie-banner, .cookie-notice, .cookie-consent-banner, .cookie-consent-modal
                    ):not(html, body) { display: none !important; }
                ` + (window === window.top ? `
                    :root, body { scrollbar-width: none !important; scrollbar-gutter: auto !important; }
                    :root::-webkit-scrollbar, body::-webkit-scrollbar { display: none !important; }
                ` : '');
                if ('adoptedStyleSheets' in document) {
                    const sheet = new CSSStyleSheet();
                    sheet.replaceSync(css);
                    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
                } else {
                    const style = document.createElement('style');
                    style.textContent = css;
                    if (document.documentElement) document.documentElement.append(style);
                    else document.addEventListener('DOMContentLoaded',
                        () => document.documentElement.append(style), { once: true });
                }
            })();
            """, injectionTime: .atDocumentStart, forMainFrameOnly: false, in: .defaultClient))
        view = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        container.wantsLayer = true
        container.layer?.backgroundColor = dashboardBackground.cgColor
        container.layer?.masksToBounds = true
        container.addSubview(view)
        loadingBackdrop.wantsLayer = true
        loadingBackdrop.layer?.backgroundColor = dashboardBackground.cgColor
        loadingBackdrop.autoresizingMask = [.width, .height]
        container.addSubview(loadingBackdrop, positioned: .above, relativeTo: view)
        view.underPageBackgroundColor = dashboardBackground
        view.pageZoom = 0.8
        view.navigationDelegate = self
        view.uiDelegate = self
        view.allowsBackForwardNavigationGestures = true
        observations = [view.observe(\.isLoading, options: [.new]) { [weak self] _, _ in self?.publish() },
                        view.observe(\.url, options: [.new]) { [weak self] _, _ in self?.publish() }]
    }

    func publish(error: String = "") {
        update?(["token": token, "url": view.url?.absoluteString ?? "",
                 "canBack": view.canGoBack, "canForward": view.canGoForward,
                 "loading": view.isLoading, "error": error])
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.targetFrame?.isMainFrame == false,
           let scheme = action.request.url?.scheme, ["about", "data", "blob"].contains(scheme) {
            decisionHandler(.allow)
            return
        }
        guard let url = action.request.url, publicWebsiteURL(url) else {
            decisionHandler(.cancel)
            if action.targetFrame?.isMainFrame != false { publish(error: "Only public websites can open here.") }
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if action.navigationType == .linkActivated,
           let url = action.request.url, publicWebsiteURL(url) { view.load(action.request) }
        return nil
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        loadingBackdrop.isHidden = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadingBackdrop.isHidden = true
        publish()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        if (error as NSError).code == NSURLErrorCancelled {
            if !view.isLoading { loadingBackdrop.isHidden = view.url == nil }
        } else {
            publish(error: "Website unavailable · open ↗")
        }
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code != NSURLErrorCancelled { publish(error: "Website unavailable · open ↗") }
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        loadingBackdrop.isHidden = false
        publish(error: "Website stopped · reload to retry")
    }
}

private struct LocalTransferStatus: Decodable {
    let downloadBytes: Int64
    let availableBytes: Int64
    let requiredBytes: Int64
    let blockedReason: String
}

private final class StorageModePanel: NSPanel {
    private let mode = NSSegmentedControl(labels: ["Local Mode", "Virtual Machine"],
                                          trackingMode: .selectOne, target: nil, action: nil)
    private let host = NSTextField(string: "")
    private let user = NSTextField(string: "ubuntu")
    private let sshPort = NSTextField(string: "22")
    private let identity = NSTextField(string: "")
    private let knownHosts = NSTextField(string: "")
    private let tunnelPort = NSTextField(string: "18787")
    private let details = NSTextField(wrappingLabelWithString: "")
    private let status = NSTextField(wrappingLabelWithString: "")
    private let save = NSButton(title: "Save", target: nil, action: nil)
    private let cancel = NSButton(title: "Cancel", target: nil, action: nil)
    private let browseIdentity = NSButton(title: "Choose…", target: nil, action: nil)
    private let browseKnownHosts = NSButton(title: "Choose…", target: nil, action: nil)
    private let copyPrompt = NSButton(title: "Copy setup prompt", target: nil, action: nil)
    private let help = NSStackView()
    private let spinner = NSProgressIndicator()
    private let form = NSStackView()
    private let content = NSStackView()
    private let activeVM: Bool
    private let hasPending: Bool
    private let localPort: Int
    private let checkLocal: (@escaping (LocalTransferStatus?, String?) -> Void) -> Void
    private let submit: (Data?, @escaping (String?) -> Void) -> Void
    private var localCheck: UUID?
    private var localDownloadAllowed = false

    init(activeVM: Bool, pending: [String: Any]?, profile: [String: Any]?, localPort: Int,
         preferredMode: Int? = nil,
         checkLocal: @escaping (@escaping (LocalTransferStatus?, String?) -> Void) -> Void,
         submit: @escaping (Data?, @escaping (String?) -> Void) -> Void) {
        self.activeVM = activeVM
        self.hasPending = pending != nil
        self.localPort = localPort
        self.checkLocal = checkLocal
        self.submit = submit
        super.init(contentRect: NSRect(x: 0, y: 0, width: 565, height: 250),
                   styleMask: [.titled], backing: .buffered, defer: false)
        title = "Storage Mode"
        isReleasedWhenClosed = false

        let heading = NSTextField(labelWithString: "Storage Mode")
        heading.font = .systemFont(ofSize: 17, weight: .semibold)
        mode.setAccessibilityLabel("Storage Mode")
        mode.selectedSegment = preferredMode
            ?? pending.map { $0["mode"] as? String == "local" ? 0 : 1 }
            ?? (activeVM ? 1 : 0)
        mode.target = self
        mode.action = #selector(updateMode)

        host.placeholderString = "Hostname or IPv4 address"
        identity.placeholderString = "/path/to/private-key"
        knownHosts.placeholderString = "/path/to/known_hosts"
        host.stringValue = profile?["host"] as? String ?? ""
        user.stringValue = profile?["user"] as? String ?? "ubuntu"
        sshPort.stringValue = String(profile?["ssh_port"] as? Int ?? 22)
        identity.stringValue = profile?["identity_file"] as? String ?? ""
        knownHosts.stringValue = profile?["known_hosts_file"] as? String ?? ""
        tunnelPort.stringValue = String(profile?["local_port"] as? Int ?? (localPort == 18787 ? 18788 : 18787))

        let rows: [(String, NSTextField, NSView)] = [
            ("VM host", host, host),
            ("SSH user", user, user),
            ("SSH port", sshPort, sshPort),
            ("SSH private key", identity, NSStackView(views: [identity, browseIdentity])),
            ("Trusted known hosts", knownHosts, NSStackView(views: [knownHosts, browseKnownHosts])),
            ("Local tunnel port", tunnelPort, tunnelPort),
        ]
        let grid = NSGridView(views: rows.map { title, field, view in
            field.setAccessibilityLabel(title)
            return [NSTextField(labelWithString: title), view]
        })
        grid.columnSpacing = 12
        grid.rowSpacing = 9
        grid.column(at: 1).xPlacement = .fill
        grid.widthAnchor.constraint(equalToConstant: 517).isActive = true

        browseIdentity.target = self
        browseIdentity.action = #selector(chooseSSHFile(_:))
        browseKnownHosts.target = self
        browseKnownHosts.action = #selector(chooseSSHFile(_:))
        let guide = NSButton(title: "VM setup guide", target: self, action: #selector(openGuide))
        for (button, symbol) in [(guide, "book"), (copyPrompt, "doc.on.doc")] {
            button.bezelStyle = .rounded
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
            button.imagePosition = .imageLeading
        }
        copyPrompt.target = self
        copyPrompt.action = #selector(copySetupPrompt)
        help.addArrangedSubview(guide)
        help.addArrangedSubview(copyPrompt)
        help.spacing = 8
        help.alignment = .centerY
        form.orientation = .vertical
        form.alignment = .leading
        form.spacing = 9
        form.addArrangedSubview(grid)

        details.textColor = .secondaryLabelColor
        status.textColor = .secondaryLabelColor
        for label in [details, status] {
            label.widthAnchor.constraint(equalToConstant: 517).isActive = true
        }
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        save.bezelStyle = .rounded
        save.keyEquivalent = "\r"
        save.target = self
        save.action = #selector(saveMode)
        cancel.bezelStyle = .rounded
        cancel.keyEquivalent = "\u{1b}"
        cancel.target = self
        cancel.action = #selector(closePanel)
        let actions = NSStackView(views: [spinner, cancel, save])
        actions.spacing = 8
        actions.alignment = .centerY
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let footer = NSStackView(views: [help, spacer, actions])
        footer.alignment = .centerY
        footer.spacing = 12
        footer.widthAnchor.constraint(equalToConstant: 517).isActive = true

        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 14
        content.edgeInsets = NSEdgeInsets(top: 22, left: 24, bottom: 20, right: 24)
        for view in [heading, mode, details, form, status, footer] {
            content.addArrangedSubview(view)
        }
        contentView = content
        updateMode()
        initialFirstResponder = mode
    }

    private var wantsVM: Bool { mode.selectedSegment == 1 }

    @objc private func updateMode() {
        localCheck = nil
        localDownloadAllowed = false
        form.isHidden = !wantsVM
        help.isHidden = !wantsVM
        details.stringValue = wantsVM
            ? "Connect to a VM already running Beings Growth. Use its verified SSH host key."
            : "Store companies, scans, and provider settings on this Mac."
        save.isEnabled = wantsVM || activeVM || hasPending
        save.title = wantsVM ? "Verify & Save" : "Save"
        if !wantsVM && activeVM {
            save.title = "Proceed"
            save.isEnabled = false
            showStatus("Checking the VM database size and available disk space…")
            let request = UUID()
            localCheck = request
            checkLocal { [weak self] result, error in
                guard let self, self.localCheck == request else { return }
                guard let result else {
                    self.showStatus(error ?? "Could not check the download size.", error: true)
                    return
                }
                let size = ByteCountFormatter.string(fromByteCount: result.downloadBytes, countStyle: .file)
                let free = ByteCountFormatter.string(fromByteCount: result.availableBytes, countStyle: .file)
                let required = ByteCountFormatter.string(fromByteCount: result.requiredBytes, countStyle: .file)
                let warning = "Switching back to local will download \(size) of data to your machine."
                let space = "\(free) available. \(required) required, including working space."
                self.localDownloadAllowed = result.blockedReason.isEmpty
                    && result.availableBytes >= result.requiredBytes
                self.save.isEnabled = self.localDownloadAllowed
                let reason = !result.blockedReason.isEmpty ? result.blockedReason
                    : self.localDownloadAllowed ? "" : "There is not enough free space."
                let prompt = self.localDownloadAllowed ? " Proceed?" : ""
                self.showStatus("\(warning)\(prompt)\n\(space)\(reason.isEmpty ? "" : " \(reason)")",
                                error: !self.localDownloadAllowed)
            }
            return
        }
        showStatus(hasPending ? "A storage change is scheduled for the next launch." : "")
    }

    private func showStatus(_ message: String, error: Bool = false) {
        status.stringValue = message
        status.textColor = error ? .systemRed : .secondaryLabelColor
        status.isHidden = message.isEmpty
        content.layoutSubtreeIfNeeded()
        setContentSize(NSSize(width: 565, height: ceil(content.fittingSize.height)))
    }

    @objc private func openGuide() {
        let deployed = URL(fileURLWithPath: "\(projectPath)/deploy/oracle/PUBLIC_SETUP.md")
        let bundled = URL(fileURLWithPath: "\(projectPath)/deploy/oracle/README.md")
        NSWorkspace.shared.open(FileManager.default.fileExists(atPath: deployed.path) ? deployed : bundled)
    }

    @objc private func copySetupPrompt() {
        let prompt = """
        Set up a private virtual machine for my Beings Growth app. Work through the setup with me
        and run the commands you can. Ask me to handle sign-in, MFA, or console steps when needed.

        Find my Beings Growth checkout or clone https://github.com/cameoed/growth. Follow its
        public Oracle setup guide at deploy/oracle/PUBLIC_SETUP.md or deploy/oracle/README.md
        in the GitHub checkout. Reuse a suitable Ubuntu VM I already own. If I need a new VM,
        confirm current Oracle Free Tier eligibility and availability before provisioning.
        Ask before creating anything that could incur charges.

        Configure key-based SSH and restrict SSH ingress to my IP. Verify the VM's SSH host key
        through the provider console and pin it in a dedicated local known_hosts file. Keep the
        private key on my Mac and use its file path in the connection profile. Keep Beings Growth
        bound to 127.0.0.1:8787 on the VM and reach it through an SSH tunnel.

        Use scripts/oracle_cloud.py and a private setup profile to install and start an empty
        Beings Growth instance if the VM is new. Preserve existing databases and settings.
        Ask me before moving any data. Verify the service through the SSH tunnel.

        When it's ready, give me the exact VM host, SSH user, SSH port, SSH private key path,
        trusted known-hosts path, and local tunnel port to enter under Beings Growth → Storage
        Mode → Virtual Machine. Then walk me through Verify & Save and reopening the app.
        """
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        if pasteboard.setString(prompt, forType: .string) {
            showStatus("Copied! Now paste into Codex or Claude")
        } else {
            showStatus("Could not copy the setup prompt.", error: true)
        }
    }

    @objc private func chooseSSHFile(_ sender: NSButton) {
        let isIdentity = sender === browseIdentity
        let picker = NSOpenPanel()
        picker.title = isIdentity ? "Choose SSH Private Key" : "Choose Trusted Known Hosts"
        picker.prompt = "Choose"
        picker.directoryURL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".ssh")
        picker.beginSheetModal(for: self) { [weak self] response in
            guard let self, response == .OK, let url = picker.url else { return }
            (isIdentity ? self.identity : self.knownHosts).stringValue = url.path
        }
    }

    private func setSaving(_ saving: Bool) {
        mode.isEnabled = !saving
        form.isHidden = !wantsVM
        for control in [host, user, sshPort, identity, knownHosts, tunnelPort, browseIdentity, browseKnownHosts, copyPrompt, save, cancel] as [NSControl] {
            control.isEnabled = !saving
        }
        if saving { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
    }

    @objc private func saveMode() {
        let downloading = !wantsVM && activeVM
        guard !downloading || localDownloadAllowed else { return }
        guard makeFirstResponder(nil) else { return }
        var profile: Data?
        if wantsVM {
            let hostname = host.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            let username = user.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard hostname.range(of: #"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$"#, options: .regularExpression) != nil,
                  username.range(of: #"^[a-z_][a-z0-9_-]{0,31}$"#, options: .regularExpression) != nil else {
                showStatus("Enter a VM hostname or IPv4 address and a valid SSH user.", error: true)
                return
            }
            guard let port = Int(sshPort.stringValue), (1...65535).contains(port) else {
                showStatus("Enter an SSH port from 1 to 65535.", error: true)
                return
            }
            guard let tunnel = Int(tunnelPort.stringValue), (1024...65535).contains(tunnel),
                  tunnel != 8787, tunnel != localPort else {
                showStatus("Enter a tunnel port from 1024 to 65535 that differs from the local dashboard port.", error: true)
                return
            }
            guard identity.stringValue.hasPrefix("/"), knownHosts.stringValue.hasPrefix("/") else {
                showStatus("Choose an SSH private key and a trusted known-hosts file.", error: true)
                return
            }
            do {
                profile = try JSONSerialization.data(withJSONObject: [
                    "enabled": true, "host": hostname, "user": username, "ssh_port": port,
                    "identity_file": identity.stringValue, "known_hosts_file": knownHosts.stringValue,
                    "local_port": tunnel,
                ])
            } catch {
                showStatus("Could not read the VM connection details.", error: true)
                return
            }
        }

        setSaving(true)
        showStatus(downloading ? "Downloading and verifying the VM database…"
            : wantsVM ? "Verifying the SSH connection and private service…" : "Saving…")
        submit(profile) { [weak self] error in
            guard let self else { return }
            self.setSaving(false)
            if let error {
                self.showStatus(error, error: true)
            } else {
                self.mode.isEnabled = false
                self.form.isHidden = true
                self.help.isHidden = true
                self.save.isHidden = true
                self.cancel.title = "Done"
                self.cancel.keyEquivalent = "\r"
                self.showStatus("Saved. Quit and reopen Beings Growth to apply this selection.")
                self.makeFirstResponder(self.cancel)
            }
        }
    }

    @objc private func closePanel() {
        sheetParent?.endSheet(self)
    }

    func present(on parent: NSWindow) {
        let monitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] event in
            guard let self, let sheetParent = self.sheetParent, event.window === sheetParent,
                  self.cancel.isEnabled, self.attachedSheet == nil,
                  !self.frame.contains(sheetParent.convertPoint(toScreen: event.locationInWindow)) else {
                return event
            }
            self.closePanel()
            return nil
        }
        parent.beginSheet(self) { _ in
            if let monitor { NSEvent.removeMonitor(monitor) }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    private var window: DashboardWindow?
    private var webView: WKWebView?
    private var serverProcess: Process?
    private var logHandle: FileHandle?
    private let connectionQueue = DispatchQueue(label: "org.beings.growth.connection")
    private var connectionTimer: Timer?
    private var connectionPending = false
    private var awaitingMaintenanceResume = false
    private var backendStartAttempted = false
    private var terminating = false
    private var displayingStatus = false
    private var dashboardHasLoaded = false
    private var dashboardContentLost = false
    private var missedHealthChecks = 0
    private var connectionNoticeShowing = false
    private var statusTitle: NSTextField?
    private var statusDetail: NSTextField?
    private var statusView: NSView?
    private var pendingDashboardReveal: WKNavigation?
    private var companyWebsite: CompanyWebsite?
    private var websiteToken = 0
    private var websiteKeyMonitor: Any?
    private let gatewayProof = UUID().uuidString
    private var verifiedGatewayPage = false

    private enum DashboardConnection {
        case unavailable, healthy, incompatible
    }

    private func trustedDashboard(_ url: URL?) -> Bool {
        guard let url else { return false }
        let trustedOrigin = url.scheme == "http" && url.host == "127.0.0.1"
            && url.port == Int(configuredPort()) && url.user == nil && url.password == nil
        return trustedOrigin && (remoteConfiguration() == nil
            || (verifiedGatewayPage && url.path == "/"))
    }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.webView === webView,
              message.frameInfo.isMainFrame, trustedDashboard(message.frameInfo.request.url),
              trustedDashboard(webView?.url), !displayingStatus else { return }
        if message.name == "interfaceZoom", let action = message.body as? String {
            if action == "in" { zoomIn(nil) }
            else if action == "out" { zoomOut(nil) }
            return
        }
        if message.name == "storageSettings", let action = message.body as? String {
            switch action {
            case "open": presentStorageSettings()
            case "open-local": presentStorageSettings(preferredMode: 0)
            case "open-virtual-machine": presentStorageSettings(preferredMode: 1)
            default: break
            }
            return
        }
        guard message.name == "companyWebsite",
              let data = message.body as? [String: Any], let action = data["action"] as? String,
              let token = data["token"] as? Int, token >= websiteToken else { return }
        if action == "clear" {
            websiteToken = token
            window?.websitePane = nil
            companyWebsite?.update = nil
            companyWebsite?.view.stopLoading()
            companyWebsite?.container.removeFromSuperview()
            companyWebsite = nil
            return
        }
        guard token == websiteToken else { return }
        let zoom = (data["zoom"] as? Double).flatMap { (0.5...2).contains($0) ? $0 : nil }
        if action == "navigate", let value = data["url"] as? String,
           let url = URL(string: value), publicWebsiteURL(url) {
            if companyWebsite == nil {
                let website = CompanyWebsite(token: token)
                website.update = { [weak self] state in
                    guard let self, self.websiteToken == token,
                          let json = try? JSONSerialization.data(withJSONObject: state),
                          let payload = String(data: json, encoding: .utf8) else { return }
                    self.webView?.evaluateJavaScript(
                        "window.dispatchEvent(new CustomEvent('beings-website-state',{detail:\(payload)}))",
                        completionHandler: nil)
                }
                companyWebsite = website
                website.container.isHidden = true
                webView?.addSubview(website.container)
                window?.websitePane = website.container
            }
            if let zoom { companyWebsite?.view.pageZoom = zoom }
            companyWebsite?.view.setAllMediaPlaybackSuspended(data["muted"] as? Bool ?? true)
            companyWebsite?.view.load(URLRequest(url: url))
            return
        }
        guard let website = companyWebsite else { return }
        switch action {
        case "bounds":
            guard !connectionNoticeShowing, !dashboardContentLost,
                  data["visible"] as? Bool == true else {
                website.container.isHidden = true
                return
            }
            guard let parent = webView,
                  let width = data["width"] as? Double, let height = data["height"] as? Double,
                  let x = data["x"] as? Double, let y = data["y"] as? Double,
                  let viewportWidth = data["viewportWidth"] as? Double,
                  let viewportHeight = data["viewportHeight"] as? Double,
                  [width, height, x, y, viewportWidth, viewportHeight].allSatisfy({ $0.isFinite }),
                  viewportWidth > 0, viewportHeight > 0, x >= 0, y >= 0,
                  width > 0, height > 0 else {
                website.container.isHidden = true
                return
            }
            let scaleX = parent.bounds.width / viewportWidth
            let scaleY = parent.bounds.height / viewportHeight
            let rect = NSRect(x: x * scaleX,
                              y: parent.isFlipped ? y * scaleY : parent.bounds.height - (y + height) * scaleY,
                              width: width * scaleX, height: height * scaleY).intersection(parent.bounds)
            guard !rect.isEmpty else {
                website.container.isHidden = true
                return
            }
            // Older dashboard pages supply only the visible rectangle.
            let contentWidth = data["contentWidth"] as? Double ?? width
            let contentHeight = data["contentHeight"] as? Double ?? height
            let insetX = data["insetX"] as? Double ?? 0
            let insetY = data["insetY"] as? Double ?? 0
            guard [contentWidth, contentHeight, insetX, insetY].allSatisfy({ $0.isFinite }),
                  contentWidth >= width, contentHeight >= height, insetX >= 0, insetY >= 0 else {
                website.container.isHidden = true
                return
            }
            website.container.frame = rect
            let radius = data["bottomRadius"] as? Double ?? 13
            let reachesBottom = contentHeight - insetY - height < 0.5
            var corners: CACornerMask = []
            if reachesBottom {
                if insetX < 0.5 { corners.insert(.layerMinXMinYCorner) }
                if contentWidth - insetX - width < 0.5 { corners.insert(.layerMaxXMinYCorner) }
            }
            website.container.layer?.maskedCorners = corners
            website.container.layer?.cornerRadius = radius.isFinite ? max(0, min(radius * scaleX, rect.width / 2, rect.height / 2)) : 0
            let fullHeight = contentHeight * scaleY
            website.view.frame = NSRect(x: -insetX * scaleX,
                                        y: rect.height - fullHeight + insetY * scaleY,
                                        width: contentWidth * scaleX, height: fullHeight)
            website.container.isHidden = false
        case "back": if website.view.canGoBack { website.view.goBack() }
        case "forward": if website.view.canGoForward { website.view.goForward() }
        case "reload": website.view.reload()
        case "zoom":
            if let zoom { website.view.pageZoom = zoom }
        case "audio":
            if let muted = data["muted"] as? Bool { website.view.setAllMediaPlaybackSuspended(muted) }
        default: break
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installMenu()
        if FileManager.default.fileExists(atPath: nextStoragePath),
           let error = storageCommand("apply-storage") {
            storageNotice("Storage preference could not be applied", detail: error)
        }
        showDashboard()
        // Key events inside the isolated website never reach the dashboard DOM.
        websiteKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, self.exitFocusedWebsite(event) else { return event }
            return nil
        }
        connectionTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) {
            [weak self] _ in self?.checkConnection()
        }
        checkConnection()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let websiteKeyMonitor { NSEvent.removeMonitor(websiteKeyMonitor) }
        terminating = true
        connectionTimer?.invalidate()
        connectionQueue.sync {} // Drain any owned health/start attempt before shutdown.
        guard let process = serverProcess, process.isRunning else {
            try? logHandle?.synchronize()
            try? logHandle?.close()
            return
        }

        // Ask Uvicorn to run its normal lifespan shutdown so active Smart Scan state and
        // completed child items reach SQLite before the native wrapper exits. The process is
        // owned only when this app instance launched it; a pre-existing dashboard is untouched.
        process.terminate()
        let deadline = Date().addingTimeInterval(12)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        try? logHandle?.synchronize()
        try? logHandle?.close()
    }

    private func exitFocusedWebsite(_ event: NSEvent) -> Bool {
        guard event.keyCode == 53, !event.isARepeat, !displayingStatus,
              event.modifierFlags.intersection([.command, .control, .option, .shift]).isEmpty,
              let window, event.window === window,
              let website = companyWebsite, !website.container.isHidden,
              let responder = window.firstResponder as? NSView,
              responder.isDescendant(of: website.view),
              let dashboard = webView, trustedDashboard(dashboard.url),
              window.makeFirstResponder(dashboard) else { return false }
        dashboard.evaluateJavaScript(
            "window.dispatchEvent(new CustomEvent('beings-website-escape',{detail:\(website.token)}))",
            completionHandler: nil)
        return true
    }

    private func installMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu(title: "Beings Growth")
        appMenu.addItem(withTitle: "About Beings Growth", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        let storageItem = appMenu.addItem(withTitle: "Storage Mode…",
                                         action: #selector(openStorageSettings(_:)), keyEquivalent: ",")
        storageItem.target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Beings Growth", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Beings Growth", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let fileItem = NSMenuItem()
        mainMenu.addItem(fileItem)
        let fileMenu = NSMenu(title: "File")
        let printItem = fileMenu.addItem(withTitle: "Print…", action: #selector(printPage(_:)), keyEquivalent: "p")
        printItem.target = self
        fileItem.submenu = fileMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenu.addItem(.separator())
        let findItem = editMenu.addItem(withTitle: "Find…", action: #selector(NSResponder.performTextFinderAction(_:)), keyEquivalent: "f")
        findItem.tag = NSTextFinder.Action.showFindInterface.rawValue
        let findNextItem = editMenu.addItem(withTitle: "Find Next", action: #selector(NSResponder.performTextFinderAction(_:)), keyEquivalent: "g")
        findNextItem.tag = NSTextFinder.Action.nextMatch.rawValue
        let findPreviousItem = editMenu.addItem(withTitle: "Find Previous", action: #selector(NSResponder.performTextFinderAction(_:)), keyEquivalent: "G")
        findPreviousItem.tag = NSTextFinder.Action.previousMatch.rawValue
        editItem.submenu = editMenu

        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "View")
        let reloadItem = viewMenu.addItem(withTitle: "Reload", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        reloadItem.target = self
        let reloadFreshItem = viewMenu.addItem(withTitle: "Reload Without Cache", action: #selector(reloadPageWithoutCache(_:)), keyEquivalent: "R")
        reloadFreshItem.target = self
        viewMenu.addItem(.separator())
        let backItem = viewMenu.addItem(withTitle: "Back", action: #selector(goBack(_:)), keyEquivalent: "[")
        backItem.target = self
        let forwardItem = viewMenu.addItem(withTitle: "Forward", action: #selector(goForward(_:)), keyEquivalent: "]")
        forwardItem.target = self
        viewMenu.addItem(.separator())
        let actualSizeItem = viewMenu.addItem(withTitle: "Actual Size", action: #selector(resetZoom(_:)), keyEquivalent: "0")
        actualSizeItem.target = self
        let zoomInItem = viewMenu.addItem(withTitle: "Zoom In", action: #selector(zoomIn(_:)), keyEquivalent: "+")
        zoomInItem.target = self
        let zoomOutItem = viewMenu.addItem(withTitle: "Zoom Out", action: #selector(zoomOut(_:)), keyEquivalent: "-")
        zoomOutItem.target = self
        viewItem.submenu = viewMenu

        let windowItem = NSMenuItem()
        mainMenu.addItem(windowItem)
        let windowMenu = NSMenu(title: "Window")
        let minimizeItem = windowMenu.addItem(withTitle: "Minimize", action: #selector(minimizeWindow(_:)), keyEquivalent: "m")
        minimizeItem.target = self
        let closeItem = windowMenu.addItem(withTitle: "Close", action: #selector(closeWindow(_:)), keyEquivalent: "w")
        closeItem.target = self
        windowItem.submenu = windowMenu
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = mainMenu
    }

    private func storageCommand(_ command: String, profile: Data? = nil,
                                receive: ((Data) -> Void)? = nil) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "\(projectPath)/.venv/bin/python")
        process.arguments = ["\(projectPath)/scripts/oracle_cloud.py", command]
        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        let response = receive == nil ? nil : Pipe()
        process.standardOutput = response.map { $0 as Any } ?? FileHandle.nullDevice
        process.standardError = output
        if profile != nil { process.arguments?.append("--stdin") }
        do {
            try process.run()
            if let profile { try input.fileHandleForWriting.write(contentsOf: profile) }
            try input.fileHandleForWriting.close()
        } catch {
            try? input.fileHandleForWriting.close()
            return "Make sure the checkout and its Python environment are installed."
        }
        let diagnostic = output.fileHandleForReading.readDataToEndOfFile()
        let result = response?.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus == 0 {
            if let result { receive?(result) }
            return nil
        }
        let message = String(decoding: diagnostic, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        // The helper emits a single, credential-free diagnostic for expected errors.
        return message.isEmpty || message.contains("\n") ? "Could not update storage settings." : message
    }

    private func storageNotice(_ title: String, detail: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = detail
        alert.runModal()
    }

    @objc private func openStorageSettings(_ sender: Any?) {
        presentStorageSettings()
    }

    private func presentStorageSettings(preferredMode: Int? = nil) {
        guard let window, window.attachedSheet == nil else { return }
        let pending = storageProfile(at: nextStoragePath)
        let pendingVM = pending?["enabled"] as? Bool == true ? pending : nil
        let activeVM = FileManager.default.fileExists(atPath: remoteConfigPath)
        let panel = StorageModePanel(
            activeVM: activeVM,
            pending: pending,
            profile: pendingVM ?? remoteConfiguration() ?? storageProfile(at: savedVMPath),
            localPort: Int(ProcessInfo.processInfo.environment["BEINGS_LEADS_PORT"] ?? "") ?? 8787,
            preferredMode: preferredMode,
            checkLocal: { [weak self] completion in
                DispatchQueue.global(qos: .userInitiated).async {
                    var result: LocalTransferStatus?
                    let error = self?.storageCommand("check-local-download") { response in
                        let decoder = JSONDecoder()
                        decoder.keyDecodingStrategy = .convertFromSnakeCase
                        result = try? decoder.decode(LocalTransferStatus.self, from: response)
                    }
                    let checked = result
                    DispatchQueue.main.async { completion(checked, error) }
                }
            }
        ) { [weak self] profile, completion in
            DispatchQueue.global(qos: .userInitiated).async {
                let command = profile == nil ? (activeVM ? "download-local" : "stage-local") : "stage-oracle"
                let error = self?.storageCommand(command, profile: profile)
                DispatchQueue.main.async { completion(error) }
            }
        }
        panel.present(on: window)
    }

    @objc private func printPage(_ sender: Any?) {
        guard let webView else { return }
        webView.printOperation(with: NSPrintInfo.shared).run()
    }

    @objc private func reloadPage(_ sender: Any?) {
        if displayingStatus {
            if !awaitingMaintenanceResume { backendStartAttempted = false }
            checkConnection()
            return
        }
        webView?.reload()
    }

    @objc private func reloadPageWithoutCache(_ sender: Any?) {
        if displayingStatus {
            reloadPage(sender)
            return
        }
        guard let url = webView?.url else { return }
        webView?.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData))
    }

    @objc private func goBack(_ sender: Any?) {
        if webView?.canGoBack == true { webView?.goBack() }
    }

    @objc private func goForward(_ sender: Any?) {
        if webView?.canGoForward == true { webView?.goForward() }
    }

    @objc private func resetZoom(_ sender: Any?) {
        webView?.pageZoom = 1
    }

    @objc private func zoomIn(_ sender: Any?) {
        guard let webView else { return }
        webView.pageZoom = min(webView.pageZoom + 0.1, 3)
    }

    @objc private func zoomOut(_ sender: Any?) {
        guard let webView else { return }
        webView.pageZoom = max(webView.pageZoom - 0.1, 0.5)
    }

    @objc private func minimizeWindow(_ sender: Any?) {
        window?.miniaturize(sender)
    }

    @objc private func closeWindow(_ sender: Any?) {
        window?.performClose(sender)
    }

    private func dashboardConnection() -> DashboardConnection {
        let remoteMode = FileManager.default.fileExists(atPath: remoteConfigPath)
        let remote = remoteConfiguration()
        if remoteMode && remote == nil { return .unavailable }
        var request = URLRequest(url: localURL(path: "/healthz"))
        let timeout: TimeInterval = remoteMode ? 6 : 3
        request.timeoutInterval = timeout
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let semaphore = DispatchSemaphore(value: 0)
        var result = DashboardConnection.unavailable
        let expectedURL = request.url
        let proof = gatewayProof
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            defer { semaphore.signal() }
            guard error == nil, let response = response as? HTTPURLResponse else { return }
            if remoteMode {
                guard response.url == expectedURL,
                      response.value(forHTTPHeaderField: "X-Beings-Desktop-Gateway") == proof
                else { return }
                if response.statusCode == 426 {
                    result = .incompatible
                    return
                }
                let value = data.flatMap {
                    try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
                }
                guard value?["status"] as? String == "ok",
                      value?["database"] as? String == "/opt/beings-growth/data/leads.db"
                else { return }
            }
            if (200..<300).contains(response.statusCode) { result = .healthy }
        }
        task.resume()
        guard semaphore.wait(timeout: .now() + timeout + 0.2) == .success else {
            task.cancel()
            return .unavailable
        }
        return result
    }

    private func checkConnection() {
        guard !connectionPending, !terminating else { return }
        connectionPending = true
        let mayStart = !backendStartAttempted && !awaitingMaintenanceResume
        connectionQueue.async { [weak self] in
            guard let self else { return }
            let maintenance = databaseMaintenanceActive()
            let connection = maintenance ? .unavailable : self.dashboardConnection()
            var started = false
            var failed = false
            if !maintenance && connection == .unavailable && mayStart
                && self.serverProcess?.isRunning != true {
                do {
                    try self.startDashboard()
                    started = true
                } catch { failed = true }
            }
            DispatchQueue.main.async {
                guard !self.terminating else { return }
                self.connectionPending = false
                if maintenance {
                    self.awaitingMaintenanceResume = true
                    self.showConnectionStatus("Database maintenance in progress",
                        detail: "The app will reconnect automatically when the service resumes.")
                } else if connection == .incompatible {
                    self.verifiedGatewayPage = false
                    self.dashboardContentLost = true
                    self.showConnectionStatus("VM update required",
                        detail: "Update Beings Growth on the virtual machine to use this Mac build.")
                } else if connection == .healthy {
                    self.awaitingMaintenanceResume = false
                    self.backendStartAttempted = false
                    self.missedHealthChecks = 0
                    if !self.dashboardContentLost { self.hideConnectionNotice() }
                    if self.displayingStatus || (self.dashboardContentLost && self.webView?.isLoading == false) {
                        self.displayingStatus = false
                        self.webView?.load(URLRequest(
                            url: self.webView?.url ?? localURL(path: "/?build=\(buildID)"),
                            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData
                        ))
                    }
                } else {
                    self.backendStartAttempted = self.backendStartAttempted || started || failed
                    self.missedHealthChecks = min(self.missedHealthChecks + 1, 3)
                    if self.dashboardHasLoaded && self.missedHealthChecks < 3 { return }
                    let title = self.dashboardHasLoaded ? "Reconnecting…"
                        : (failed ? "Service unavailable" : "Connecting…")
                    self.showConnectionStatus(title,
                        detail: self.awaitingMaintenanceResume
                            ? "Waiting for maintenance to resume the local service."
                            : failed ? "Use View → Reload to retry." : "")
                }
            }
        }
    }

    private func hideConnectionNotice() {
        guard connectionNoticeShowing else { return }
        connectionNoticeShowing = false
        if let webView, trustedDashboard(webView.url) {
            webView.evaluateJavaScript("""
                document.dispatchEvent(new CustomEvent('beings-connection-status', {detail: null}));
                window.dispatchEvent(new CustomEvent('beings-website-reconnect', {detail: false}));
                """, completionHandler: nil)
        }
    }

    private func showConnectionStatus(_ title: String, detail: String = "") {
        companyWebsite?.container.isHidden = true
        if dashboardHasLoaded, !dashboardContentLost, let webView, trustedDashboard(webView.url) {
            connectionNoticeShowing = true
            webView.callAsyncJavaScript("""
                document.dispatchEvent(new CustomEvent('beings-connection-status', {detail: {title, detail}}));
                window.dispatchEvent(new CustomEvent('beings-website-reconnect', {detail: true}));
                """, arguments: ["title": title, "detail": detail], in: nil, in: .page,
                completionHandler: nil)
            return
        }
        showInitialConnectionStatus(title, detail: detail)
    }

    private func showInitialConnectionStatus(_ title: String, detail: String) {
        pendingDashboardReveal = nil
        if statusView == nil {
            let root = NSView()
            root.wantsLayer = true
            root.layer?.backgroundColor = dashboardBackground.cgColor
            let spinner = NSProgressIndicator()
            spinner.style = .spinning
            spinner.controlSize = .regular
            spinner.startAnimation(nil)
            let heading = NSTextField(labelWithString: title)
            heading.font = .systemFont(ofSize: 18, weight: .semibold)
            let explanation = NSTextField(wrappingLabelWithString: detail)
            explanation.font = .systemFont(ofSize: 13)
            explanation.textColor = .secondaryLabelColor
            explanation.alignment = .center
            let stack = NSStackView(views: [spinner, heading, explanation])
            stack.orientation = .vertical
            stack.alignment = .centerX
            stack.spacing = 12
            stack.translatesAutoresizingMaskIntoConstraints = false
            root.addSubview(stack)
            NSLayoutConstraint.activate([
                stack.centerXAnchor.constraint(equalTo: root.centerXAnchor),
                stack.centerYAnchor.constraint(equalTo: root.centerYAnchor),
                stack.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
                stack.widthAnchor.constraint(lessThanOrEqualTo: root.widthAnchor, constant: -48),
            ])
            statusTitle = heading
            statusDetail = explanation
            statusView = root
        }
        statusTitle?.stringValue = title
        statusDetail?.stringValue = detail
        statusDetail?.isHidden = detail.isEmpty
        displayingStatus = true
        if let statusView, let content = window?.contentView {
            if statusView.superview == nil {
                statusView.frame = content.bounds
                statusView.autoresizingMask = [.width, .height]
                content.addSubview(statusView, positioned: .above, relativeTo: nil)
            }
            statusView.isHidden = false
        }
    }

    private func startDashboard() throws {
        let dataDirectory = "\(projectPath)/data"
        let logPath = "\(dataDirectory)/beings-growth.log"
        try FileManager.default.createDirectory(
            atPath: dataDirectory,
            withIntermediateDirectories: true
        )
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }

        guard let handle = FileHandle(forWritingAtPath: logPath) else {
            throw CocoaError(.fileWriteUnknown)
        }
        // Early-startup diagnostics are session-only; the Python worker rotates its runtime log.
        try handle.truncate(atOffset: 0)
        try handle.seekToEnd()
        logHandle = handle

        let process = Process()
        if FileManager.default.fileExists(atPath: remoteConfigPath) {
            // Remote configuration always fails closed: never launch an empty/stale local DB.
            guard remoteConfiguration() != nil else { throw CocoaError(.fileReadCorruptFile) }
            guard let dashboard = Bundle.main.url(forResource: "dashboard", withExtension: nil)
            else { throw CocoaError(.fileNoSuchFile) }
            process.executableURL = URL(fileURLWithPath: "\(projectPath)/.venv/bin/python")
            process.arguments = ["-m", "beings_leads.desktop_gateway", dashboard.path,
                                 remoteConfigPath]
        } else {
            process.executableURL = URL(fileURLWithPath: "\(projectPath)/.venv/bin/beings-dashboard")
        }
        process.currentDirectoryURL = URL(fileURLWithPath: projectPath)
        process.standardOutput = handle
        process.standardError = handle
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["BEINGS_LEADS_FILE_LOG"] = "1"
        environment["BEINGS_GATEWAY_PROOF"] = gatewayProof
        if FileManager.default.fileExists(atPath: remoteConfigPath),
           let runtime = Bundle.main.resourceURL?.appendingPathComponent("oracle-runtime/src"),
           FileManager.default.fileExists(atPath: runtime.appendingPathComponent("beings_leads/__init__.py").path) {
            environment["PYTHONPATH"] = runtime.path
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
        }
        process.environment = environment
        try process.run()
        serverProcess = process
    }

    private func showDashboard() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(self, name: "companyWebsite")
        configuration.userContentController.add(self, name: "storageSettings")
        configuration.userContentController.add(self, name: "interfaceZoom")
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.wantsLayer = true
        view.layer?.backgroundColor = dashboardBackground.cgColor
        view.underPageBackgroundColor = dashboardBackground
        view.navigationDelegate = self
        view.uiDelegate = self
        // The dashboard is an app surface, not a swipe-navigable browser.
        view.allowsBackForwardNavigationGestures = false

        let visibleFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let frame = NSRect(
            x: visibleFrame.minX + 20,
            y: visibleFrame.minY + 20,
            width: max(1100, visibleFrame.width - 40),
            height: max(720, visibleFrame.height - 40)
        )
        let appWindow = DashboardWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        appWindow.appearance = NSAppearance(named: .darkAqua)
        appWindow.backgroundColor = dashboardBackground
        appWindow.title = "Beings Growth"
        appWindow.titleVisibility = .hidden
        appWindow.titlebarAppearsTransparent = true
        appWindow.isReleasedWhenClosed = false
        // Keep a painted canvas and the loading view in place while WebKit navigates.
        let content = NSView(frame: appWindow.contentView?.bounds ?? .zero)
        content.wantsLayer = true
        content.layer?.backgroundColor = dashboardBackground.cgColor
        view.frame = content.bounds
        view.autoresizingMask = [.width, .height]
        content.addSubview(view)
        appWindow.contentView = content
        window = appWindow
        webView = view
        showConnectionStatus("Connecting…")
        appWindow.setFrame(frame, display: true)
        appWindow.makeKeyAndOrderFront(nil)

        NSApp.activate(ignoringOtherApps: true)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        if !dashboardHasLoaded {
            showConnectionStatus("Reconnecting…")
        }
        checkConnection()
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        pendingDashboardReveal = nil
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        // Dashboard tab changes share a document and keep the embedded browser alive.
        companyWebsite?.update = nil
        companyWebsite?.view.stopLoading()
        companyWebsite?.container.removeFromSuperview()
        companyWebsite = nil
        window?.websitePane = nil
        websiteToken = 0
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard trustedDashboard(webView.url) else { return }
        guard statusView?.isHidden == false else {
            finishDashboardLoad()
            return
        }
        pendingDashboardReveal = navigation
        // Ask WebKit to render before revealing it. Animation frames can stall behind the cover.
        let snapshot = WKSnapshotConfiguration()
        snapshot.rect = NSRect(x: 0, y: 0, width: 1, height: 1)
        webView.takeSnapshot(with: snapshot) { [weak self, weak webView] _, _ in
            guard let self, let webView, self.pendingDashboardReveal === navigation,
                  self.trustedDashboard(webView.url) else { return }
            self.pendingDashboardReveal = nil
            self.finishDashboardLoad()
        }
    }

    private func finishDashboardLoad() {
        statusView?.isHidden = true
        dashboardHasLoaded = true
        dashboardContentLost = false
        if missedHealthChecks < 3 && !awaitingMaintenanceResume {
            if connectionNoticeShowing {
                hideConnectionNotice()
            } else {
                webView?.evaluateJavaScript(
                    "window.dispatchEvent(new CustomEvent('beings-website-reconnect', {detail: false}))",
                    completionHandler: nil)
            }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard remoteConfiguration() != nil, response.isForMainFrame else {
            decisionHandler(.allow)
            return
        }
        verifiedGatewayPage = false
        if let http = response.response as? HTTPURLResponse,
           let url = http.url,
           url.path == "/", url.scheme == "http", url.host == "127.0.0.1",
           url.port == Int(configuredPort()), http.statusCode == 200,
           http.value(forHTTPHeaderField: "X-Beings-Desktop-Gateway") == gatewayProof {
            verifiedGatewayPage = true
        }
        decisionHandler(verifiedGatewayPage ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard !dashboardHasLoaded else { return }
        self.webView(webView, didFailProvisionalNavigation: navigation, withError: error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        dashboardContentLost = true
        companyWebsite?.container.isHidden = true
        showInitialConnectionStatus("Reconnecting…", detail: "Restoring the app window.")
        checkConnection()
    }

    private func isLocal(_ url: URL) -> Bool {
        url.host == "127.0.0.1" || url.host == "localhost"
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if navigationAction.navigationType == .linkActivated,
           url == localURL(path: "/api/sales-engine/outbound/export.csv")
            || (trustedDashboard(webView.url) && url.scheme == "http" && url.host == "127.0.0.1"
                && url.port == Int(configuredPort()) && url.path.hasPrefix("/api/")
                && url.path.hasSuffix(".md")) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        if isLocal(url) {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocal(url) {
                webView.load(URLRequest(url: url))
            } else {
                NSWorkspace.shared.open(url)
            }
        }
        return nil
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
