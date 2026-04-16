// tts-hook — Claude Code Stop / PreToolUse hook, native Swift port.
//
// Replaces the Python hooks at speak-response.py and pre-tool-speak.py
// to cut ~150–200 ms of Python cold-start per turn.
//
// Usage:
//   tts-hook --mode stop       (Stop hook: speak last assistant message)
//   tts-hook --mode pretool    (PreToolUse hook: speak unsent mid-turn text)
//
// Behaviour is byte-for-byte identical to the Python version — same wire
// JSON, same voice_hash, same pan, same filter rules. See
// docs/voice-pipeline-spec.md and integrations/claude-code/hook_common.py
// for the contract.
//
// Build: swiftc -O tts-hook.swift -o tts-hook
//
// Critical invariant: on ANY failure we exit 0. A crashing hook must
// never propagate an error code up to Claude Code.

import AppKit
import CryptoKit
import Darwin
import Foundation

// MARK: - Constants

let UNIX_SOCKET_PATH = ProcessInfo.processInfo.environment["TTS_SOCKET_PATH"] ?? "/tmp/tts-daemon.sock"
let MUTE_PATH = ProcessInfo.processInfo.environment["TTS_MUTE_PATH"] ?? "/tmp/tts-mute"
let DEBUG_LOG_PATH = ProcessInfo.processInfo.environment["TTS_HOOK_DEBUG_LOG"] ?? "/tmp/wednesday-tts-hook-debug.log"
let PRE_TOOL_MAX_CHARS = 2400
let PRE_TOOL_MIN_SENTENCE_CUT = 1200
let CONNECT_TIMEOUT_SEC_DEFAULT: Double = 1.0
let CONNECT_TIMEOUT_SEC_KICK: Double = 10.0
let RECV_ACK_TIMEOUT_SEC: Double = 0.5

// MARK: - Entry point

enum Mode { case stop, pretool }

func main() {
    let args = CommandLine.arguments
    var mode: Mode = .stop
    if let idx = args.firstIndex(of: "--mode"), idx + 1 < args.count {
        switch args[idx + 1] {
        case "stop": mode = .stop
        case "pretool": mode = .pretool
        default:
            fputs("tts-hook: unknown mode '\(args[idx + 1])'\n", stderr)
            exit(0)
        }
    }

    let wallTime = Date().timeIntervalSince1970

    if isMuted() {
        exit(0)
    }

    guard let payload = readJSONFromStdin() else {
        exit(0)
    }

    logPayloadDebug(payload: payload, hookName: mode == .stop ? "speak-response" : "pre-tool-speak")

    if isSubagent(payload) {
        exit(0)
    }

    let sessionId = (payload["session_id"] as? String) ?? ""
    let cwd = (payload["cwd"] as? String) ?? (FileManager.default.currentDirectoryPath)

    switch mode {
    case .stop:
        handleStop(payload: payload, sessionId: sessionId, cwd: cwd, wallTime: wallTime)
    case .pretool:
        handlePreTool(payload: payload, sessionId: sessionId, cwd: cwd, wallTime: wallTime)
    }

    exit(0)
}

// MARK: - Stop hook

func handleStop(payload: [String: Any], sessionId: String, cwd: String, wallTime: Double) {
    var text = (payload["last_assistant_message"] as? String) ?? ""
    if text.isEmpty {
        text = lastAssistantFromTranscript(payload["transcript_path"] as? String)
    }
    if text.trimmingCharacters(in: .whitespacesAndNewlines).count < 5 {
        return
    }

    var msg: [String: Any] = [
        "command": "speak",
        "text": text,
        "normalization": "markdown",
        "voice_hash": computeVoiceHash(cwd: cwd),
        "timestamp": wallTime,
    ]
    if !sessionId.isEmpty {
        msg["session_id"] = sessionId
    }
    let pan = computePan()
    if pan != 0.5 {
        msg["pan"] = pan
    }

    sendSpeak(msg, kickOnTimeout: true)
}

// MARK: - PreToolUse hook

