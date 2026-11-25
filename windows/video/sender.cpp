#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#ifndef WINVER
#define WINVER 0x0A00
#endif
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#include <windows.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mftransform.h>
#ifdef __MINGW32__
#include <icodecapi.h>
#else
#include <strmif.h>
#endif
#include <codecapi.h>
#include <wrl/client.h>

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <cstdio>
#include <stdexcept>
#include <string_view>
#include <thread>

#include "protocol.h"

namespace {
using Microsoft::WRL::ComPtr;
using namespace video;

// Microsoft Media Foundation H.264 Encoder (CMSH264EncoderMFT).
constexpr GUID encoder_clsid{0x6ca50344, 0x051a, 0x4ded, {0x97,0x79,0xa4,0x33,0x05,0x16,0x5e,0x35}};

void check(bool condition, const char* context) {
    if (!condition) throw std::runtime_error(context);
}

void mf(HRESULT result, const char* context) {
    if (FAILED(result)) {
        char message[160];
        std::snprintf(message, sizeof(message), "%s (0x%08lx)", context, static_cast<unsigned long>(result));
        throw std::runtime_error(message);
    }
}

enum ExitCode : UINT { stopped = 0, failed = 1, expired = 2, parent_gone = 3, bad_control = 4 };

[[noreturn]] void stop(ExitCode code) {
    // The capture/codec or pipe might be blocked. Exit without draining video.
    TerminateProcess(GetCurrentProcess(), code);
    std::abort();
}

struct Session {
    HANDLE parent;
    HANDLE commands;
    HANDLE output;
    std::atomic<uint64_t> deadline;
    std::atomic<bool> key_requested{false};

    void check_lease() const {
        const DWORD state = WaitForSingleObject(parent, 0);
        if (state != WAIT_TIMEOUT) stop(parent_gone);
        if (GetTickCount64() >= deadline.load()) stop(expired);
    }

    void watch() {
        while (true) {
            for (unsigned i = 0; i < 32; ++i) {
                check_lease();
                DWORD available = 0;
                if (!PeekNamedPipe(commands, nullptr, 0, nullptr, &available, nullptr)) stop(bad_control);
                if (available < command_size) break;
                Command command{};
                DWORD read = 0;
                if (!ReadFile(commands, command.data(), DWORD(command.size()), &read, nullptr)
                    || read != command.size()) stop(bad_control);
                check_lease();
                const uint64_t value = read_le(command.data() + 1, 8);
                switch (command[0]) {
                    case 'S':
                        stop(value == 0 ? stopped : bad_control);
                    case 'K':
                        if (value != 0) stop(bad_control);
                        key_requested.store(true);
                        break;
                    case 'R': {
                        const uint64_t now = GetTickCount64();
                        if (now >= deadline.load() || !valid_deadline(now, value)) stop(expired);
                        deadline.store(value);
                        break;
                    }
                    default:
                        stop(bad_control);
                }
            }
            if (WaitForSingleObject(parent, 5) != WAIT_TIMEOUT) stop(parent_gone);
        }
    }

    void write(const uint8_t* bytes, DWORD size) const {
        check_lease();
        DWORD written = 0;
        check(WriteFile(output, bytes, size, &written, nullptr) && written == size, "frame pipe closed");
    }
};

DWORD WINAPI watch_session(void* data) {
    static_cast<Session*>(data)->watch();
    return 0;
}

bool named_object(HANDLE handle, std::wstring_view expected) {
    wchar_t name[64]{};
    DWORD needed = 0;
    return handle && GetUserObjectInformationW(handle, UOI_NAME, name, sizeof(name), &needed)
        && expected == name;
}

Bounds desktop_bounds() {
    return {GetSystemMetrics(SM_XVIRTUALSCREEN), GetSystemMetrics(SM_YVIRTUALSCREEN),
            GetSystemMetrics(SM_CXVIRTUALSCREEN), GetSystemMetrics(SM_CYVIRTUALSCREEN)};
}

void check_desktop() {
    HDESK input = OpenInputDesktop(0, FALSE, DESKTOP_READOBJECTS);
    const bool active = named_object(input, L"Default");
    if (input) CloseDesktop(input);
    check(active, "interactive desktop unavailable");
}

class Desktop {
    HDC screen_ = nullptr;
    HDC memory_ = nullptr;
    HBITMAP bitmap_ = nullptr;
    HGDIOBJ previous_ = nullptr;
    void* pixels_ = nullptr;

