// mic_capture.exe [--mode auto|speech|normal] [--device <name>]: the microphone for dictate.py through WASAPI shared mode
// with an audio category, so the driver's own processing can run. Speech mode on the MSI Claw 8 AI+ (Intel Smart
// Sound mic array, intelliGo effects) removes what the speakers play, music and the assistant's voice alike, and keeps
// the user's voice: over music dictation went from 21% to 4% word errors. auto = speech when the endpoint lists
// the SPEECH processing mode, else normal. sounddevice cannot set a category, hence this helper.
// Output: one text line "<rate> <channels> <mode> <endpoint name>" (or "ERR <message>"), then float32 interleaved
// samples on stdout. Exits when stdin closes, so it never outlives dictate.py.
// Build: csc /nologo /optimize+ /platform:x64 /out:mic_capture.exe MicCapture.cs (in-box .NET Framework compiler).
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator { }

[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator
{
    int EnumAudioEndpoints(int flow, int mask, out IntPtr coll);
    int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice d);
    int GetDevice([MarshalAs(UnmanagedType.LPWStr)] string id, out IMMDevice d);
}

[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice
{
    int Activate(ref Guid id, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o);
    int OpenPropertyStore(int access, out IntPtr store);
    int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
}

[StructLayout(LayoutKind.Sequential)]
struct AudioClientProperties { public int cbSize; public int bIsOffload; public int eCategory; public int Options; }

[Guid("726778CD-F60A-4EDA-82DE-E47610CD78AA"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioClient2
{
    int Initialize(int share, int flags, long dur, long period, IntPtr fmt, IntPtr session);
    int GetBufferSize(out int frames);
    int GetStreamLatency(out long l);
    int GetCurrentPadding(out int p);
    int IsFormatSupported(int share, IntPtr fmt, out IntPtr closest);
    int GetMixFormat(out IntPtr fmt);
    int GetDevicePeriod(out long def, out long min);
    int Start();
    int Stop();
    int Reset();
    int SetEventHandle(IntPtr h);
    int GetService(ref Guid id, [MarshalAs(UnmanagedType.IUnknown)] out object o);
    int IsOffloadCapable(int cat, out int capable);
    int SetClientProperties(ref AudioClientProperties p);
}

[Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioCaptureClient
{
    int GetBuffer(out IntPtr data, out int frames, out int flags, out long pos, out long qpc);
    int ReleaseBuffer(int frames);
    int GetNextPacketSize(out int frames);
}

static class MicCapture
{
    const string Endpoints = @"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture";
    const string SpeechMode = "{FC1CFC9B-B9D6-4CFA-B5E0-4BB2166878B2}";
    const string MfxModes = "{d3993a3f-99c2-4402-b5ec-a92a0367664b},6";

    static void Check(int hr, string what) { if (hr < 0) throw new Exception(what + " failed 0x" + hr.ToString("X8")); }

    // the name Windows shows ("Набор микрофонов (Технология Intel® Smart Sound ...)") and the endpoint's registry key
    static string Name(RegistryKey ep)
    {
        using (var p = ep.OpenSubKey("Properties"))
        {
            if (p == null) return "";
            string n = p.GetValue("{a45c254e-df1c-4efd-8020-67d146a850e0},2") as string ?? "";
            string d = p.GetValue("{b3f8fa53-0004-438e-9003-51a46e139bfc},6") as string ?? "";
            return d.Length > 0 ? n + " (" + d + ")" : n;
        }
    }

    /// <summary>The endpoint whose name starts with want (the settings keep MME names, cut to 31 characters).</summary>
    static string FindId(string want)
    {
        if (string.IsNullOrEmpty(want)) return null;
        using (var root = Registry.LocalMachine.OpenSubKey(Endpoints))
        {
            if (root == null) return null;
            foreach (string guid in root.GetSubKeyNames())
                using (var ep = root.OpenSubKey(guid))
                {
                    if (ep == null || !(ep.GetValue("DeviceState") is int) || (int)ep.GetValue("DeviceState") != 1) continue;
                    string n = Name(ep);
                    if (n.Length > 0 && (n.StartsWith(want, StringComparison.OrdinalIgnoreCase) || want.StartsWith(n, StringComparison.OrdinalIgnoreCase)))
                        return "{0.0.1.00000000}." + guid;
                }
        }
        return null;
    }

    static RegistryKey EndpointKey(string id)
    {
        int dot = id.LastIndexOf('.');
        return Registry.LocalMachine.OpenSubKey(Endpoints + "\\" + id.Substring(dot + 1));
    }

    static bool HasSpeechMode(RegistryKey ep)
    {
        using (var fx = ep == null ? null : ep.OpenSubKey("FxProperties"))
        {
            var modes = fx == null ? null : fx.GetValue(MfxModes) as string[];
            if (modes == null) return false;
            foreach (string m in modes) if (string.Equals(m, SpeechMode, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }
    }

    [STAThread]  // in an MTA the capture buffer came back stale: the same packet again and again
    static int Main(string[] a)
    {
        string mode = "auto", device = "";
        for (int i = 0; i + 1 < a.Length; i += 2)
        {
            if (a[i] == "--mode") mode = a[i + 1];
            else if (a[i] == "--device") device = a[i + 1];
        }
        var stdout = Console.OpenStandardOutput();
        try
        {
            var en = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            IMMDevice dev = null;
            string id = FindId(device);
            if (id == null || en.GetDevice(id, out dev) < 0)
            {
                Check(en.GetDefaultAudioEndpoint(1, 0, out dev), "default capture");
                Check(dev.GetId(out id), "GetId");
            }
            string name;
            using (var ep = EndpointKey(id))
            {
                name = ep == null ? id : Name(ep);
                if (mode == "auto") mode = HasSpeechMode(ep) ? "speech" : "normal";
            }
            Guid iid = typeof(IAudioClient2).GUID;
            object o;
            Check(dev.Activate(ref iid, 23, IntPtr.Zero, out o), "activate");
            var ac = (IAudioClient2)o;
            var props = new AudioClientProperties { cbSize = 16, eCategory = mode == "speech" ? 9 : 0 };  // 9 = Speech
            Check(ac.SetClientProperties(ref props), "SetClientProperties");
            IntPtr fmt;
            Check(ac.GetMixFormat(out fmt), "GetMixFormat");
            short ch = Marshal.ReadInt16(fmt, 2), bits = Marshal.ReadInt16(fmt, 14);
            int rate = Marshal.ReadInt32(fmt, 4);
            if (bits != 32) throw new Exception("mix format is not float32 (" + bits + " bits)");
            // AUTOCONVERTPCM | SRC_DEFAULT_QUALITY; without them the buffer did not refresh either
            Check(ac.Initialize(0, unchecked((int)0x88000000), 2000000, 0, fmt, IntPtr.Zero), "Initialize");
            Guid cid = typeof(IAudioCaptureClient).GUID;
            Check(ac.GetService(ref cid, out o), "GetService");
            var cap = (IAudioCaptureClient)o;

            var head = System.Text.Encoding.UTF8.GetBytes(rate + " " + ch + " " + mode + " " + name + "\n");
            stdout.Write(head, 0, head.Length);
            stdout.Flush();
            bool run = true;
            new Thread(() => { try { Console.OpenStandardInput().Read(new byte[1], 0, 1); } catch { } run = false; }) { IsBackground = true }.Start();
            Check(ac.Start(), "Start");
            var buf = new byte[0];
            while (run)
            {
                int next;
                Check(cap.GetNextPacketSize(out next), "GetNextPacketSize");  // fails when the device goes away
                if (next == 0) { Thread.Sleep(5); continue; }
                IntPtr data; int frames, flags; long pos, qpc;
                Check(cap.GetBuffer(out data, out frames, out flags, out pos, out qpc), "GetBuffer");
                int bytes = frames * ch * 4;
                if (buf.Length < bytes) buf = new byte[bytes];
                if ((flags & 2) != 0) Array.Clear(buf, 0, bytes); else Marshal.Copy(data, buf, 0, bytes);  // 2 = silent
                cap.ReleaseBuffer(frames);
                stdout.Write(buf, 0, bytes);
                stdout.Flush();
            }
            ac.Stop();
            return 0;
        }
        catch (IOException) { return 0; }  // dictate.py closed the pipe
        catch (Exception e)
        {
            try
            {
                var msg = System.Text.Encoding.UTF8.GetBytes("ERR " + e.Message.Replace("\n", " ") + "\n");
                stdout.Write(msg, 0, msg.Length);
                stdout.Flush();
            }
            catch { }
            return 1;
        }
    }
}