func handlePreTool(payload: [String: Any], sessionId: String, cwd: String, wallTime: Double) {
    let transcript = payload["transcript_path"] as? String
    let texts = unsentAssistantTexts(transcript)
    if texts.isEmpty { return }

    var combined = texts.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
    if combined.count < 5 { return }
    combined = truncateAtSentence(combined)

    var msg: [String: Any] = [
        "command": "speak",
        "text": combined,
        "normalization": "markdown",
        "session_id": sessionId.isEmpty ? "unknown" : sessionId,
        "timestamp": wallTime,
    ]
    if !cwd.isEmpty {
        msg["voice_hash"] = computeVoiceHash(cwd: cwd)
    }
    let pan = computePan()
    if pan != 0.5 {
        msg["pan"] = pan
    }

    sendSpeak(msg, kickOnTimeout: false)
}

// MARK: - JSON stdin / debug log

func readJSONFromStdin() -> [String: Any]? {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    if data.isEmpty { return nil }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
}

func logPayloadDebug(payload: [String: Any], hookName: String) {
    var safe: [String: Any] = [:]
    for (k, v) in payload {
        // Serialise each value to JSON, truncate >2000 chars, decode back.
        if let bytes = try? JSONSerialization.data(withJSONObject: v, options: [.fragmentsAllowed]) {
            if let str = String(data: bytes, encoding: .utf8) {
                if str.count > 2000 {
                    safe[k] = String(str.prefix(2000)) + "...[truncated]"
                } else {
                    safe[k] = (try? JSONSerialization.jsonObject(with: bytes, options: [.fragmentsAllowed])) ?? str
                }
            } else {
                safe[k] = "<unserialisable>"
            }
        } else {
            safe[k] = "<unserialisable>"
        }
    }
    let entry: [String: Any] = [
        "t": Date().timeIntervalSince1970,
        "hook": hookName,
        "payload": safe,
    ]
    guard let line = try? JSONSerialization.data(withJSONObject: entry) else { return }
    guard let f = FileHandle(forWritingAtPath: DEBUG_LOG_PATH) ?? createFile(DEBUG_LOG_PATH) else { return }
    f.seekToEndOfFile()
    f.write(line)
    f.write(Data([0x0A]))
    try? f.close()
}

func createFile(_ path: String) -> FileHandle? {
    FileManager.default.createFile(atPath: path, contents: nil)
    return FileHandle(forWritingAtPath: path)
}

// MARK: - Mute / sub-agent filter

func isMuted() -> Bool {
    if FileManager.default.fileExists(atPath: MUTE_PATH) { return true }
    if let v = ProcessInfo.processInfo.environment["TTS_MUTE"], !v.isEmpty { return true }
    return false
}

func isSubagent(_ payload: [String: Any]) -> Bool {
    for key in ["agent_id", "agent_type", "team_name", "teammate_name"] {
        if let v = payload[key], !isEmptyValue(v) { return true }
    }
    if transcriptIsTeammate(payload["transcript_path"] as? String) { return true }
    if let sid = payload["session_id"] as? String, !sid.isEmpty,
       sessionIsNonLeadTeammate(sessionId: sid) {
        return true
    }
    return false
}

func isEmptyValue(_ v: Any) -> Bool {
    if let s = v as? String { return s.isEmpty }
    if v is NSNull { return true }
    return false
}

func transcriptIsTeammate(_ path: String?) -> Bool {
    guard let path = path, !path.isEmpty,
          FileManager.default.fileExists(atPath: path) else { return false }
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)) else { return false }
    guard let text = String(data: data, encoding: .utf8) else { return false }
    var i = 0
    for rawLine in text.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\n" }) {
        if i >= 20 { break }
        let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty { continue }
        i += 1
        guard let ld = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: ld) as? [String: Any] else { continue }
        if let tn = obj["teamName"] as? String, !tn.isEmpty { return true }
        if let an = obj["agentName"] as? String, !an.isEmpty { return true }
        if obj["teamName"] != nil || obj["agentName"] != nil { return true }
    }
    return false
}

