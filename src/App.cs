// SaySo: tray app of the voice assistant (was ClawVoice). Supervises the Python stack (dictate.py, npu_server.py),
// shows the on-screen messages (UDP 47330, was ClawOverlay.exe) and the confirmation dialog (UDP 47331, was
// confirm.ps1), and gives one menu for dictation, wake listening, Gemma load/unload and settings.
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Microsoft.Win32;
using Forms = System.Windows.Forms;

namespace ClawVoice
{
    public sealed class App : Application
    {
        const string Dictate = "dictate.py", Server = "npu_server.py";
        const int OverlayPort = 47330, ConfirmPort = 47331;
        public const string QuitEvent = "Local\\SaySo.Quit", ShowEvent = "Local\\SaySo.Show";

        Forms.NotifyIcon _tray = null!;
        OverlayWindow _overlay = null!;
        SettingsWindow? _settings;
        readonly DispatcherTimer _watch = new() { Interval = TimeSpan.FromSeconds(10) };  // one WMI scan per tick
        bool _dictationWanted = true;
        public bool DictationWanted => _dictationWanted;
        public static volatile bool DictationRunning, GemmaLoaded;  // last supervisor scan, for the Settings window
        int _dictateRestarts;
        DateTime _serverStartedAt = DateTime.MinValue;
        static volatile string _status = "";
        bool _brokenShown;
        Mutex? _overlayMutex;

