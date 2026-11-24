// SystemAudioCapture -- macOS system audio capture helper.
//
// Captures mixed system audio (never screen video, never the microphone) and
// writes mono signed 16-bit little-endian PCM to stdout at the requested rate.
//
// Backends:
//   - coreaudio (default on macOS 14.2+): Core Audio Process Tap
//     (AudioHardwareCreateProcessTap + private Aggregate Device).
//     Audio-only permission via NSAudioCaptureUsageDescription ("System Audio
//     Recording Only"), scoped to the Chocolate Egg Cream .app bundle.
//   - screencapturekit (explicit fallback, or automatic on macOS 13.0-14.1):
//     ScreenCaptureKit audio stream.
//
// Build:
//   swiftc -O -o tools/SystemAudioCapture tools/SystemAudioCapture.swift
//
// Flags:
//   --rate <hz>            Output sample rate (default: 48000)
//   --channels <n>         Preferred capture channels for SCK (default: 2)
//   --backend <mode>       auto | coreaudio | screencapturekit (default: auto)

import AudioToolbox
import CoreAudio
import CoreMedia
import Darwin
import Foundation
import ScreenCaptureKit

// Ignore SIGPIPE so closing stdout from Python exits cleanly.
signal(SIGPIPE, SIG_IGN)

func logInfo(_ message: String) {
    FileHandle.standardError.write(Data("info: \(message)\n".utf8))
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data("error: \(message)\n".utf8))
    exit(1)
}

// ---------------------------------------------------------------- arguments

enum CaptureBackend: String {
    case auto
    case coreaudio
    case screencapturekit
}

var targetSampleRate: Int = 48000
var channelCount: Int = 2
var requestedBackend: CaptureBackend = .auto

let arguments = CommandLine.arguments
var index = 1
while index < arguments.count {
    switch arguments[index] {
    case "--rate":
        index += 1
        guard index < arguments.count, let value = Int(arguments[index]), value > 0 else {
            fail("--rate needs a positive integer")
        }
        targetSampleRate = value
    case "--channels":
        index += 1
        guard index < arguments.count, let value = Int(arguments[index]), value > 0 else {
            fail("--channels needs a positive integer")
        }
        channelCount = value
    case "--backend":
        index += 1
        guard index < arguments.count,
              let parsed = CaptureBackend(rawValue: arguments[index].lowercased()) else {
            fail("--backend must be one of: auto, coreaudio, screencapturekit")
        }
        requestedBackend = parsed
    default:
        break
    }
    index += 1
}

// ------------------------------------------------------------- stdout writer

func writeAllToStdout(_ data: Data) {
    guard !data.isEmpty else { return }
    data.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var written = 0
        let total = raw.count
        while written < total {
            let result = write(STDOUT_FILENO, base.advanced(by: written), total - written)
            if result <= 0 {
                cleanupAndExit(0)
            }
            written += Int(result)
        }
    }
}

// ------------------------------------------------------------- linear resampler

/// Lightweight mono float32 -> int16 PCM converter with optional linear resampling.
final class MonoResampler {
    private let inRate: Double
    private let outRate: Double
    private var lastSample: Float = 0.0
    private var phase: Double = 0.0

    init(inRate: Double, outRate: Double) {
        self.inRate = max(1.0, inRate)
        self.outRate = max(1.0, outRate)
    }

    func convertToInt16LE(_ mono: [Float]) -> Data {
        guard !mono.isEmpty else { return Data() }

        if abs(inRate - outRate) < 1.0 {
            var out = Data(capacity: mono.count * 2)
            for sample in mono {
                let clamped = max(-1.0, min(1.0, sample))
                let pcm = Int16(clamped * Float(Int16.max))
                withUnsafeBytes(of: pcm.littleEndian) { out.append(contentsOf: $0) }
            }
            lastSample = mono[mono.count - 1]
            return out
        }

        let step = inRate / outRate
        let estimated = Int(Double(mono.count) / step) + 4
        var out = Data(capacity: max(2, estimated * 2))
        var pos = phase

        while pos < Double(mono.count) {
            let idx = Int(floor(pos))
            let frac = Float(pos - Double(idx))
            let s0: Float = (idx < 0) ? lastSample : mono[idx]
            let s1: Float = (idx + 1 < mono.count) ? mono[idx + 1] : mono[mono.count - 1]
            let interp = s0 + (s1 - s0) * frac
            let clamped = max(-1.0, min(1.0, interp))
            let pcm = Int16(clamped * Float(Int16.max))
            withUnsafeBytes(of: pcm.littleEndian) { out.append(contentsOf: $0) }
            pos += step
        }

        phase = pos - Double(mono.count)
        lastSample = mono[mono.count - 1]
        return out
    }
}