func sessionIsNonLeadTeammate(sessionId: String) -> Bool {
    let home = NSHomeDirectory()
    let teamsDir = "\(home)/.claude/teams"
    var isDir: ObjCBool = false
    guard FileManager.default.fileExists(atPath: teamsDir, isDirectory: &isDir), isDir.boolValue else {
        return false
    }
    guard let entries = try? FileManager.default.contentsOfDirectory(atPath: teamsDir) else {
        return false
    }
    for entry in entries {
        let cfgPath = "\(teamsDir)/\(entry)/config.json"
        guard FileManager.default.fileExists(atPath: cfgPath),
              let data = try? Data(contentsOf: URL(fileURLWithPath: cfgPath)),
              let cfg = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            continue
        }
        if let lead = cfg["leadSessionId"] as? String, lead == sessionId {
            return false
        }
        // Raw substring search across the full JSON — matches Python behaviour.
        if let raw = try? JSONSerialization.data(withJSONObject: cfg),
           let rawStr = String(data: raw, encoding: .utf8),
           rawStr.contains(sessionId) {
            return true
        }
    }
    return false
}

// MARK: - Transcript text extraction

func lastAssistantFromTranscript(_ path: String?) -> String {
    guard let path = path, !path.isEmpty,
          FileManager.default.fileExists(atPath: path),
          let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
          let text = String(data: data, encoding: .utf8) else { return "" }

    var messages: [[String: Any]] = []
    for raw in text.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\n" }) {
        let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty { continue }
        if let ld = line.data(using: .utf8),
           let obj = try? JSONSerialization.jsonObject(with: ld) as? [String: Any] {
            messages.append(obj)
        }
    }

    for msg in messages.reversed() {
        guard (msg["type"] as? String) == "assistant" else { continue }
        let envelope = (msg["message"] as? [String: Any]) ?? [:]
        let content = envelope["content"]
        if let list = content as? [Any] {
            var parts: [String] = []
            for block in list {
                guard let dict = block as? [String: Any] else { continue }
                guard (dict["type"] as? String) == "text" else { continue }
                if let t = dict["text"] as? String, !t.isEmpty { parts.append(t) }
            }
            let joined = parts.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
            if !joined.isEmpty { return joined }
        } else if let s = content as? String {
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { return t }
        }
    }
    return ""
}

