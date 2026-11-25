import CoreMedia
import CoreVideo
import CryptoKit
import Dispatch
import Foundation

@main
struct DecodeCLI {
    enum InputError: Error {
        case truncatedRecord
        case oversizedRecord
        case unexpectedPixelLayout
        case pixelBufferLockFailed
        case timestampMismatch
    }

    static func main() {
        do {
            try run()
        } catch {
            FileHandle.standardError.write(Data("decode-cli: \(error)\n".utf8))
            exit(1)
        }
    }

    private static func run() throws {
        let input = FileHandle.standardInput
        let decoder = H264Decoder()
        var index: Int64 = 0
        while let header = try readExactly(4, from: input, allowEOF: true) {
            let length = header.reduce(0) { ($0 << 8) | Int($1) }
            guard length <= H264Decoder.maximumAccessUnitBytes else { throw InputError.oversizedRecord }
            index += 1
            if length == 0 {
                decoder.reset(keepingParameterSets: true)
                try emit(["index": index, "kind": "reset"])
                continue
            }
            guard let bytes = try readExactly(length, from: input) else { throw InputError.truncatedRecord }

            let start = DispatchTime.now().uptimeNanoseconds
            let pts = CMTime(value: index, timescale: 30)
            let output: H264Decoder.Output
            do {
                output = try decoder.decode(bytes, presentationTime: pts)
            } catch {
                try emit(["index": index, "kind": "decodeError", "error": String(describing: error)])
                continue
            }
            let elapsed = DispatchTime.now().uptimeNanoseconds - start
            switch output {
            case .noPicture:
                try emit(["index": index, "kind": "noPicture"])
            case .needsIDR:
                try emit(["index": index, "kind": "needsIDR"])
            case .frame(let frame):
                guard CMTimeCompare(frame.presentationTime, pts) == 0 else { throw InputError.timestampMismatch }
                let pixels = frame.pixels
                let hardware: Any = frame.usingHardwareDecoder.map { $0 as Any } ?? NSNull()
                try emit([
                    "index": index, "kind": "frame", "decode_ms": Double(elapsed) / 1_000_000,
                    "width": CVPixelBufferGetWidth(pixels), "height": CVPixelBufferGetHeight(pixels),
                    "hardware": hardware, "sha256": try pixelDigest(pixels)
                ])
            }
        }
    }

    private static func readExactly(_ size: Int, from file: FileHandle, allowEOF: Bool = false) throws -> Data? {
        var bytes = Data()
        while bytes.count < size {
            guard let chunk = try file.read(upToCount: size - bytes.count), !chunk.isEmpty else {
                if bytes.isEmpty && allowEOF { return nil }
                throw InputError.truncatedRecord
            }
            bytes.append(chunk)
        }
        return bytes
    }

    private static func pixelDigest(_ pixels: CVPixelBuffer) throws -> String {
        guard CVPixelBufferGetPixelFormatType(pixels) == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
              CVPixelBufferGetPlaneCount(pixels) == 2 else { throw InputError.unexpectedPixelLayout }
        guard CVPixelBufferLockBaseAddress(pixels, .readOnly) == kCVReturnSuccess else {
            throw InputError.pixelBufferLockFailed
        }
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }

        var hash = SHA256()
        // NV12 has one byte per luma sample and two per interleaved chroma sample.
        for plane in 0..<2 {
            let rowBytes = CVPixelBufferGetWidthOfPlane(pixels, plane) * (plane + 1)
            let stride = CVPixelBufferGetBytesPerRowOfPlane(pixels, plane)
            guard let base = CVPixelBufferGetBaseAddressOfPlane(pixels, plane), rowBytes <= stride else {
                throw InputError.unexpectedPixelLayout
            }
            for row in 0..<CVPixelBufferGetHeightOfPlane(pixels, plane) {
                hash.update(bufferPointer: UnsafeRawBufferPointer(start: base + row * stride, count: rowBytes))
            }
        }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func emit(_ row: [String: Any]) throws {
        let json = try JSONSerialization.data(withJSONObject: row, options: [.sortedKeys])
        try FileHandle.standardOutput.write(contentsOf: json + Data([10]))
    }
}
