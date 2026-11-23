// SystemAudioCapture -- macOS system audio via ScreenCaptureKit.
//
// Captures the mixed system audio (not the microphone) and writes it to
// stdout as mono signed 16-bit little-endian PCM at the requested rate.
// Chocolate Egg Cream reads that stream and treats it exactly like the
// WASAPI / PulseAudio capture on the other platforms.
//
// Build:
//   swiftc -O -o tools/SystemAudioCapture tools/SystemAudioCapture.swift
//
// Requires macOS 13+ and Screen Recording permission for the terminal or
// app that launches this helper.

import AudioToolbox
import CoreMedia
import Darwin
import Foundation
import ScreenCaptureKit

// Ignore SIGPIPE so closing stdout from Python exits cleanly instead of crashing.
signal(SIGPIPE, SIG_IGN)

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("error: \(message)\n".utf8))
    exit(1)
}

// ---------------------------------------------------------------- arguments

var sampleRate: Int = 48000
var channelCount: Int = 2

let arguments = CommandLine.arguments
var index = 1
while index < arguments.count {
    switch arguments[index] {
    case "--rate":
        index += 1
        guard index < arguments.count, let value = Int(arguments[index]), value > 0 else {
            fail("--rate needs a positive integer")
        }
        sampleRate = value
    case "--channels":
        index += 1
        guard index < arguments.count, let value = Int(arguments[index]), value > 0 else {
            fail("--channels needs a positive integer")
        }
        channelCount = value
    default:
        break
    }
    index += 1
}

// ------------------------------------------------------------- PCM handling

/// Copies the raw PCM out of a CMSampleBuffer and mixes it down to mono
/// signed 16-bit little-endian PCM. Handles both non-interleaved and
/// interleaved float32 / int16 formats.
func monoPcm(from sampleBuffer: CMSampleBuffer) -> Data? {
    guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
        return nil
    }
    guard let basic = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription) else {
        return nil
    }
    let stream = basic.pointee
    let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)
    guard frameCount > 0 else { return nil }

    let channels = Int(stream.mChannelsPerFrame)
    guard channels > 0 else { return nil }

    // First query the exact byte size needed for the AudioBufferList
    // (non-interleaved stereo requires room for 2 AudioBuffers, not 1).
    var bufferListSizeNeeded = 0
    let sizeStatus = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sampleBuffer,
        bufferListSizeNeededOut: &bufferListSizeNeeded,
        bufferListOut: nil,
        bufferListSize: 0,
        blockBufferStructureAllocator: nil,
        blockBufferMemoryAllocator: nil,
        flags: 0,
        blockBufferOut: nil
    )
    guard sizeStatus == noErr, bufferListSizeNeeded > 0 else { return nil }

    let rawBufferList = UnsafeMutableRawPointer.allocate(
        byteCount: bufferListSizeNeeded,
        alignment: MemoryLayout<AudioBufferList>.alignment
    )
    defer { rawBufferList.deallocate() }
    let audioBufferListPtr = rawBufferList.bindMemory(to: AudioBufferList.self, capacity: 1)

    var retainedBlockBuffer: CMBlockBuffer?
    let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sampleBuffer,
        bufferListSizeNeededOut: nil,
        bufferListOut: audioBufferListPtr,
        bufferListSize: bufferListSizeNeeded,
        blockBufferStructureAllocator: nil,
        blockBufferMemoryAllocator: nil,
        flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
        blockBufferOut: &retainedBlockBuffer
    )
    guard status == noErr else { return nil }

    let buffers = UnsafeMutableAudioBufferListPointer(audioBufferListPtr)
    guard !buffers.isEmpty else { return nil }

    let isFloat = (stream.mFormatFlags & kAudioFormatFlagIsFloat) != 0
    let isNonInterleaved = (stream.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
    let bytesPerSample = isFloat ? MemoryLayout<Float>.size : MemoryLayout<Int16>.size

    var out = Data(capacity: frameCount * 2)

    if !isNonInterleaved && buffers.count == 1 {
        guard let base = buffers[0].mData else { return nil }
        let totalSamples = Int(buffers[0].mDataByteSize) / bytesPerSample
        let usable = min(frameCount, totalSamples / channels)
        if isFloat {
            let samples = base.assumingMemoryBound(to: Float.self)
            for frame in 0..<usable {
                var total: Float = 0
                for ch in 0..<channels {
                    total += samples[frame * channels + ch]
                }
                let clamped = max(-1.0, min(1.0, total / Float(channels)))
                let mixed = Int16(clamped * Float(Int16.max))
                withUnsafeBytes(of: mixed.littleEndian) { out.append(contentsOf: $0) }
            }
        } else {
            let samples = base.assumingMemoryBound(to: Int16.self)
            for frame in 0..<usable {
                var total: Float = 0
                for ch in 0..<channels {
                    total += Float(samples[frame * channels + ch]) / Float(Int16.max)
                }
                let clamped = max(-1.0, min(1.0, total / Float(channels)))
                let mixed = Int16(clamped * Float(Int16.max))
                withUnsafeBytes(of: mixed.littleEndian) { out.append(contentsOf: $0) }
            }
        }
        return out
    }

    // Non-interleaved (standard ScreenCaptureKit format): one buffer per channel.
    var channelData: [(base: UnsafeRawPointer, frames: Int)] = []
    for buffer in buffers {
        guard let data = buffer.mData else { return nil }
        let frames = Int(buffer.mDataByteSize) / bytesPerSample
        channelData.append((UnsafeRawPointer(data), frames))
    }
    guard !channelData.isEmpty else { return nil }

    let usable = min(frameCount, channelData.map(\.frames).min() ?? 0)
    let divisor = Float(channelData.count)
    for frame in 0..<usable {
        var total: Float = 0
        for channel in channelData {
            if isFloat {
                total += channel.base.assumingMemoryBound(to: Float.self)[frame]
            } else {
                total += Float(channel.base.assumingMemoryBound(to: Int16.self)[frame]) / Float(Int16.max)
            }
        }
        let clamped = max(-1.0, min(1.0, total / divisor))
        let mixed = Int16(clamped * Float(Int16.max))
        withUnsafeBytes(of: mixed.littleEndian) { out.append(contentsOf: $0) }
    }
    return out
}

