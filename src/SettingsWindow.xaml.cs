using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Threading;

namespace ClawVoice
{
    /// <summary>SaySo settings. Everything applies at once: settings.json is re-read by Python on use; language,
    /// microphone and speech device restart the dictation; secrets go to the DPAPI file via sayso_cli.</summary>
    public partial class SettingsWindow : Window
    {
        bool _loading, _gemmaBusy;
        readonly ClawButtonsPanel _clawButtons = new();  // offers to put the new hotkeys on M1/M2 after a change
        readonly DispatcherTimer _tick = new() { Interval = TimeSpan.FromSeconds(1) };
        App AppRef => (App)Application.Current;

        public SettingsWindow()
        {
            InitializeComponent();
            Responsive.Attach(this, 900, PagesScroll, TabsGrid);  // portrait Claw: one column, scrolling, centered
            TitleBar.MouseLeftButtonDown += (_, e) => { if (e.ButtonState == MouseButtonState.Pressed) DragMove(); };
            CloseBtn.Click += (_, _) => Close();
            // closing destroys the window (App makes a new one on show): a hidden WPF window keeps its pages'
            // memory. Only a download in progress keeps it, hidden, so its progress and result are not lost.
            Closing += (_, e) => { if (_modelBusy || _gemmaBusy) { e.Cancel = true; Hide(); } };
            foreach (var (tab, page) in Tabs()) tab.Checked += (_, _) => ShowPage(page);

            // main
            DictationSwitch.Click += (_, _) => { bool on = DictationSwitch.IsChecked == true; Task.Run(() => AppRef.SetDictation(on)); };
            GemmaSwitch.Click += (_, _) => { bool on = GemmaSwitch.IsChecked == true; Task.Run(() => AppRef.SetGemma(on)); };
            ListenSwitch.Click += (_, _) => Stack.Listening = ListenSwitch.IsChecked == true;
            WarmSwitch.Click += (_, _) => Set("warm_mic", WarmSwitch.IsChecked == true);
            BeepSwitch.Click += (_, _) => Set("beeps", BeepSwitch.IsChecked == true);
            LogSwitch.Click += (_, _) =>
            {
                Set("dictation_log", LogSwitch.IsChecked == true);
                LogBtn.IsEnabled = LogSwitch.IsChecked == true;
                AppRef.RestartDictationSoon();  // dictate.py opens its log at start
            };
            AutostartSwitch.Click += (_, _) => Autostart.Apply(AutostartSwitch.IsChecked == true);
            // turns the assistant on right here (commands without a model work); the model stays optional, one click away
            EnableAssistantBtn.Click += (_, _) =>
            {
                Set("assistant", true);
                Gate();
                Reload();
                AssistantOnNote.Visibility = Visibility.Visible;
            };
            ToModelLink.Click += (_, _) => TabModel.IsChecked = true;
            CommandsBtn.Click += (_, _) => AppRef.ShowCommands();
            AmbiBtn.Click += (_, _) => App.OpenAmbilight();
            AmbiBtn.Visibility = App.AmbilightInstalled ? Visibility.Visible : Visibility.Collapsed;
            LogBtn.Click += (_, _) => Stack.Open(Stack.DictateLog);
            HotkeyDictation.Click += (_, _) => Capture(HotkeyDictation);
            HotkeyGemma.Click += (_, _) => Capture(HotkeyGemma);
            HotkeyReset.Click += (_, _) => { Set("hotkey_dictation", DefaultDictation); Set("hotkey_gemma", DefaultGemma);
                                             HotkeyHint.Text = "Вернула сочетания по умолчанию"; ShowHotkeys(); _ = _clawButtons.Refresh(); };
            PreviewKeyDown += OnCaptureKey;
            _tick.Tick += (_, _) => States();
            IsVisibleChanged += (_, _) => { if (IsVisible) _tick.Start(); else _tick.Stop(); };

            // voice
            NameBtn.Click += (_, _) => SaveName();
            NameBox.KeyDown += (_, e) => { if (e.Key == Key.Return) SaveName(); };
            TrainBtn.Click += (_, _) => AppRef.ShowTrainer();
            foreach (var (rb, lang) in new[] { (LangRu, "ru"), (LangEn, "en"), (LangAuto, "auto") })
                rb.Click += (_, _) => { Set("language", lang); AppRef.RestartDictationSoon(); };
            MicBox.SelectionChanged += (_, _) =>
            {
                if (_loading || MicBox.SelectedIndex < 0) return;
                Set("mic_device", MicBox.SelectedIndex == 0 ? "" : (string)MicBox.SelectedItem);
                AppRef.RestartDictationSoon();
            };
            foreach (var (rb, proc) in new[] { (MicProcHw, "auto"), (MicProcOff, "normal") })
                rb.Click += (_, _) => { Set("mic_processing", proc); AppRef.RestartDictationSoon(); MicFxHint(); };
            MicFxBtn.Click += (_, _) => Stack.Open("ms-settings:sound-defaultinputproperties");
            Activated += (_, _) => MicFxHint();  // back from the Windows sound settings
            ClawButtonsHost.Content = _clawButtons;
            FollowSlider.ValueChanged += (_, _) => { Labels(); if (!_loading) Set("follow_sec", (int)FollowSlider.Value); };
            RateSlider.ValueChanged += (_, _) => { Labels(); if (!_loading) Set("tts_rate", (int)RateSlider.Value); };
            VolumeSlider.ValueChanged += (_, _) => { Labels(); if (!_loading) Set("tts_volume", (int)VolumeSlider.Value); };
            VoiceBox.SelectionChanged += (_, _) =>
            {
                if (_loading || VoiceBox.SelectedIndex < 0) return;
                Set("tts_voice", VoiceBox.SelectedIndex == 0 ? "" : (string)VoiceBox.SelectedItem);
            };
            VoiceTestBtn.Click += (_, _) => Voices.Preview(VoiceBox.SelectedIndex <= 0 ? "" : (string)VoiceBox.SelectedItem,
                (int)RateSlider.Value, (int)VolumeSlider.Value, AssistantName() + ": так звучит мой голос.");

            // model
            // another variant: the last check's "Ответила за 1.0 с" belonged to the old one
            foreach (var rb in new[] { ProvNone, ProvNpu, ProvServer, ProvCloud }) rb.Click += (_, _) => { ModelStatus.Text = ""; ModelFields(); };
            // "Скачать Gemma" needs the token first: grey until one is pasted
            HfTokenBox.PasswordChanged += (_, _) => GemmaGetBtn.IsEnabled = HfTokenBox.Password.Trim().Length > 0;
            GemmaGetBtn.IsEnabled = false;
            ModelSaveBtn.Click += (_, _) => SaveModel();
            ModelTestBtn.Click += async (_, _) => await TestModel();
            GeminiKeyBtn.Click += (_, _) => Stack.Open("https://aistudio.google.com/apikey");
            GemmaHowBtn.Click += (_, _) => Stack.Open(GuideUrl + "#gemma-на-этом-компьютере");
            GemmaGetBtn.Click += async (_, _) => await GetGemma();
            _ = DetectNpu();
            OfflineSwitch.Click += (_, _) => { Set("offline", OfflineSwitch.IsChecked == true); OfflineGate(); ModelFields(); ShowModelNow(); };
            CityBtn.Click += async (_, _) => await SetCity();
            CityBox.KeyDown += async (_, e) => { if (e.Key == Key.Return) await SetCity(); };

            // telegram
            TgLinkBtn.Click += async (_, _) => await TgLink();
            TgTestBtn.Click += async (_, _) =>
            {
                if (Read()["telegram_chat_id"] is null) { Toast("Сначала привяжи бота: напиши ему /start и нажми «Привязать»", false); return; }
                TgStatus.Text = "Отправляю…";
                var r = await Task.Run(() => Stack.Cli("tg-test"));
                TgStatus.Text = r["error"] is { } err ? "Не получилось: " + err : "Отправила, проверь Телеграм";
                Toast(TgStatus.Text, r["error"] is null);
            };

            // commands
            CmdDo.ItemsSource = Actions.Select(a => a.Title).ToList();
            CmdDo.SelectionChanged += (_, _) => { CmdArgLabel.Text = CmdDo.SelectedIndex >= 0 ? Actions[CmdDo.SelectedIndex].ArgTitle : "ПАРАМЕТР"; CmdArgPlaceholder(); };
            CmdArg.PreviewKeyDown += OnCmdArgKey;
            CmdArg.TextChanged += (_, _) => CmdArgPlaceholder();
            CmdList.SelectionChanged += (_, _) => CmdShow();
            CmdNewBtn.Click += (_, _) =>
            {
                _cmds.Add(new JsonObject { ["say"] = new JsonArray("новая команда"), ["do"] = "open_url", ["url"] = "https://" });
                CmdWrite(); CmdFill(); CmdList.SelectedIndex = _cmds.Count - 1;
            };
            CmdDelBtn.Click += (_, _) => { if (CmdList.SelectedIndex >= 0) { _cmds.RemoveAt(CmdList.SelectedIndex); CmdWrite(); CmdFill(); } };
            CmdSaveBtn.Click += (_, _) => CmdSave();
            CmdFolderBtn.Click += (_, _) => { if (!File.Exists(CmdFile())) CmdWrite(); Stack.Open(CmdFile()); };
            CmdTestBtn.Click += async (_, _) => await CmdTest();
            CmdTestBox.KeyDown += async (_, e) => { if (e.Key == Key.Return) await CmdTest(); };

            // plugins
            PlugFolderBtn.Click += (_, _) => { Directory.CreateDirectory(Stack.UserPluginsDir); Stack.Open(Stack.UserPluginsDir); };

            // system
            foreach (var (rb, dev) in new[] { (DevAuto, "auto"), (DevNpu, "NPU"), (DevGpu, "GPU"), (DevCpu, "CPU") })
                rb.Click += async (_, _) => { Set("stt_device", dev); await RestartAndWait(SysStatus); };
            foreach (var (rb, dev) in new[] { (LlmDevAuto, "auto"), (LlmDevNpu, "NPU"), (LlmDevGpu, "GPU"), (LlmDevCpu, "CPU") })
                rb.Click += (_, _) => ChooseLlmDevice(dev);
            foreach (var (rb, size) in new[] { (SttAuto, "auto"), (SttSmall, "small"), (SttTurbo, "turbo") })
                rb.Click += async (_, _) => await ChooseStt(size);
            AddAppFolderBtn.Click += async (_, _) => await AddAppFolder();
            ExportBtn.Click += (_, _) => Export();
            ImportBtn.Click += (_, _) => Import();
            DiagBtn.Click += async (_, _) => await Diagnostics();
            UpdateBtn.Click += async (_, _) => await CheckUpdate();
            UpdateGetBtn.Click += (_, _) => { if (_updatePage.Length > 0) Stack.Open(_updatePage); };
            RepoBtn.Click += (_, _) => Stack.Open(RepoUrl);
            GuideBtn.Click += (_, _) => Stack.Open(GuideUrl);
            VersionText.Text = "SaySo " + (typeof(App).Assembly.GetName().Version?.ToString(3) ?? "") + " (бета)";
        }

