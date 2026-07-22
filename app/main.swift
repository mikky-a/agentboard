// Agent Board — нативная обёртка: окно с WKWebView поверх localhost:8787.
// Бейдж на иконке в доке = сколько агентов ждут ответа.
import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var timer: Timer?
    var serverProc: Process?
    var lastSpawn: Date = .distantPast

    // DMG-сборка несёт сервер/python/tmux внутри Resources; dev-сборка — пустая
    // обёртка поверх launchd-сервера из ~/.agentboard (как раньше)
    var bundledRes: String? {
        guard let res = Bundle.main.resourcePath,
              FileManager.default.fileExists(atPath: res + "/server/agentboard.py")
        else { return nil }
        return res
    }

    // curl-установка жила на launchd (KeepAlive держит порт 8787) — снимаем её,
    // данные (~/.agentboard/board.json) остаются на месте и подхватываются
    func migrateFromLaunchd() {
        let plist = NSHomeDirectory() + "/Library/LaunchAgents/com.agentboard.plist"
        guard FileManager.default.fileExists(atPath: plist) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["unload", plist]
        try? p.run()
        p.waitUntilExit()
        try? FileManager.default.removeItem(atPath: plist)
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        if bundledRes != nil {
            migrateFromLaunchd()
            spawnServerIfNeeded()
        }
        webView = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // не белый, а фон окна

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Agent Board"
        window.isReleasedWhenClosed = false  // иначе SIGSEGV при reopen после закрытия окна
        window.center()
        window.setFrameAutosaveName("AgentBoardMain")
        window.contentView = webView
        window.backgroundColor = NSColor(red: 0.055, green: 0.09, blue: 0.075, alpha: 1)
        window.makeKeyAndOrderFront(nil)

        // пока сервер не ответил — тёмная заглушка вместо белого экрана
        webView.loadHTMLString(
            "<body style='background:#0a100d;color:#5f7a6b;font:14px ui-monospace,monospace;" +
            "display:flex;align-items:center;justify-content:center;height:96vh'>" +
            "starting the board\u{2026}</body>", baseURL: nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in self?.load() }
        timer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            self?.updateBadge()
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func load() {
        var req = URLRequest(url: URL(string: "http://localhost:8787")!)
        req.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        webView.load(req)
    }

    @objc func reloadPage() { load() }

    // сервер ещё поднимается — пробуем снова (и поднимаем его сами, если launchd не смог)
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { retry() }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { retry() }
    func retry() {
        spawnServerIfNeeded()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in self?.load() }
    }

    // launchd мог не стартовать (заблокирован в Login Items, кривой PATH…) —
    // тогда сервер запускает само приложение. Если порт занят живым сервером,
    // сюда не попадаем; дубль умрёт сам на «Address already in use».
    func spawnServerIfNeeded() {
        if let p = serverProc, p.isRunning { return }
        if Date().timeIntervalSince(lastSpawn) < 10 { return }
        let p = Process()
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "\(NSHomeDirectory())/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
            + (env["PATH"] ?? "/usr/bin:/bin")
        if let res = bundledRes {
            p.executableURL = URL(fileURLWithPath: res + "/python/bin/python3")
            p.arguments = ["-u", res + "/server/agentboard.py"]
            env["AGENTBOARD_DATA"] = NSHomeDirectory() + "/.agentboard"
            env["AGENTBOARD_TMUX"] = res + "/tmux/bin/tmux"
            env["AGENTBOARD_TMUX_SOCKET"] = "agentboard"
            // __pycache__ внутри подписанного бандла сломал бы печать подписи
            env["PYTHONDONTWRITEBYTECODE"] = "1"
        } else {
            let script = NSString(string: "~/.agentboard/agentboard.py").expandingTildeInPath
            guard FileManager.default.fileExists(atPath: script) else { return }
            p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            p.arguments = ["python3", "-u", script]
        }
        p.environment = env
        try? p.run()
        serverProc = p
        lastSpawn = Date()
    }

    // confirm()/alert() со страницы -> нативные диалоги
    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Отмена")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }
    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.runModal()
        completionHandler()
    }

    func updateBadge() {
        guard let url = URL(string: "http://localhost:8787/api/agents") else { return }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let agents = obj["agents"] as? [[String: Any]] else { return }
            let waiting = agents.filter { ($0["status"] as? String) == "waiting" }.count
            DispatchQueue.main.async {
                NSApp.dockTile.badgeLabel = waiting > 0 ? String(waiting) : nil
            }
        }.resume()
    }

    // закрыл окно — приложение живёт в доке; клик по иконке возвращает окно
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
        return true
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate

// меню, чтобы работали Cmd+Q / C / V / W
let mainMenu = NSMenu()
let appItem = NSMenuItem(); mainMenu.addItem(appItem)
let appMenu = NSMenu()
appMenu.addItem(NSMenuItem(title: "Quit Agent Board",
                           action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
appItem.submenu = appMenu
let editItem = NSMenuItem(); mainMenu.addItem(editItem)
let editMenu = NSMenu(title: "Edit")
editMenu.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
editMenu.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
editMenu.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
editMenu.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
editItem.submenu = editMenu
let viewItem = NSMenuItem(); mainMenu.addItem(viewItem)
let viewMenu = NSMenu(title: "View")
let reloadItem = NSMenuItem(title: "Reload", action: #selector(AppDelegate.reloadPage), keyEquivalent: "r")
reloadItem.target = delegate
viewMenu.addItem(reloadItem)
viewItem.submenu = viewMenu
let winItem = NSMenuItem(); mainMenu.addItem(winItem)
let winMenu = NSMenu(title: "Window")
winMenu.addItem(NSMenuItem(title: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"))
winMenu.addItem(NSMenuItem(title: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"))
winItem.submenu = winMenu
app.mainMenu = mainMenu

app.run()
