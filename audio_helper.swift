import Foundation
import AVFoundation
import CoreAudio
import AppKit

// audio_helper - native macOS companion for main.py: capture one app's audio.
//
// Modes:
//   audio_helper --list
//       JSON array of apps that are connected to Core Audio:
//       [{"bundle": "com.microsoft.teams2", "name": "Microsoft Teams", "playing": true, "pids": [..]}]
//       Helper processes (e.g. "Microsoft Teams ModuleHost", "Google Chrome Helper") are
//       grouped under the app whose bundle contains them.
//   audio_helper --capture <bundle-id>
//       Raw float32 little-endian, 16 kHz mono on stdout, paced by the wall clock: when the
//       app is silent (or not playing at all) zeros are written, so the sample count always
//       matches elapsed time. The tap follows the app: processes that start/stop playing and
//       output-device changes rebuild it. Status lines go to stderr.
//
// Uses Core Audio Process Taps (macOS 14.2+). The app keeps playing to its device as usual.
// Permission: "System Audio Recording" (TCC kTCCServiceAudioCapture). It is asked of the
// *responsible* app; when run from a terminal, the helper re-launches itself as its own
// responsible process (the terminal has no NSAudioCaptureUsageDescription, so macOS would
// silently deliver zeros). Set AUDIO_HELPER_NO_DISCLAIM=1 inside an app bundle that has
// the usage description (Voice Rec.app).

let kOutRate = 16000.0

func log(_ s: String) { FileHandle.standardError.write(("[audio_helper] " + s + "\n").data(using: .utf8)!) }

// ── Become our own responsible process (console mode) ────────────────────────

func relaunchDisclaimedIfNeeded() {
    let env = ProcessInfo.processInfo.environment
    if env["AUDIO_HELPER_DISCLAIMED"] != nil || env["AUDIO_HELPER_NO_DISCLAIM"] != nil { return }
    typealias Disclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "responsibility_spawnattrs_setdisclaim"),
          let exe = Bundle.main.executablePath else { return }
    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    _ = unsafeBitCast(sym, to: Disclaim.self)(&attr, 1)
    setenv("AUDIO_HELPER_DISCLAIMED", "1", 1)
    var pid: pid_t = 0
    let rc = posix_spawn(&pid, exe, nil, &attr, CommandLine.unsafeArgv, environ)
    posix_spawnattr_destroy(&attr)
    guard rc == 0 else { log("relaunch failed (\(rc)), continuing in-process"); unsetenv("AUDIO_HELPER_DISCLAIMED"); return }
    // Forward termination to the child, then mirror its exit status.
    for s in [SIGINT, SIGTERM, SIGHUP] {
        signal(s, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: s, queue: .main)
        src.setEventHandler { kill(pid, s) }
        src.resume()
        gSignalSources.append(src)
    }
    DispatchQueue.global().async {
        var status: Int32 = 0
        while waitpid(pid, &status, 0) == -1 && errno == EINTR {}
        exit((status & 0x7f) == 0 ? (status >> 8) & 0xff : 1)
    }
    dispatchMain()
}
var gSignalSources: [DispatchSourceSignal] = []

// ── Core Audio property helpers ──────────────────────────────────────────────

func addr(_ sel: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func getValue<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ initial: T) -> T? {
    var a = addr(sel)
    var size = UInt32(MemoryLayout<T>.size)
    var v = initial
    return withUnsafeMutablePointer(to: &v) { p in
        AudioObjectGetPropertyData(obj, &a, 0, nil, &size, p) == noErr
    } ? v : nil
}

func getString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var a = addr(sel)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var s: Unmanaged<CFString>? = nil
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &s) == noErr, let s else { return nil }
    return s.takeRetainedValue() as String
}