/// Mix an AudioBufferList (float32 or int16, interleaved or non-interleaved) down to mono float32.
func extractMonoFloat(
    from buffers: UnsafeMutableAudioBufferListPointer,
    format: AudioStreamBasicDescription,
    frameCountHint: Int? = nil
) -> [Float] {
    guard !buffers.isEmpty else { return [] }
    let channels = max(1, Int(format.mChannelsPerFrame))
    let isFloat = (format.mFormatFlags & kAudioFormatFlagIsFloat) != 0
    let isNonInterleaved = (format.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
    let bytesPerSample = isFloat ? MemoryLayout<Float>.size : MemoryLayout<Int16>.size
    guard bytesPerSample > 0 else { return [] }

    if !isNonInterleaved && buffers.count == 1 {
        guard let base = buffers[0].mData else { return [] }
        let totalSamples = Int(buffers[0].mDataByteSize) / bytesPerSample
        let availableFrames = totalSamples / channels
        let usable = frameCountHint.map { min($0, availableFrames) } ?? availableFrames
        guard usable > 0 else { return [] }

        var mono = [Float](repeating: 0, count: usable)
        let invCh = 1.0 / Float(channels)
        if isFloat {
            let ptr = base.assumingMemoryBound(to: Float.self)
            for frame in 0..<usable {
                var sum: Float = 0
                let offset = frame * channels
                for ch in 0..<channels {
                    sum += ptr[offset + ch]
                }
                mono[frame] = sum * invCh
            }
        } else {
            let ptr = base.assumingMemoryBound(to: Int16.self)
            let invScale = invCh / Float(Int16.max)
            for frame in 0..<usable {
                var sum: Float = 0
                let offset = frame * channels
                for ch in 0..<channels {
                    sum += Float(ptr[offset + ch])
                }
                mono[frame] = sum * invScale
            }
        }
        return mono
    }

    var channelSlices: [(base: UnsafeRawPointer, frames: Int)] = []
    channelSlices.reserveCapacity(buffers.count)
    for buffer in buffers {
        guard let data = buffer.mData else { continue }
        let frames = Int(buffer.mDataByteSize) / bytesPerSample
        if frames > 0 {
            channelSlices.append((UnsafeRawPointer(data), frames))
        }
    }
    guard !channelSlices.isEmpty else { return [] }

    let minFrames = channelSlices.map(\.frames).min() ?? 0
    let usable = frameCountHint.map { min($0, minFrames) } ?? minFrames
    guard usable > 0 else { return [] }

    var mono = [Float](repeating: 0, count: usable)
    let invCh = 1.0 / Float(channelSlices.count)
    if isFloat {
        for frame in 0..<usable {
            var sum: Float = 0
            for slice in channelSlices {
                sum += slice.base.assumingMemoryBound(to: Float.self)[frame]
            }
            mono[frame] = sum * invCh
        }
    } else {
        let invScale = invCh / Float(Int16.max)
        for frame in 0..<usable {
            var sum: Float = 0
            for slice in channelSlices {
                sum += Float(slice.base.assumingMemoryBound(to: Int16.self)[frame])
            }
            mono[frame] = sum * invScale
        }
    }
    return mono
}

// ------------------------------------------------------------- cleanup hook

var activeCleanup: (() -> Void)?

func cleanupAndExit(_ code: Int32) -> Never {
    let fn = activeCleanup
    activeCleanup = nil
    fn?()
    exit(code)
}

var signalSources: [DispatchSourceSignal] = []

func installSignalHandlers() {
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        source.setEventHandler {
            cleanupAndExit(0)
        }
        source.resume()
        signalSources.append(source)
    }
}

// --------------------------------------------------------- Core Audio Taps

@available(macOS 14.2, *)
final class CoreAudioTapCapture {
    private var tapID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateDeviceID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private let audioQueue = DispatchQueue(label: "com.chocolateeggcream.coreaudio-tap")
    private var streamFormat = AudioStreamBasicDescription()
    private var resampler: MonoResampler?