        [STAThread]
        public static void Main(string[] args)
        {
            if (Array.IndexOf(args, "--quit") >= 0)
            {
                if (EventWaitHandle.TryOpenExisting(QuitEvent, out var q)) q.Set();
                return;
            }
            int ri = Array.IndexOf(args, "--render");  // --render <tab> <png>: a settings page to PNG, no window
            if (ri >= 0 && ri + 2 < args.Length) { RenderPage(args[ri + 1], args[ri + 2]); return; }
            int ai = Array.IndexOf(args, "--set-autostart");  // --set-autostart on|off: the installer, no window
            if (ai >= 0 && ai + 1 < args.Length) { Autostart.Apply(args[ai + 1] == "on"); return; }
            string? command = Command(args);  // --dictation on|off, --gemma on|off (Desktop bats)
            using var mutex = new Mutex(true, "Local\\SaySo", out bool first);
            if (!first)  // already running: forward the command, or open its settings
            {
                if (Array.IndexOf(args, Autostart.Arg) >= 0) return;
                Responsive.AllowForeground();
                if (EventWaitHandle.TryOpenExisting(command != null ? "Local\\SaySo." + command : ShowEvent, out var s)) s.Set();
                return;
            }
            var app = new App { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            app.Resources.MergedDictionaries.Add(new ResourceDictionary { Source = new Uri("pack://application:,,,/SaySo;component/Theme.xaml") });
            Theme.Apply(app.Resources);
            SystemEvents.UserPreferenceChanged += (_, _) => app.Dispatcher.BeginInvoke(() => Theme.Apply(app.Resources));
            app.Startup += (_, _) =>
            {
                app.Start(Array.IndexOf(args, "--settings") >= 0);
                if (command != null) app.Exec(command);
            };
            app.Run();
        }

        /// <summary>Visual check without showing anything: lays out one settings tab (or the trainer) offscreen
        /// and saves it as a PNG.</summary>
        static void RenderPage(string tab, string png)
        {
            var app = new App { ShutdownMode = ShutdownMode.OnExplicitShutdown };
            app.Resources.MergedDictionaries.Add(new ResourceDictionary { Source = new Uri("pack://application:,,,/SaySo;component/Theme.xaml") });
            Theme.Apply(app.Resources);
            Window w;
            bool narrow = tab.EndsWith(":narrow");  // "main:narrow": the portrait Claw layout, 690 DIP wide
            tab = tab.Replace(":narrow", "");
            if (tab == "trainer") w = new TrainerWindow();
            else if (tab == "wizard") w = new WizardWindow();
            else
            {
                var s = new SettingsWindow(); s.Reload(); s.ShowTab(tab); w = s;
                if (narrow) { Responsive.Preview(s, true, s.TabsGrid); s.Width = 690; }
            }
            var root = (FrameworkElement)w.Content;
            w.Content = null;
            var holder = new Border { Background = System.Windows.Media.Brushes.DimGray, Child = root, Width = w.Width };
            holder.SetResourceReference(System.Windows.Documents.TextElement.ForegroundProperty, "Fg");
            holder.Measure(new System.Windows.Size(w.Width, double.PositiveInfinity));
            holder.Arrange(new System.Windows.Rect(holder.DesiredSize));
            holder.UpdateLayout();
            var bmp = new System.Windows.Media.Imaging.RenderTargetBitmap((int)holder.ActualWidth, (int)holder.ActualHeight, 96, 96,
                                                                          System.Windows.Media.PixelFormats.Pbgra32);
            bmp.Render(holder);
            var enc = new System.Windows.Media.Imaging.PngBitmapEncoder();
            enc.Frames.Add(System.Windows.Media.Imaging.BitmapFrame.Create(bmp));
            using var fs = File.Create(png);
            enc.Save(fs);
        }

        void Start(bool showSettings)
        {
            // hold the old overlay's mutex: dictate.py then never launches ClawOverlay.exe next to us
            _overlayMutex = new Mutex(true, "Local\\ClawOverlay", out bool ownOverlay);
            if (!ownOverlay) Log.Write("ClawOverlay.exe is running: stop it to free UDP 47330");
            _overlay = new OverlayWindow();
            _overlay.Prewarm();
            new Thread(ListenOverlay) { IsBackground = true, Name = "overlay-udp" }.Start();
            new Thread(ListenConfirm) { IsBackground = true, Name = "confirm-udp" }.Start();

            _tray = new Forms.NotifyIcon { Icon = MakeIcon(), Text = "SaySo", Visible = true };
            _tray.MouseClick += (_, e) => { if (e.Button == Forms.MouseButtons.Left) ShowSettings(); };
            _tray.ContextMenuStrip = BuildMenu();

            var show = new EventWaitHandle(false, EventResetMode.AutoReset, ShowEvent);
            new Thread(() => { while (show.WaitOne()) Dispatcher.Invoke(ShowSettings); }) { IsBackground = true }.Start();
            var quit = new EventWaitHandle(false, EventResetMode.AutoReset, QuitEvent);
            new Thread(() =>
            {
                quit.WaitOne();
                Dispatcher.BeginInvoke(() => Exit(stopStack: false));
                Thread.Sleep(2000);  // a busy or stuck UI thread must not keep the old exe alive during an update
                Environment.Exit(0);
            }) { IsBackground = true }.Start();

            foreach (string c in Commands)
            {
                var ev = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\SaySo." + c);
                string cmd = c;
                new Thread(() => { while (ev.WaitOne()) Exec(cmd); }) { IsBackground = true }.Start();
            }
            _watch.Tick += (_, _) => Task.Run(Supervise);
            _watch.Start();
            Task.Run(Supervise);
            Log.Write("SaySo started");
            Task.Run(Autostart.Migrate);  // schtasks takes ~0.2 s
            if (showSettings) ShowSettings();
            if (SettingsWindow.Read()["setup_done"] is null) new WizardWindow().Show();  // first start after installing
        }

        // ---------------- supervisor: keeps the Python stack in the wanted state

        void Supervise()
        {
            try
            {
                if (!Stack.PythonHealthy())  // starting it would only pop the launcher's error box again and again
                {
                    _status = "Установка повреждена: запусти установщик SaySo ещё раз";
                    if (!_brokenShown)
                    {
                        _brokenShown = true;
                        Log.Write("the app's Python is broken: nothing started");
                        Dispatcher.Invoke(() => _tray.ShowBalloonTip(15000, "SaySo",
                            "Установка повреждена. Запусти установщик SaySo ещё раз, он всё починит.", Forms.ToolTipIcon.Error));
                    }
                    return;
                }
                var py = Stack.PythonCommandLines();
                bool dictate = Stack.Has(py, Dictate);
                if (_dictationWanted && !dictate)
                {
                    if (_dictateRestarts++ < 5) Stack.Start(Dictate);  // crash loop guard: 5 tries, then the menu
                    else if (_dictateRestarts == 7) Log.Write("dictation keeps failing: restart it from the menu");
                }
                else if (dictate) _dictateRestarts = 0;

                bool server = Stack.Has(py, Server);
                // Gemma: start when wanted (replaces the Startup shortcut); give a fresh start 3 min to load
                if (Stack.GemmaWanted && !server && DateTime.Now - _serverStartedAt > TimeSpan.FromMinutes(3))
                {
                    _serverStartedAt = DateTime.Now;
                    Stack.Start(Server);
                }
                bool ready = server && Stack.GemmaReady();
                DictationRunning = dictate; GemmaLoaded = ready;
                _status = $"Диктовка: {(dictate ? "работает" : _dictationWanted ? "запускается" : "выключена")} · " +
                          $"слушание: {(Stack.Listening ? "вкл" : "выкл")} · " +
                          $"Gemma: {(ready ? "загружена" : Stack.GemmaWanted ? "загружается" : "выгружена")}";
            }
            catch (Exception e) { Log.Write("supervise: " + e.Message); }
        }

        public static string StatusLine() => _status;

        static readonly string[] Commands = { "dictation-on", "dictation-off", "gemma-on", "gemma-off" };

        static string? Command(string[] args)
        {
            for (int i = 0; i + 1 < args.Length; i++)
                if (args[i] is "--dictation" or "--gemma" && args[i + 1] is "on" or "off")
                    return args[i].TrimStart('-') + "-" + args[i + 1];
            return null;
        }

        void Exec(string command) => Task.Run(() =>
        {
            Log.Write("command " + command);
            if (command.StartsWith("dictation")) SetDictation(command.EndsWith("on"));
            else SetGemma(command.EndsWith("on"));
        });

        public void SetDictation(bool on)
        {
            _dictationWanted = on;
            _dictateRestarts = 0;
            if (on) Stack.Start(Dictate); else Stack.Stop(Dictate);
            Supervise();
        }

        System.Threading.Timer? _restart;

        public void RestartDictationSoon()
        {
            _restart?.Dispose();
            _restart = new System.Threading.Timer(_ => { if (_dictationWanted) { Stack.Stop(Dictate); Thread.Sleep(800); Stack.Start(Dictate); } },
                                                  null, 1500, Timeout.Infinite);
        }

        TrainerWindow? _trainer;

        public void ShowTrainer()
        {
            if (_trainer == null || !_trainer.IsLoaded) _trainer = new TrainerWindow();
            _trainer.Show();
            _trainer.Activate();
        }

        public void SetGemma(bool on)
        {
            Stack.GemmaWanted = on;
            if (on) { _serverStartedAt = DateTime.Now; Stack.Start(Server); }
            else Stack.Stop(Server);
            Dispatcher.BeginInvoke(() => _overlay.ShowMessage(new Msg { Kind = "info", Title = "Gemma",
                Text = on ? "Загружаю, это секунд десять" : "Выгружена, память свободна. Простые команды работают" }));
            Supervise();
        }

        // ---------------- tray menu

        Forms.ContextMenuStrip BuildMenu()
        {
            var m = new Forms.ContextMenuStrip { Renderer = new MenuRenderer(), ShowImageMargin = false, ShowCheckMargin = true,
                Font = new Font("Segoe UI", 10f) };
            var status = new Forms.ToolStripMenuItem { Enabled = false };
            var dictation = new Forms.ToolStripMenuItem("Диктовка и голосовые команды");
            dictation.Click += (_, _) => Task.Run(() => SetDictation(!_dictationWanted));
            var restart = new Forms.ToolStripMenuItem("Перезапустить диктовку");
            restart.Click += (_, _) => Task.Run(() =>
            {
                _dictationWanted = true; _dictateRestarts = 0;
                Stack.Stop(Dictate); Thread.Sleep(800); Stack.Start(Dictate); Supervise();
            });
            var listen = new Forms.ToolStripMenuItem("Откликаться на имя");
            listen.Click += (_, _) => { Stack.Listening = !Stack.Listening; Task.Run(Supervise); };
            var gemma = new Forms.ToolStripMenuItem("Gemma загружена");
            gemma.Click += (_, _) => Task.Run(() => SetGemma(!Stack.GemmaWanted));
            m.Items.Add(status);
            m.Items.Add(new Forms.ToolStripSeparator());
            m.Items.Add(dictation);
            m.Items.Add(listen);
            m.Items.Add(gemma);
            m.Items.Add(new Forms.ToolStripSeparator());
            m.Items.Add("Настройки…", null, (_, _) => ShowSettings());
            m.Items.Add("Подсветка…", null, (_, _) => OpenAmbilight());
            m.Items.Add("Список команд", null, (_, _) => ShowCommands());
            m.Items.Add("Журнал диктовки", null, (_, _) => Stack.Open(Stack.DictateLog));
            m.Items.Add(restart);
            m.Items.Add(new Forms.ToolStripSeparator());
            m.Items.Add("Выход (остановить помощника)", null, (_, _) => Exit(stopStack: true));
            m.Opening += (_, _) =>
            {
                status.Text = string.IsNullOrEmpty(_status) ? "Проверяю…" : _status.Replace(" · ", "\n");
                dictation.Checked = _dictationWanted;
                listen.Checked = Stack.Listening;
                gemma.Checked = Stack.GemmaWanted;
            };
            return m;
        }

        void ShowSettings()
        {
            _settings ??= new SettingsWindow();
            _settings.Reload();
            Responsive.BringToFront(_settings);
        }

        public void ShowCommands()
        {
            string text;
            try { text = File.ReadAllText(Stack.CommandsFile); }
            catch { text = "Список появится после первого запуска диктовки."; }
            var w = new Window
            {
                Title = "Команды", Icon = System.Windows.Media.Imaging.BitmapFrame.Create(new Uri("pack://application:,,,/app.ico")), Width = 640, SizeToContent = SizeToContent.Height, ResizeMode = ResizeMode.NoResize,
                WindowStyle = WindowStyle.None, AllowsTransparency = true, Background = System.Windows.Media.Brushes.Transparent,
                WindowStartupLocation = WindowStartupLocation.CenterScreen,
                FontFamily = new System.Windows.Media.FontFamily("Segoe UI Variable Text, Segoe UI"), FontSize = 14,
            };
            w.SetResourceReference(Control.ForegroundProperty, "Fg");
            var close = new Button { Style = (Style)FindResource("Close"), HorizontalAlignment = HorizontalAlignment.Right };
            close.Click += (_, _) => w.Close();
            var body = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, LineHeight = 24, Margin = new Thickness(0, 6, 0, 0) };
            var panel = new StackPanel { Margin = new Thickness(22, 16, 22, 20) };
            panel.Children.Add(close);
            panel.Children.Add(body);
            w.Content = new Border { Style = (Style)FindResource("Frame"), Child = panel };
            w.MouseLeftButtonDown += (_, _) => { try { w.DragMove(); } catch { } };
            w.KeyDown += (_, e) => { if (e.Key == System.Windows.Input.Key.Escape) w.Close(); };
            w.Show();
            w.Activate();
        }

