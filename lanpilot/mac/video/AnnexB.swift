import Foundation

enum H264InputError: Error {
    case malformedAccessUnit
    case accessUnitTooLarge
    case tooManyNALUnits
    case invalidParameterSet
    case unsupportedFormat
}

struct AnnexBAccessUnit {
    struct Picture {
        let ppsID: Int
        let isIDR: Bool
    }

    struct PPS: Equatable {
        let spsID: Int
        let bytes: Data
    }

    var sps: [Int: Data] = [:]
    var pps: [Int: PPS] = [:]
    var picture: Picture?
    var sample = Data()

    init(_ data: Data) throws {
        try data.withUnsafeBytes { (bytes: UnsafeRawBufferPointer) in
            var start: Int?
            var zeros = 0
            var count = 0
            for index in bytes.indices {
                let byte = bytes[index]
                if byte == 0 {
                    zeros += 1
                    continue
                }
                if byte == 1 && zeros >= 2 {
                    if let start {
                        count += 1
                        guard count < 256 else { throw H264InputError.tooManyNALUnits }
                        try addNAL(UnsafeRawBufferPointer(rebasing: bytes[start..<(index - zeros)]))
                    } else if index != zeros {
                        throw H264InputError.malformedAccessUnit
                    }
                    start = index + 1
                } else if start == nil {
                    throw H264InputError.malformedAccessUnit
                }
                zeros = 0
            }
            guard let start else { throw H264InputError.malformedAccessUnit }
            try addNAL(UnsafeRawBufferPointer(rebasing: bytes[start..<(bytes.count - zeros)]))
        }
    }

    private mutating func addNAL(_ nal: UnsafeRawBufferPointer) throws {
        guard let header = nal.first, header & 0x80 == 0 else {
            throw H264InputError.malformedAccessUnit
        }
        let type = header & 31
        var bits = RBSPBits(bytes: nal)
        switch type {
        case 7:
            guard picture == nil, nal.count <= 4096 else { throw H264InputError.invalidParameterSet }
            let profile = try bits.read(8)
            _ = try bits.read(16)
            let id = try bits.ue(max: 31)
            switch profile {
            case 66, 77, 88:
                break  // These profiles imply 8-bit 4:2:0.
            case 100, 110, 122, 244, 44:
                let chromaFormat = try bits.ue(max: 3)
                guard chromaFormat == 1 else { throw H264InputError.unsupportedFormat }
                let lumaDepth = try bits.ue(max: 6)
                let chromaDepth = try bits.ue(max: 6)
                guard lumaDepth == 0, chromaDepth == 0 else {
                    throw H264InputError.unsupportedFormat
                }
            default:
                throw H264InputError.unsupportedFormat
            }
            sps[id] = Data(nal)
        case 8:
            guard picture == nil, nal.count <= 4096 else { throw H264InputError.invalidParameterSet }
            let id = try bits.ue(max: 255)
            pps[id] = PPS(spsID: try bits.ue(max: 31), bytes: Data(nal))
        case 1, 5:
            let firstMacroblock = try bits.ue(max: 262_143)
            let sliceType = try bits.ue(max: 9) % 5
            let id = try bits.ue(max: 255)
            guard sliceType == 0 || sliceType == 2 else { throw H264InputError.unsupportedFormat }
            if let picture {
                guard firstMacroblock != 0, picture.ppsID == id, picture.isIDR == (type == 5) else {
                    throw H264InputError.malformedAccessUnit
                }
            } else {
                guard firstMacroblock == 0 else { throw H264InputError.malformedAccessUnit }
                picture = Picture(ppsID: id, isIDR: type == 5)
            }
            appendNAL(nal)
        case 6:
            appendNAL(nal)
        case 9, 10, 11, 12:
            break  // Access-unit delimiter, end markers, and filler.
        default:
            throw H264InputError.unsupportedFormat
        }
    }

    private mutating func appendNAL(_ nal: UnsafeRawBufferPointer) {
        var length = UInt32(nal.count).bigEndian
        withUnsafeBytes(of: &length) { sample.append(contentsOf: $0) }
        sample.append(contentsOf: nal)
    }
}

private struct RBSPBits {
    let bytes: UnsafeRawBufferPointer
    private var offset = 1  // Skip the NAL header.
    private var zeros = 0
    private var byte = 0
    private var remaining = 0

    init(bytes: UnsafeRawBufferPointer) {
        self.bytes = bytes
    }

    mutating func read(_ count: Int) throws -> Int {
        var value = 0
        for _ in 0..<count {
            if remaining == 0 {
                guard offset < bytes.count else { throw H264InputError.malformedAccessUnit }
                if zeros == 2 && bytes[offset] == 3 {
                    offset += 1
                    zeros = 0
                    guard offset < bytes.count, bytes[offset] <= 3 else {
                        throw H264InputError.malformedAccessUnit
                    }
                }
                byte = Int(bytes[offset])
                offset += 1
                zeros = byte == 0 ? zeros + 1 : 0
                remaining = 8
            }
            remaining -= 1
            value = (value << 1) | ((byte >> remaining) & 1)
        }
        return value
    }

    mutating func ue(max: Int) throws -> Int {
        var zeros = 0
        while try read(1) == 0 {
            zeros += 1
            guard 1 << zeros <= max + 1 else { throw H264InputError.malformedAccessUnit }
        }
        let value = (1 << zeros) - 1 + (try read(zeros))
        guard value <= max else { throw H264InputError.malformedAccessUnit }
        return value
    }
}