    func start(outputRate: Int) throws {
        // Global system audio tap (never captures microphone or screen).
        let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        tapDescription.uuid = UUID()
        tapDescription.muteBehavior = .unmuted
        tapDescription.name = "ChocolateEggCreamSystemAudioTap"
        tapDescription.isPrivate = true

        var createdTapID = AudioObjectID(kAudioObjectUnknown)
        let tapStatus = AudioHardwareCreateProcessTap(tapDescription, &createdTapID)
        guard tapStatus == noErr, createdTapID != AudioObjectID(kAudioObjectUnknown) else {
            throw NSError(
                domain: NSOSStatusErrorDomain,
                code: Int(tapStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioHardwareCreateProcessTap failed (OSStatus \(tapStatus)). "
                        + "Make sure Chocolate Egg Cream is allowed under System Settings -> "
                        + "Privacy & Security -> Screen & System Audio Recording -> "
                        + "System Audio Recording Only."
                ]
            )
        }
        tapID = createdTapID

        // Query the tap's native PCM format so we mix and resample accurately.
        var format = AudioStreamBasicDescription()
        var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var formatAddress = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let fmtStatus = AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &format)
        if fmtStatus != noErr || format.mSampleRate <= 0 {
            format.mSampleRate = Double(outputRate)
            format.mChannelsPerFrame = 2
            format.mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked
        }
        streamFormat = format
        resampler = MonoResampler(inRate: format.mSampleRate, outRate: Double(outputRate))

        let tapEntry: [String: Any] = [
            kAudioSubTapUIDKey as String: tapDescription.uuid.uuidString,
            kAudioSubTapDriftCompensationKey as String: true,
        ]
        let aggregateDescription: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "ChocolateEggCreamTapDevice",
            kAudioAggregateDeviceUIDKey as String: "com.chocolateeggcream.tap.\(UUID().uuidString)",
            kAudioAggregateDeviceTapListKey as String: [tapEntry],
            kAudioAggregateDeviceTapAutoStartKey as String: true,
            kAudioAggregateDeviceIsPrivateKey as String: true,
        ]

        var createdAggregateID = AudioObjectID(kAudioObjectUnknown)
        let aggStatus = AudioHardwareCreateAggregateDevice(
            aggregateDescription as CFDictionary,
            &createdAggregateID
        )
        guard aggStatus == noErr, createdAggregateID != AudioObjectID(kAudioObjectUnknown) else {
            stop()
            throw NSError(
                domain: NSOSStatusErrorDomain,
                code: Int(aggStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioHardwareCreateAggregateDevice failed (OSStatus \(aggStatus))."
                ]
            )
        }
        aggregateDeviceID = createdAggregateID

        var createdIOProcID: AudioDeviceIOProcID?
        let ioStatus = AudioDeviceCreateIOProcIDWithBlock(
            &createdIOProcID,
            aggregateDeviceID,
            audioQueue
        ) { [weak self] _, inInputData, _, _, _ in
            guard let self = self else { return }
            let mutablePtr = UnsafeMutablePointer<AudioBufferList>(mutating: inInputData)
            let buffers = UnsafeMutableAudioBufferListPointer(mutablePtr)
            let mono = extractMonoFloat(from: buffers, format: self.streamFormat)
            guard !mono.isEmpty, let resampler = self.resampler else { return }
            let pcm = resampler.convertToInt16LE(mono)
            writeAllToStdout(pcm)
        }
        guard ioStatus == noErr, let procID = createdIOProcID else {
            stop()
            throw NSError(
                domain: NSOSStatusErrorDomain,
                code: Int(ioStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioDeviceCreateIOProcIDWithBlock failed (OSStatus \(ioStatus))."
                ]
            )
        }
        ioProcID = procID

        let startStatus = AudioDeviceStart(aggregateDeviceID, procID)
        guard startStatus == noErr else {
            stop()
            throw NSError(
                domain: NSOSStatusErrorDomain,
                code: Int(startStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioDeviceStart failed (OSStatus \(startStatus)). "
                        + "Check System Audio Recording Only permission for Chocolate Egg Cream."
                ]
            )
        }

        logInfo(
            "backend=coreaudio native_rate=\(Int(format.mSampleRate)) "
            + "output_rate=\(outputRate) channels=\(format.mChannelsPerFrame)"
        )
    }

    func stop() {
        if let procID = ioProcID, aggregateDeviceID != AudioObjectID(kAudioObjectUnknown) {
            AudioDeviceStop(aggregateDeviceID, procID)
            AudioDeviceDestroyIOProcID(aggregateDeviceID, procID)
            ioProcID = nil
        }
        if aggregateDeviceID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            aggregateDeviceID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != AudioObjectID(kAudioObjectUnknown) {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
    }

    deinit {
        stop()
    }
}

// -------------------------------------------------- ScreenCaptureKit fallback