        public static void OpenAmbilight()
        {
            string exe = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                @"Programs\ClawAmbilight\ClawAmbilight.exe");
            if (File.Exists(exe)) Stack.Open(exe);  // a second start of ClawAmbilight shows its window
        }

        void Exit(bool stopStack)
        {
            _watch.Stop();
            if (stopStack) { Stack.Stop(Dictate); Stack.Stop(Server); }
            _tray.Visible = false;
            _tray.Dispose();
            Shutdown();
        }

        // ---------------- UDP: overlay messages and confirmation requests

        void ListenOverlay()
        {
            try
            {
                using var udp = new UdpClient(new IPEndPoint(IPAddress.Loopback, OverlayPort));
                var any = new IPEndPoint(IPAddress.Any, 0);
                var opts = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
                while (true)
                {
                    try
                    {
                        var msg = JsonSerializer.Deserialize<Msg>(Encoding.UTF8.GetString(udp.Receive(ref any)), opts);
                        if (msg != null) Dispatcher.BeginInvoke(() => _overlay.ShowMessage(msg));
                    }
                    catch (SocketException) { throw; }
                    catch { /* bad datagram */ }
                }
            }
            catch (Exception e) { Log.Write("overlay port: " + e.Message); }
        }

        /// <summary>gemma_agent.confirm(): JSON request, we answer {"ack":1} at once (so it does not fall back
        /// to confirm.ps1), then {"yes":true|false} when the dialog closes.</summary>
        void ListenConfirm()
        {
            try
            {
                using var udp = new UdpClient(new IPEndPoint(IPAddress.Loopback, ConfirmPort));
                var opts = new JsonSerializerOptions { PropertyNameCaseInsensitive = true };
                while (true)
                {
                    var from = new IPEndPoint(IPAddress.Any, 0);
                    byte[] data;
                    try { data = udp.Receive(ref from); } catch (SocketException) { throw; } catch { continue; }
                    ConfirmRequest? req;
                    try { req = JsonSerializer.Deserialize<ConfirmRequest>(Encoding.UTF8.GetString(data), opts); } catch { continue; }
                    if (req == null) continue;
                    var reply = from;
                    udp.Send(Encoding.UTF8.GetBytes("{\"ack\":1}"), reply);
                    Dispatcher.BeginInvoke(() =>
                    {
                        var w = new ConfirmWindow(req);
                        w.Closed += (_, _) =>
                        {
                            try { udp.Send(Encoding.UTF8.GetBytes(w.Yes ? "{\"yes\":true}" : "{\"yes\":false}"), reply); }
                            catch (Exception e) { Log.Write("confirm reply: " + e.Message); }
                            Log.Write($"confirm '{req.Title}': {(w.Yes ? "yes" : "no")}");
                        };
                        w.Show();
                    });
                }
            }
            catch (Exception e) { Log.Write("confirm port: " + e.Message); }
        }

