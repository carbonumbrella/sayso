using System;
using System.Diagnostics;
using System.IO;
using System.Security;
using System.Text;

namespace ClawVoice
{
    /// <summary>Autostart as a per-user logon task instead of the Run key. Windows' Xbox full screen experience
    /// defers Run-key apps until the first exit to the desktop; a logon task starts at sign-in in either mode.
    /// Registering a logon task for oneself needs no admin rights (schtasks /XML with the user's own trigger).</summary>
    static class LogonTask
    {
        public static bool Exists(string name) => Schtasks($"/Query /TN \"{name}\"") == 0;

        public static bool Create(string name, string exe, string args, string description)
        {
            string user = System.Security.Principal.WindowsIdentity.GetCurrent().Name;
            string x(string s) => SecurityElement.Escape(s) ?? "";
            string xml = $"""
                <?xml version="1.0" encoding="UTF-16"?>
                <Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
                  <RegistrationInfo><Description>{x(description)}</Description></RegistrationInfo>
                  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{x(user)}</UserId></LogonTrigger></Triggers>
                  <Principals><Principal id="Author"><UserId>{x(user)}</UserId><LogonType>InteractiveToken</LogonType>
                    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
                  <Settings>
                    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
                    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
                    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
                    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
                    <Priority>4</Priority>
                  </Settings>
                  <Actions Context="Author"><Exec><Command>{x(exe)}</Command><Arguments>{x(args)}</Arguments>
                    <WorkingDirectory>{x(Path.GetDirectoryName(exe) ?? "")}</WorkingDirectory></Exec></Actions>
                </Task>
                """;
            // (Priority 4 = normal: the task default 7 runs the app and its children below normal)
            string file = Path.Combine(Path.GetTempPath(), $"{name}-task.xml");
            try
            {
                File.WriteAllText(file, xml, Encoding.Unicode);
                return Schtasks($"/Create /TN \"{name}\" /XML \"{file}\" /F") == 0;
            }
            finally { try { File.Delete(file); } catch { } }
        }

        public static void Delete(string name)
        {
            if (Exists(name)) Schtasks($"/Delete /TN \"{name}\" /F");
        }

        static int Schtasks(string args)
        {
            try
            {
                using var p = Process.Start(new ProcessStartInfo("schtasks.exe", args)
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true })!;
                p.StandardOutput.ReadToEnd();
                string err = p.StandardError.ReadToEnd();
                p.WaitForExit(10000);
                if (p.ExitCode != 0 && !args.StartsWith("/Query")) Log.Write($"schtasks {args.Split(' ')[0]}: {err.Trim()}");
                return p.ExitCode;
            }
            catch (Exception e) { Log.Write("schtasks: " + e.Message); return -1; }
        }
    }
}