// ------------------------------------------------------------------ stream

@available(macOS 13.0, *)
final class AudioOutput: NSObject, SCStreamOutput, SCStreamDelegate {
    let onSample: (CMSampleBuffer) -> Void
    let onStop: (Error) -> Void

    init(
        onSample: @escaping (CMSampleBuffer) -> Void,
        onStop: @escaping (Error) -> Void
    ) {
        self.onSample = onSample
        self.onStop = onStop
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        onSample(sampleBuffer)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onStop(error)
    }
}

guard #available(macOS 13.0, *) else {
    fail("System audio capture requires macOS 13 (Ventura) or newer.")
}

// Retain stream and output for the lifetime of the process.
var retainedStream: SCStream?
var retainedOutput: AudioOutput?
let audioQueue = DispatchQueue(label: "chocolate-egg-cream.audio")

Task {
    do {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false,
            onScreenWindowsOnly: false
        )
        guard let display = content.displays.first else {
            fail("No display found to capture audio from.")
        }

        let filter = SCContentFilter(
            display: display,
            excludingApplications: [],
            exceptingWindows: []
        )

        let configuration = SCStreamConfiguration()
        configuration.capturesAudio = true
        configuration.sampleRate = sampleRate
        configuration.channelCount = channelCount
        configuration.excludesCurrentProcessAudio = false
        // Minimize dummy video overhead since we only read the audio stream.
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let output = AudioOutput(
            onSample: { sampleBuffer in
                guard let pcm = monoPcm(from: sampleBuffer), !pcm.isEmpty else { return }
                pcm.withUnsafeBytes { raw in
                    guard let base = raw.baseAddress else { return }
                    var written = 0
                    let total = raw.count
                    while written < total {
                        let result = write(STDOUT_FILENO, base.advanced(by: written), total - written)
                        if result <= 0 {
                            exit(0)
                        }
                        written += Int(result)
                    }
                }
            },
            onStop: { error in
                fail("ScreenCaptureKit stream stopped: \(error.localizedDescription)")
            }
        )

        let stream = SCStream(filter: filter, configuration: configuration, delegate: output)
        try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: audioQueue)
        try await stream.startCapture()

        retainedOutput = output
        retainedStream = stream
    } catch {
        let description = error.localizedDescription
        let nsError = error as NSError
        if nsError.code == -3801 || description.lowercased().contains("declined") {
            fail(
                "Screen Recording permission is required to capture Mac system audio. "
                + "Open System Settings -> Privacy & Security -> Screen & System Audio Recording, "
                + "enable your Terminal app, then restart."
            )
        }
        fail("ScreenCaptureKit failed: \(description)")
    }
}

// Stay alive until terminated by the parent process.
RunLoop.main.run()