    void draw_cursor() {
        CURSORINFO cursor{};
        cursor.cbSize = sizeof(cursor);
        if (!GetCursorInfo(&cursor) || cursor.flags != CURSOR_SHOWING) return;
        ICONINFO icon{};
        if (!GetIconInfo(cursor.hCursor, &icon)) return;
        BITMAP bitmap{};
        const bool measured = GetObjectW(icon.hbmColor ? icon.hbmColor : icon.hbmMask,
                                          sizeof(bitmap), &bitmap) == sizeof(bitmap);
        if (measured) {
            const int cursor_height = icon.hbmColor ? bitmap.bmHeight : bitmap.bmHeight / 2;
            const int x = cursor.ptScreenPos.x - bounds.x - static_cast<int>(icon.xHotspot);
            const int y = cursor.ptScreenPos.y - bounds.y - static_cast<int>(icon.yHotspot);
            DrawIconEx(memory_, MulDiv(x, width, bounds.width), MulDiv(y, height, bounds.height),
                       cursor.hCursor, std::max(1, MulDiv(bitmap.bmWidth, width, bounds.width)),
                       std::max(1, MulDiv(cursor_height, height, bounds.height)), 0, nullptr, DI_NORMAL);
        }
        if (icon.hbmColor) DeleteObject(icon.hbmColor);
        if (icon.hbmMask) DeleteObject(icon.hbmMask);
    }

public:
    Bounds bounds{};
    unsigned width = 0, height = 0;
    ~Desktop() {
        if (previous_ && previous_ != HGDI_ERROR) SelectObject(memory_, previous_);
        if (bitmap_) DeleteObject(bitmap_);
        if (memory_) DeleteDC(memory_);
        if (screen_) ReleaseDC(nullptr, screen_);
    }

    void init(unsigned max_width, unsigned max_height) {
        check(named_object(GetProcessWindowStation(), L"WinSta0")
            && named_object(GetThreadDesktop(GetCurrentThreadId()), L"Default"), "worker desktop required");
        check_desktop();
        bounds = desktop_bounds();
        check(bounds.width >= 64 && bounds.height >= 64, "invalid virtual desktop size");
        const double scale = std::min({1.0, double(max_width)/bounds.width, double(max_height)/bounds.height});
        width = unsigned(bounds.width * scale) & ~1u;
        height = unsigned(bounds.height * scale) & ~1u;
        check(width >= 64 && height >= 64, "encoded desktop too narrow");
        screen_ = GetDC(nullptr);
        check(screen_ != nullptr, "GetDC");
        memory_ = CreateCompatibleDC(screen_);
        check(memory_ != nullptr, "CreateCompatibleDC");
        BITMAPINFO format{};
        format.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
        format.bmiHeader.biWidth = LONG(width);
        format.bmiHeader.biHeight = -LONG(height);
        format.bmiHeader.biPlanes = 1;
        format.bmiHeader.biBitCount = 32;
        format.bmiHeader.biCompression = BI_RGB;
        bitmap_ = CreateDIBSection(screen_, &format, DIB_RGB_COLORS, &pixels_, nullptr, 0);
        check(bitmap_ && pixels_, "CreateDIBSection");
        previous_ = SelectObject(memory_, bitmap_);
        check(previous_ && previous_ != HGDI_ERROR, "SelectObject");
        check(SetStretchBltMode(memory_, HALFTONE) && SetBrushOrgEx(memory_, 0, 0, nullptr), "GDI scaling");
    }