func unsentAssistantTexts(_ path: String?) -> [String] {
    guard let path = path, !path.isEmpty,
          FileManager.default.fileExists(atPath: path),
          let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
          let text = String(data: data, encoding: .utf8) else { return [] }

    var messages: [[String: Any]] = []
    for raw in text.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "\n" }) {
        let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if line.isEmpty { continue }
        if let ld = line.data(using: .utf8),
           let obj = try? JSONSerialization.jsonObject(with: ld) as? [String: Any] {
            let type = obj["type"] as? String
            if type == "assistant" || type == "user" {
                messages.append(obj)
            }
        }
    }

    var lastUserIdx = -1
    for (i, msg) in messages.enumerated() {
        if (msg["type"] as? String) == "user" { lastUserIdx = i }
    }
    if lastUserIdx < 0 { return [] }

    var texts: [String] = []
    for msg in messages[(lastUserIdx + 1)...] {
        guard (msg["type"] as? String) == "assistant" else { continue }
        let envelope = (msg["message"] as? [String: Any]) ?? [:]
        guard let list = envelope["content"] as? [Any] else { continue }
        for block in list {
            guard let dict = block as? [String: Any] else { continue }
            guard (dict["type"] as? String) == "text" else { continue }
            if let t = (dict["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
               !t.isEmpty {
                texts.append(t)
            }
        }
    }
    return texts
}

func truncateAtSentence(_ text: String) -> String {
    if text.count <= PRE_TOOL_MAX_CHARS { return text }
    let prefix = String(text.prefix(PRE_TOOL_MAX_CHARS))
    let candidates = [". ", "! ", "? "]
    var lastSentence = -1
    for delim in candidates {
        if let r = prefix.range(of: delim, options: .backwards) {
            let idx = prefix.distance(from: prefix.startIndex, to: r.lowerBound)
            if idx > lastSentence { lastSentence = idx }
        }
    }
    if lastSentence > PRE_TOOL_MIN_SENTENCE_CUT {
        // Python: text[:last_sentence + 1] — keeps the final punctuation.
        let end = text.index(text.startIndex, offsetBy: lastSentence + 1)
        return String(text[..<end])
    }
    if let spaceRange = prefix.range(of: " ", options: .backwards) {
        let idx = prefix.distance(from: prefix.startIndex, to: spaceRange.lowerBound)
        if idx > 0 {
            let end = text.index(text.startIndex, offsetBy: idx)
            return String(text[..<end])
        }
    }
    return prefix
}

// MARK: - voice_hash

func computeVoiceHash(cwd: String) -> String {
    let key = findGitRoot(from: cwd) ?? cwd
    let digest = SHA256.hash(data: Data(key.utf8))
    let hex = digest.map { String(format: "%02x", $0) }.joined()
    return String(hex.prefix(8))
}

func findGitRoot(from path: String) -> String? {
    let fm = FileManager.default
    var dir: String
    if let resolved = try? fm.destinationOfSymbolicLink(atPath: path) {
        dir = (resolved as NSString).standardizingPath
    } else {
        dir = (path as NSString).standardizingPath
    }
    // Resolve the full path to match git rev-parse --show-toplevel behaviour.
    dir = (dir as NSString).resolvingSymlinksInPath
    while dir != "/" && !dir.isEmpty {
        if fm.fileExists(atPath: dir + "/.git") {
            return dir
        }
        dir = (dir as NSString).deletingLastPathComponent
    }
    return nil
}

// MARK: - Pan (cached bounds, async osascript refresh)

let BOUNDS_CACHE_PATH = "/tmp/tts-iterm-bounds.json"
let BOUNDS_CACHE_MAX_AGE: Double = 600 // 10 min staleness is fine

func computePan() -> Double {
    guard let iterm = ProcessInfo.processInfo.environment["ITERM_SESSION_ID"],
          let colonIdx = iterm.firstIndex(of: ":") else {
        return 0.5
    }
    let sessionUUID = String(iterm[iterm.index(after: colonIdx)...])

    // Try cached bounds first — microsecond read vs ~400ms osascript.
    let cached = readBoundsCache(sessionUUID: sessionUUID)
    let windowCentreX: Double

    if let c = cached {
        windowCentreX = c
        refreshBoundsAsync(sessionUUID: sessionUUID)
    } else {
        // No cache — query synchronously this one time, cache the result.
        if let fresh = queryItermBounds(sessionUUID: sessionUUID) {
            windowCentreX = fresh
        } else {
            refreshBoundsAsync(sessionUUID: sessionUUID)
            return 0.5
        }
    }

    return panFromWindowCentre(windowCentreX)
}

func panFromWindowCentre(_ windowCentreX: Double) -> Double {
    var screens: [(x: Double, y: Double, w: Double, h: Double)] = []
    for s in NSScreen.screens {
        let f = s.frame
        screens.append((x: f.origin.x, y: f.origin.y, w: f.size.width, h: f.size.height))
    }
    if screens.isEmpty { return 0.5 }

    let spatial = loadSpatialConfig()
    let viewingDist = spatial["viewing_distance_mm"] as? Double ?? 1000.0
    let maxAngle = spatial["max_angle"] as? Double ?? 90.0
    let mmPerPt = spatial["mm_per_point"] as? Double ?? 0.22
    let gapMm = spatial["gap_mm"] as? Double ?? 70.0

    let centreLogicalX: Double
    if let cx = spatial["centre_x"] as? Double {
        centreLogicalX = cx
    } else if let main = screens.first(where: { $0.x == 0 && $0.y == 0 }) {
        centreLogicalX = main.w / 2.0
    } else {
        let left = screens.map { $0.x }.min() ?? 0
        let right = screens.map { $0.x + $0.w }.max() ?? 0
        centreLogicalX = (left + right) / 2.0
    }

    let windowMm = logicalXToMm(windowCentreX, screens: screens, mmPerPt: mmPerPt, gapMm: gapMm)
    let centreMm = logicalXToMm(centreLogicalX, screens: screens, mmPerPt: mmPerPt, gapMm: gapMm)
    let dxMm = windowMm - centreMm
    let angleDeg = atan2(dxMm, viewingDist) * 180.0 / .pi
    let pan = 0.5 + (angleDeg / maxAngle) * 0.5
    return max(0.0, min(1.0, pan))
}

func readBoundsCache(sessionUUID: String) -> Double? {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: BOUNDS_CACHE_PATH)),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let ts = obj["t"] as? Double,
          Date().timeIntervalSince1970 - ts < BOUNDS_CACHE_MAX_AGE,
          let entries = obj["sessions"] as? [String: Any],
          let bounds = entries[sessionUUID] as? [String: Any],
          let x1 = bounds["x1"] as? Double,
          let x2 = bounds["x2"] as? Double else { return nil }
    return (x1 + x2) / 2.0
}

