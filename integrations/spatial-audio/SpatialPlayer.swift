// SpatialPlayer — plays a WAV file via AVPlayer pinned to a specific device.
// AVPlayer goes through the system spatial mixer, enabling head tracking.
//
// Usage: SpatialPlayer <wav_path> [device_uid]

import AVFoundation
import Foundation

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: SpatialPlayer <wav_path> [device_uid]\n", stderr)
    exit(1)
}

let wavPath   = CommandLine.arguments[1]
let deviceUID = CommandLine.arguments.count >= 3 ? CommandLine.arguments[2] : nil

let url = URL(fileURLWithPath: wavPath)
let asset = AVURLAsset(url: url)
let item = AVPlayerItem(asset: asset)
let player = AVPlayer(playerItem: item)

if let uid = deviceUID {
    player.audioOutputDeviceUniqueID = uid
    fputs("[SpatialPlayer] pinned to device: \(uid)\n", stderr)
}

// Allow spatial audio for mono and stereo content
item.allowedAudioSpatializationFormats = .monoAndStereo

// Wait for ready
var ready = false
let semaphore = DispatchSemaphore(value: 0)

let observer = item.observe(\.status) { item, _ in
    if item.status == .readyToPlay || item.status == .failed {
        if !ready {
            ready = true
            semaphore.signal()
        }
    }
}

// Poll status with run loop so KVO fires
let loadDeadline = Date().addingTimeInterval(5.0)
while item.status == .unknown && Date() < loadDeadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
}
observer.invalidate()

guard item.status == .readyToPlay else {
    fputs("[SpatialPlayer] failed to load: \(item.error?.localizedDescription ?? "status=\(item.status.rawValue)")\n", stderr)
    exit(1)
}

let duration = CMTimeGetSeconds(asset.duration)
fputs("[SpatialPlayer] playing \(String(format: "%.1f", duration))s\n", stderr)

player.play()

// Run loop so AVPlayer actually delivers audio and notifications fire
let deadline = Date().addingTimeInterval(duration + 1.0)
while Date() < deadline && player.rate > 0 {
    RunLoop.current.run(until: Date().addingTimeInterval(0.1))
}

// Small grace period
RunLoop.current.run(until: Date().addingTimeInterval(0.5))
fputs("[SpatialPlayer] done\n", stderr)
