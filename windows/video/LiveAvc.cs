using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32.SafeHandles;

// The desktop worker owns authorization. Pipe I/O never runs on its input loop.
public sealed class LiveAvc : IDisposable {
    const int HeaderSize = 48, MaxPayload = 2 * 1024 * 1024;
    const uint ExtendedStartup = 0x00080000, NoWindow = 0x08000000;
    readonly object gate = new object();
    readonly SafeFileHandle process;
    readonly AutoResetEvent changed = new AutoResetEvent(false);
    const int MaxQueuedFrames = 8, MaxQueuedBytes = 4 * 1024 * 1024;
    readonly Queue<Frame> frames = new Queue<Frame>();
    int queuedBytes;
    bool stopped, failed, writerDone, keyRequested;
    ulong pendingDeadline, lastDeadline;
    const int HighProfile = 1, LowProfile = 2;
    int desiredProfile = HighProfile, sentProfile = HighProfile;

    public sealed class Frame {
        public ulong Sequence, CaptureTick;
        public int X, Y, Width, Height;
        public bool Idr;
        public byte[] Bytes;
    }

    public static LiveAvc Start(string path, ulong deadline) {
        IntPtr childIn = IntPtr.Zero, childOut = IntPtr.Zero, childErr = IntPtr.Zero;
        IntPtr parentHandle = IntPtr.Zero, attributes = IntPtr.Zero, handles = IntPtr.Zero;
        SafeFileHandle input = null, output = null, error = null, process = null;
        var info = new ProcessInfo();
        bool initialized = false, transferred = false;
        try {
            input = Pipe(false, out childIn);
            output = Pipe(true, out childOut);
            error = Pipe(true, out childErr);
            IntPtr self = GetCurrentProcess();
            Check(DuplicateHandle(self, self, self, out parentHandle, 0x00101000, true, 0));

            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            attributes = Marshal.AllocHGlobal(size);
            Check(InitializeProcThreadAttributeList(attributes, 1, 0, ref size));
            initialized = true;
            var inherit = new[] {childIn, childOut, childErr, parentHandle};
            handles = Marshal.AllocHGlobal(inherit.Length * IntPtr.Size);
            Marshal.Copy(inherit, 0, handles, inherit.Length);
            Check(UpdateProcThreadAttribute(attributes, 0, new IntPtr(0x00020002), handles,
                                            new IntPtr(inherit.Length * IntPtr.Size), IntPtr.Zero, IntPtr.Zero));
            var startup = new StartupEx();
            startup.Info.Size = Marshal.SizeOf(typeof(StartupEx));
            startup.Info.Flags = 0x100;
            startup.Info.Input = childIn;
            startup.Info.Output = childOut;
            startup.Info.Error = childErr;
            startup.Attributes = attributes;
            string command = String.Format(CultureInfo.InvariantCulture,
                "\"{0}\" {1} {2} 1280 720 20 3500000", path, parentHandle.ToInt64(), deadline);
            Check(CreateProcess(path, new System.Text.StringBuilder(command), IntPtr.Zero, IntPtr.Zero,
                                true, ExtendedStartup | NoWindow, IntPtr.Zero, null, ref startup, out info));
            process = new SafeFileHandle(info.Process, true);
            var helper = new LiveAvc(process, input, output, error, deadline);
            transferred = true;
            return helper;
        } finally {
            Close(childIn); Close(childOut); Close(childErr); Close(parentHandle); Close(info.Thread);
            if (initialized) DeleteProcThreadAttributeList(attributes);
            Marshal.FreeHGlobal(attributes);
            Marshal.FreeHGlobal(handles);
            if (!transferred) {
                if (process != null) {
                    TerminateProcess(info.Process, 1);
                    process.Dispose();
                }
                if (input != null) input.Dispose();
                if (output != null) output.Dispose();
                if (error != null) error.Dispose();
            }
        }
    }

    LiveAvc(SafeFileHandle process, SafeFileHandle input, SafeFileHandle output, SafeFileHandle error, ulong deadline) {
        this.process = process;
        lastDeadline = deadline;
        Background(() => ReadFrames(output));
        Background(() => WriteCommands(input));
        Background(() => DrainError(error));
    }

    static void Background(ThreadStart action) {
        new Thread(action) { IsBackground = true }.Start();
    }

    public void Maintain(ulong deadline, bool requestKey, int profile) {
        lock (gate) {
            if (stopped || failed || GetTickCount64() >= deadline || GetTickCount64() >= lastDeadline)
                throw new IOException("AVC helper unavailable or lease expired");
            // Shortening a lease also replaces any queued renewal.
            pendingDeadline = deadline != lastDeadline ? deadline : 0;
            keyRequested |= requestKey;
            desiredProfile = profile == LowProfile ? LowProfile : HighProfile;
            if (pendingDeadline != 0 || requestKey || desiredProfile != sentProfile) changed.Set();
        }
    }

    public Frame TakeFrame() {
        lock (gate) {
            if (failed) throw new IOException("AVC helper stopped producing frames");
            if (frames.Count == 0) return null;
            Frame frame = frames.Dequeue();
            queuedBytes -= frame.Bytes.Length;
            return frame;
        }
    }