func writeBoundsCache(sessionUUID: String, x1: Double, y1: Double, x2: Double, y2: Double) {
    // Merge with existing cache to preserve other sessions.
    var obj: [String: Any] = [:]
    var sessions: [String: Any] = [:]
    if let data = try? Data(contentsOf: URL(fileURLWithPath: BOUNDS_CACHE_PATH)),
       let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        obj = existing
        sessions = existing["sessions"] as? [String: Any] ?? [:]
    }
    sessions[sessionUUID] = ["x1": x1, "y1": y1, "x2": x2, "y2": y2]
    obj["t"] = Date().timeIntervalSince1970
    obj["sessions"] = sessions
    if let json = try? JSONSerialization.data(withJSONObject: obj) {
        try? json.write(to: URL(fileURLWithPath: BOUNDS_CACHE_PATH))
    }
}

func queryItermBounds(sessionUUID: String) -> Double? {
    let boundsScript = """
tell application "iTerm2"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if unique ID of s contains "\(sessionUUID)" then
                    set b to bounds of w
                    return "" & item 1 of b & "," & item 2 of b & "," & item 3 of b & "," & item 4 of b
                end if
            end repeat
        end repeat
    end repeat
    return "not found"
end tell
"""
    let boundsOut = runCommand(path: "/usr/bin/osascript", args: ["-e", boundsScript], timeout: 3.0)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    if boundsOut == "not found" || boundsOut.isEmpty { return nil }
    let parts = boundsOut.split(separator: ",").map { Double($0.trimmingCharacters(in: .whitespaces)) ?? .nan }
    guard parts.count == 4, !parts.contains(where: { $0.isNaN }) else { return nil }
    writeBoundsCache(sessionUUID: sessionUUID, x1: parts[0], y1: parts[1], x2: parts[2], y2: parts[3])
    return (parts[0] + parts[2]) / 2.0
}

func refreshBoundsAsync(sessionUUID: String) {
    // Fork a detached shell that runs osascript and writes the cache.
    // The child survives our exit — no readability handler needed.
    let shellCmd = """
    b=$(/usr/bin/osascript -e '
    tell application "iTerm2"
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    if unique ID of s contains "\(sessionUUID)" then
                        set b to bounds of w
                        return "" & item 1 of b & "," & item 2 of b & "," & item 3 of b & "," & item 4 of b
                    end if
                end repeat
            end repeat
        end repeat
        return "not found"
    end tell
    ' 2>/dev/null)
    [ "$b" = "not found" ] && exit 0
    [ -z "$b" ] && exit 0
    IFS=, read x1 y1 x2 y2 <<< "$b"
    t=$(python3 -c "import time; print(time.time())")
    printf '{"t":%s,"sessions":{"\(sessionUUID)":{"x1":%s,"y1":%s,"x2":%s,"y2":%s}}}' "$t" "$x1" "$y1" "$x2" "$y2" > "\(BOUNDS_CACHE_PATH)"
    """
    var pid: pid_t = 0
    let argv: [UnsafeMutablePointer<CChar>?] = [
        strdup("/bin/bash"), strdup("-c"), strdup(shellCmd), nil
    ]
    posix_spawn(&pid, "/bin/bash", nil, nil, argv, environ)
    // Don't waitpid — child runs detached, writes cache, exits on its own.
}

