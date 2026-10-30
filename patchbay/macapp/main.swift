// PatchBay for macOS: a small native window around PatchBay's page.
// It starts PatchBay (the same Python and folder it was built for) if it isn't running yet, shows the page in its
// own window, and stops PatchBay again on Quit if it was the one that started it.
// Built on the user's Mac by mac_app.py with: swiftc -O main.swift -framework Cocoa -framework WebKit
// The three values below are filled in by mac_app.py.

import Cocoa
import WebKit

let PYTHON = "__PYTHON__"
let FOLDER = "__FOLDER__"
let PORT = "__PORT__"
let HOME_URL = URL(string: "http://127.0.0.1:\(PORT)/")!

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var status: NSTextField!
    var server: Process?
    var downloads: [AnyObject] = []

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let config = WKWebViewConfiguration()
        config.preferences.javaScriptCanOpenWindowsAutomatically = true
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1300, height: 860), configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.autoresizingMask = [.width, .height]
        web.isHidden = true

        status = NSTextField(labelWithString: "Starting PatchBay…")
        status.font = NSFont.systemFont(ofSize: 15)
        status.textColor = .secondaryLabelColor
        status.alignment = .center
        status.frame = NSRect(x: 0, y: 400, width: 1300, height: 30)
        status.autoresizingMask = [.width, .minYMargin, .maxYMargin]

        let content = NSView(frame: web.frame)
        content.addSubview(web)
        content.addSubview(status)

        window = NSWindow(contentRect: web.frame, styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "PatchBay"
        window.contentView = content
        window.minSize = NSSize(width: 700, height: 500)
        window.center()
        window.setFrameAutosaveName("PatchbayMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        DispatchQueue.global().async {
            if !self.isUp() { self.startServer() }
            var tries = 0
            while !self.isUp() && tries < 120 { Thread.sleep(forTimeInterval: 0.5); tries += 1 }
            let failed = tries >= 120
            DispatchQueue.main.async {
                if failed {
                    self.status.stringValue = "PatchBay didn't start. See data-cache/patchbay.log in \(FOLDER)."
                    return
                }
                self.status.isHidden = true
                self.web.isHidden = false
                self.web.load(URLRequest(url: HOME_URL))
            }
        }
    }

    // Is PatchBay's page answering?
    func isUp() -> Bool {
        final class Answer { var ok = false }
        let answer = Answer()
        let done = DispatchSemaphore(value: 0)
        var req = URLRequest(url: HOME_URL)
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            answer.ok = resp != nil
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 3)
        return answer.ok
    }

    func startServer() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: PYTHON)
        p.arguments = ["patchbay.py", "--no-browser"]
        p.currentDirectoryURL = URL(fileURLWithPath: FOLDER)
        let logs = URL(fileURLWithPath: FOLDER).appendingPathComponent("data-cache")
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logFile = logs.appendingPathComponent("patchbay.log")
        if !FileManager.default.fileExists(atPath: logFile.path) {
            FileManager.default.createFile(atPath: logFile.path, contents: nil)
        }
        if let h = try? FileHandle(forWritingTo: logFile) {
            h.seekToEndOfFile()
            p.standardOutput = h
            p.standardError = h
        }
        do {
            try p.run()
            server = p
        } catch {
            DispatchQueue.main.async { self.status.stringValue = "Couldn't start Python at \(PYTHON): \(error.localizedDescription)" }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        // Only stop PatchBay if this window started it (not if it runs at login on its own).
        if let p = server, p.isRunning { p.terminate() }
    }

    // ---- menus (Edit is needed for copy and paste in the page)

    func buildMenu() {
        let main = NSMenu()
        func add(_ title: String, _ items: [NSMenuItem]) {
            let top = NSMenuItem()
            let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            top.submenu = m
            main.addItem(top)
        }
        func item(_ t: String, _ a: Selector?, _ k: String, _ mods: NSEvent.ModifierFlags = [.command]) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            return i
        }
        add("PatchBay", [
            item("About PatchBay", #selector(NSApplication.orderFrontStandardAboutPanel(_:)), ""),
            NSMenuItem.separator(),
            item("Hide PatchBay", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:)), ""),
            NSMenuItem.separator(),
            item("Quit PatchBay", #selector(NSApplication.terminate(_:)), "q"),
        ])
        add("Edit", [
            item("Undo", Selector(("undo:")), "z"),
            item("Redo", Selector(("redo:")), "z", [.command, .shift]),
            NSMenuItem.separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"),
            item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"),
            item("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        add("View", [
            item("Reload", #selector(reload), "r"),
            item("Devices", #selector(goHome), "1"),
            item("Open in Browser", #selector(openInBrowser), "b", [.command, .shift]),
            NSMenuItem.separator(),
            item("Bigger Text", #selector(zoomIn), "+"),
            item("Smaller Text", #selector(zoomOut), "-"),
            item("Normal Text Size", #selector(zoomReset), "0"),
        ])
        add("Window", [
            item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:)), ""),
        ])
        NSApp.mainMenu = main
    }

    @objc func reload() { web.reload() }
    @objc func goHome() { web.load(URLRequest(url: HOME_URL)) }
    @objc func openInBrowser() { NSWorkspace.shared.open(web.url ?? HOME_URL) }
    @objc func zoomIn() { web.pageZoom = min(web.pageZoom + 0.1, 2.0) }
    @objc func zoomOut() { web.pageZoom = max(web.pageZoom - 0.1, 0.6) }
    @objc func zoomReset() { web.pageZoom = 1.0 }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil)
        return true
    }

    // ---- links: PatchBay's own pages stay here, everything else opens in the normal browser

    func isOwn(_ url: URL?) -> Bool {
        guard let u = url else { return true }
        return u.scheme == "about" || u.scheme == "blob" || u.scheme == "data" ||
            ((u.host == "127.0.0.1" || u.host == "localhost") && "\(u.port ?? 80)" == PORT)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if !isOwn(action.request.url) {
            if let u = action.request.url { NSWorkspace.shared.open(u) }
            decisionHandler(.cancel)
            return
        }
        if #available(macOS 11.3, *), action.shouldPerformDownload {
            decisionHandler(.download)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        var attachment = false
        if let http = response.response as? HTTPURLResponse,
           let cd = http.value(forHTTPHeaderField: "Content-Disposition"), cd.lowercased().contains("attachment") {
            attachment = true
        }
        if #available(macOS 11.3, *), attachment || !response.canShowMIMEType {
            decisionHandler(.download)
            return
        }
        decisionHandler(.allow)
    }

    @available(macOS 11.3, *)
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = DownloadSaver.shared
    }

    @available(macOS 11.3, *)
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = DownloadSaver.shared
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url {
            if isOwn(u) { webView.load(URLRequest(url: u)) } else { NSWorkspace.shared.open(u) }
        }
        return nil
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }

    // ---- the page's alert(), confirm(), prompt() and file pickers

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert()
        a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = field
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { r in completionHandler(r == .OK ? panel.urls : nil) }
    }
}

// Saves downloads into ~/Downloads (with a new name if one is already there) and shows them in Finder.
@available(macOS 11.3, *)
final class DownloadSaver: NSObject, WKDownloadDelegate {
    static let shared = DownloadSaver()
    var targets: [ObjectIdentifier: URL] = [:]

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var url = folder.appendingPathComponent(suggestedFilename)
        let base = url.deletingPathExtension().lastPathComponent, ext = url.pathExtension
        var n = 2
        while FileManager.default.fileExists(atPath: url.path) {
            url = folder.appendingPathComponent(ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)")
            n += 1
        }
        targets[ObjectIdentifier(download)] = url
        completionHandler(url)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = targets.removeValue(forKey: ObjectIdentifier(download)) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        targets.removeValue(forKey: ObjectIdentifier(download))
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