    void ReadFrames(SafeFileHandle pipe) {
        try {
            using (pipe)
            using (var stream = new FileStream(pipe, FileAccess.Read)) {
                var header = new byte[HeaderSize];
                while (true) {
                    ReadExactly(stream, header, 0, HeaderSize);
                    uint length = BitConverter.ToUInt32(header, 4);
                    uint flags = BitConverter.ToUInt32(header, 28);
                    if (BitConverter.ToUInt32(header, 0) != 0x3141504c || length == 0 ||
                        length > MaxPayload || flags > 1)
                        throw new IOException("Invalid AVC frame header");
                    var frame = new Frame {
                        Sequence = BitConverter.ToUInt64(header, 8),
                        CaptureTick = BitConverter.ToUInt64(header, 16),
                        X = BitConverter.ToInt32(header, 32), Y = BitConverter.ToInt32(header, 36),
                        Width = BitConverter.ToInt32(header, 40), Height = BitConverter.ToInt32(header, 44),
                        Idr = flags == 1, Bytes = new byte[HeaderSize + (int)length]
                    };
                    Buffer.BlockCopy(header, 0, frame.Bytes, 0, HeaderSize);
                    ReadExactly(stream, frame.Bytes, HeaderSize, (int)length);
                    lock (gate) {
                        if (stopped) return;
                        // Keep the freshest bounded range. Gaps in the sequence
                        // force an IDR on the upload side before more P frames.
                        while (frames.Count >= MaxQueuedFrames ||
                               queuedBytes + frame.Bytes.Length > MaxQueuedBytes)
                            queuedBytes -= frames.Dequeue().Bytes.Length;
                        frames.Enqueue(frame);
                        queuedBytes += frame.Bytes.Length;
                    }
                }
            }
        } catch (IOException) {
            lock (gate) { failed = true; }
        }
    }

    void WriteCommands(SafeFileHandle pipe) {
        try {
            using (pipe)
            using (var stream = new FileStream(pipe, FileAccess.Write)) {
                while (true) {
                    changed.WaitOne();
                    ulong deadline;
                    bool requestKey;
                    int profileChange;
                    lock (gate) {
                        if (stopped) return;  // Closing stdin also stops the helper.
                        deadline = pendingDeadline;
                        requestKey = keyRequested;
                        profileChange = desiredProfile == sentProfile ? 0 : desiredProfile;
                        sentProfile = desiredProfile;
                        if (deadline != 0) lastDeadline = deadline;
                        pendingDeadline = 0;
                        keyRequested = false;
                    }
                    if (deadline != 0) Command(stream, (byte)'R', deadline);
                    if (profileChange != 0) Command(stream, (byte)'Q', (ulong)profileChange);
                    if (requestKey) Command(stream, (byte)'K', 0);
                }
            }
        } catch (IOException) {
            lock (gate) { failed = true; }
        } finally {
            lock (gate) {
                writerDone = true;
                changed.Dispose();
            }
        }
    }

    static void Command(Stream stream, byte opcode, ulong argument) {
        var command = new byte[9];
        command[0] = opcode;
        Buffer.BlockCopy(BitConverter.GetBytes(argument), 0, command, 1, 8);
        stream.Write(command, 0, command.Length);
        stream.Flush();
    }

    static void ReadExactly(Stream stream, byte[] buffer, int offset, int remaining) {
        while (remaining != 0) {
            int read = stream.Read(buffer, offset, remaining);
            if (read == 0) throw new EndOfStreamException();
            offset += read;
            remaining -= read;
        }
    }

    static void DrainError(SafeFileHandle pipe) {
        try {
            using (pipe)
            using (var stream = new StreamReader(new FileStream(pipe, FileAccess.Read))) {
                string line;
                while ((line = stream.ReadLine()) != null) Console.Error.WriteLine(line);
            }
        } catch (IOException) {}
    }

    public void Dispose() {
        lock (gate) {
            if (stopped) return;
            stopped = true;
            frames.Clear();
            queuedBytes = 0;
            if (!writerDone) changed.Set();
        }
        // Also interrupt a blocked helper write or a stuck encoder immediately.
        TerminateProcess(process.DangerousGetHandle(), 0);
        process.Dispose();
    }

    static SafeFileHandle Pipe(bool parentReads, out IntPtr child) {
        var security = new Security {Size = Marshal.SizeOf(typeof(Security)), Inherit = true};
        IntPtr read, write;
        Check(CreatePipe(out read, out write, ref security, 0));
        child = parentReads ? write : read;
        var parent = new SafeFileHandle(parentReads ? read : write, true);
        if (SetHandleInformation(parent, 1, 0)) return parent;
        int error = Marshal.GetLastWin32Error();
        parent.Dispose();
        throw new Win32Exception(error);
    }

    static void Check(bool success) {
        if (!success) throw new Win32Exception();
    }

    static void Close(IntPtr handle) {
        if (handle != IntPtr.Zero) CloseHandle(handle);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct Security { public int Size; public IntPtr Descriptor; public bool Inherit; }
    [StructLayout(LayoutKind.Sequential)]
    struct Startup {
        public int Size;
        public IntPtr Reserved, Desktop, Title;
        public int X, Y, Width, Height, Columns, Rows, Fill, Flags;
        public short Show, ReservedSize;
        public IntPtr ReservedBytes, Input, Output, Error;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct StartupEx { public Startup Info; public IntPtr Attributes; }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr Process, Thread; public uint ProcessId, ThreadId; }

    [DllImport("kernel32.dll")]
    public static extern ulong GetTickCount64();
    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr source, IntPtr targetProcess,
                                       out IntPtr target, uint access, bool inherit, uint options);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CreatePipe(out IntPtr read, out IntPtr write, ref Security security, uint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetHandleInformation(SafeFileHandle handle, uint mask, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, int flags, ref IntPtr size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, IntPtr attribute, IntPtr value,
                                                IntPtr size, IntPtr previous, IntPtr returned);
    [DllImport("kernel32.dll")]
    static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(string path, System.Text.StringBuilder command, IntPtr processSecurity,
                                    IntPtr threadSecurity, bool inherit, uint flags, IntPtr environment,
                                    string directory, ref StartupEx startup, out ProcessInfo info);
    [DllImport("kernel32.dll")]
    static extern bool TerminateProcess(IntPtr process, uint exitCode);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
}