func loadSpatialConfig() -> [String: Any] {
    let path = NSHomeDirectory() + "/.claude/tts-config.json"
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let s = obj["spatial"] as? [String: Any] else { return [:] }
    return s
}

func logicalXToMm(_ x: Double, screens: [(x: Double, y: Double, w: Double, h: Double)],
                  mmPerPt: Double, gapMm: Double) -> Double {
    let sorted = screens.sorted { $0.x < $1.x }
    var total = 0.0
    for (i, scr) in sorted.enumerated() {
        if i > 0 { total += gapMm }
        let scrLeft = scr.x
        let scrRight = scr.x + scr.w
        let scrWidthMm = scr.w * mmPerPt
        if x <= scrLeft {
            break
        } else if x >= scrRight {
            total += scrWidthMm
        } else {
            total += (x - scrLeft) * mmPerPt
            return total
        }
    }
    return total
}

// MARK: - Socket send

func sendSpeak(_ msg: [String: Any], kickOnTimeout: Bool) {
    guard let body = try? JSONSerialization.data(withJSONObject: msg) else { return }
    var payload = body
    payload.append(0x0A)

    let connectTimeout = kickOnTimeout ? CONNECT_TIMEOUT_SEC_KICK : CONNECT_TIMEOUT_SEC_DEFAULT
    let result = socketSend(path: UNIX_SOCKET_PATH,
                            data: payload,
                            connectTimeout: connectTimeout,
                            recvTimeout: RECV_ACK_TIMEOUT_SEC)
    switch result {
    case .ok, .connectFailed:
        // connectFailed == daemon not listening; Python silently drops.
        return
    case .writeOrReadTimeout:
        if kickOnTimeout { kickDaemonIfDying() }
    case .writeOrReadError:
        return
    }
}

enum SocketResult {
    case ok
    case connectFailed
    case writeOrReadTimeout
    case writeOrReadError
}

func socketSend(path: String, data: Data, connectTimeout: Double, recvTimeout: Double) -> SocketResult {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    if fd < 0 { return .connectFailed }
    defer { close(fd) }

    // Connect with timeout via non-blocking connect + select.
    var flags = fcntl(fd, F_GETFL, 0)
    _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)

    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let pathBytes = Array(path.utf8)
    if pathBytes.count >= MemoryLayout.size(ofValue: addr.sun_path) {
        return .connectFailed
    }
    withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
        ptr.withMemoryRebound(to: Int8.self, capacity: pathBytes.count + 1) { cPtr in
            for (i, b) in pathBytes.enumerated() { cPtr[i] = Int8(bitPattern: b) }
            cPtr[pathBytes.count] = 0
        }
    }

    let addrSize = socklen_t(MemoryLayout<sockaddr_un>.size)
    let rc = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.connect(fd, $0, addrSize)
        }
    }
    if rc < 0 {
        if errno == EINPROGRESS {
            // Wait up to connectTimeout for writable.
            var wfds = fd_set()
            fdZero(&wfds)
            fdSet(fd, &wfds)
            var tv = timeval(tv_sec: Int(connectTimeout),
                             tv_usec: Int32((connectTimeout - Double(Int(connectTimeout))) * 1_000_000))
            let sr = select(fd + 1, nil, &wfds, nil, &tv)
            if sr <= 0 { return .connectFailed }
            var err: Int32 = 0
            var len = socklen_t(MemoryLayout<Int32>.size)
            if getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len) < 0 || err != 0 {
                return .connectFailed
            }
        } else {
            return .connectFailed
        }
    }

    // Switch back to blocking with SO_SNDTIMEO/SO_RCVTIMEO for write/read.
    flags = fcntl(fd, F_GETFL, 0)
    _ = fcntl(fd, F_SETFL, flags & ~O_NONBLOCK)

    var sndTv = timeval(tv_sec: Int(connectTimeout),
                        tv_usec: Int32((connectTimeout - Double(Int(connectTimeout))) * 1_000_000))
    _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &sndTv, socklen_t(MemoryLayout<timeval>.size))

    // Send all bytes.
    let writeResult: Int = data.withUnsafeBytes { rawBuf -> Int in
        guard let base = rawBuf.baseAddress else { return -1 }
        var remaining = data.count
        var ptr = base
        while remaining > 0 {
            let n = Darwin.send(fd, ptr, remaining, 0)
            if n <= 0 {
                if errno == EAGAIN || errno == EWOULDBLOCK { return -2 }
                return -1
            }
            remaining -= n
            ptr = ptr.advanced(by: n)
        }
        return 0
    }
    if writeResult == -2 { return .writeOrReadTimeout }
    if writeResult < 0 { return .writeOrReadError }

    // Read ack with recvTimeout. Matches Python: settimeout(0.5) then try recv,
    // swallow TimeoutError. We don't care about the content — just drain briefly.
    var rcvTv = timeval(tv_sec: Int(recvTimeout),
                        tv_usec: Int32((recvTimeout - Double(Int(recvTimeout))) * 1_000_000))
    _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &rcvTv, socklen_t(MemoryLayout<timeval>.size))
    var buf = [UInt8](repeating: 0, count: 64)
    _ = Darwin.recv(fd, &buf, buf.count, 0)
    return .ok
}

