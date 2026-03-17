// SpatialStream — reads raw float32 mono PCM chunks from stdin,
// plays via AVAudioEngine with AVAudioEnvironmentNode for spatial positioning.
//
// Usage: SpatialStream <sample_rate> [device_uid] [pan]
//   pan: 0.0 = full left, 0.5 = centre, 1.0 = full right (default 0.5)
//
// Feed raw float32 mono PCM on stdin. Send EOF when done.
// Designed to be a long-running subprocess that the daemon pipes audio into.

import AVFoundation
import Foundation

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: SpatialStream <sample_rate> [device_uid] [pan]\n", stderr)
    exit(1)
}

let sampleRate = Double(CommandLine.arguments[1])!
let deviceUID  = CommandLine.arguments.count >= 3 ? CommandLine.arguments[2] : nil
let pan        = CommandLine.arguments.count >= 4 ? Float(CommandLine.arguments[3]) ?? 0.5 : 0.5

// Map pan (0..1) to x position (-1..1) for 3D space
// 0.0 = full left (-1), 0.5 = centre (0), 1.0 = full right (1)
let xPos = (pan - 0.5) * 2.0

let engine = AVAudioEngine()
let environment = AVAudioEnvironmentNode()
let player = AVAudioPlayerNode()

engine.attach(environment)
engine.attach(player)

// Mono input format with proper channel layout
let monoLayout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_Mono)!
let inputFormat = AVAudioFormat(
    commonFormat: .pcmFormatFloat32,
    sampleRate: sampleRate,
    interleaved: false,
    channelLayout: monoLayout
)

// Stereo output format
let stereoLayout = AVAudioChannelLayout(layoutTag: kAudioChannelLayoutTag_Stereo)!
let outputFormat = AVAudioFormat(
    commonFormat: .pcmFormatFloat32,
    sampleRate: 48000,
    interleaved: false,
    channelLayout: stereoLayout
)

// Connect: player -> environment -> mainMixer -> output
engine.connect(player, to: environment, format: inputFormat)
engine.connect(environment, to: engine.mainMixerNode, format: outputFormat)

// Spatial positioning
environment.listenerPosition = AVAudio3DPoint(x: 0, y: 0, z: 0)
player.position = AVAudio3DPoint(x: xPos, y: 0, z: -1.0)  // negative z = in front

// Enable head tracking
environment.isListenerHeadTrackingEnabled = true
environment.outputType = .headphones

// Set rendering algorithm
player.renderingAlgorithm = .auto
player.sourceMode = .spatializeIfMono

do {
    try engine.start()
} catch {
    fputs("[SpatialStream] engine start failed: \(error)\n", stderr)
    exit(1)
}

fputs("[SpatialStream] ready rate=\(sampleRate) pan=\(pan) x=\(xPos) device=\(deviceUID ?? "default")\n", stderr)

// Read stdin in chunks and schedule buffers
let CHUNK_SAMPLES = 4096
let CHUNK_BYTES = CHUNK_SAMPLES * 4  // float32
let stdin = FileHandle.standardInput
var totalFrames: Int64 = 0

while true {
    let data = stdin.readData(ofLength: CHUNK_BYTES)
    if data.isEmpty { break }

    let sampleCount = data.count / 4
    let buffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: AVAudioFrameCount(sampleCount))!
    buffer.frameLength = AVAudioFrameCount(sampleCount)

    data.withUnsafeBytes { rawPtr in
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

// Wait for playback to drain
let remainingDuration = Double(totalFrames) / sampleRate
fputs("[SpatialStream] EOF, waiting for \(String(format: "%.1f", remainingDuration))s of audio to finish\n", stderr)

// Schedule an empty buffer with a completion handler
let silentBuf = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: 1)!
silentBuf.frameLength = 1
silentBuf.floatChannelData![0][0] = 0
let done = DispatchSemaphore(value: 0)
player.scheduleBuffer(silentBuf) {
    done.signal()
}

// Run loop while waiting so audio continues playing
while done.wait(timeout: .now() + 0.1) == .timedOut {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}

engine.stop()
fputs("[SpatialStream] done\n", stderr)