func getIDs(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> [AudioObjectID] {
    var a = addr(sel)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &a, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

let systemObject = AudioObjectID(kAudioObjectSystemObject)

func defaultOutputDevice() -> AudioObjectID {
    getValue(systemObject, kAudioHardwarePropertyDefaultSystemOutputDevice, AudioObjectID(0)) ?? 0
}

// ── Audio processes, grouped by app ──────────────────────────────────────────

struct AudioProc {
    let objectID: AudioObjectID
    let pid: pid_t
    let bundleID: String
    let playing: Bool
}

func audioProcesses() -> [AudioProc] {
    getIDs(systemObject, kAudioHardwarePropertyProcessObjectList).map { id in
        AudioProc(objectID: id,
                  pid: getValue(id, kAudioProcessPropertyPID, pid_t(0)) ?? 0,
                  bundleID: getString(id, kAudioProcessPropertyBundleID) ?? "",
                  playing: (getValue(id, kAudioProcessPropertyIsRunningOutput, UInt32(0)) ?? 0) != 0)
    }
}

func executablePath(_ pid: pid_t) -> String? {
    var buf = [CChar](repeating: 0, count: 4096)
    return proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 ? String(cString: buf) : nil
}

/// The outermost .app that contains the process (so helpers map to their app).
/// Falls back to the process's own bundle id (e.g. Safari's audio runs in com.apple.WebKit.GPU).
func owningApp(_ p: AudioProc) -> (bundle: String, name: String, isApp: Bool) {
    if let path = executablePath(p.pid), let r = path.range(of: ".app/") {
        let appPath = String(path[..<r.lowerBound]) + ".app"
        if let b = Bundle(path: appPath), let id = b.bundleIdentifier {
            let name = (b.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String)
                ?? (b.object(forInfoDictionaryKey: "CFBundleName") as? String)
                ?? URL(fileURLWithPath: appPath).deletingPathExtension().lastPathComponent
            return (id, name, true)
        }
    }
    let name = NSRunningApplication(processIdentifier: p.pid)?.localizedName ?? p.bundleID
    return (p.bundleID, name, false)
}

func listApps() {
    var apps: [String: [String: Any]] = [:]
    var order: [String] = []
    let me = getpid()
    for p in audioProcesses() where p.pid != me && p.pid > 0 {
        let (bundle, name, isApp) = owningApp(p)
        if bundle.isEmpty || !isApp { continue }     // daemons and system services
        if apps[bundle] == nil {
            apps[bundle] = ["bundle": bundle, "name": name, "playing": false, "pids": [Int]()]
            order.append(bundle)
        }
        apps[bundle]!["pids"] = (apps[bundle]!["pids"] as! [Int]) + [Int(p.pid)]
        if p.playing { apps[bundle]!["playing"] = true }
    }
    let list = order.map { apps[$0]! }.sorted {
        let a = ($0["playing"] as! Bool) ? 0 : 1, b = ($1["playing"] as! Bool) ? 0 : 1
        return a != b ? a < b : ($0["name"] as! String).localizedCaseInsensitiveCompare($1["name"] as! String) == .orderedAscending
    }
    let data = try! JSONSerialization.data(withJSONObject: list, options: [.prettyPrinted, .sortedKeys])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

// ── Sample FIFO (tap IO thread → paced writer) ──────────────────────────────

final class Fifo {
    private var buf: [Float] = []
    private let lock = NSLock()
    private let maxSamples = Int(kOutRate * 2)      // drop the oldest beyond 2 s (clock drift guard)

    func push(_ p: UnsafePointer<Float>, _ n: Int) {
        lock.lock(); defer { lock.unlock() }
        buf.append(contentsOf: UnsafeBufferPointer(start: p, count: n))
        if buf.count > maxSamples { buf.removeFirst(buf.count - maxSamples) }
    }

    /// Exactly n samples: what has arrived, padded with zeros.
    func pop(_ n: Int) -> [Float] {
        lock.lock(); defer { lock.unlock() }
        let k = min(n, buf.count)
        var out = Array(buf.prefix(k))
        buf.removeFirst(k)
        if k < n { out.append(contentsOf: [Float](repeating: 0, count: n - k)) }
        return out
    }

    func clear() { lock.lock(); buf.removeAll(); lock.unlock() }
}

// ── Process tap on a set of processes ────────────────────────────────────────

final class Tap {
    var tapID = AudioObjectID(kAudioObjectUnknown)
    var aggID = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?

    init(processes: [AudioObjectID], outputDevice: AudioObjectID, fifo: Fifo) throws {
        let desc = CATapDescription(stereoMixdownOfProcesses: processes)
        desc.uuid = UUID()
        desc.muteBehavior = .unmuted
        desc.isPrivate = true
        try check(AudioHardwareCreateProcessTap(desc, &tapID), "create tap")

        let outUID = getString(outputDevice, kAudioDevicePropertyDeviceUID) ?? ""
        let dict: [String: Any] = [
            kAudioAggregateDeviceNameKey: "voice-rec app audio",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
        try check(AudioHardwareCreateAggregateDevice(dict as CFDictionary, &aggID), "create aggregate device")

        guard var asbd = getValue(tapID, kAudioTapPropertyFormat, AudioStreamBasicDescription()) else { throw err("tap format") }
        // The tap may report 48 kHz while the device runs at another rate (AirPods in a call:
        // 24 kHz). The IO proc is clocked by the aggregate device, so its rate is the real one.
        if let rate = getValue(aggID, kAudioDevicePropertyNominalSampleRate, Float64(0)), rate > 0,
           rate != asbd.mSampleRate {
            log("tap reports \(Int(asbd.mSampleRate)) Hz, device runs at \(Int(rate)) Hz")
            asbd.mSampleRate = rate
        }
        guard let inFmt = AVAudioFormat(streamDescription: &asbd),
              let outFmt = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: kOutRate, channels: 1, interleaved: false),
              let conv = AVAudioConverter(from: inFmt, to: outFmt) else { throw err("tap format") }
        conv.downmix = true
        log("tap: \(Int(inFmt.sampleRate)) Hz × \(inFmt.channelCount) → 16 kHz mono")

        try check(AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, DispatchQueue(label: "audio_helper.io")) { _, input, _, _, _ in
            guard let inBuf = AVAudioPCMBuffer(pcmFormat: inFmt, bufferListNoCopy: input, deallocator: nil),
                  inBuf.frameLength > 0 else { return }
            let cap = AVAudioFrameCount(Double(inBuf.frameLength) * kOutRate / inFmt.sampleRate) + 32
            guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
            var fed = false
            var e: NSError?
            conv.convert(to: outBuf, error: &e) { _, status in
                if fed { status.pointee = .noDataNow; return nil }
                fed = true
                status.pointee = .haveData
                return inBuf
            }
            if outBuf.frameLength > 0, let ch = outBuf.floatChannelData {
                fifo.push(ch[0], Int(outBuf.frameLength))
            }
        }, "create IO proc")
        try check(AudioDeviceStart(aggID, procID), "start device")
    }

    func close() {
        if let procID { AudioDeviceStop(aggID, procID); AudioDeviceDestroyIOProcID(aggID, procID) }
        if aggID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggID) }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        procID = nil; aggID = AudioObjectID(kAudioObjectUnknown); tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

func err(_ what: String, _ code: OSStatus = -1) -> NSError {
    NSError(domain: "audio_helper", code: Int(code), userInfo: [NSLocalizedDescriptionKey: "\(what) failed (\(code))"])
}
func check(_ status: OSStatus, _ what: String) throws { if status != noErr { throw err(what, status) } }

// ── Capture loop ─────────────────────────────────────────────────────────────

final class Capture {
    let bundle: String
    let fifo = Fifo()
    var tap: Tap?
    var signature = ""          // playing process objects + output device and rate of the current tap
    var lastStatus = ""
    let out = FileHandle.standardOutput
    var written = 0
    let start = Date()

    init(bundle: String) { self.bundle = bundle }

    /// Re-scan once a second; rebuild the tap when the playing processes, the device or its rate change.
    func refresh() {
        let me = getpid()
        let procs = audioProcesses().filter { $0.pid != me && $0.playing && owningApp($0).bundle == bundle }
        let dev = defaultOutputDevice()
        let rate = getValue(dev, kAudioDevicePropertyNominalSampleRate, Float64(0)) ?? 0
        let sig = procs.map { String($0.objectID) }.sorted().joined(separator: ",") + "@\(dev)/\(Int(rate))"
        guard sig != signature else { return }
        signature = sig
        tap?.close(); tap = nil
        fifo.clear()
        if procs.isEmpty { status("waiting: \(bundle) is not playing audio"); return }
        do {
            tap = try Tap(processes: procs.map { $0.objectID }, outputDevice: dev, fifo: fifo)
            status("capturing \(bundle): pids " + procs.map { String($0.pid) }.joined(separator: ","))
        } catch {
            status("tap error: \(error.localizedDescription)")
            signature = ""      // retry on the next scan
        }
    }

    func status(_ s: String) {
        if s != lastStatus { log(s); lastStatus = s }
    }

    /// Write what the wall clock says is due (zeros where the app gave nothing).
    func pump() {
        let due = Int(Date().timeIntervalSince(start) * kOutRate) - written
        guard due > 0 else { return }
        let samples = fifo.pop(due)
        written += due
        samples.withUnsafeBufferPointer { p in
            do { try out.write(contentsOf: Data(buffer: p)) }
            catch { tap?.close(); exit(0) }       // reader went away
        }
    }

    func run() {
        signal(SIGPIPE, SIG_IGN)
        for s in [SIGINT, SIGTERM, SIGHUP] {
            signal(s, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: s, queue: .main)
            src.setEventHandler { [self] in tap?.close(); exit(0) }
            src.resume()
            gSignalSources.append(src)
        }
        refresh()
        let scan = DispatchSource.makeTimerSource(queue: .main)
        scan.schedule(deadline: .now() + 1, repeating: 1)
        scan.setEventHandler { [self] in refresh() }
        scan.resume()
        let pacer = DispatchSource.makeTimerSource(queue: .main)
        pacer.schedule(deadline: .now() + 0.1, repeating: 0.1)
        pacer.setEventHandler { [self] in pump() }
        pacer.resume()
        gTimers = [scan, pacer]
        dispatchMain()
    }
}
var gTimers: [DispatchSourceTimer] = []

// ── main ─────────────────────────────────────────────────────────────────────

let args = CommandLine.arguments
if args.count >= 2 && args[1] == "--list" {
    listApps()
    exit(0)
}
guard args.count >= 3 && args[1] == "--capture" else {
    log("usage: audio_helper --list | --capture <bundle-id>")
    exit(2)
}
relaunchDisclaimedIfNeeded()
Capture(bundle: args[2]).run()
