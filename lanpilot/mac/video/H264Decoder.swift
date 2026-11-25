import CoreMedia
import CoreVideo
import Foundation
import VideoToolbox

/// Use from one serial receiver. One access unit is processed per call, with no internal queue.
final class H264Decoder {
    static let maximumAccessUnitBytes = 2 * 1024 * 1024

    struct Frame {
        /// Retained for the caller. Treat the pixel data as read-only.
        let pixels: CVPixelBuffer
        let presentationTime: CMTime
        let usingHardwareDecoder: Bool?
    }

    enum Output {
        case frame(Frame)
        case noPicture
        case needsIDR
    }

    enum DecodeError: Error {
        case absentOutput
        case unsupportedDimensions
        case videoToolbox(String, OSStatus)
    }

    private struct Parameters: Equatable {
        let sps: Data
        let pps: Data
    }

    private struct Session {
        let parameters: Parameters
        let format: CMVideoFormatDescription
        let decoder: VTDecompressionSession
        var usingHardware: Bool?
    }

    // VT guarantees completion before return when both async and temporal flags are clear.
    private final class CallbackResult: @unchecked Sendable {
        var output: (status: OSStatus, flags: VTDecodeInfoFlags, buffer: CVImageBuffer?, pts: CMTime)?
    }

    private var sps: [Int: Data] = [:]
    private var pps: [Int: AnnexBAccessUnit.PPS] = [:]
    private var session: Session?

    deinit {
        invalidateSession()
    }

    /// Call after a dropped access unit. A different sender or stream also needs fresh SPS/PPS.
    func reset(keepingParameterSets: Bool = false) {
        invalidateSession()
        if !keepingParameterSets {
            sps.removeAll()
            pps.removeAll()
        }
    }

    /// An error discards the current reference chain. Resume at an IDR.
    func decode(_ data: Data, presentationTime: CMTime = .invalid) throws -> Output {
        do {
            return try decodeAccessUnit(data, presentationTime: presentationTime)
        } catch {
            invalidateSession()
            throw error
        }
    }

    private func decodeAccessUnit(_ data: Data, presentationTime: CMTime) throws -> Output {
        guard data.count <= Self.maximumAccessUnitBytes else { throw H264InputError.accessUnitTooLarge }
        let accessUnit = try AnnexBAccessUnit(data)
        sps.merge(accessUnit.sps) { _, new in new }
        pps.merge(accessUnit.pps) { _, new in new }

        guard let picture = accessUnit.picture else { return .noPicture }
        guard let pps = pps[picture.ppsID], let sps = sps[pps.spsID] else {
            invalidateSession()
            return .needsIDR
        }
        let parameters = Parameters(sps: sps, pps: pps.bytes)
        if session?.parameters != parameters {
            invalidateSession()
            guard picture.isIDR else { return .needsIDR }
            session = try makeSession(parameters)
        }
        guard let current = session else { return .needsIDR }

        let sample = try makeSample(accessUnit.sample, format: current.format, pts: presentationTime)
        let result = CallbackResult()
        // IP-only video uses decoding order as presentation order.
        let status = VTDecompressionSessionDecodeFrame(
            current.decoder, sampleBuffer: sample, flags: [], infoFlagsOut: nil
        ) { status, flags, buffer, pts, _ in
            result.output = (status, flags, buffer, pts)
        }
        try check(status, "submit")
        guard let output = result.output else { throw DecodeError.absentOutput }
        try check(output.status, "decode")
        guard !output.flags.contains(.frameDropped), let pixels = output.buffer else {
            throw DecodeError.absentOutput
        }

        if session?.usingHardware == nil {
            var property: Unmanaged<CFTypeRef>?
            let status = VTSessionCopyProperty(
                current.decoder, key: kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder,
                allocator: nil, valueOut: &property
            )
            if status == noErr {
                session?.usingHardware = (property?.takeRetainedValue() as? NSNumber)?.boolValue
            }
        }
        return .frame(Frame(pixels: pixels, presentationTime: output.pts,
                            usingHardwareDecoder: session?.usingHardware))
    }

    private func invalidateSession() {
        if let session {
            VTDecompressionSessionInvalidate(session.decoder)
            self.session = nil
        }
    }

    private func makeSession(_ parameters: Parameters) throws -> Session {
        var format: CMFormatDescription?
        let formatStatus = parameters.sps.withUnsafeBytes { (sps: UnsafeRawBufferPointer) in
            parameters.pps.withUnsafeBytes { (pps: UnsafeRawBufferPointer) in
                CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    allocator: kCFAllocatorDefault, parameterSetCount: 2,
                    parameterSetPointers: [sps.bindMemory(to: UInt8.self).baseAddress!,
                                           pps.bindMemory(to: UInt8.self).baseAddress!],
                    parameterSetSizes: [sps.count, pps.count], nalUnitHeaderLength: 4,
                    formatDescriptionOut: &format
                )
            }
        }
        try check(formatStatus, "create format")
        guard let format else { throw DecodeError.absentOutput }
        let size = CMVideoFormatDescriptionGetDimensions(format)
        guard (1...1920).contains(size.width), (1...1088).contains(size.height),
              size.width.isMultiple(of: 2), size.height.isMultiple(of: 2) else {
            throw DecodeError.unsupportedDimensions
        }

        let specification = [kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder: true] as CFDictionary
        let attributes = [
            kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferIOSurfacePropertiesKey: [:] as [String: Any]
        ] as CFDictionary
        var decoder: VTDecompressionSession?
        try check(VTDecompressionSessionCreate(
            allocator: nil, formatDescription: format, decoderSpecification: specification,
            imageBufferAttributes: attributes, outputCallback: nil, decompressionSessionOut: &decoder
        ), "create decoder")
        guard let decoder else { throw DecodeError.absentOutput }
        let realtime = VTSessionSetProperty(decoder, key: kVTDecompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        if realtime != noErr {
            VTDecompressionSessionInvalidate(decoder)
            throw DecodeError.videoToolbox("set realtime", realtime)
        }
        return Session(parameters: parameters, format: format, decoder: decoder)
    }

    private func makeSample(_ bytes: Data, format: CMVideoFormatDescription, pts: CMTime) throws -> CMSampleBuffer {
        var block: CMBlockBuffer?
        try check(CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: bytes.count,
            blockAllocator: kCFAllocatorDefault, customBlockSource: nil, offsetToData: 0,
            dataLength: bytes.count, flags: kCMBlockBufferAssureMemoryNowFlag, blockBufferOut: &block
        ), "allocate sample")
        guard let block else { throw DecodeError.absentOutput }
        try check(bytes.withUnsafeBytes { (data: UnsafeRawBufferPointer) in
            CMBlockBufferReplaceDataBytes(with: data.baseAddress!, blockBuffer: block,
                                          offsetIntoDestination: 0, dataLength: bytes.count)
        }, "copy sample")

        var timing = CMSampleTimingInfo(duration: .invalid, presentationTimeStamp: pts, decodeTimeStamp: .invalid)
        var length = bytes.count
        var sample: CMSampleBuffer?
        try check(CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault, dataBuffer: block, formatDescription: format,
            sampleCount: 1, sampleTimingEntryCount: 1, sampleTimingArray: &timing,
            sampleSizeEntryCount: 1, sampleSizeArray: &length, sampleBufferOut: &sample
        ), "create sample")
        guard let sample else { throw DecodeError.absentOutput }
        return sample
    }

    private func check(_ status: OSStatus, _ operation: String) throws {
        guard status == noErr else { throw DecodeError.videoToolbox(operation, status) }
    }
}