        IEnumerable<(RadioButton, FrameworkElement)> Tabs() => new (RadioButton, FrameworkElement)[]
            { (TabMain, PageMain), (TabVoice, PageVoice), (TabModel, PageModel), (TabTg, PageTg), (TabCmd, PageCmd),
              (TabPlug, PagePlug), (TabSys, PageSys), (TabMore, PageMore) };

        void ShowPage(FrameworkElement page)
        {
            foreach (var (_, p) in Tabs()) p.Visibility = p == page ? Visibility.Visible : Visibility.Collapsed;
            if (page == PageVoice) LoadVoicePage();
            if (page == PageCmd) CmdFill();
            if (page == PagePlug) PlugFill();
            if (page == PageSys) { LoadSysPage(); _ = FillModels(); }
            if (page == PageMore) _ = FillAppFolders();
            if (page == PageTg)
            {
                var id = Read()["telegram_chat_id"];
                TgChatBox.Text = id?.ToString() ?? "";
                TgStatus.Text = id is not null ? $"Привязан чат {id}" : "Бот ещё не привязан";
            }
        }

        public void ShowTab(string name)
        {
            var tab = name switch { "voice" => TabVoice, "model" => TabModel, "telegram" => TabTg, "commands" => TabCmd,
                                    "plugins" => TabPlug, "system" => TabSys, "more" => TabMore, _ => TabMain };
            tab.IsChecked = true;
        }

        // ---------------- settings file

        public static JsonObject Read()
        {
            try { return JsonNode.Parse(File.ReadAllText(Stack.SettingsFile))!.AsObject(); }
            catch { return new JsonObject(); }
        }

        static readonly JsonSerializerOptions Pretty = new() { WriteIndented = true,
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

        public static void Set(string key, JsonNode? value)
        {
            var s = Read();
            s[key] = value;
            try
            {
                Directory.CreateDirectory(Stack.UserDir);
                File.WriteAllText(Stack.SettingsFile, s.ToJsonString(Pretty));
            }
            catch (Exception e) { Log.Write("settings: " + e.Message); }
        }

        static string AssistantName() => (string?)Read()["assistant_name"] ?? "Сэйсо";

        public void Reload()
        {
            _loading = true;
            var s = Read();
            FollowSlider.Value = (double?)s["follow_sec"] ?? 6;
            RateSlider.Value = (double?)s["tts_rate"] ?? 0;
            VolumeSlider.Value = (double?)s["tts_volume"] ?? 100;
            WarmSwitch.IsChecked = (bool?)s["warm_mic"] ?? true;
            BeepSwitch.IsChecked = (bool?)s["beeps"] ?? true;
            LogSwitch.IsChecked = LogBtn.IsEnabled = DictationLogOn;
            OfflineSwitch.IsChecked = (bool?)s["offline"] ?? false;
            OfflineGate();
            AutostartSwitch.IsChecked = Autostart.IsSet();
            NameBox.Text = AssistantName();
            string lang = (string?)s["language"] ?? "ru";
            (lang == "en" ? LangEn : lang == "auto" ? LangAuto : LangRu).IsChecked = true;
            ((string?)s["mic_processing"] == "normal" ? MicProcOff : MicProcHw).IsChecked = true;
            MicFxHint();
            string dev = (string?)s["stt_device"] ?? "auto";
            (dev == "NPU" ? DevNpu : dev == "GPU" ? DevGpu : dev == "CPU" ? DevCpu : DevAuto).IsChecked = true;
            string stt = (string?)s["stt_model"] ?? "auto";
            (stt == "small" ? SttSmall : stt == "turbo" ? SttTurbo : SttAuto).IsChecked = true;
            Gate();
            var llm = s["llm"] as JsonObject ?? new JsonObject();
            string prov = (string?)llm["provider"] ?? "none";
            (prov == "none" ? ProvNone : prov == "server" ? ProvServer : prov == "cloud" ? ProvCloud : ProvNpu).IsChecked = true;
            UrlBox.Text = (string?)llm["url"] ?? "";
            ModelBox.Text = (string?)llm["model"] ?? "";
            ShowModelNow();
            KeyBox.Password = "";
            ModelFields();
            CityNow.Text = "Сейчас: " + CurrentCity();
            AmbiBtn.Visibility = File.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                @"Programs\ClawAmbilight\ClawAmbilight.exe")) ? Visibility.Visible : Visibility.Hidden;
            _capturing = null;
            ShowHotkeys();
            States();
            _loading = false;
            Labels();
        }

        void Labels()
        {
            FollowText.Text = FollowSlider.Value == 0 ? "выкл" : $"{FollowSlider.Value:0} с";
            RateText.Text = RateSlider.Value > 0 ? $"+{RateSlider.Value:0}" : $"{RateSlider.Value:0}";
            VolumeText.Text = $"{VolumeSlider.Value:0}%";
        }

        void States()
        {
            DictationSwitch.IsChecked = AppRef.DictationWanted;
            DictationSub.Text = App.DictationRunning ? "Работает: диктовка, кнопка команды, имя"
                : AppRef.DictationWanted ? "Запускается…" : "Выключена, кнопки не работают";
            bool listen = Stack.Listening;
            ListenTitle.Text = $"Откликаться на «{AssistantName()}»";
            ListenSwitch.IsChecked = listen;
            ListenSub.Text = listen ? "Слушаю имя от сети, микрофон всегда открыт" : "Выключено, зови кнопкой";
            string prov = (string?)(Read()["llm"] as JsonObject)?["provider"] ?? "none";
            bool npu = prov == "npu";
            // the switch only loads / unloads the local Gemma: with any other variant it would stay grey for nothing
            GemmaRow.Visibility = GemmaLine.Visibility = npu ? Visibility.Visible : Visibility.Collapsed;
            GemmaSwitch.IsEnabled = npu;
            GemmaTitle.Text = npu ? "Gemma загружена" : "Языковая модель";
            bool gemma = Stack.GemmaWanted;
            GemmaSwitch.IsChecked = npu && gemma;
            GemmaSub.Text = !npu ? (prov == "none" ? "Без модели: команды и ответы из интернета" : "Внешняя модель, настраивается на вкладке «Помощник»")
                : App.GemmaLoaded ? "В памяти, около 3.8 ГБ. Выключи, чтобы освободить" : gemma ? "Загружается…" : "Выгружена: простые команды работают, разговор нет";
            StatusText.Text = "";
        }

        // ---------------- hotkeys (voice_settings.parse_hotkey format; Python re-registers within a second)

        public const string DefaultDictation = "Win+Ctrl+Alt+W", DefaultGemma = "Win+Ctrl+Shift+G";
        const int CaptureSec = 10;