    void capture(const Session& session, uint8_t* destination) {
        check_desktop();
        check(bounds == desktop_bounds(), "virtual desktop changed");
        session.check_lease();
        check(StretchBlt(memory_, 0, 0, int(width), int(height), screen_, bounds.x, bounds.y,
                         bounds.width, bounds.height, SRCCOPY | CAPTUREBLT), "desktop capture");
        draw_cursor();
        check(GdiFlush(), "flush capture");
        check_desktop();
        check(bounds == desktop_bounds(), "virtual desktop changed");
        session.check_lease();
        bgra_to_nv12(static_cast<const uint8_t*>(pixels_), destination, width, height);
    }
};

class BufferLock {
    IMFMediaBuffer* buffer_;
public:
    BYTE* bytes = nullptr;
    DWORD size = 0;
    explicit BufferLock(IMFMediaBuffer* buffer) : buffer_(buffer) {
        mf(buffer_->Lock(&bytes, nullptr, &size), "Lock media buffer");
    }
    ~BufferLock() { buffer_->Unlock(); }
};

void codec_setting(ICodecAPI* codec, const GUID& key, ULONG value) {
    VARIANT setting{};
    setting.vt = VT_UI4;
    setting.ulVal = value;
    mf(codec->SetValue(&key, &setting), "Set encoder numeric property");
}

class Encoder {
    ComPtr<IMFTransform> transform_;
    ComPtr<ICodecAPI> codec_;
    DWORD input_size_ = 0;
    uint64_t frame_index_ = 0;
    unsigned fps_ = 0;

    static ComPtr<IMFMediaType> type(const GUID& subtype, unsigned width, unsigned height, unsigned fps) {
        ComPtr<IMFMediaType> result;
        mf(MFCreateMediaType(&result), "MFCreateMediaType");
        mf(result->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video), "video type");
        mf(result->SetGUID(MF_MT_SUBTYPE, subtype), "video subtype");
        mf(MFSetAttributeSize(result.Get(), MF_MT_FRAME_SIZE, width, height), "frame size");
        mf(MFSetAttributeRatio(result.Get(), MF_MT_FRAME_RATE, fps, 1), "frame rate");
        mf(MFSetAttributeRatio(result.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1), "pixel aspect");
        mf(result->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive), "progressive scan");
        mf(result->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT601), "color matrix");
        mf(result->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235), "video range");
        return result;
    }

    Bytes parameter_sets() {
        ComPtr<IMFMediaType> type;
        mf(transform_->GetOutputCurrentType(0, &type), "GetOutputCurrentType");
        UINT32 size = 0;
        mf(type->GetBlobSize(MF_MT_MPEG_SEQUENCE_HEADER, &size), "SPS/PPS size");
        check(size != 0 && size <= 64 * 1024, "invalid SPS/PPS size");
        Bytes data(size);
        mf(type->GetBlob(MF_MT_MPEG_SEQUENCE_HEADER, data.data(), size, nullptr), "SPS/PPS");
        check(nal_flags(data) == (sps | pps), "invalid Annex B SPS/PPS");
        return data;
    }

