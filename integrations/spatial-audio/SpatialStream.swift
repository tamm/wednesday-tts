// SpatialStream — reads raw float32 mono PCM chunks from stdin,
// plays via AVAudioEngine with AVAudioEnvironmentNode for spatial positioning.
//
// Usage: SpatialStream <sample_rate> [device_uid] [pan]
//   pan: 0.0 = full left, 0.5 = centre, 1.0 = full right (default 0.5)
//
// Stdin protocol:
//   Audio data: raw float32 mono PCM samples
//   Pan update: 4-byte magic "PAN!" followed by 4-byte float32 (new pan 0..1)
//   EOF: stop playback
//
// The player position updates in real time — no need to restart.

import AVFoundation
import Foundation

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: SpatialStream <sample_rate> [device_uid] [pan]\n", stderr)
    exit(1)
}

let sampleRate = Double(CommandLine.arguments[1])!
let deviceUID  = CommandLine.arguments.count >= 3 ? CommandLine.arguments[2] : nil
var pan        = CommandLine.arguments.count >= 4 ? Float(CommandLine.arguments[3]) ?? 0.5 : 0.5

let engine = AVAudioEngine()
let environment = AVAudioEnvironmentNode()
let player = AVAudioPlayerNode()

engine.attach(environment)
engine.attach(player)

let monoLayout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_Mono)!
let inputFormat = AVAudioFormat(
    commonFormat: .pcmFormatFloat32,
    sampleRate: sampleRate,
    interleaved: false,
    channelLayout: monoLayout
)

let stereoLayout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_Stereo)!
let outputFormat = AVAudioFormat(
    commonFormat: .pcmFormatFloat32,
    sampleRate: 48000,
    interleaved: false,
    channelLayout: stereoLayout
)

engine.connect(player, to: environment, format: inputFormat)
engine.connect(environment, to: engine.mainMixerNode, format: outputFormat)

func updatePosition(_ p: Float) {
    let x = (p - 0.5) * 2.0
    player.position = AVAudio3DPoint(x: x, y: 0, z: -1.0)
}

environment.listenerPosition = AVAudio3DPoint(x: 0, y: 0, z: 0)
updatePosition(pan)

environment.isListenerHeadTrackingEnabled = true
environment.outputType = .headphones
player.renderingAlgorithm = .auto
player.sourceMode = .spatializeIfMono

do {
    try engine.start()
} catch {
    fputs("[SpatialStream] engine start failed: \(error)\n", stderr)
    exit(1)
}

fputs("[SpatialStream] ready rate=\(sampleRate) pan=\(pan) device=\(deviceUID ?? "default")\n", stderr)

let CHUNK_SAMPLES = 4096
let CHUNK_BYTES = CHUNK_SAMPLES * 4
let PAN_MAGIC = Data("PAN!".utf8)  // 4 bytes
let stdinHandle = FileHandle.standardInput
var totalFrames: Int64 = 0
var leftover = Data()

while true {
    let raw = stdinHandle.readData(ofLength: CHUNK_BYTES)
    if raw.isEmpty && leftover.isEmpty { break }

    var data = leftover + raw
    leftover = Data()

    // Process pan commands and audio data
    while !data.isEmpty {
        // Check for PAN! magic (8 bytes: 4 magic + 4 float)
        if data.count >= 8 && data.prefix(4) == PAN_MAGIC {
            let panBytes = data[data.startIndex+4 ..< data.startIndex+8]
            pan = panBytes.withUnsafeBytes { $0.load(as: Float.self) }
            updatePosition(pan)
            data = Data(data.dropFirst(8))
            continue
        }

        // If we have less than 4 bytes, could be a partial PAN! — save as leftover
        if data.count < 4 {
            leftover = data
            break
        }

        // Check if PAN! starts partway through — split there
        var audioEnd = data.count
        for i in 1..<data.count {
            let remaining = data.count - i
            if remaining >= 4 && data[data.startIndex+i ..< data.startIndex+i+4] == PAN_MAGIC {
                audioEnd = i
                break
            }
        }

        // Align to float32 boundary
        let alignedEnd = (audioEnd / 4) * 4
        if alignedEnd == 0 {
            leftover = data
            break
        }

        let audioData = data.prefix(alignedEnd)
        data = Data(data.dropFirst(alignedEnd))

        let sampleCount = audioData.count / 4
        let buffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: AVAudioFrameCount(sampleCount))!
        buffer.frameLength = AVAudioFrameCount(sampleCount)

        audioData.withUnsafeBytes { rawPtr in
            let src = rawPtr.bindMemory(to: Float.self)
            let dst = buffer.floatChannelData![0]
            for i in 0..<sampleCount {
                dst[i] = src[i]
            }
        }

        player.scheduleBuffer(buffer)
        if totalFrames == 0 {
            player.play()
        }
        totalFrames += Int64(sampleCount)
    }
}

// Wait for playback to drain
let silentBuf = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: 1)!
silentBuf.frameLength = 1
silentBuf.floatChannelData![0][0] = 0
let done = DispatchSemaphore(value: 0)
player.scheduleBuffer(silentBuf) {
    done.signal()
}

while done.wait(timeout: .now() + 0.1) == .timedOut {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}

engine.stop()
fputs("[SpatialStream] done\n", stderr)