        /// <summary>A combination as the user reads it: Win first, then Ctrl, Alt, Shift, the key (any stored order).</summary>
        public static string HotkeyText(string? combo)
        {
            if (string.IsNullOrWhiteSpace(combo)) return "";
            string[] order = { "win", "ctrl", "alt", "shift" };
            var parts = combo.Replace(" ", "").Split('+', StringSplitOptions.RemoveEmptyEntries);
            return string.Join("+", parts.OrderBy(p => { int i = Array.IndexOf(order, p.ToLowerInvariant()); return i < 0 ? 9 : i; }));
        }
        Button? _capturing;
        readonly DispatcherTimer _captureTimer = new() { Interval = TimeSpan.FromMilliseconds(200) };
        DateTime _captureUntil;
        bool _captureWired;

        void ShowHotkeys()
        {
            var s = Read();
            HotkeyDictation.Content = HotkeyText((string?)s["hotkey_dictation"] ?? DefaultDictation);
            HotkeyGemma.Content = HotkeyText((string?)s["hotkey_gemma"] ?? DefaultGemma);
        }

        void Capture(Button b)
        {
            ShowHotkeys();
            _capturing = b;
            _captureUntil = DateTime.Now.AddSeconds(CaptureSec);
            CaptureCountdown();
            b.Focus();
            if (!_captureWired)
            {
                _captureWired = true;
                _captureTimer.Tick += (_, _) => CaptureCountdown();
                Deactivated += (_, _) => StopCapture();  // switched away: nothing to wait for
            }
            _captureTimer.Start();
        }

        /// <summary>"Нажми сочетание… 9": waiting gives up after CaptureSec and shows the old combination again.</summary>
        void CaptureCountdown()
        {
            if (_capturing == null) { _captureTimer.Stop(); return; }
            double left = (_captureUntil - DateTime.Now).TotalSeconds;
            if (left <= 0) { StopCapture(); return; }
            _capturing.Content = $"Нажми сочетание…  {Math.Ceiling(left):0}";
        }

        void StopCapture()
        {
            _captureTimer.Stop();
            if (_capturing == null) return;
            _capturing = null;
            ShowHotkeys();
        }

        /// <summary>The pressed combination as "Win+Ctrl+Alt+Shift+Key", or null while only modifiers are down.</summary>
        string? PressedCombo(KeyEventArgs e)
        {
            Key key = e.Key == Key.System ? e.SystemKey : e.Key;
            if (key is Key.LeftCtrl or Key.RightCtrl or Key.LeftAlt or Key.RightAlt or Key.LeftShift or Key.RightShift
                or Key.LWin or Key.RWin) return null;
            var mods = Keyboard.Modifiers;
            bool win = Keyboard.IsKeyDown(Key.LWin) || Keyboard.IsKeyDown(Key.RWin);
            return (win ? "Win+" : "") + (mods.HasFlag(ModifierKeys.Control) ? "Ctrl+" : "") +
                   (mods.HasFlag(ModifierKeys.Alt) ? "Alt+" : "") + (mods.HasFlag(ModifierKeys.Shift) ? "Shift+" : "") + KeyName(key);
        }

        /// <summary>Commands tab, action "Нажать сочетание клавиш": the field records the pressed keys (typing
        /// "Ctrl+Shift+M" by hand still works). A plain key without a modifier is typed as text; Backspace clears.</summary>
        void CmdArgPlaceholder()
        {
            bool keys = CmdDo.SelectedIndex >= 0 && Actions[CmdDo.SelectedIndex].Arg == "keys";
            CmdArgHint.Text = "Введи сочетание клавиш";
            CmdArgHint.Visibility = keys && CmdArg.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        }

        void OnCmdArgKey(object sender, KeyEventArgs e)
        {
            if (CmdDo.SelectedIndex < 0 || Actions[CmdDo.SelectedIndex].Arg != "keys") return;
            Key key = e.Key == Key.System ? e.SystemKey : e.Key;
            bool win = Keyboard.IsKeyDown(Key.LWin) || Keyboard.IsKeyDown(Key.RWin);
            if (Keyboard.Modifiers == ModifierKeys.None && !win && key is not (>= Key.F1 and <= Key.F24)) return;
            e.Handled = true;  // also keeps Alt from opening a menu and Ctrl+A from selecting
            if (PressedCombo(e) is { } combo) CmdArg.Text = combo;
        }

        void OnCaptureKey(object sender, KeyEventArgs e)
        {
            if (_capturing == null) return;
            e.Handled = true;
            Key key = e.Key == Key.System ? e.SystemKey : e.Key;
            if (key == Key.Escape) { StopCapture(); return; }
            if (key is Key.LeftCtrl or Key.RightCtrl or Key.LeftAlt or Key.RightAlt or Key.LeftShift or Key.RightShift
                or Key.LWin or Key.RWin) return;  // wait for the main key
            var mods = Keyboard.Modifiers;
            bool win = Keyboard.IsKeyDown(Key.LWin) || Keyboard.IsKeyDown(Key.RWin);
            bool lone = key is >= Key.F13 and <= Key.F24 || key is Key.Pause or Key.Scroll;  // keys nobody types
            if (mods == ModifierKeys.None && !win && !lone)
            {
                HotkeyHint.Text = "Нужен хотя бы один модификатор (Ctrl, Alt, Shift, Win) или F13–F24 / Pause";
                return;
            }
            string combo = (win ? "Win+" : "") + (mods.HasFlag(ModifierKeys.Control) ? "Ctrl+" : "") +
                           (mods.HasFlag(ModifierKeys.Alt) ? "Alt+" : "") + (mods.HasFlag(ModifierKeys.Shift) ? "Shift+" : "") + KeyName(key);
            var other = _capturing == HotkeyDictation ? HotkeyGemma : HotkeyDictation;
            if (string.Equals(other.Content as string, combo, StringComparison.OrdinalIgnoreCase))
            {
                HotkeyHint.Text = "Это сочетание уже занято другой кнопкой";
                return;
            }
            Set(_capturing == HotkeyDictation ? "hotkey_dictation" : "hotkey_gemma", combo);
            _capturing = null;
            _captureTimer.Stop();
            ShowHotkeys();
            HotkeyHint.Text = $"Сохранено: {combo}. Работает через секунду; если занято другой программой, останется старое";
            _ = _clawButtons.Refresh();  // the back buttons now differ: the panel offers to update them
        }

        static string KeyName(Key key)
        {
            if (key is >= Key.A and <= Key.Z) return key.ToString();
            if (key is >= Key.D0 and <= Key.D9) return ((int)(key - Key.D0)).ToString();
            if (key is >= Key.F1 and <= Key.F24) return key.ToString();
            return key switch
            {
                Key.Pause => "Pause", Key.Space => "Space", Key.PageUp => "PageUp", Key.PageDown => "PageDown",
                Key.End => "End", Key.Home => "Home", Key.Insert => "Insert", Key.Delete => "Delete",
                Key.PrintScreen => "PrintScreen", Key.Scroll => "ScrollLock", Key.Left => "Left", Key.Up => "Up",
                Key.Right => "Right", Key.Down => "Down", Key.Tab => "Tab",
                _ => "0x" + KeyInterop.VirtualKeyFromKey(key).ToString("X2"),
            };
        }

        // ---------------- voice page

        bool _voiceLoaded;

        async void LoadVoicePage()
        {
            if (_voiceLoaded) return;
            _voiceLoaded = true;
            _loading = true;
            var voices = new List<string> { "Голос системы по умолчанию" };
            voices.AddRange(Voices.Installed());
            VoiceBox.ItemsSource = voices;
            VoiceBox.SelectedIndex = Math.Max(0, voices.IndexOf((string?)Read()["tts_voice"] ?? ""));
            MicBox.ItemsSource = new List<string> { "Загружаю список…" };
            MicBox.SelectedIndex = 0;
            _loading = false;
            var r = await Task.Run(() => Stack.Cli("mics"));
            _loading = true;
            var mics = new List<string> { "Микрофон по умолчанию" };
            if (r["devices"] is JsonArray arr) mics.AddRange(arr.Select(x => (string)x!));
            MicBox.ItemsSource = mics;
            MicBox.SelectedIndex = Math.Max(0, mics.IndexOf((string?)Read()["mic_device"] ?? ""));
            _loading = false;
        }

        /// <summary>Hardware processing needs Windows "Audio enhancements" on: without them the driver adds no gain,
        /// the voice is 3-4x quieter and dictation falls back to the normal silence gate (dictate.py sysfx_off).</summary>
        void MicFxHint()
        {
            bool off = MicProcHw.IsChecked == true && SysFxOff();
            MicProcHint.Text = off ? "«Улучшение звука» у микрофона выключено, поэтому аппаратная работает как обычная. Включить: кнопка справа."
                                   : "Аппаратная работает, если драйвер это умеет, иначе как обычная.";
            MicProcHint.Foreground = (System.Windows.Media.Brush)FindResource(off ? "Danger" : "Sub");
        }