func monoPcmFromSampleBuffer(_ sampleBuffer: CMSampleBuffer) -> Data? {
    guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer),
          let basic = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription) else {
        return nil
    }
    let stream = basic.pointee
    let frameCount = CMSampleBufferGetNumSamples(sampleBuffer)
    guard frameCount > 0, stream.mChannelsPerFrame > 0 else { return nil }

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
    let mono = extractMonoFloat(from: buffers, format: stream, frameCountHint: frameCount)
    guard !mono.isEmpty else { return nil }

    var out = Data(capacity: mono.count * 2)
    for sample in mono {
        let clamped = max(-1.0, min(1.0, sample))
        let pcm = Int16(clamped * Float(Int16.max))
        withUnsafeBytes(of: pcm.littleEndian) { out.append(contentsOf: $0) }
    }
    return out
}

@available(macOS 13.0, *)
final class SCKAudioOutput: NSObject, SCStreamOutput, SCStreamDelegate {
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

@available(macOS 13.0, *)
func startScreenCaptureKit() {
    var retainedStream: SCStream?
    var retainedOutput: SCKAudioOutput?
    let sckQueue = DispatchQueue(label: "com.chocolateeggcream.sck-audio")

    activeCleanup = {
        if let stream = retainedStream {
            let sem = DispatchSemaphore(value: 0)
            Task {
                try? await stream.stopCapture()
                sem.signal()
            }
            _ = sem.wait(timeout: .now() + 1.0)
        }
        retainedStream = nil
        retainedOutput = nil
    }

    Task {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false,
                onScreenWindowsOnly: false
            )
            guard let display = content.displays.first else {
                fail("No display found for ScreenCaptureKit fallback.")
            }

            let filter = SCContentFilter(
                display: display,
                excludingApplications: [],
                exceptingWindows: []
            )

            let configuration = SCStreamConfiguration()
            configuration.capturesAudio = true
            configuration.sampleRate = targetSampleRate
            configuration.channelCount = channelCount
            configuration.excludesCurrentProcessAudio = false
            // Never register a .screen output; keep dummy frame config minimal.
            configuration.width = 2
            configuration.height = 2
            configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            let output = SCKAudioOutput(
                onSample: { sampleBuffer in
                    guard let pcm = monoPcmFromSampleBuffer(sampleBuffer), !pcm.isEmpty else {
                        return
                    }
                    writeAllToStdout(pcm)
                },
                onStop: { error in
                    fail("ScreenCaptureKit stream stopped: \(error.localizedDescription)")
                }
            )

            let stream = SCStream(filter: filter, configuration: configuration, delegate: output)
            try stream.addStreamOutput(output, type: .audio, sampleHandlerQueue: sckQueue)
            try await stream.startCapture()

            retainedOutput = output
            retainedStream = stream
            logInfo("backend=screencapturekit output_rate=\(targetSampleRate) channels=\(channelCount)")
        } catch {
            let description = error.localizedDescription
            let nsError = error as NSError
            if nsError.code == -3801 || description.lowercased().contains("declined") {
                fail(
                    "Screen & System Audio Recording permission is required for the "
                    + "ScreenCaptureKit fallback. Open System Settings -> Privacy & Security -> "
                    + "Screen & System Audio Recording, enable Chocolate Egg Cream, then restart."
                )
            }
            fail("ScreenCaptureKit failed: \(description)")
        }
    }
}

// ---------------------------------------------------------------------- main

installSignalHandlers()

var retainedCoreAudioTap: AnyObject?

switch requestedBackend {
case .coreaudio:
    if #available(macOS 14.2, *) {
        let tap = CoreAudioTapCapture()
        do {
            try tap.start(outputRate: targetSampleRate)
            retainedCoreAudioTap = tap
            activeCleanup = { tap.stop() }
        } catch {
            fail(error.localizedDescription)
        }
    } else {
        fail("Core Audio Taps require macOS 14.2 or newer. Use --backend screencapturekit on macOS 13.")
    }

case .screencapturekit:
    if #available(macOS 13.0, *) {
        startScreenCaptureKit()
    } else {
        fail("System audio capture requires macOS 13 (Ventura) or newer.")
    }

case .auto:
    if #available(macOS 14.2, *) {
        let tap = CoreAudioTapCapture()
        do {
            try tap.start(outputRate: targetSampleRate)
            retainedCoreAudioTap = tap
            activeCleanup = { tap.stop() }
        } catch {
            fail(error.localizedDescription)
        }
    } else if #available(macOS 13.0, *) {
        logInfo("macOS < 14.2 detected; falling back to ScreenCaptureKit compatibility backend")
        startScreenCaptureKit()
    } else {
        fail("System audio capture requires macOS 13 (Ventura) or newer.")
    }
}

_ = retainedCoreAudioTap
RunLoop.main.run()