public:
    void init(unsigned width, unsigned height, unsigned fps, unsigned bitrate) {
        fps_ = fps;
        input_size_ = width * height * 3 / 2;
        mf(CoCreateInstance(encoder_clsid, nullptr, CLSCTX_INPROC_SERVER,
                            IID_PPV_ARGS(transform_.GetAddressOf())), "Microsoft H.264 encoder");
        mf(transform_.As(&codec_), "ICodecAPI");
        VARIANT latency{};
        latency.vt = VT_BOOL;
        latency.boolVal = VARIANT_TRUE;
        mf(codec_->SetValue(&CODECAPI_AVLowLatencyMode, &latency), "enable low-latency encoder");
        codec_setting(codec_.Get(), CODECAPI_AVEncCommonRateControlMode, eAVEncCommonRateControlMode_CBR);

        auto output = type(MFVideoFormat_H264, width, height, fps);
        mf(output->SetUINT32(MF_MT_AVG_BITRATE, bitrate), "average bitrate");
        mf(output->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_Base), "baseline profile");
        mf(transform_->SetOutputType(0, output.Get(), 0), "set AVC output");
        auto input = type(MFVideoFormat_NV12, width, height, fps);
        mf(input->SetUINT32(MF_MT_DEFAULT_STRIDE, width), "NV12 stride");
        mf(transform_->SetInputType(0, input.Get(), 0), "set NV12 input");
        codec_setting(codec_.Get(), CODECAPI_AVEncMPVGOPSize, 2 * fps);
        mf(transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0), "begin streaming");
        mf(transform_->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0), "start stream");
    }

    Bytes encode(Desktop& desktop, const Session& session, bool force, bool& key) {
        ComPtr<IMFSample> sample;
        ComPtr<IMFMediaBuffer> buffer;
        mf(MFCreateSample(&sample), "input sample");
        mf(MFCreateMemoryBuffer(input_size_, &buffer), "NV12 buffer");
        {
            BufferLock lock(buffer.Get());
            desktop.capture(session, lock.bytes);
        }
        mf(buffer->SetCurrentLength(input_size_), "NV12 length");
        mf(sample->AddBuffer(buffer.Get()), "attach NV12");
        const LONGLONG time = 10000000LL * frame_index_ / fps_;
        const LONGLONG next = 10000000LL * ++frame_index_ / fps_;
        mf(sample->SetSampleTime(time), "sample time");
        mf(sample->SetSampleDuration(next - time), "sample duration");
        if (force) codec_setting(codec_.Get(), CODECAPI_AVEncVideoForceKeyFrame, 1);
        session.check_lease();
        mf(transform_->ProcessInput(0, sample.Get(), 0), "encode NV12");

        MFT_OUTPUT_STREAM_INFO info{};
        mf(transform_->GetOutputStreamInfo(0, &info), "encoder output info");
        ComPtr<IMFSample> storage;
        if (!(info.dwFlags & (MFT_OUTPUT_STREAM_PROVIDES_SAMPLES | MFT_OUTPUT_STREAM_CAN_PROVIDE_SAMPLES))) {
            check(info.cbSize <= max_payload, "encoder buffer exceeds frame bound");
            ComPtr<IMFMediaBuffer> output;
            constexpr DWORD capacity = max_payload;
            if (info.cbAlignment) {
                mf(MFCreateAlignedMemoryBuffer(capacity, info.cbAlignment - 1, &output), "aligned AVC buffer");
            } else {
                mf(MFCreateMemoryBuffer(capacity, &output), "AVC buffer");
            }
            mf(MFCreateSample(&storage), "output sample");
            mf(storage->AddBuffer(output.Get()), "attach AVC buffer");
        }
        MFT_OUTPUT_DATA_BUFFER result{};
        result.pSample = storage.Get();
        DWORD status = 0;
        const HRESULT hr = transform_->ProcessOutput(0, 1, &result, &status);
        ComPtr<IMFCollection> events;
        events.Attach(result.pEvents);
        if (result.pSample != storage.Get()) storage.Attach(result.pSample);
        mf(hr, "get immediate AVC output");
        check(storage && !result.dwStatus && !status, "unexpected encoder output status");

        DWORD length = 0;
        mf(storage->GetTotalLength(&length), "AVC length");
        check(length > 0 && length <= max_payload, "invalid AVC frame length");
        ComPtr<IMFMediaBuffer> avc;
        mf(storage->ConvertToContiguousBuffer(&avc), "contiguous AVC");
        BufferLock lock(avc.Get());
        check(lock.size == length, "invalid AVC buffer length");
        Bytes bytes(lock.bytes, lock.bytes + length);
        unsigned flags = nal_flags(bytes);
        check(flags & slice, "invalid Annex B video sample");
        key = (flags & idr) != 0;
        check((!force && frame_index_ != 1) || key, "encoder did not produce requested IDR");
        if (key && (flags & (sps | pps)) != (sps | pps)) {
            Bytes header = parameter_sets();
            check(header.size() + bytes.size() <= max_payload, "AVC with SPS/PPS exceeds frame bound");
            header.insert(header.end(), bytes.begin(), bytes.end());
            return header;
        }
        return bytes;
    }
};