        /// <summary>Any working capture endpoint with "Audio enhancements" off (Disable_SysFx = 1, readable by users).</summary>
        static bool SysFxOff()
        {
            try
            {
                using var root = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
                    @"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture");
                if (root == null) return false;
                foreach (string ep in root.GetSubKeyNames())
                {
                    using var k = root.OpenSubKey(ep);
                    if (k?.GetValue("DeviceState") is not int state || state != 1) continue;
                    using var fx = k.OpenSubKey("FxProperties");
                    if (fx?.GetValue("{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5") is int v && v == 1) return true;
                }
            }
            catch (Exception e) { Log.Write("sysfx check: " + e.Message); }
            return false;
        }

        void SaveName()
        {
            string name = NameBox.Text.Trim();
            if (name.Length < 2) { NameHint.Text = "Имя слишком короткое"; return; }
            var names = (Read()["wake_names"] as JsonArray)?.Select(x => (string)x!).ToList() ?? new List<string>();
            string low = name.ToLowerInvariant();
            // a new name starts its own list: the old name's spellings kept waking the assistant on the old name
            if (!string.Equals(AssistantName(), name, StringComparison.OrdinalIgnoreCase)) names.Clear();
            if (!names.Contains(low)) names.Insert(0, low);
            Set("assistant_name", name);
            Set("wake_names", new JsonArray(names.Select(n => (JsonNode)n!).ToArray()));
            NameHint.Text = $"Готово: зови «{name}, …». Чтобы узнавала лучше, нажми «Тренировать имя».";
        }

        // ---------------- model page

        const string GuideUrl = "https://github.com/carbonumbrella/sayso/blob/main/docs/ИНСТРУКЦИЯ.md";
        const string GeminiUrl = "https://generativelanguage.googleapis.com/v1beta/openai", GeminiModel = "gemini-3.5-flash-lite";
        bool _gemmaHere;

        void ModelFields()
        {
            bool external = ProvServer.IsChecked == true || ProvCloud.IsChecked == true;
            ServerFields.Visibility = external ? Visibility.Visible : Visibility.Collapsed;
            GeminiKeyBtn.Visibility = ProvCloud.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
            NpuFields.Visibility = ProvNpu.IsChecked == true ? Visibility.Visible : Visibility.Collapsed;
            GemmaState.Text = _gemmaHere ? "Gemma скачана. Включается тумблером на вкладке «Основное»."
                : "Gemma ещё не скачана (3.3 ГБ). Нужен бесплатный аккаунт Hugging Face, где принята лицензия Gemma, и его токен.";
            GemmaGet.Visibility = _gemmaHere ? Visibility.Collapsed : Visibility.Visible;
            ProvHint.Text = ProvNone.IsChecked == true ? (OfflineSwitch.IsChecked == true  // offline + no model: commands only
                    ? "Только команды: ответы карточкой, без голоса. Интернета и свободного разговора нет."
                    : "Команды, плагины и ответы из интернета работают без модели. Свободного разговора нет.")
                : ProvNpu.IsChecked == true ? "Gemma 3 4B прямо на этом компьютере, без интернета. Около 4 ГБ памяти, устройство выбирается ниже."
                : ProvServer.IsChecked == true ? "Свой сервер: Ollama (http://127.0.0.1:11434), LM Studio (http://127.0.0.1:1234), llama.cpp."
                : "Облачная модель. Бесплатный Gemini: адрес и модель уже подставлены, нужен только ключ. Фразы для модели уходят в интернет.";
            if (ProvServer.IsChecked == true && (string.IsNullOrWhiteSpace(UrlBox.Text) || UrlBox.Text.Contains(":8081")))
                UrlBox.Text = "http://127.0.0.1:11434";
            if (ProvCloud.IsChecked == true && (string.IsNullOrWhiteSpace(UrlBox.Text) || UrlBox.Text.Contains("127.0.0.1")))
            {
                UrlBox.Text = GeminiUrl;
                if (string.IsNullOrWhiteSpace(ModelBox.Text)) ModelBox.Text = GeminiModel;
            }
        }

        /// <summary>"Локально" on every PC (on a CPU it is slow, the hint says so); the NPU option only where there is
        /// one. Also: is Gemma downloaded.</summary>
        async Task DetectNpu()
        {
            var hw = await Task.Run(() => Stack.Cli("hw"));
            var m = await Task.Run(() => Stack.Cli("models"));
            _gemmaHere = (bool?)m["gemma"] == true;
            LlmDevNpu.Visibility = (bool?)hw["npu"] == true ? Visibility.Visible : Visibility.Collapsed;
            string dev = (string?)Read()["llm_device"] ?? "auto";
            (dev == "NPU" ? LlmDevNpu : dev == "GPU" ? LlmDevGpu : dev == "CPU" ? LlmDevCpu : LlmDevAuto).IsChecked = true;
            ModelFields();
        }

        /// <summary>Where the local Gemma runs; a loaded one is reloaded on the new device.</summary>
        void ChooseLlmDevice(string dev)
        {
            Set("llm_device", dev);
            if (!App.GemmaLoaded && !Stack.GemmaWanted) return;
            GemmaState.Text = "Перезагружаю Gemma на новом устройстве, это до полуминуты…";
            Task.Run(() => { AppRef.SetGemma(false); System.Threading.Thread.Sleep(1500); AppRef.SetGemma(true); });
        }

        async Task GetGemma()
        {
            string token = HfTokenBox.Password.Trim();
            if (token.Length == 0) { GemmaState.Text = "Вставь токен Hugging Face."; return; }
            GemmaGetBtn.IsEnabled = false;
            GemmaState.Text = "Скачиваю Gemma, 3.3 ГБ. Это может занять от нескольких минут до получаса, окно можно закрыть позже…";
            var progress = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            progress.Tick += (_, _) => { if (ReadProgress("gemma") is { } p) GemmaState.Text = $"Скачиваю Gemma: {p}. Окно можно закрыть позже."; };
            progress.Start();
            _gemmaBusy = true;
            var r = await Task.Run(() => Stack.Cli("gemma-download", token, 3 * 3600 * 1000));
            _gemmaBusy = false;
            progress.Stop();
            HfTokenBox.Password = "";  // (greys the button again: a new try needs the token pasted again)
            if (r["error"] is { } err) { GemmaState.Text = "Не скачалась: " + err; return; }
            _gemmaHere = true;
            ModelFields();
        }

        string ProviderKey() => ProvNone.IsChecked == true ? "none" : ProvServer.IsChecked == true ? "server"
                              : ProvCloud.IsChecked == true ? "cloud" : "npu";

        void SaveModel()
        {
            string prov = ProviderKey();
            string url = prov == "npu" ? "http://127.0.0.1:8081" : UrlBox.Text.Trim();
            Set("llm", new JsonObject { ["provider"] = prov, ["url"] = url, ["model"] = prov == "npu" ? "" : ModelBox.Text.Trim() });
            if (KeyBox.Password.Length > 0)
            {
                var r = Stack.Cli("secret llm_api_key", KeyBox.Password);
                KeyBox.Password = "";
                if (r["error"] is { } err) { ModelStatus.Text = "Ключ не сохранён: " + err; return; }
            }
            bool unlocked = !AssistantOn;
            Set("assistant", true);  // a dictation-only install gets the assistant once a variant is chosen here
            Gate();
            ShowModelNow();
            ModelStatus.Text = "Сохранено. " + (unlocked ? "Помощник включён. " : "") +
                               (prov == "npu" ? "Gemma включается тумблером на вкладке «Основное»." : "");
            if (prov != "npu" && Stack.GemmaWanted) Task.Run(() => AppRef.SetGemma(false));  // free the NPU memory
        }

        /// <summary>The saved variant as a green "✓ Подключено: …" box (grey for "Без модели"), so the tab shows what
        /// works now, not only the draft choice of the segment.</summary>
        void ShowModelNow()
        {
            var s = Read();
            var llm = s["llm"] as JsonObject ?? new JsonObject();
            string prov = (string?)llm["provider"] ?? "none", model = (string?)llm["model"] ?? "";
            bool blocked = (bool?)s["offline"] == true && prov is "cloud" or "server";  // offline mode turns them off
            bool on = prov != "none" && AssistantOn && !blocked;
            string dev = ((string?)s["llm_device"] ?? "auto") switch { "NPU" => "NPU", "GPU" => "видеокарта", "CPU" => "процессор", _ => "авто" };
            ModelNow.Text = !AssistantOn ? "Помощник выключен: установлена только диктовка"
                : blocked ? (prov == "cloud" ? "Облако выключено" : "Свой сервер выключен") + " режимом «Полностью офлайн»: команды работают без модели"
                : prov == "cloud" ? $"✓  Подключено: облако, {(model.Length > 0 ? model : "модель не указана")}"
                : prov == "server" ? $"✓  Подключено: свой сервер, {(model.Length > 0 ? model : (string?)llm["url"] ?? "")}"
                : prov == "npu" ? $"✓  Подключено: Gemma на этом компьютере ({dev}), {(App.GemmaLoaded ? "загружена" : Stack.GemmaWanted ? "загружается" : "выгружена")}"
                : "Без модели: команды работают, свободного разговора нет";
            var green = System.Windows.Media.Color.FromRgb(0x3F, 0xB9, 0x6E);
            ModelNow.Foreground = on ? new System.Windows.Media.SolidColorBrush(green) : (System.Windows.Media.Brush)FindResource("Sub");
            ModelNowBox.BorderBrush = on ? new System.Windows.Media.SolidColorBrush(green) : (System.Windows.Media.Brush)FindResource("Border");
            ModelNowBox.Background = on ? new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromArgb(0x22, 0x3F, 0xB9, 0x6E))
                                        : (System.Windows.Media.Brush)FindResource("Card");
        }

        /// <summary>"Полностью офлайн": the network variants (cloud, own server) are greyed and cannot be picked; a draft
        /// on one of them moves to "Без модели". The saved choice is kept and works again when offline is off.</summary>
        void OfflineGate()
        {
            bool off = OfflineSwitch.IsChecked == true;
            foreach (var rb in new[] { ProvServer, ProvCloud })
            {
                rb.IsEnabled = !off;
                rb.ToolTip = off ? "Выключено: включён режим «Полностью офлайн»" : null;
            }
            if (off && (ProvServer.IsChecked == true || ProvCloud.IsChecked == true))
            {
                ProvNone.IsChecked = true;
                ModelFields();
            }
        }

        async Task TestModel()
        {
            SaveModel();
            ModelStatus.Text = "Спрашиваю модель…";
            var r = await Task.Run(() => Stack.Cli("llm-test", null, 120000));
            ModelStatus.Text = r["error"] is { } err ? "Не ответила: " + err : $"Ответила за {r["seconds"]} с: «{r["text"]}»";
            Toast(r["error"] is null ? $"Модель работает, ответила за {r["seconds"]} с" : ModelStatus.Text, r["error"] is null);
        }

        async Task SetCity()
        {
            string city = CityBox.Text.Trim();
            if (city.Length == 0) return;
            CityBtn.IsEnabled = false;
            CityNow.Text = "Ищу…";
            var r = await Task.Run(() => Stack.Cli("location " + Stack.Quote(city)));
            CityNow.Text = (string?)r["text"] ?? "Не получилось: " + r["error"];
            CityBtn.IsEnabled = true;
        }

        static string CurrentCity()
        {
            try
            {
                var j = JsonNode.Parse(File.ReadAllText(Stack.LocationFile))!;
                return $"{j["name"]}, {j["country"]}";
            }
            catch { return "не задан"; }
        }

        // ---------------- telegram page

        /// <summary>A result the user must not miss ("Проверить"): a card over the window for a few seconds.</summary>
        void Toast(string text, bool ok)
        {
            var body = new TextBlock { Text = (ok ? "✓  " : "✕  ") + text, TextWrapping = TextWrapping.Wrap, MaxWidth = 460, FontSize = 15,
                                       Foreground = System.Windows.Media.Brushes.White };
            var card = new Border { CornerRadius = new CornerRadius(14), Padding = new Thickness(20, 14, 20, 14), Child = body,
                Background = new System.Windows.Media.SolidColorBrush(ok ? System.Windows.Media.Color.FromRgb(0x2E, 0x7D, 0x4F)
                                                                         : System.Windows.Media.Color.FromRgb(0xA8, 0x3A, 0x3A)),
                Effect = new System.Windows.Media.Effects.DropShadowEffect { BlurRadius = 18, ShadowDepth = 3, Opacity = 0.4 } };
            var pop = new System.Windows.Controls.Primitives.Popup { Child = card, AllowsTransparency = true, PlacementTarget = this,
                Placement = System.Windows.Controls.Primitives.PlacementMode.Center, StaysOpen = true, IsOpen = true };
            card.MouseLeftButtonUp += (_, _) => pop.IsOpen = false;
            var t = new DispatcherTimer { Interval = TimeSpan.FromSeconds(ok ? 3 : 6) };
            t.Tick += (_, _) => { t.Stop(); pop.IsOpen = false; };
            t.Start();
        }

        async Task TgLink()
        {
            string token = TgTokenBox.Password.Trim(), chat = TgChatBox.Text.Trim();
            if (chat.Length > 0 && !long.TryParse(chat, out _)) { TgStatus.Text = "ID чата это число, например 123456789"; return; }
            if (token.Length == 0 && chat.Length == 0) { TgStatus.Text = "Вставь токен бота от @BotFather"; return; }
            if (token.Length > 0 && token.Length < 20) { TgStatus.Text = "Это не похоже на токен бота от @BotFather"; return; }
            if (chat.Length > 0)  // a known chat id: nothing to wait for
            {
                if (token.Length > 0)
                {
                    var s = await Task.Run(() => Stack.Cli("secret telegram_token", token));
                    if (s["error"] is { } e) { TgStatus.Text = "Токен не сохранён: " + e; return; }
                    TgTokenBox.Password = "";
                }
                Set("telegram_chat_id", long.Parse(chat));
                TgStatus.Text = $"Сохранено: чат {chat}. Нажми «Отправить проверку».";
                Toast(TgStatus.Text, true);
                return;
            }
            TgStatus.Text = "Жду твоё сообщение боту (до 20 секунд): напиши ему /start или что угодно…";
            var r = await Task.Run(() => Stack.Cli("tg-link " + Stack.Quote(token)));
            TgTokenBox.Password = "";
            if (r["chat_id"] is { } id) TgChatBox.Text = id.ToString();
            TgStatus.Text = r["error"] is { } err ? "Не получилось: " + err
                : r["chat_id"] is null ? "Токен сохранён, но сообщения боту не пришло. Напиши ему что угодно и нажми «Привязать» ещё раз, или впиши ID чата вручную."
                : $"Привязано: {r["name"]} (чат {r["chat_id"]}). ID можно скопировать из поля.";
            Toast(TgStatus.Text, r["error"] is null && r["chat_id"] is not null);
        }

        // ---------------- commands page (JSON file, same format as commands.py)

        sealed record Act(string Do, string Title, string Arg, string ArgTitle);
        static readonly Act[] Actions =
        {
            new("open_app", "Открыть программу", "app", "ПРОГРАММА (НАЗВАНИЕ ИЛИ ПУТЬ)"),
            new("open_url", "Открыть сайт", "url", "АДРЕС ({*} = ХВОСТ ФРАЗЫ)"),
            new("open_folder", "Открыть папку", "path", "ПАПКА"),
            new("hotkey", "Нажать сочетание клавиш", "keys", "СОЧЕТАНИЕ КЛАВИШ"),
            new("type_text", "Напечатать текст", "text", "ТЕКСТ ({*} = ХВОСТ ФРАЗЫ)"),
            new("webhook", "Вызвать webhook", "url", "АДРЕС WEBHOOK"),
            new("script", "Команда PowerShell (с подтверждением)", "command", "КОМАНДА POWERSHELL"),
            new("say", "Только ответить", "reply", "НЕ НУЖЕН"),
        };
        List<JsonObject> _cmds = new();

        static string CmdFile()
        {
            string lang = (string?)Read()["language"] ?? "ru";
            return Path.Combine(Stack.UserCommandsDir, (lang == "auto" ? "ru" : lang) + ".json");
        }

        void CmdFill()
        {
            try { _cmds = JsonNode.Parse(File.ReadAllText(CmdFile()))!.AsArray().OfType<JsonObject>().Select(o => (JsonObject)o.DeepClone()).ToList(); }
            catch { }
            int sel = CmdList.SelectedIndex;
            CmdList.ItemsSource = _cmds.Select(c =>
            {
                string say = (c["say"] as JsonArray)?.FirstOrDefault()?.ToString() ?? "?";
                string act = Actions.FirstOrDefault(a => a.Do == (string?)c["do"])?.Title ?? (string?)c["do"] ?? "";
                return $"{say}\n   {act}";
            }).ToList();
            CmdList.SelectedIndex = _cmds.Count == 0 ? -1 : Math.Clamp(sel, 0, _cmds.Count - 1);
        }

        void CmdShow()
        {
            int i = CmdList.SelectedIndex;
            if (i < 0 || i >= _cmds.Count) return;
            var c = _cmds[i];
            CmdSay.Text = string.Join("\n", (c["say"] as JsonArray)?.Select(x => x?.ToString()) ?? Array.Empty<string>());
            int a = Math.Max(0, Array.FindIndex(Actions, x => x.Do == (string?)c["do"]));
            CmdDo.SelectedIndex = a;
            CmdArg.Text = Actions[a].Arg == "reply" ? "" : (string?)c[Actions[a].Arg] ?? "";
            CmdReply.Text = (string?)c["reply"] ?? "";
            CmdStatus.Text = "";
        }

        void CmdSave()
        {
            int i = CmdList.SelectedIndex;
            if (i < 0 || CmdDo.SelectedIndex < 0) { CmdStatus.Text = "Выбери команду слева или создай новую"; return; }
            var phrases = CmdSay.Text.Split('\n').Select(x => x.Trim()).Where(x => x.Length > 0).ToArray();
            if (phrases.Length == 0) { CmdStatus.Text = "Нужна хотя бы одна фраза"; return; }
            var act = Actions[CmdDo.SelectedIndex];
            var c = new JsonObject { ["say"] = new JsonArray(phrases.Select(p => (JsonNode)p!).ToArray()), ["do"] = act.Do };
            if (act.Arg != "reply") c[act.Arg] = CmdArg.Text.Trim();
            if (CmdReply.Text.Trim().Length > 0) c["reply"] = CmdReply.Text.Trim();
            _cmds[i] = c;
            CmdWrite();
            CmdFill();
            CmdStatus.Text = "Сохранено, работает сразу";
        }

        void CmdWrite()
        {
            Directory.CreateDirectory(Stack.UserCommandsDir);
            var arr = new JsonArray(_cmds.Select(c => (JsonNode)c.DeepClone()).ToArray());
            File.WriteAllText(CmdFile(), arr.ToJsonString(Pretty));
        }

        async Task CmdTest()
        {
            string phrase = CmdTestBox.Text.Trim();
            if (phrase.Length == 0) return;
            var r = await Task.Run(() => Stack.Cli("match " + Stack.Quote(phrase)));
            CmdStatus.Text = r["command"] is JsonObject c
                ? $"Сработает: «{(c["say"] as JsonArray)?.FirstOrDefault()}»" + ((string?)r["tail"] is { Length: > 0 } t ? $", хвост «{t}»" : "")
                : "Ни одна моя команда не подходит: фразу разберут встроенные команды или модель";
        }

        // ---------------- plugins page

        void PlugFill()
        {
            var disabled = (Read()["plugins_disabled"] as JsonArray)?.Select(x => (string)x!).ToHashSet() ?? new HashSet<string>();
            var rows = new List<FrameworkElement>();
            foreach (var (dir, kind) in new[] { (Stack.BuiltinPluginsDir, "встроенный"), (Stack.UserPluginsDir, "свой") })
            {
                if (!Directory.Exists(dir)) continue;
                foreach (string f in Directory.GetFiles(dir, "*.py").OrderBy(x => x))
                {
                    string name = Path.GetFileNameWithoutExtension(f);
                    if (name.StartsWith("_")) continue;
                    // ABOUT = "..." in the plugin is its user-facing description; else its first comment line
                    var aboutLine = File.ReadLines(f).Select(l => System.Text.RegularExpressions.Regex.Match(l, "^ABOUT\\s*=\\s*\"(.*)\""))
                                        .FirstOrDefault(m => m.Success);
                    string about = aboutLine?.Groups[1].Value ?? File.ReadLines(f).FirstOrDefault()?.TrimStart('#', ' ') ?? "";
                    var sw = new CheckBox { Style = (Style)FindResource("Switch"), IsChecked = !disabled.Contains(name),
                                            HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Center };
                    sw.Click += (_, _) => TogglePlugin(name, sw.IsChecked == true);
                    var text = new StackPanel { Margin = new Thickness(0, 0, 60, 0) };
                    text.Children.Add(new TextBlock { Text = $"{name}  ·  {kind}", FontWeight = FontWeights.SemiBold });
                    var sub = new TextBlock { Text = about, FontSize = 12, TextWrapping = TextWrapping.Wrap };
                    sub.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
                    text.Children.Add(sub);
                    var g = new Grid { Margin = new Thickness(0, 2, 0, 2), Width = 780 };
                    g.Children.Add(text);
                    g.Children.Add(sw);
                    rows.Add(g);
                }
            }
            if (rows.Count == 0) rows.Add(new TextBlock { Text = "Плагинов нет" });
            PlugList.ItemsSource = rows;
        }

        static void TogglePlugin(string name, bool on)
        {
            var list = (Read()["plugins_disabled"] as JsonArray)?.Select(x => (string)x!).ToList() ?? new List<string>();
            list.Remove(name);
            if (!on) list.Add(name);
            Set("plugins_disabled", new JsonArray(list.Select(x => (JsonNode)x!).ToArray()));
        }

        // ---------------- system page

        bool _sysLoaded;

        async void LoadSysPage()
        {
            if (_sysLoaded) return;
            _sysLoaded = true;
            var r = await Task.Run(() => Stack.Cli("hw"));
            _hasNpu = (bool?)r["npu"] == true;
            // "NPU, видеокарта Arc 140V, процессор": the GPU's own name goes next to it, without the (R)(TM)(16GB) noise
            var found = (r["openvino"] as JsonArray)?.Select(x => (string)x!).ToList() ?? new List<string>();
            string gpu = System.Text.RegularExpressions.Regex.Replace((string?)r["gpu"] ?? "", @"\((R|TM|\d+GB|iGPU|dGPU)\)|Intel|GPU", "").Trim();
            gpu = System.Text.RegularExpressions.Regex.Replace(gpu, @"\s+", " ");
            var names = new List<string>();
            if (found.Contains("NPU")) names.Add("NPU");
            if (found.Any(d => d.StartsWith("GPU"))) names.Add(gpu.Length > 0 ? $"видеокарта {gpu}" : "видеокарта");
            names.Add("процессор");
            string rec = (string?)r["recommend"] switch { "NPU" => "NPU", "GPU" => "видеокарта", _ => "процессор" };
            HwText.Text = r["error"] is { } err ? "Не удалось определить железо: " + err
                : $"Найдено: {string.Join(", ", names)}. Рекомендую: {rec}.";
            _ = FillModels();
        }

        // ---------------- assistant on/off (dictation-only install)

        static bool AssistantOn => (bool?)Read()["assistant"] != false;

        /// <summary>Settings "dictation_log" (default on): dictate.log keeps the recognized text and events.</summary>
        public static bool DictationLogOn => (bool?)Read()["dictation_log"] != false;

        /// <summary>Dictation only: everything that needs the assistant is greyed, the banner on "Основное" says how to
        /// turn it on. The "Модель" tab stays open: choosing a variant there turns the assistant on.</summary>
        void Gate()
        {
            bool on = AssistantOn;
            MinimalBanner.Visibility = on ? Visibility.Collapsed : Visibility.Visible;
            foreach (UIElement e in new UIElement[] { ListenRow, GemmaRow, HotkeyGemma, NameSection, FollowLabel, FollowRow, VoiceSection, TabTg, TabCmd, TabPlug })
            {
                e.IsEnabled = on;
                e.Opacity = on ? 1 : 0.45;
            }
            if (!on && (TabTg.IsChecked == true || TabCmd.IsChecked == true || TabPlug.IsChecked == true)) TabMain.IsChecked = true;
        }

        // ---------------- speech models (system page)

        bool _hasNpu, _modelBusy;
        JsonObject _models = new();

        /// <summary>The device dictation runs on now (stt_state.json), else what the hardware check recommends.</summary>
        string RunningDevice() => (string?)(_models["state"] as JsonObject)?["device"] ?? (_hasNpu ? "NPU" : "CPU");

        /// <summary>The catalog key a size needs on a device: the same rule as voice_settings.whisper_key.</summary>
        static string SttKey(string size, string device)
        {
            if (size is not ("small" or "turbo")) size = device == "GPU" ? "turbo" : "small";
            return size == "turbo" && device == "NPU" ? "turbo-npu" : size;
        }

        static readonly (string Key, string Title, string Note)[] ModelCatalog =
        {
            ("small", "Быстрая (Whisper small)", "~0.8 ГБ памяти"),
            ("turbo", "Точная (Whisper turbo) для видеокарты и процессора", "~1.3-2 ГБ памяти"),
            ("turbo-npu", "Точная (Whisper turbo) для NPU", "первый запуск ~6 мин, ~2 ГБ памяти"),
            ("gemma", "Gemma 3 4B, языковая модель", "~4 ГБ памяти, нужен токен"),
        };

        async Task FillModels()
        {
            var m = await Task.Run(() => Stack.Cli("models"));
            if (m["error"] is { } err) { ModelsStatus.Text = "Не удалось прочитать модели: " + err; return; }
            _models = m;
            var info = m["info"] as JsonObject ?? new JsonObject();
            string preparing = (string?)(m["state"] as JsonObject)?["preparing"] ?? "";
            ModelRows.Children.Clear();
            foreach (var (key, title, note) in ModelCatalog)
            {
                if (info[key] is not JsonObject mi) continue;
                if (key == "turbo-npu" && !_hasNpu && (bool?)mi["installed"] != true) continue;
                bool installed = (bool?)mi["installed"] == true, active = (bool?)mi["active"] == true;
                if (ModelRows.Children.Count > 0) ModelRows.Children.Add(new Border { Height = 1, Background = (System.Windows.Media.Brush)FindResource("Border") });
                var row = new Grid { Margin = new Thickness(0, 8, 0, 8) };
                row.ColumnDefinitions.Add(new ColumnDefinition());
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var texts = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
                texts.Children.Add(new TextBlock { Text = title, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap });
                string state = !installed ? "не скачана" : preparing == key ? "готовится для NPU" : active ? "используется" : "скачана";
                var sub = new TextBlock { Text = $"{FormatMb((int?)mi["mb"] ?? 0)}, {state}. {char.ToUpper(note[0]) + note[1..]}", FontSize = 12, TextWrapping = TextWrapping.Wrap };
                sub.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
                texts.Children.Add(sub);
                row.Children.Add(texts);
                var btn = new Button { Style = (Style)FindResource("Flat"), Height = 30, FontSize = 12, VerticalAlignment = VerticalAlignment.Center,
                                       Content = installed ? "Удалить" : "Скачать", IsEnabled = !_modelBusy && !active && preparing != key };
                if (active) btn.ToolTip = "Сейчас используется: сначала выбери другую модель";
                Grid.SetColumn(btn, 1);
                _modelBtn[key] = btn;
                string k = key, dir = (string?)mi["dir"] ?? "";
                int mb = (int?)mi["mb"] ?? 0;
                btn.Click += async (_, _) =>
                {
                    if (installed) await RemoveModel(k, title);
                    else if (k == "gemma") { TabModel.IsChecked = true; ProvNpu.IsChecked = true; ModelFields(); }
                    else await DownloadModel(k, dir, mb);
                };
                row.Children.Add(btn);
                ModelRows.Children.Add(row);
            }
        }

        // one style with the other texts ("0.8 ГБ", "3.3 ГБ"): decimal point, 1 ГБ = 1000 МБ as the catalog sizes are given
        static string FormatMb(int mb) => mb >= 1000 ? (mb / 1000.0).ToString("0.0", System.Globalization.CultureInfo.InvariantCulture) + " ГБ" : $"{mb} МБ";

        async Task<bool> DownloadModel(string key, string dir, int mb)
        {
            if (_modelBusy) return false;
            _modelBusy = true;
            string path = Path.Combine(Stack.Dir, dir);
            var progress = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            progress.Tick += (_, _) =>
            {
                string p = ReadProgress(key) ?? $"{DirMb(path)} из {mb} МБ";
                ModelsStatus.Text = $"Скачиваю: {p}. Окно можно закрыть, загрузка продолжится.";
                if (_modelBtn.TryGetValue(key, out var b)) b.Content = p.Split(',')[0];  // "37%" right on the row
            };
            ModelsStatus.Text = "Скачиваю…";
            await FillModels();
            if (_modelBtn.TryGetValue(key, out var first)) first.Content = "0%";
            progress.Start();
            var r = await Task.Run(() => Stack.Cli("download " + key, null, 3 * 3600 * 1000));
            progress.Stop();
            _modelBusy = false;
            bool ok = r["error"] is null;
            ModelsStatus.Text = ok ? "Модель скачана." : $"Не скачалась: {r["error"]}. Возможно, для скачивания потребуется VPN. Повтори загрузку: скачанное не пропадёт.";
            await FillModels();
            return ok;
        }

        async Task RemoveModel(string key, string title)
        {
            if (_modelBusy) return;
            _modelBusy = true;
            ModelsStatus.Text = "Удаляю…";
            var r = await Task.Run(() => Stack.Cli("remove " + key));
            _modelBusy = false;
            ModelsStatus.Text = r["error"] is { } err ? "Не удалилась: " + err : $"Удалено: {title}.";
            if (key == "gemma") _gemmaHere = r["error"] is not null;
            await FillModels();
        }

        /// <summary>A new speech model size: download it first when this device has no copy, then restart dictation.</summary>
        async Task ChooseStt(string size)
        {
            Set("stt_model", size);
            if (_models["info"] is null) await FillModels();
            string key = SttKey(size, RunningDevice());
            if (_models["info"]?[key] is JsonObject mi && (bool?)mi["installed"] != true)
            {
                if (!await DownloadModel(key, (string?)mi["dir"] ?? "", (int?)mi["mb"] ?? 0)) return;
            }
            await RestartAndWait(ModelsStatus, key == "turbo-npu"
                ? " Точная модель для NPU в первый раз готовится около 6 минут, пока работает быстрая." : "");
            await FillModels();
        }

        bool _restarting;

        /// <summary>Restart dictation and show that it is not working meanwhile: a ticking "Перезапускаю…" until dictate.py
        /// writes stt_state.json with the model it loaded (up to 2 min), the device and model choices locked.</summary>
        async Task RestartAndWait(TextBlock status, string extra = "")
        {
            if (_restarting) return;
            _restarting = true;
            var locked = new UIElement[] { DevAuto, DevNpu, DevGpu, DevCpu, SttAuto, SttSmall, SttTurbo };
            foreach (var e in locked) e.IsEnabled = false;
            string file = Path.Combine(Stack.Dir, "stt_state.json");
            DateTime since = DateTime.Now;
            AppRef.RestartDictationSoon();
            string[] spin = { "◐", "◓", "◑", "◒" };
            int i = 0;
            JsonNode? state = null;
            while ((DateTime.Now - since).TotalSeconds < 120)
            {
                status.Text = $"{spin[i++ % 4]}  Перезапускаю распознавание, диктовка пока не работает… {(int)(DateTime.Now - since).TotalSeconds} с";
                await Task.Delay(500);
                try
                {
                    if (File.Exists(file) && File.GetLastWriteTime(file) > since) { state = JsonNode.Parse(File.ReadAllText(file)); break; }
                }
                catch { }
            }
            foreach (var e in locked) e.IsEnabled = true;
            _restarting = false;
            status.Text = state is null ? "Распознавание долго не запускается. Подробности в dictate.log в папке программы."
                : $"Готово: {state["model"]} на {state["device"]}.{extra}";
        }

        // ---------------- program folders (system page): settings "app_folders", searched by "открой …"

        static List<string> AppFolders() => (Read()["app_folders"] as JsonArray)?.Select(x => (string)x!).ToList() ?? new List<string>();

        async Task FillAppFolders()
        {
            var folders = AppFolders();
            AppFolderRows.Children.Clear();
            if (folders.Count == 0)
            {
                var none = new TextBlock { Text = "Пока нет: «открой …» знает программы из меню «Пуск».", FontSize = 12, Margin = new Thickness(0, 8, 0, 8), TextWrapping = TextWrapping.Wrap };
                none.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
                AppFolderRows.Children.Add(none);
                return;
            }
            var r = await Task.Run(() => Stack.Cli("folder-apps"));
            foreach (string folder in folders)
            {
                if (AppFolderRows.Children.Count > 0) AppFolderRows.Children.Add(new Border { Height = 1, Background = (System.Windows.Media.Brush)FindResource("Border") });
                var info = r[folder] as JsonObject;
                int n = (int?)info?["count"] ?? 0;
                var names = (info?["names"] as JsonArray)?.Select(x => (string)x!).ToList() ?? new List<string>();
                string sub = info == null ? "не удалось прочитать" : (bool?)info["exists"] == false ? "папка не найдена"
                    : n == 0 ? "программ не нашлось" : $"{n} {Plural(n)}: {string.Join(", ", names.Take(6))}{(n > 6 ? "…" : "")}\nНажми, чтобы увидеть все";
                var row = new Grid { Margin = new Thickness(0, 8, 0, 8) };
                row.ColumnDefinitions.Add(new ColumnDefinition());
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var texts = new StackPanel { Margin = new Thickness(0, 0, 10, 0) };
                texts.Children.Add(new TextBlock { Text = folder, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap });
                var s = new TextBlock { Text = sub, FontSize = 12, TextWrapping = TextWrapping.Wrap };
                s.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
                texts.Children.Add(s);
                var remove = new Button { Content = "Убрать", Style = (Style)FindResource("Flat"), Height = 30, FontSize = 12, VerticalAlignment = VerticalAlignment.Center };
                string f = folder;
                remove.Click += async (_, _) =>
                {
                    Set("app_folders", new JsonArray(AppFolders().Where(x => x != f).Select(x => (JsonNode)x!).ToArray()));
                    AppFoldersStatus.Text = $"Убрано: {f}";
                    await FillAppFolders();
                };
                Grid.SetColumn(remove, 1);
                row.Children.Add(texts); row.Children.Add(remove);
                if (names.Count > 0)  // a tap on the folder: every name "открой …" knows from it
                {
                    texts.Background = System.Windows.Media.Brushes.Transparent;  // the whole text area takes the tap
                    texts.Cursor = Cursors.Hand;
                    texts.MouseLeftButtonUp += (_, _) => ShowFolderApps(texts, f, names);
                }
                AppFolderRows.Children.Add(row);
            }
        }

        /// <summary>All program names found in a folder, in a card under the row, scrolled when long.</summary>
        void ShowFolderApps(FrameworkElement anchor, string folder, List<string> names)
        {
            var list = new StackPanel();
            var head = new TextBlock { Text = $"«Открой …» знает из этой папки: {names.Count}", FontWeight = FontWeights.SemiBold,
                                       Margin = new Thickness(0, 0, 0, 6), TextWrapping = TextWrapping.Wrap };
            list.Children.Add(head);
            foreach (var n in names.Distinct().OrderBy(x => x, StringComparer.CurrentCultureIgnoreCase))
                list.Children.Add(new TextBlock { Text = "•  " + n, Margin = new Thickness(0, 2, 0, 2), TextWrapping = TextWrapping.Wrap });
            var scroll = new ScrollViewer { Content = list, MaxHeight = 360, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                                            PanningMode = PanningMode.VerticalOnly };
            var card = new Border { CornerRadius = new CornerRadius(12), Padding = new Thickness(14, 10, 8, 12), BorderThickness = new Thickness(1),
                Width = Math.Max(280, anchor.ActualWidth), Child = scroll,
                Background = (System.Windows.Media.Brush)FindResource("Card"), BorderBrush = (System.Windows.Media.Brush)FindResource("Border"),
                Effect = new System.Windows.Media.Effects.DropShadowEffect { BlurRadius = 16, ShadowDepth = 3, Opacity = 0.35 } };
            // the popup is its own tree: brushes are copied, not referenced
            foreach (TextBlock t in list.Children) t.Foreground = (System.Windows.Media.Brush)FindResource("Fg");
            new System.Windows.Controls.Primitives.Popup { Child = card, PlacementTarget = anchor, AllowsTransparency = true, StaysOpen = false,
                Placement = System.Windows.Controls.Primitives.PlacementMode.Bottom, IsOpen = true };
        }

        static string Plural(int n) => n % 10 == 1 && n % 100 != 11 ? "программа"
            : n % 10 is >= 2 and <= 4 && (n % 100 < 12 || n % 100 > 14) ? "программы" : "программ";

        async Task AddAppFolder()
        {
            var dlg = new Microsoft.Win32.OpenFolderDialog { Title = "Папка с программами или играми" };
            if (dlg.ShowDialog(this) != true) return;
            var list = AppFolders();
            if (list.Any(x => string.Equals(x, dlg.FolderName, StringComparison.OrdinalIgnoreCase)))
            {
                AppFoldersStatus.Text = "Эта папка уже в списке";
                return;
            }
            list.Add(dlg.FolderName);
            Set("app_folders", new JsonArray(list.Select(x => (JsonNode)x!).ToArray()));
            AppFoldersStatus.Text = "Добавлено. Скажи «открой …» и название программы.";
            await FillAppFolders();
        }

        readonly Dictionary<string, Button> _modelBtn = new();

        /// <summary>"37%, 290 из 789 МБ" from download_progress.json (sayso_cli writes it every second), or null.</summary>
        static string? ReadProgress(string key)
        {
            try
            {
                var j = JsonNode.Parse(File.ReadAllText(Path.Combine(Stack.Dir, "download_progress.json")))!;
                if ((string?)j["key"] != key) return null;
                int mb = (int?)j["mb"] ?? 0, total = Math.Max(1, (int?)j["total"] ?? 1);
                return $"{Math.Min(99, mb * 100 / total)}%, {mb} из {total} МБ";
            }
            catch { return null; }
        }

        static long DirMb(string dir)
        {
            try { return Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories).Sum(f => new FileInfo(f).Length) / (1 << 20); }
            catch { return 0; }
        }

        void Export()
        {
            string dest = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Desktop), $"SaySo-settings-{DateTime.Now:yyyy-MM-dd}.zip");
            try
            {
                if (File.Exists(dest)) File.Delete(dest);
                using var zip = ZipFile.Open(dest, ZipArchiveMode.Create);
                foreach (string f in Directory.GetFiles(Stack.UserDir, "*", SearchOption.AllDirectories))
                {
                    if (f.EndsWith("secrets.bin") || f.EndsWith(".tmp")) continue;  // secrets are bound to this PC
                    zip.CreateEntryFromFile(f, Path.GetRelativePath(Stack.UserDir, f));
                }
                MoreStatus.Text = "Сохранено на рабочий стол: " + Path.GetFileName(dest) + ". Токены и ключи не входят, их вводят заново.";
            }
            catch (Exception e) { MoreStatus.Text = "Не получилось: " + e.Message; }
        }

        void Import()
        {
            var dlg = new Microsoft.Win32.OpenFileDialog { Filter = "Настройки SaySo (*.zip)|*.zip" };
            if (dlg.ShowDialog(this) != true) return;
            try
            {
                using var zip = ZipFile.OpenRead(dlg.FileName);
                foreach (var e in zip.Entries)
                {
                    if (e.FullName.EndsWith("/") || e.FullName.Contains("..")) continue;
                    string path = Path.Combine(Stack.UserDir, e.FullName);
                    Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                    e.ExtractToFile(path, true);
                }
                _voiceLoaded = false;
                Reload();
                AppRef.RestartDictationSoon();
                MoreStatus.Text = "Настройки загружены";
            }
            catch (Exception e) { MoreStatus.Text = "Не получилось: " + e.Message; }
        }

        async Task Diagnostics()
        {
            MoreStatus.Text = "Собираю…";
            string dest = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Desktop), $"SaySo-report-{DateTime.Now:yyyy-MM-dd-HHmm}.zip");
            var hw = await Task.Run(() => Stack.Cli("hw"));
            try
            {
                using var zip = ZipFile.Open(dest, ZipArchiveMode.Create);
                foreach (string log in new[] { "dictate.log", "sayso.log", "hotkey_helper.log", "npu_server.log" })
                {
                    string p = Path.Combine(Stack.Dir, log);
                    if (!File.Exists(p)) continue;
                    using var fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                    using var w = zip.CreateEntry(log).Open();
                    fs.CopyTo(w);
                }
                if (File.Exists(Stack.SettingsFile)) zip.CreateEntryFromFile(Stack.SettingsFile, "settings.json");
                using (var w = new StreamWriter(zip.CreateEntry("hardware.json").Open())) w.Write(hw.ToJsonString());
                using (var w = new StreamWriter(zip.CreateEntry("system.txt").Open()))
                    w.Write($"SaySo {typeof(App).Assembly.GetName().Version}\nWindows {Environment.OSVersion}\n" +
                            $"App {Stack.Dir}\nUser data {Stack.UserDir}\n");
                MoreStatus.Text = "Отчёт на рабочем столе: " + Path.GetFileName(dest) + ". Токенов и ключей в нём нет.";
            }
            catch (Exception e) { MoreStatus.Text = "Не получилось: " + e.Message; }
        }

        /// <summary>Update check against a small JSON {"version": "x.y.z", "url": "..."} (settings "update_url").
        /// Empty until SaySo is published.</summary>
        const string RepoUrl = "https://github.com/carbonumbrella/sayso";
        string _updatePage = "";

        /// <summary>The newest release on GitHub (pre-releases count: every SaySo release is a beta) against this build.
        /// Nothing is downloaded here: a newer one shows a button that opens its page.</summary>
        async Task CheckUpdate()
        {
            UpdateBtn.IsEnabled = false;
            UpdateStatus.Text = "Проверяю…";
            try
            {
                using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                http.DefaultRequestHeaders.UserAgent.ParseAdd("SaySo-update-check");  // GitHub's API refuses requests without one
                var all = JsonNode.Parse(await http.GetStringAsync("https://api.github.com/repos/carbonumbrella/sayso/releases?per_page=10"))!.AsArray();
                var mine = typeof(App).Assembly.GetName().Version!;
                Version best = new(0, 0); string page = "";
                foreach (var r in all)
                {
                    if ((bool?)r?["draft"] == true) continue;
                    if (Version.TryParse(((string?)r?["tag_name"] ?? "").TrimStart('v'), out var v) && v > best) { best = v; page = (string?)r?["html_url"] ?? ""; }
                }
                bool newer = best > new Version(mine.Major, mine.Minor, mine.Build);
                _updatePage = newer ? page : "";
                UpdateGetBtn.Visibility = newer ? Visibility.Visible : Visibility.Collapsed;
                UpdateGetBtn.Content = $"Скачать SaySo {best}";
                UpdateStatus.Text = newer ? $"Есть новая версия {best}. Скачай установщик и запусти поверх: настройки и модели сохранятся."
                    : best.Major + best.Minor + best.Build == 0 ? "Выпусков на GitHub не нашлось" : $"Установлена последняя версия ({mine.ToString(3)})";
                Toast(UpdateStatus.Text.Split('.')[0], true);
            }
            catch (Exception e) { UpdateStatus.Text = "Не удалось проверить: " + e.Message + ". Возможно, нет интернета."; }
            finally { UpdateBtn.IsEnabled = true; }
        }
    }
}
