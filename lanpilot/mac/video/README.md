# macOS H.264 receiver decoder

`H264Decoder` consumes one complete Annex B access unit per call, in decoding order. It accepts 8-bit 4:2:0 H.264 with IDR and dependent P pictures, including multi-slice pictures and three- or four-byte start codes. An access unit can carry SPS/PPS by themselves or before its picture. The decoder rejects 4:4:4, higher bit depths and B slices. It caps the input at 2 MiB and 256 NAL units per call, each cached parameter set at 4 KiB, and format dimensions at 1920×1088 to accommodate a coded height rounded to the macroblock boundary.

## Receiver integration

- Include `AnnexB.swift` and `H264Decoder.swift`. Use one instance per stream and call it from a serial receiver off the UI thread. Each call runs through the VideoToolbox output callback before returning. There is no internal input queue and temporal processing is disabled. The caller owns the transport and any queue, and should bound its work and release pixel buffers promptly.
- Pass the caller's presentation timestamp to `decode(_:presentationTime:)`. A successful frame owns a read-only, IOSurface-backed NV12 video-range pixel buffer. `usingHardwareDecoder` comes from the active VideoToolbox session and is optional when querying the property fails. The session requests hardware acceleration and real-time decoding.
- After a sequence gap or a dropped access unit, call `reset(keepingParameterSets: true)` before decoding another picture of the same stream. After a stream restart or sender change, call `reset()` and supply fresh SPS/PPS. On `.needsIDR` or a thrown error, have the sender issue a fresh IDR with SPS/PPS. Duplicate keyframe requests can be coalesced until that picture arrives. `.noPicture` is expected for a parameter-set-only access unit.
- A picture selects its PPS and SPS by ID. A change to the selected pair invalidates the active session. A new session starts with an IDR, including when dimensions change. Any malformed access unit or VideoToolbox decode failure also invalidates the current reference chain.

[Apple VideoToolbox decoding APIs](https://developer.apple.com/documentation/videotoolbox/vtdecompressionsession-api-collection) · [Apple hardware decoder properties](https://developer.apple.com/documentation/videotoolbox/decompression-properties)

## Standalone verification

Run from the repository root on macOS with Xcode and Python with PyAV, libx264 and Pillow available.

```sh
sh lanpilot/mac/video/build.sh /tmp/lanpilot-decode
python lanpilot/mac/video/tests/verify.py
```

The CLI reads a big-endian 32-bit length followed by the Annex B payload from stdin. A zero length signals a discontinuity within the same stream. It emits one JSON line per record with decode status, and for a picture, dimensions, elapsed decode time, hardware status and a SHA-256 hash of the visible NV12 bytes. This framing belongs to the test CLI. The reusable class receives the Annex B payload directly from its caller.

The test builds the release CLI in a temporary directory. It generates scrolling 720p and 1080p samples with libx264, feeds 240 access units per resolution at 30 fps, and compares every visible luma and chroma byte via SHA-256 against PyAV/FFmpeg software decoding. It requires hardware acceleration by default. It also verifies standalone SPS/PPS, loss recovery, resolution changes, malformed input, 4:4:4 rejection and record limits.

Timing covers entry to `decode` through return. The first call includes session creation. Stable P50 and P95 discard the first 10 samples of each stream. Input transport, pixel hashing and display are outside the timed interval.
