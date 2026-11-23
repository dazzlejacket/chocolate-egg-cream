// SystemAudioCapture -- macOS system audio via ScreenCaptureKit.
//
// Captures the mixed system audio (not the microphone) and writes it to
// stdout as mono signed 16-bit little-endian PCM at the requested rate.
// Chocolate Egg Cream reads that stream and treats it exactly like the
// WASAPI / PulseAudio capture on the other platforms.
//
// Build:
//   swiftc -O -o SystemAudioCapture SystemAudioCapture.swift
//
// Requires macOS 13+ and Screen Recording permission for the terminal or
// app that launches this helper.

import AudioToolbox
import CoreMedia
import Foundation
import ScreenCaptureKit

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("error: \(message)\n".utf8))
    exit(1)
}

// ---------------------------------------------------------------- arguments

var sampleRate = 48000.0
var channelCount: UInt32 = 2

let arguments = CommandLine.arguments
var index = 1
while index < arguments.count {
    switch arguments[index] {
    case "--rate":
        index += 1
        guard index < arguments.count, let value = Double(arguments[index]) else {
            fail("--rate needs a number")
        }
        sampleRate = value
    case "--channels":
        index += 1
        guard index < arguments.count, let value = UInt32(arguments[index]) else {
            fail("--channels needs a number")
        }
        channelCount = value
    default:
        break
    }
    index += 1
}

// ------------------------------------------------------------- PCM handling

/// Copies the raw PCM out of a CMSampleBuffer and mixes it down to mono
/// int16. ScreenCaptureKit usually delivers non-interleaved float32, but
/// int16 shows up too, so both are handled.
func monoPcm(from sampleBuffer: CMSampleBuffer) -> Data? {
    guard let formatDescription = CMSampleBufferGetFormatDescriptions(sampleBuffer).first
    else { return nil }
    let description = formatDescription as! CMAudioFormatDescription
    guard let basic = CMAudioFormatDescriptionGetStreamBasicDescription(description) else {
        return nil
    }
    let stream = basic.pointee
    let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)

    var audioBufferList = AudioBufferList()
    var retainedBlockBuffer: CMBlockBuffer?
    let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sampleBuffer,
        Int(stream.mBytesPerFrame) * Int(frameCount),
        &audioBufferList,
        nil,
        nil,
        kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
        nil,
        nil,
        nil,
        &retainedBlockBuffer
    )
    guard status == noErr else { return nil }
    defer {
        if let retainedBlockBuffer = retainedBlockBuffer {
            CFRelease(retainedBlockBuffer)
        }
    }

    let buffers = UnsafeMutableAudioBufferListPointer(&audioBufferList)
    guard !buffers.isEmpty else { return nil }

    let bytesPerFrame = Int(stream.mBytesPerFrame)
    let channels = Int(stream.mChannelsPerFrame)
    guard channels > 0, bytesPerFrame > 0 else { return nil }

    // One buffer per channel for non-interleaved data, one shared buffer
    // for interleaved data.
    let isInterleaved = buffers.count == 1 && channels > 1
    let isFloat = stream.mFormatFlags & kAudioFormatFlagIsFloat != 0

    var out = Data(capacity: frameCount * 2)

    if isInterleaved {
        guard let base = buffers[0].mData else { return nil }
        let available = Int(buffers[0].mDataByteSize) / MemoryLayout<Int16>.size
        let samples = base.assumingMemoryBound(to: Int16.self)
        let usable = min(frameCount, available / channels)
        for frame in 0..<usable {
            var total: Float = 0
            for channel in 0..<channels {
                total += Float(samples[frame * channels + channel]) / Float(Int16.max)
            }
            let mixed = Int16(max(-1.0, min(1.0, total / Float(channels))) * Float(Int16.max))
            withUnsafeBytes(of: mixed.littleEndian) { out.append(contentsOf: $0) }
        }
        return out
    }

    // Non-interleaved: average the channel buffers.
    var channelData: [(base: UnsafeRawPointer, frames: Int)] = []
    for buffer in buffers {
        guard let data = buffer.mData else { return nil }
        let bytesPerSample = bytesPerFrame / channels
        let frames = Int(buffer.mDataByteSize) / max(1, bytesPerSample)
        channelData.append((UnsafeRawPointer(data), frames))
    }

    let usable = min(frameCount, channelData.map(\.frames).min() ?? 0)
    for frame in 0..<usable {
        var total: Float = 0
        for channel in channelData {
            if isFloat {
                total += channel.base.assumingMemoryBound(to: Float.self)[frame]
            } else {
                total += Float(channel.base.assumingMemoryBound(to: Int16.self)[frame]) / Float(Int16.max)
            }
        }
        let mixed = Int16(max(-1.0, min(1.0, total / Float(channels))) * Float(Int16.max))
        withUnsafeBytes(of: mixed.littleEndian) { out.append(contentsOf: $0) }
    }
    return out
}

// ------------------------------------------------------------------ stream

final class AudioOutput: NSObject, SCStreamOutput {
    let onSample: (CMSampleBuffer) -> Void

    init(onSample: @escaping (CMSampleBuffer) -> Void) {
        self.onSample = onSample
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        ofType type: SCStreamOutputType
    ) {
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        onSample(sampleBuffer)
    }
}

let semaphore = DispatchSemaphore(value: 0)
var capturedContent: SCShareableContent?

SCShareableContent.currentShareableContent(completionHandler: { content, error in
    if let error = error {
        fail("ScreenCaptureKit is unavailable: \(error.localizedDescription)")
    }
    capturedContent = content
    semaphore.signal()
})
semaphore.wait()

guard let content = capturedContent, let display = content.displays.first else {
    fail("No display found to capture audio from")
}

let filter = SCContentFilter(display: display, excludingApplications: [])
let configuration = SCStreamConfiguration()
configuration.capturesAudio = true
configuration.sampleRate = sampleRate
configuration.channelCount = channelCount
configuration.excludesCurrentProcessAudio = false
configuration.shouldCaptureEntireScreen = false

let output = AudioOutput { sampleBuffer in
    guard let pcm = monoPcm(from: sampleBuffer) else { return }
    pcm.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var written = 0
        let total = raw.count
        while written < total {
            let result = write(1, base.advanced(by: written), total - written)
            if result <= 0 { return }
            written += Int(result)
        }
    }
}

let stream = SCStream(filter: filter, configuration: configuration, delegate: nil)
let queue = DispatchQueue(label: "chocolate-egg-cream.audio")

do {
    try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: queue)
} catch {
    fail("Could not attach the audio output: \(error.localizedDescription)")
}

let startSemaphore = DispatchSemaphore(value: 0)
var startError: Error?
stream.startCapture(completionHandler: { error in
    startError = error
    startSemaphore.signal()
})
startSemaphore.wait()

if let startError = startError {
    fail("Could not start the capture: \(startError.localizedDescription)")
}

// Stay alive until the process is stopped. The main run loop also drains
// the main dispatch queue, which ScreenCaptureKit uses for its callbacks.
RunLoop.main.run()