// fd_set helpers — Swift doesn't expose FD_ZERO / FD_SET inline macros
func fdZero(_ set: inout fd_set) {
    set = fd_set()
}
func fdSet(_ fd: Int32, _ set: inout fd_set) {
    let intOffset = Int(fd / 32)
    let bitOffset = fd % 32
    let mask: Int32 = 1 << bitOffset
    withUnsafeMutablePointer(to: &set.fds_bits) { ptr in
        ptr.withMemoryRebound(to: Int32.self, capacity: 32) { bitsPtr in
            bitsPtr[intOffset] |= mask
        }
    }
}

// MARK: - Daemon kick

func kickDaemonIfDying() {
    // Ping the daemon.
    var resp: Data = Data()
    let pingMsg = "{\"command\":\"ping\"}\n".data(using: .utf8)!
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    if fd >= 0 {
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(UNIX_SOCKET_PATH.utf8)
        withUnsafeMutablePointer(to: &addr.sun_path) { ptr in
            ptr.withMemoryRebound(to: Int8.self, capacity: pathBytes.count + 1) { cPtr in
                for (i, b) in pathBytes.enumerated() { cPtr[i] = Int8(bitPattern: b) }
                cPtr[pathBytes.count] = 0
            }
        }
        var tv = timeval(tv_sec: 2, tv_usec: 0)
        _ = setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if rc == 0 {
            _ = pingMsg.withUnsafeBytes { rb -> Int in
                Darwin.send(fd, rb.baseAddress, pingMsg.count, 0)
            }
            var one = timeval(tv_sec: 1, tv_usec: 0)
            _ = setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &one, socklen_t(MemoryLayout<timeval>.size))
            var buf = [UInt8](repeating: 0, count: 64)
            let n = Darwin.recv(fd, &buf, buf.count, 0)
            if n > 0 {
                resp = Data(buf.prefix(n))
            }
        }
    }

    let trimmed = String(data: resp, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if trimmed == "dying" || trimmed.isEmpty {
        let uid = getuid()
        _ = runCommand(path: "/bin/launchctl",
                       args: ["kickstart", "-k", "gui/\(uid)/com.tamm.wednesday-tts"],
                       timeout: 5.0)
    }
}

// MARK: - Process helper

func runCommand(path: String, args: [String], timeout: Double) -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let out = Pipe()
    p.standardOutput = out
    p.standardError = Pipe()
    do { try p.run() } catch { return "" }

    // Timeout via a background waiter — kill if still running.
    let deadline = DispatchTime.now() + timeout
    let group = DispatchGroup()
    group.enter()
    DispatchQueue.global().async {
        p.waitUntilExit()
        group.leave()
    }
    if group.wait(timeout: deadline) == .timedOut {
        p.terminate()
        _ = group.wait(timeout: .now() + 0.5)
        return ""
    }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    return String(data: data, encoding: .utf8) ?? ""
}

main()