uint64_t number(const char* argument, uint64_t minimum, uint64_t maximum) {
    std::string_view text(argument);
    uint64_t value = 0;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    check(result.ec == std::errc{} && result.ptr == text.data() + text.size()
          && value >= minimum && value <= maximum, "invalid argument");
    return value;
}

void run(Session& session, unsigned max_width, unsigned max_height, unsigned fps, unsigned bitrate) {
    check(SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2) != FALSE,
          "set physical screen coordinates");
    Desktop desktop;
    desktop.init(max_width, max_height);
    mf(CoInitializeEx(nullptr, COINIT_MULTITHREADED), "CoInitializeEx");
    mf(MFStartup(MF_VERSION, MFSTARTUP_LITE), "MFStartup");
    Encoder encoder;
    encoder.init(desktop.width, desktop.height, fps, bitrate);

    using Clock = std::chrono::steady_clock;
    const auto period = std::chrono::nanoseconds(1000000000 / fps);
    for (uint64_t sequence = 1;; ++sequence) {
        session.check_lease();
        const auto start = Clock::now();
        const auto capture_tick = GetTickCount64();
        bool key = false;
        const auto payload = encoder.encode(desktop, session, session.key_requested.exchange(false), key);
        check_desktop();
        const auto header = frame_header(uint32_t(payload.size()), sequence, capture_tick,
                                         uint16_t(desktop.width), uint16_t(desktop.height), key, desktop.bounds);
        session.write(header.data(), DWORD(header.size()));
        session.write(payload.data(), DWORD(payload.size()));
        std::this_thread::sleep_until(start + period);
    }
}
}  // namespace

int main(int argc, char** argv) {
    try {
        check(argc == 7, "usage: lanpilot-avc PARENT_HANDLE DEADLINE_TICK MAX_WIDTH MAX_HEIGHT FPS BITRATE");
        const auto parent = reinterpret_cast<HANDLE>(uintptr_t(number(argv[1], 1, UINTPTR_MAX - 1)));
        const auto deadline = number(argv[2], 1, UINT64_MAX);
        const auto width = unsigned(number(argv[3], 64, 1920));
        const auto height = unsigned(number(argv[4], 64, 1080));
        const auto fps = unsigned(number(argv[5], 1, 30));
        const auto bitrate = unsigned(number(argv[6], 100000, 20000000));
        static Session session{parent, GetStdHandle(STD_INPUT_HANDLE), GetStdHandle(STD_OUTPUT_HANDLE), deadline};
        check(GetFileType(session.commands) == FILE_TYPE_PIPE
            && GetFileType(session.output) == FILE_TYPE_PIPE, "private stdin/stdout pipes required");
        DWORD parent_session = 0, own_session = 0;
        const DWORD parent_pid = GetProcessId(parent);
        check(parent_pid && parent_pid != GetCurrentProcessId()
            && ProcessIdToSessionId(parent_pid, &parent_session)
            && ProcessIdToSessionId(GetCurrentProcessId(), &own_session)
            && parent_session == own_session && own_session != 0, "interactive worker process handle required");
        session.check_lease();
        check(valid_deadline(GetTickCount64(), deadline), "initial lease exceeds maximum");
        HANDLE watcher = CreateThread(nullptr, 0, watch_session, &session, 0, nullptr);
        check(watcher != nullptr, "start authorization watchdog");
        CloseHandle(watcher);
        run(session, width, height, fps, bitrate);
    } catch (const std::exception& error) {
        std::fprintf(stderr, "lanpilot-avc: %s\n", error.what());
        stop(failed);
    }
}