        // ---------------- icon: a mic in the accent color

        static Icon MakeIcon()
        {
            try  // the exe's own app.ico (same picture as the taskbar), drawn fallback below
            {
                var own = Icon.ExtractAssociatedIcon(Environment.ProcessPath!);
                if (own != null) return new Icon(own, Forms.SystemInformation.SmallIconSize);
            }
            catch { }
            using var bmp = new Bitmap(32, 32);
            using (var g = Graphics.FromImage(bmp))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                using var bg = new SolidBrush(Color.FromArgb(62, 123, 250));
                g.FillEllipse(bg, 1, 1, 30, 30);
                using var w = new SolidBrush(Color.White);
                using var pen = new Pen(Color.White, 2.4f);
                g.FillRectangle(w, 13, 7, 6, 11);
                g.FillEllipse(w, 13, 4, 6, 6);
                g.FillEllipse(w, 13, 15, 6, 6);
                g.DrawArc(pen, 9, 9, 14, 13, 0, 180);
                g.DrawLine(pen, 16, 22, 16, 26);
                g.DrawLine(pen, 12, 26, 20, 26);
            }
            return Icon.FromHandle(bmp.GetHicon());
        }
    }

    /// <summary>Rounded, theme-colored tray menu (the stock WinForms menu looks like Windows 7).</summary>
    sealed class MenuRenderer : Forms.ToolStripProfessionalRenderer
    {
        static Color C(string hex) => ColorTranslator.FromHtml(hex);
        static bool L => Theme.Light;
        static Color Bg => L ? C("#F7F7F8") : C("#202024");
        static Color Fg => L ? C("#1B1B1F") : C("#F3F3F5");
        static Color Sub => L ? C("#6B6B73") : C("#A4A4AE");
        static Color Hover => L ? C("#E8E8ED") : C("#34343B");

        public MenuRenderer() : base(new Colors()) { RoundedEdges = true; }

        protected override void OnRenderToolStripBackground(Forms.ToolStripRenderEventArgs e)
        {
            using var b = new SolidBrush(Bg);
            e.Graphics.FillRectangle(b, e.AffectedBounds);
        }
        protected override void OnRenderToolStripBorder(Forms.ToolStripRenderEventArgs e)
        {
            using var p = new Pen(L ? C("#E3E3E8") : C("#38383F"));
            e.Graphics.DrawRectangle(p, 0, 0, e.AffectedBounds.Width - 1, e.AffectedBounds.Height - 1);
        }
        protected override void OnRenderMenuItemBackground(Forms.ToolStripItemRenderEventArgs e)
        {
            var r = new Rectangle(4, 1, e.Item.Width - 8, e.Item.Height - 2);
            using var b = new SolidBrush(e.Item.Selected && e.Item.Enabled ? Hover : Bg);
            using var path = Round(r, 6);
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            e.Graphics.FillPath(b, path);
        }
        protected override void OnRenderItemText(Forms.ToolStripItemTextRenderEventArgs e)
        {
            e.TextColor = e.Item.Enabled ? Fg : Sub;
            base.OnRenderItemText(e);
        }
        protected override void OnRenderItemCheck(Forms.ToolStripItemImageRenderEventArgs e)
        {
            var r = e.ImageRectangle;
            using var p = new Pen(C("#3E7BFA"), 2f);
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            e.Graphics.DrawLines(p, new[] { new System.Drawing.Point(r.Left + 3, r.Top + r.Height / 2), new System.Drawing.Point(r.Left + r.Width / 2 - 1, r.Bottom - 4),
                                            new System.Drawing.Point(r.Right - 3, r.Top + 4) });
        }
        protected override void OnRenderSeparator(Forms.ToolStripSeparatorRenderEventArgs e)
        {
            using var p = new Pen(L ? C("#E3E3E8") : C("#38383F"));
            int y = e.Item.Height / 2;
            e.Graphics.DrawLine(p, 10, y, e.Item.Width - 10, y);
        }
        static GraphicsPath Round(Rectangle r, int rad)
        {
            var g = new GraphicsPath();
            g.AddArc(r.X, r.Y, rad * 2, rad * 2, 180, 90);
            g.AddArc(r.Right - rad * 2, r.Y, rad * 2, rad * 2, 270, 90);
            g.AddArc(r.Right - rad * 2, r.Bottom - rad * 2, rad * 2, rad * 2, 0, 90);
            g.AddArc(r.X, r.Bottom - rad * 2, rad * 2, rad * 2, 90, 90);
            g.CloseFigure();
            return g;
        }
        sealed class Colors : Forms.ProfessionalColorTable
        {
            public override Color ToolStripDropDownBackground => Bg;
            public override Color ImageMarginGradientBegin => Bg;
            public override Color ImageMarginGradientMiddle => Bg;
            public override Color ImageMarginGradientEnd => Bg;
        }
    }

    /// <summary>Autostart = the logon task "SaySo" (see LogonTask: the Run key waits for the desktop in Xbox mode).
    /// The Run key is only a fallback when the task cannot be created, and old Run entries are migrated.</summary>
    static class Autostart
    {
        const string Key = @"Software\Microsoft\Windows\CurrentVersion\Run", Name = "SaySo";
        public const string Arg = "--autostart";  // a second copy started by it stays silent (no settings window)

        static bool InRun()
        {
            using var k = Registry.CurrentUser.OpenSubKey(Key);
            return k?.GetValue(Name) != null || k?.GetValue("ClawVoice") != null;
        }

        public static bool IsSet() => LogonTask.Exists(Name) || InRun();

        /// <summary>At every start: an old Run entry (also the ClawVoice one) becomes the task; an existing task is
        /// re-created so it follows the exe if the app moved.</summary>
        public static void Migrate()
        {
            try { if (IsSet()) Apply(true); }
            catch (Exception e) { Log.Write("autostart migrate: " + e.Message); }
        }

        public static void Apply(bool on)
        {
            try
            {
                bool task = on && LogonTask.Create(Name, Environment.ProcessPath!, Arg, "SaySo voice assistant (tray)");
                if (on && !task) Log.Write("autostart: logon task failed, using the Run key");
                if (!task) LogonTask.Delete(Name);
                using var k = Registry.CurrentUser.OpenSubKey(Key, true);
                if (k == null) return;
                if (on && !task) k.SetValue(Name, $"\"{Environment.ProcessPath}\"");
                else if (k.GetValue(Name) != null) k.DeleteValue(Name);
                if (k.GetValue("ClawVoice") != null) k.DeleteValue("ClawVoice");
            }
            catch (Exception e) { Log.Write("autostart: " + e.Message); }
        }
    }
}
