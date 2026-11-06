using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;

namespace ClawVoice
{
    /// <summary>The Python side of SaySo: dictation (dictate.py: Whisper, hotkeys, wake phrase, agent) and the
    /// optional NPU model server (npu_server.py). Layout: &lt;app&gt;\*.py + .venv + models, &lt;app&gt;\app\SaySo.exe
    /// (here C:\AI\openvino-npu, installed: %LOCALAPPDATA%\Programs\SaySo). User data: %APPDATA%\SaySo.
    /// State shared with Python through files: listen_state.txt, gemma_state.txt, settings.json.</summary>
    public static class Stack
    {
        public static readonly string Dir = FindAppDir();
        public static readonly string UserDir = Environment.GetEnvironmentVariable("SAYSO_HOME") is { Length: > 0 } h
            ? h : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "SaySo");
        public static readonly string PythonW = Path.Combine(Dir, @".venv\Scripts\pythonw.exe");
        public static readonly string Python = Path.Combine(Dir, @".venv\Scripts\python.exe");
        public static readonly string ListenFile = Path.Combine(Dir, "listen_state.txt");
        public static readonly string GemmaFile = Path.Combine(Dir, "gemma_state.txt");
        public static readonly string SettingsFile = Path.Combine(UserDir, "settings.json");
        public static readonly string LocationFile = Path.Combine(Dir, "location.json");
        public static readonly string CommandsFile = Path.Combine(Dir, "commands.txt");
        public static readonly string DictateLog = Path.Combine(Dir, "dictate.log");
        public static readonly string UserCommandsDir = Path.Combine(UserDir, "commands");
        public static readonly string UserPluginsDir = Path.Combine(UserDir, "plugins");
        public static readonly string BuiltinPluginsDir = Path.Combine(Dir, "plugins");
        static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(1.5) };

        /// <summary>The folder above app\ (the exe's folder) that holds dictate.py; the old fixed path as a fallback.</summary>
        static string FindAppDir()
        {
            string exeDir = Path.GetDirectoryName(Environment.ProcessPath ?? "") ?? "";
            string up = Path.GetDirectoryName(exeDir) ?? "";
            foreach (string d in new[] { up, exeDir })
                if (d.Length > 0 && File.Exists(Path.Combine(d, "dictate.py"))) return d;
            return @"C:\AI\openvino-npu";
        }

        /// <summary>Runs sayso_cli.py and returns its JSON answer ({"error": ...} on failure).</summary>
        public static System.Text.Json.Nodes.JsonObject Cli(string args, string? stdin = null, int timeoutMs = 60000)
        {
            if (!PythonHealthy()) return new System.Text.Json.Nodes.JsonObject { ["error"] = "установка повреждена, запустите установщик ещё раз" };
            try
            {
                var psi = new ProcessStartInfo(Python, $"\"{Path.Combine(Dir, "sayso_cli.py")}\" {args}")
                {
                    WorkingDirectory = Dir, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
                    RedirectStandardInput = stdin != null, StandardOutputEncoding = System.Text.Encoding.UTF8,
                };
                psi.Environment["PYTHONIOENCODING"] = "utf-8";
                using var p = Process.Start(psi)!;
                if (stdin != null) { p.StandardInput.Write(stdin); p.StandardInput.Close(); }
                string text = p.StandardOutput.ReadToEnd();
                p.WaitForExit(timeoutMs);
                var last = text.Trim().Split('\n')[^1];
                return System.Text.Json.Nodes.JsonNode.Parse(last)!.AsObject();
            }
            catch (Exception e) { return new System.Text.Json.Nodes.JsonObject { ["error"] = e.Message }; }
        }

        public static string Quote(string s) => "\"" + s.Replace("\"", "") + "\"";

        /// <summary>pythonw processes running `script` (the venv launcher and its child: both).</summary>
        public static List<int> Pids(string script)
        {
            var pids = new List<int>();
            try
            {
                using var q = new ManagementObjectSearcher(
                    "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'");
                foreach (ManagementObject o in q.Get())
                    if ((o["CommandLine"] as string ?? "").Contains(script, StringComparison.OrdinalIgnoreCase))
                        pids.Add(Convert.ToInt32(o["ProcessId"]));
            }
            catch (Exception e) { Log.Write("process scan: " + e.Message); }
            return pids;
        }

        public static bool Running(string script) => Pids(script).Count > 0;

        static readonly Dictionary<int, string> CmdCache = new();

        /// <summary>Command lines of the running python processes. The process list is a cheap snapshot; WMI
        /// (~50 ms CPU per query) runs only when a python PID appears that is not cached yet.</summary>
        public static List<string> PythonCommandLines()
        {
            var pids = new HashSet<int>();
            foreach (string name in new[] { "pythonw", "python" })
                foreach (var p in Process.GetProcessesByName(name)) { pids.Add(p.Id); p.Dispose(); }
            lock (CmdCache)
            {
                foreach (int gone in new List<int>(CmdCache.Keys)) if (!pids.Contains(gone)) CmdCache.Remove(gone);
                if (!pids.IsSubsetOf(CmdCache.Keys))
                    try
                    {
                        using var q = new ManagementObjectSearcher(
                            "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'");
                        foreach (ManagementObject o in q.Get())
                            CmdCache[Convert.ToInt32(o["ProcessId"])] = o["CommandLine"] as string ?? "";
                    }
                    catch (Exception e) { Log.Write("process scan: " + e.Message); }
                var list = new List<string>();
                foreach (int pid in pids) if (CmdCache.TryGetValue(pid, out var c)) list.Add(c);
                return list;
            }
        }

        public static bool Has(List<string> cmdLines, string script) =>
            cmdLines.Exists(c => c.Contains(script, StringComparison.OrdinalIgnoreCase));

        /// <summary>The app's Python can start: pythonw.exe, pyvenv.cfg and the Python its "home" points at. A
        /// half-deleted venv makes every start pop the launcher's modal "No pyvenv.cfg file" box, and the
        /// supervisor restarting it every 10 s buried the desktop under them (Claw A8).</summary>
        public static bool PythonHealthy()
        {
            try
            {
                string cfg = Path.Combine(Dir, @".venv\pyvenv.cfg");
                if (!File.Exists(PythonW) || !File.Exists(cfg)) return false;
                foreach (string line in File.ReadAllLines(cfg))
                    if (line.StartsWith("home", StringComparison.OrdinalIgnoreCase) && line.Contains('='))
                        return File.Exists(Path.Combine(line[(line.IndexOf('=') + 1)..].Trim(), "python.exe"));
                return false;
            }
            catch { return false; }
        }

        static readonly Dictionary<string, Process> Started = new();  // to say how the previous run of a script ended

        public static void Start(string script)
        {
            if (Running(script)) return;
            if (!PythonHealthy()) { Log.Write($"not starting {script}: the app's Python is broken, run the installer again"); return; }
            if (Started.TryGetValue(script, out var old))
            {
                try { if (old.HasExited) Log.Write($"{script} had ended: {ExitText(old.ExitCode)}"); } catch { }
                old.Dispose();
            }
            Started[script] = Process.Start(new ProcessStartInfo(PythonW, $"\"{Path.Combine(Dir, script)}\"")
            { WorkingDirectory = Dir, UseShellExecute = false, CreateNoWindow = true })!;
            Log.Write("started " + script);
        }

        /// <summary>An exit code in words: a normal stop, the "no model" exit of dictate.py, a native crash (an NTSTATUS
        /// such as 0xC0000005: the stack is in the script's log, written by faulthandler) or a Python error.</summary>
        static string ExitText(int code) => code switch
        {
            0 => "exit 0 (normal)",
            1 => "exit 1 (Python error, the traceback is in its log)",
            3 => "exit 3 (no downloaded speech model for the device)",
            _ when (uint)code >= 0xC0000000 => $"exit 0x{(uint)code:X8} (crashed inside a native library, see its log)",
            _ => $"exit {code}",
        };

        public static void Stop(string script)
        {
            foreach (int pid in Pids(script))
                try { Process.GetProcessById(pid).Kill(); } catch { }
            if (Started.Remove(script, out var p)) p.Dispose();  // killed on purpose: not reported as a crash later
            Log.Write("stopped " + script);
        }

        public static bool Listening
        {
            get { try { return File.ReadAllText(ListenFile).Trim() != "0"; } catch { return true; } }
            set => File.WriteAllText(ListenFile, value ? "1" : "0");  // dictate.py applies it within 5 s
        }

        public static bool GemmaWanted
        {
            get { try { return File.ReadAllText(GemmaFile).Trim() == "on"; } catch { return false; } }  // unloaded by default
            set => File.WriteAllText(GemmaFile, value ? "on" : "off");
        }

        /// <summary>The Gemma server answers on :8081 (loaded on the NPU, not just started).</summary>
        public static bool GemmaReady()
        {
            try { return Http.GetAsync("http://127.0.0.1:8081/v1/models").Result.IsSuccessStatusCode; }
            catch { return false; }
        }

        public static void Open(string path)
        {
            try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
            catch (Exception e) { Log.Write("open: " + e.Message); }
        }

        /// <summary>Geocode a city with the same Python code the voice command uses ("я сейчас в …").</summary>
        public static string SetLocation(string city)
        {
            var psi = new ProcessStartInfo(Python, "-c \"import sys, net_skills; sys.stdout.reconfigure(encoding='utf-8'); " +
                "print(net_skills.set_location(sys.argv[1]))\" \"" + city.Replace("\"", "") + "\"")
            { WorkingDirectory = Dir, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
              StandardOutputEncoding = System.Text.Encoding.UTF8 };
            using var p = Process.Start(psi)!;
            string outp = p.StandardOutput.ReadToEnd().Trim();
            p.WaitForExit(20000);
            return outp;
        }
    }

    /// <summary>The elevated hotkey listener (hotkey_helper.py, logon task SaySoHotkeys). SaySo cannot kill an
    /// elevated process, so it asks it to leave over its ping port; running the task needs no elevation.</summary>
    static class Hotkeys
    {
        static readonly IPEndPoint Ping = new(IPAddress.Loopback, 47320);

        public static bool Alive()
        {
            try
            {
                using var u = new UdpClient(0, AddressFamily.InterNetwork) { Client = { ReceiveTimeout = 400 } };
                u.Send(System.Text.Encoding.ASCII.GetBytes("ping"), 4, Ping);
                var from = new IPEndPoint(IPAddress.Any, 0);
                return System.Text.Encoding.ASCII.GetString(u.Receive(ref from)) == "pong";
            }
            catch { return false; }
        }

        public static void Quit()
        {
            try
            {
                using var u = new UdpClient();
                u.Send(System.Text.Encoding.ASCII.GetBytes("quit"), 4, Ping);
                Log.Write("hotkey helper asked to quit");
            }
            catch (Exception e) { Log.Write("hotkey quit: " + e.Message); }
        }

        /// <summary>After "Выход" the helper is gone until the next logon: start its task again.</summary>
        public static void EnsureRunning()
        {
            if (Alive()) return;
            try
            {
                var psi = new ProcessStartInfo("schtasks.exe", "/run /tn SaySoHotkeys")
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                using var p = Process.Start(psi)!;
                p.WaitForExit(10000);
                Log.Write("hotkey helper not answering: started task SaySoHotkeys, exit " + p.ExitCode);
            }
            catch (Exception e) { Log.Write("hotkey task: " + e.Message); }
        }
    }

    static class Log
    {
        static readonly object Gate = new();
        public static readonly string FilePath = Path.Combine(Stack.Dir, "sayso.log");
        public static void Write(string msg)
        {
            try
            {
                lock (Gate)
                {
                    if (File.Exists(FilePath) && new FileInfo(FilePath).Length > 256 * 1024) File.Delete(FilePath);
                    File.AppendAllText(FilePath, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {msg}{Environment.NewLine}");
                }
            }
            catch { }
        }
    }
}
