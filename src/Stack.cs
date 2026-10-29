using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Management;
using System.Net.Http;

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

        public static void Start(string script)
        {
            if (Running(script)) return;
            Process.Start(new ProcessStartInfo(PythonW, $"\"{Path.Combine(Dir, script)}\"")
            { WorkingDirectory = Dir, UseShellExecute = false, CreateNoWindow = true });
            Log.Write("started " + script);
        }

        public static void Stop(string script)
        {
            foreach (int pid in Pids(script))
                try { Process.GetProcessById(pid).Kill(); } catch { }
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
