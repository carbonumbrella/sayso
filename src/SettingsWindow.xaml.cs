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
        bool _loading;
        readonly DispatcherTimer _tick = new() { Interval = TimeSpan.FromSeconds(1) };
        App AppRef => (App)Application.Current;

        public SettingsWindow()
        {
            InitializeComponent();
            Responsive.Attach(this, 900, PagesScroll, TabsGrid);  // portrait Claw: one column, scrolling, centered
            TitleBar.MouseLeftButtonDown += (_, e) => { if (e.ButtonState == MouseButtonState.Pressed) DragMove(); };
            CloseBtn.Click += (_, _) => Hide();
            Closing += (_, e) => { e.Cancel = true; Hide(); };
            foreach (var (tab, page) in Tabs()) tab.Checked += (_, _) => ShowPage(page);

            // main
            DictationSwitch.Click += (_, _) => { bool on = DictationSwitch.IsChecked == true; Task.Run(() => AppRef.SetDictation(on)); };
            GemmaSwitch.Click += (_, _) => { bool on = GemmaSwitch.IsChecked == true; Task.Run(() => AppRef.SetGemma(on)); };
            ListenSwitch.Click += (_, _) => Stack.Listening = ListenSwitch.IsChecked == true;
            WarmSwitch.Click += (_, _) => Set("warm_mic", WarmSwitch.IsChecked == true);
            AutostartSwitch.Click += (_, _) => Autostart.Apply(AutostartSwitch.IsChecked == true);
            CommandsBtn.Click += (_, _) => AppRef.ShowCommands();
            AmbiBtn.Click += (_, _) => App.OpenAmbilight();
            LogBtn.Click += (_, _) => Stack.Open(Stack.DictateLog);
            HotkeyDictation.Click += (_, _) => Capture(HotkeyDictation);
            HotkeyGemma.Click += (_, _) => Capture(HotkeyGemma);
            HotkeyReset.Click += (_, _) => { Set("hotkey_dictation", DefaultDictation); Set("hotkey_gemma", DefaultGemma);
                                             HotkeyHint.Text = "Вернула сочетания по умолчанию"; ShowHotkeys(); };
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
            foreach (var rb in new[] { ProvNone, ProvNpu, ProvServer, ProvCloud }) rb.Click += (_, _) => ModelFields();
            ModelSaveBtn.Click += (_, _) => SaveModel();
            ModelTestBtn.Click += async (_, _) => await TestModel();
            GeminiKeyBtn.Click += (_, _) => Stack.Open("https://aistudio.google.com/apikey");
            GemmaHowBtn.Click += (_, _) => Stack.Open(GuideUrl + "#gemma-на-npu");
            GemmaGetBtn.Click += async (_, _) => await GetGemma();
            _ = DetectNpu();
            OfflineSwitch.Click += (_, _) => Set("offline", OfflineSwitch.IsChecked == true);
            CityBtn.Click += async (_, _) => await SetCity();
            CityBox.KeyDown += async (_, e) => { if (e.Key == Key.Return) await SetCity(); };

            // telegram
            TgLinkBtn.Click += async (_, _) => await TgLink();
            TgTestBtn.Click += async (_, _) =>
            {
                TgStatus.Text = "Отправляю…";
                var r = await Task.Run(() => Stack.Cli("tg-test"));
                TgStatus.Text = r["error"] is { } err ? "Не получилось: " + err : "Отправила, проверь Телеграм";
            };

            // commands
            CmdDo.ItemsSource = Actions.Select(a => a.Title).ToList();
            CmdDo.SelectionChanged += (_, _) => CmdArgLabel.Text = CmdDo.SelectedIndex >= 0 ? Actions[CmdDo.SelectedIndex].ArgTitle : "ПАРАМЕТР";
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
                rb.Click += (_, _) => { Set("stt_device", dev); AppRef.RestartDictationSoon(); SysStatus.Text = "Перезапускаю диктовку с новым устройством…"; };
            ExportBtn.Click += (_, _) => Export();
            ImportBtn.Click += (_, _) => Import();
            DiagBtn.Click += async (_, _) => await Diagnostics();
            UpdateBtn.Click += async (_, _) => await CheckUpdate();
            VersionText.Text = "SaySo " + (typeof(App).Assembly.GetName().Version?.ToString(3) ?? "");
        }

        IEnumerable<(RadioButton, FrameworkElement)> Tabs() => new (RadioButton, FrameworkElement)[]
            { (TabMain, PageMain), (TabVoice, PageVoice), (TabModel, PageModel), (TabTg, PageTg), (TabCmd, PageCmd),
              (TabPlug, PagePlug), (TabSys, PageSys) };

        void ShowPage(FrameworkElement page)
        {
            foreach (var (_, p) in Tabs()) p.Visibility = p == page ? Visibility.Visible : Visibility.Collapsed;
            if (page == PageVoice) LoadVoicePage();
            if (page == PageCmd) CmdFill();
            if (page == PagePlug) PlugFill();
            if (page == PageSys) LoadSysPage();
            if (page == PageTg) TgStatus.Text = Read()["telegram_chat_id"] is { } id ? $"Привязан чат {id}" : "Бот ещё не привязан";
        }

        public void ShowTab(string name)
        {
            var tab = name switch { "voice" => TabVoice, "model" => TabModel, "telegram" => TabTg, "commands" => TabCmd,
                                    "plugins" => TabPlug, "system" => TabSys, _ => TabMain };
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

        static string AssistantName() => (string?)Read()["assistant_name"] ?? "Мурмур";

        public void Reload()
        {
            _loading = true;
            var s = Read();
            FollowSlider.Value = (double?)s["follow_sec"] ?? 6;
            RateSlider.Value = (double?)s["tts_rate"] ?? 0;
            VolumeSlider.Value = (double?)s["tts_volume"] ?? 100;
            WarmSwitch.IsChecked = (bool?)s["warm_mic"] ?? true;
            OfflineSwitch.IsChecked = (bool?)s["offline"] ?? false;
            AutostartSwitch.IsChecked = Autostart.IsSet();
            NameBox.Text = AssistantName();
            string lang = (string?)s["language"] ?? "ru";
            (lang == "en" ? LangEn : lang == "auto" ? LangAuto : LangRu).IsChecked = true;
            string dev = (string?)s["stt_device"] ?? "auto";
            (dev == "NPU" ? DevNpu : dev == "GPU" ? DevGpu : dev == "CPU" ? DevCpu : DevAuto).IsChecked = true;
            var llm = s["llm"] as JsonObject ?? new JsonObject();
            string prov = (string?)llm["provider"] ?? "none";
            (prov == "none" ? ProvNone : prov == "server" ? ProvServer : prov == "cloud" ? ProvCloud : ProvNpu).IsChecked = true;
            UrlBox.Text = (string?)llm["url"] ?? "";
            ModelBox.Text = (string?)llm["model"] ?? "";
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
            GemmaSwitch.IsEnabled = npu;
            GemmaTitle.Text = npu ? "Gemma загружена" : "Языковая модель";
            bool gemma = Stack.GemmaWanted;
            GemmaSwitch.IsChecked = npu && gemma;
            GemmaSub.Text = !npu ? (prov == "none" ? "Без модели: команды и ответы из интернета" : "Внешняя модель, настраивается на вкладке «Модель»")
                : App.GemmaLoaded ? "На NPU, около 3.8 ГБ памяти" : gemma ? "Загружается…" : "Выгружена: простые команды работают, разговор нет";
            StatusText.Text = "";
        }

        // ---------------- hotkeys (voice_settings.parse_hotkey format; Python re-registers within a second)

        const string DefaultDictation = "Ctrl+Alt+Win+W", DefaultGemma = "Ctrl+Win+Shift+G";
        const int CaptureSec = 10;
        Button? _capturing;
        readonly DispatcherTimer _captureTimer = new() { Interval = TimeSpan.FromMilliseconds(200) };
        DateTime _captureUntil;
        bool _captureWired;

        void ShowHotkeys()
        {
            var s = Read();
            HotkeyDictation.Content = (string?)s["hotkey_dictation"] ?? DefaultDictation;
            HotkeyGemma.Content = (string?)s["hotkey_gemma"] ?? DefaultGemma;
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
            string combo = (mods.HasFlag(ModifierKeys.Control) ? "Ctrl+" : "") + (mods.HasFlag(ModifierKeys.Alt) ? "Alt+" : "") +
                           (win ? "Win+" : "") + (mods.HasFlag(ModifierKeys.Shift) ? "Shift+" : "") + KeyName(key);
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

        void SaveName()
        {
            string name = NameBox.Text.Trim();
            if (name.Length < 2) { NameHint.Text = "Имя слишком короткое"; return; }
            var names = (Read()["wake_names"] as JsonArray)?.Select(x => (string)x!).ToList() ?? new List<string>();
            string low = name.ToLowerInvariant();
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
            ProvHint.Text = ProvNone.IsChecked == true ? "Команды, плагины и ответы из интернета работают без модели. Свободного разговора нет."
                : ProvNpu.IsChecked == true ? "Gemma 3 4B прямо на NPU этого компьютера, без интернета. Загрузка около 10 секунд, 3.8 ГБ памяти."
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

        /// <summary>"На NPU" only where there is an NPU: Gemma makes no sense on a CPU. Also: is Gemma downloaded.</summary>
        async Task DetectNpu()
        {
            var hw = await Task.Run(() => Stack.Cli("hw"));
            var m = await Task.Run(() => Stack.Cli("models"));
            _gemmaHere = (bool?)m["gemma"] == true;
            bool npu = (bool?)hw["npu"] == true;
            ProvNpu.Visibility = npu ? Visibility.Visible : Visibility.Collapsed;
            if (!npu && ProvNpu.IsChecked == true) ProvNone.IsChecked = true;
            ModelFields();
        }

        async Task GetGemma()
        {
            string token = HfTokenBox.Password.Trim();
            if (token.Length == 0) { GemmaState.Text = "Вставь токен Hugging Face."; return; }
            GemmaGetBtn.IsEnabled = false;
            GemmaState.Text = "Скачиваю Gemma, 3.3 ГБ. Это может занять от нескольких минут до получаса, окно можно закрыть позже…";
            var r = await Task.Run(() => Stack.Cli("gemma-download", token, 3 * 3600 * 1000));
            HfTokenBox.Password = "";
            GemmaGetBtn.IsEnabled = true;
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
            ModelStatus.Text = "Сохранено. " + (prov == "npu" ? "Gemma включается тумблером на вкладке «Основное»." : "");
            if (prov != "npu" && Stack.GemmaWanted) Task.Run(() => AppRef.SetGemma(false));  // free the NPU memory
        }

        async Task TestModel()
        {
            SaveModel();
            ModelStatus.Text = "Спрашиваю модель…";
            var r = await Task.Run(() => Stack.Cli("llm-test", null, 120000));
            ModelStatus.Text = r["error"] is { } err ? "Не ответила: " + err : $"Ответила за {r["seconds"]} с: «{r["text"]}»";
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

        async Task TgLink()
        {
            string token = TgTokenBox.Password.Trim();
            if (token.Length < 20) { TgStatus.Text = "Вставь токен бота от @BotFather"; return; }
            TgStatus.Text = "Ищу твоё сообщение /start…";
            var r = await Task.Run(() => Stack.Cli("tg-link " + Stack.Quote(token)));
            TgTokenBox.Password = "";
            TgStatus.Text = r["error"] is { } err ? "Не получилось: " + err
                : r["chat_id"] is null ? "Токен сохранён, но сообщений нет. Напиши боту /start и нажми «Привязать» ещё раз."
                : $"Привязано: {r["name"]} (чат {r["chat_id"]})";
        }

        // ---------------- commands page (JSON file, same format as commands.py)

        sealed record Act(string Do, string Title, string Arg, string ArgTitle);
        static readonly Act[] Actions =
        {
            new("open_app", "Открыть программу", "app", "ПРОГРАММА (НАЗВАНИЕ ИЛИ ПУТЬ)"),
            new("open_url", "Открыть сайт", "url", "АДРЕС ({*} = ХВОСТ ФРАЗЫ)"),
            new("open_folder", "Открыть папку", "path", "ПАПКА"),
            new("hotkey", "Нажать сочетание клавиш", "keys", "СОЧЕТАНИЕ, НАПРИМЕР Ctrl+Shift+M"),
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
            var m = await Task.Run(() => Stack.Cli("models"));
            string devs = string.Join(", ", (r["openvino"] as JsonArray)?.Select(x => (string)x!) ?? Array.Empty<string>());
            string models = string.Join(", ", m.Where(kv => kv.Value is JsonValue v && v.TryGetValue(out bool b) && b).Select(kv => kv.Key));
            HwText.Text = r["error"] is { } err ? "Не удалось определить железо: " + err
                : $"Найдено: {devs}" + ((string?)r["gpu"] is { Length: > 0 } g ? $" ({g})" : "") +
                  $". Рекомендую: {r["recommend"]}. Модели распознавания: {(models.Length > 0 ? models : "нет")}.";
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
                SysStatus.Text = "Сохранено на рабочий стол: " + Path.GetFileName(dest) + ". Токены и ключи не входят, их вводят заново.";
            }
            catch (Exception e) { SysStatus.Text = "Не получилось: " + e.Message; }
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
                SysStatus.Text = "Настройки загружены";
            }
            catch (Exception e) { SysStatus.Text = "Не получилось: " + e.Message; }
        }

        async Task Diagnostics()
        {
            SysStatus.Text = "Собираю…";
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
                SysStatus.Text = "Отчёт на рабочем столе: " + Path.GetFileName(dest) + ". Токенов и ключей в нём нет.";
            }
            catch (Exception e) { SysStatus.Text = "Не получилось: " + e.Message; }
        }

        /// <summary>Update check against a small JSON {"version": "x.y.z", "url": "..."} (settings "update_url").
        /// Empty until SaySo is published.</summary>
        async Task CheckUpdate()
        {
            string url = (string?)Read()["update_url"] ?? "";
            if (url.Length == 0) { UpdateStatus.Text = "Адрес обновлений появится после публикации SaySo"; return; }
            try
            {
                using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                var j = JsonNode.Parse(await http.GetStringAsync(url))!;
                var latest = Version.Parse((string)j["version"]!);
                var mine = typeof(App).Assembly.GetName().Version!;
                if (latest > mine) { UpdateStatus.Text = $"Есть версия {latest}. Открываю страницу загрузки"; Stack.Open((string)j["url"]!); }
                else UpdateStatus.Text = "Установлена последняя версия";
            }
            catch (Exception e) { UpdateStatus.Text = "Не удалось проверить: " + e.Message; }
        }
    }
}
