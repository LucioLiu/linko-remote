# Windows AVC sender

`lanpilot-avc.exe` is an on-demand child process for the interactive desktop worker. It uses GDI to capture the WinSta0 Default desktop across the virtual screen, scales into a top-down DIB, converts to NV12 and encodes with the Windows Media Foundation software H.264 MFT. The encoder uses Baseline, CBR, a two-second GOP setting and low-latency mode. Capture is paced from each frame's start. There is one capture/encode/write loop and no application-level frame queue. The helper terminates on a desktop or virtual-screen geometry change so the worker can revalidate and restart. It does not draw a cursor overlay. It opens no network connection or output file.

The target is Windows 10 version 1703 or later with Media Foundation and the Microsoft H.264 encoder present. Software encoding is the first path. DXGI, hardware MFT selection and product wiring are outside this implementation. Windows N installations may require the system Media Feature Pack. See the [Microsoft encoder](https://learn.microsoft.com/en-us/windows/win32/medfound/h-264-video-encoder) and [low-latency property](https://learn.microsoft.com/en-us/windows/win32/medfound/codecapi-avlowlatencymode).

## Worker contract

Launch after the existing worker validates the active live session. Use a fully qualified helper path, the worker's interactive account/session and desktop, and `CREATE_NO_WINDOW`. Pass private byte-mode pipes as stdin/stdout, redirect stderr for diagnostics, and pass a real inheritable process handle for the worker itself with `SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION`. An inheritance allowlist via `STARTUPINFOEX` keeps unrelated handles out of the child. The decimal handle is the value seen by the child, e.g. an inheritable duplicate of the worker's process pseudo-handle. Close unused pipe ends and the parent's copy of the inheritable handle after launch.

```text
lanpilot-avc.exe PARENT_HANDLE DEADLINE_TICK MAX_WIDTH MAX_HEIGHT FPS BITRATE
```

All six arguments are unsigned decimal. Max width is 64–1920 and max height is 64–1080. The sender keeps the desktop aspect within rounding, never upscales, and rounds dimensions down to even numbers. FPS is 1–30 and bitrate is 100000–20000000 bits/s. Suggested initial trial parameters are `1280 720 20 3500000`.

`DEADLINE_TICK` is an absolute Windows `GetTickCount64()` value at most 4,000 ms ahead. For the existing live mailbox, map the remaining lease duration to this time base, conservatively round down, and cap it at four seconds. Use the deadline for the currently validated session ID only. For each renewal, revalidate the worker stop state, session ID and active lease before sending an updated absolute deadline. On revoke, stop, or a session change, send stop or close stdin immediately. Start another helper only after validating a fresh session or usable lease.

Stdin commands are fixed nine bytes with the last eight in little endian. Serialize writes and flush any buffered writer.

| Opcode | Argument | Effect |
| --- | --- | --- |
| `R` | Absolute deadline tick | Replace the live lease with the supplied deadline. An expired process cannot be resumed. |
| `K` | Zero | Request IDR for the next input processed after the command is handled. Pending requests coalesce. |
| `S` | Zero | Stop without draining buffered video. |

An independent watchdog checks the process handle, command pipe and lease while the main thread is in GDI, Media Foundation or a blocking stdout write. It polls commands every 5 ms subject to scheduler granularity. A stop can leave a partial record or buffered earlier bytes in the pipe. The worker must discard partial records and suppress any AVC publication once its own session check fails.

## Stdout frame records

The 48-byte little-endian header is followed immediately by the indicated number of bytes. Payloads contain one H.264 access unit in Annex B format. Sequence starts at one per process. Bit zero is set only for an IDR and each IDR has SPS/PPS, taken from the MFT's output media type if needed.

| Offset | Type | Meaning |
| --- | --- | --- |
| 0 | 4 bytes | ASCII `LPA1` |
| 4 | u32 | Payload length, 1–2,097,152 |
| 8 | u64 | Sequence |
| 16 | u64 | Frame start tick from `GetTickCount64()`, in ms |
| 24 | u16 | Encoded width |
| 26 | u16 | Encoded height |
| 28 | u32 | Flags, bit zero = IDR |
| 32 | i32 | Virtual desktop X origin |
| 36 | i32 | Virtual desktop Y origin |
| 40 | u32 | Source desktop width |
| 44 | u32 | Source desktop height |

Drain stdout off the worker's control path. Use a deadline for the first complete IDR. If encoded dependent frames are dropped, discard dependent frames until the next IDR and ask for one with `K`. Keep existing JPEG available for a still-authorized session on launch failure, protocol error, timeout or encoder failure. Exit codes are 0 for explicit stop, 1 for launch/capture/encoder/output error with a diagnostic if available, 2 for elapsed or invalid renewal deadline, 3 for a signaled/unusable worker process handle after startup, and 4 for closed or malformed stdin. The worker should use its own lease state to distinguish a codec fallback from a revoked session.

## Build and verification

Cross-build with llvm-mingw. Keep binaries in the caller's build directory.

```sh
CXX=x86_64-w64-mingw32-clang++ sh windows/video/build.sh /tmp/lanpilot-avc-x64.exe
CXX=aarch64-w64-mingw32-clang++ sh windows/video/build.sh /tmp/lanpilot-avc-arm64.exe
clang++ -std=c++17 -Wall -Wextra -Werror -fsanitize=address,undefined \
  windows/video/test_protocol.cpp -o /tmp/lanpilot-avc-test
/tmp/lanpilot-avc-test
```

From a Visual Studio Developer Command Prompt with a Windows SDK:

```bat
cl /nologo /std:c++17 /O2 /EHsc /MT windows\video\sender.cpp /Fe:lanpilot-avc.exe mfplat.lib mfuuid.lib ole32.lib uuid.lib gdi32.lib user32.lib
```

Run the Windows smoke test only from an authorized interactive test desktop. It reads frames in RAM and exercises the first IDR and parameter sets, sequence and dimensions, key requests, independent stop, expiry with unread stdout, watched process exit, and a closed control pipe.

```powershell
py -3 windows/video/test_windows.py C:\path\to\lanpilot-avc.exe
```

Verified on an Apple Silicon Mac with llvm-mingw 20251007. The x64 and ARM64 builds both linked, and PE inspection found imports from Windows system DLLs only. The portable protocol, NAL marker and NV12 conversion tests passed under AddressSanitizer and UndefinedBehaviorSanitizer. All five Windows smoke cases were discovered and skipped on macOS. Windows GDI capture, actual MFT output and watchdog behavior have yet to be exercised on Windows. The Visual Studio build recipe was not executed.
