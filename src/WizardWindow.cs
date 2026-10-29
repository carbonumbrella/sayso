using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;

namespace ClawVoice
{
    /// <summary>First start after installing (settings without "setup_done"): language, microphone with a test
    /// recording, hotkeys, language model, then a first command to try. Everything can be changed later in Settings.</summary>
    public sealed class WizardWindow : Window
    {
        readonly TextBlock _title, _text;
        readonly StackPanel _body;
        readonly Button _back, _next;
        int _page;
        JsonObject _hw = new();

        static Style S(string key) => (Style)Application.Current.FindResource(key);

        public WizardWindow()
        {
            Title = "SaySo"; Width = 640; SizeToContent = SizeToContent.Height; ResizeMode = ResizeMode.NoResize;
            WindowStyle = WindowStyle.None; AllowsTransparency = true; Background = Brushes.Transparent;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            FontFamily = new FontFamily("Segoe UI Variable Text, Segoe UI"); FontSize = 14;
            Icon = System.Windows.Media.Imaging.BitmapFrame.Create(new Uri("pack://application:,,,/app.ico"));
            SetResourceReference(ForegroundProperty, "Fg");
            _title = new TextBlock { FontSize = 22, FontWeight = FontWeights.SemiBold };
            _text = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 10, 0, 0) };
            _text.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
            _body = new StackPanel { Margin = new Thickness(0, 16, 0, 0), MinHeight = 150 };
            _back = new Button { Content = "Назад", Style = S("Flat") };
            _next = new Button { Content = "Дальше", Style = S("Flat"), Foreground = Brushes.White };
            _next.SetResourceReference(BackgroundProperty, "Accent");
            _back.Click += (_, _) => Show(_page - 1);
            _next.Click += (_, _) => { if (_page == 4) Finish(); else Show(_page + 1); };
            var buttons = new Grid { Margin = new Thickness(0, 20, 0, 0) };
            buttons.ColumnDefinitions.Add(new ColumnDefinition());
            buttons.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(8) });
            buttons.ColumnDefinitions.Add(new ColumnDefinition());
            Grid.SetColumn(_next, 2);
            buttons.Children.Add(_back); buttons.Children.Add(_next);
            var panel = new StackPanel { Margin = new Thickness(28, 24, 28, 24) };
            foreach (var c in new UIElement[] { _title, _text, _body, buttons }) panel.Children.Add(c);
            Content = new Border { Style = S("Frame"), Child = panel };
            MouseLeftButtonDown += (_, _) => { try { DragMove(); } catch { } };
            Task.Run(() => _hw = Stack.Cli("hw"));
            Show(0);
        }

        static TextBlock Section(string t)
        {
            var b = new TextBlock { Text = t, Style = S("Label") };
            return b;
        }

        void Show(int page)
        {
            _page = Math.Clamp(page, 0, 4);
            _body.Children.Clear();
            _back.Visibility = _page == 0 ? Visibility.Hidden : Visibility.Visible;
            _next.Content = _page == 4 ? "Готово" : "Дальше";
            var s = SettingsWindow.Read();
            string name = (string?)s["assistant_name"] ?? "Мурмур";
            switch (_page)
            {
                case 0:
                    _title.Text = "Привет! Это SaySo";
                    _text.Text = "Голосовой помощник, который работает на этом компьютере: диктовка в любое поле ввода и команды голосом. " +
                                 "Настроим за минуту.";
                    _body.Children.Add(Section("ЯЗЫК"));
                    var seg = new UniformGrid { Rows = 1 };
                    foreach (var (title, code) in new[] { ("Русский", "ru"), ("English", "en"), ("Авто", "auto") })
                    {
                        var rb = new RadioButton { Content = title, GroupName = "wlang", Style = S("Seg"),
                                                   IsChecked = ((string?)s["language"] ?? "ru") == code };
                        rb.Click += (_, _) => SettingsWindow.Set("language", code);
                        seg.Children.Add(rb);
                    }
                    _body.Children.Add(new Border { Style = S("SegBox"), Child = seg });
                    break;
                case 1:
                    _title.Text = "Микрофон";
                    _text.Text = "Выбери микрофон и нажми «Проверить»: скажи любую фразу, и я покажу, что услышала.";
                    var box = new ComboBox { Style = S("Combo") };
                    var heard = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(4, 10, 0, 0) };
                    var test = new Button { Content = "Проверить", Style = S("Flat"), Margin = new Thickness(0, 10, 0, 0) };
                    _body.Children.Add(Section("МИКРОФОН")); _body.Children.Add(box); _body.Children.Add(test); _body.Children.Add(heard);
                    Task.Run(() => Stack.Cli("mics")).ContinueWith(t => Dispatcher.Invoke(() =>
                    {
                        var list = new List<string> { "Микрофон по умолчанию" };
                        if (t.Result["devices"] is JsonArray a) list.AddRange(a.Select(x => (string)x!));
                        box.ItemsSource = list;
                        box.SelectedIndex = Math.Max(0, list.IndexOf((string?)SettingsWindow.Read()["mic_device"] ?? ""));
                        box.SelectionChanged += (_, _) =>
                        {
                            SettingsWindow.Set("mic_device", box.SelectedIndex <= 0 ? "" : (string)box.SelectedItem);
                            ((App)Application.Current).RestartDictationSoon();
                            heard.Text = "Микрофон сменён, через пару секунд можно проверять";
                        };
                    }));
                    test.Click += async (_, _) =>
                    {
                        test.IsEnabled = false;
                        string r = await Record(4, s => heard.Text = s);
                        test.IsEnabled = true;
                        heard.Text = r.StartsWith("!") ? "Не получилось: " + r[1..] : r.Length == 0 ? "Ничего не услышала. Проверь микрофон"
                                   : $"Услышала: «{r}»";
                    };
                    break;
                case 2:
                    _title.Text = "Кнопки";
                    string dict = (string?)s["hotkey_dictation"] ?? "Ctrl+Alt+Win+W", cmd = (string?)s["hotkey_gemma"] ?? "Ctrl+Win+Shift+G";
                    _text.Text = $"Диктовка: держи {dict}, говори, отпусти: текст вставится туда, где курсор. " +
                                 $"Команда: нажми {cmd} и скажи, например, «громкость сорок». " +
                                 $"А ещё можно позвать по имени: «{name}, сколько времени?». Сочетания меняются в настройках.";
                    break;
                case 3:
                    _title.Text = "Языковая модель (необязательно)";
                    _text.Text = "Без модели работают все команды, диктовка и ответы из интернета. Модель нужна для свободного разговора " +
                                 "и сложных просьб.";
                    // Gemini (free key) for everyone; Gemma only where there is an NPU (on a CPU it makes no sense)
                    bool npu = (bool?)_hw["npu"] == true;
                    bool gemma = (bool?)Stack.Cli("models")["gemma"] == true;
                    var prov = (string?)(s["llm"] as JsonObject)?["provider"] ?? "none";
                    var mseg = new UniformGrid { Rows = 1 };
                    var keyPanel = new StackPanel { Visibility = prov == "cloud" ? Visibility.Visible : Visibility.Collapsed };
                    var note = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 12, Margin = new Thickness(4, 8, 0, 0) };
                    note.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
                    void Note(string code) => note.Text = code == "cloud"
                        ? "Бесплатный ключ Gemini: нажми «Получить ключ», войди в Google-аккаунт, нажми «Create API key» и вставь ключ сюда."
                        : code == "npu" ? (gemma ? "Gemma уже скачана и будет работать прямо на NPU, без интернета."
                                                 : "Gemma скачивается потом в настройках, вкладка «Модель». Нужен бесплатный аккаунт Hugging Face.")
                        : "Модель можно подключить позже на вкладке «Модель».";
                    var options = new List<(string, string)> { ("Без модели", "none"), ("Gemini, бесплатно", "cloud") };
                    if (npu) options.Add(("На NPU", "npu"));
                    foreach (var (title, code) in options)
                    {
                        var rb = new RadioButton { Content = title, GroupName = "wprov", Style = S("Seg"), IsChecked = prov == code };
                        rb.Click += (_, _) =>
                        {
                            SettingsWindow.Set("llm", new JsonObject
                            {
                                ["provider"] = code,
                                ["url"] = code == "cloud" ? "https://generativelanguage.googleapis.com/v1beta/openai" : "http://127.0.0.1:8081",
                                ["model"] = code == "cloud" ? "gemini-3.5-flash-lite" : ""
                            });
                            keyPanel.Visibility = code == "cloud" ? Visibility.Visible : Visibility.Collapsed;
                            Note(code);
                        };
                        mseg.Children.Add(rb);
                    }
                    _body.Children.Add(Section("МОДЕЛЬ"));
                    _body.Children.Add(new Border { Style = S("SegBox"), Child = mseg });
                    var key = new PasswordBox { Style = S("Secret") };
                    var keyRow = new Grid { Margin = new Thickness(0, 8, 0, 0) };
                    keyRow.ColumnDefinitions.Add(new ColumnDefinition());
                    keyRow.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(8) });
                    keyRow.ColumnDefinitions.Add(new ColumnDefinition());
                    var getKey = new Button { Content = "Получить ключ", Style = S("Flat") };
                    getKey.Click += (_, _) => Stack.Open("https://aistudio.google.com/apikey");
                    var saveKey = new Button { Content = "Сохранить ключ", Style = S("Flat") };
                    saveKey.Click += (_, _) =>
                    {
                        if (key.Password.Trim().Length == 0) return;
                        var r = Stack.Cli("secret llm_api_key", key.Password.Trim());
                        key.Password = "";
                        note.Text = r["error"] is { } err ? "Ключ не сохранён: " + err : "Ключ сохранён. Можно говорить «давай поговорим».";
                    };
                    Grid.SetColumn(saveKey, 2);
                    keyRow.Children.Add(getKey);
                    keyRow.Children.Add(saveKey);
                    keyPanel.Children.Add(Section("КЛЮЧ GEMINI (ХРАНИТСЯ ЗАШИФРОВАННЫМ)"));
                    keyPanel.Children.Add(key);
                    keyPanel.Children.Add(keyRow);
                    _body.Children.Add(keyPanel);
                    Note(prov);
                    _body.Children.Add(note);
                    break;
                case 4:
                    _title.Text = "Готово";
                    _text.Text = $"Попробуй: «{name}, сколько времени?» или нажми {(string?)s["hotkey_gemma"] ?? "Ctrl+Win+Shift+G"} и скажи " +
                                 "«что ты умеешь». Чтобы я лучше узнавала имя по твоему голосу, пройди короткую тренировку.";
                    var train = new Button { Content = "Тренировать имя", Style = S("Flat") };
                    train.Click += (_, _) => ((App)Application.Current).ShowTrainer();
                    _body.Children.Add(train);
                    break;
            }
        }

        /// <summary>Records through dictate.py (UDP 47323). UDP "connection reset" means nobody listens yet: right
        /// after the microphone is changed dictate.py restarts, and on a CPU (AMD) the model takes 10-20 s to load,
        /// so keep asking for up to 40 s before calling it not running.</summary>
        static async Task<string> Record(int sec, Action<string> status)
        {
            var until = DateTime.Now.AddSeconds(40);
            while (true)
            {
                try
                {
                    status("Говори…");
                    using var udp = new UdpClient(0, AddressFamily.InterNetwork);
                    byte[] req = Encoding.UTF8.GetBytes($"{{\"cmd\":\"record\",\"sec\":{sec}}}");
                    await udp.SendAsync(req, req.Length, new IPEndPoint(IPAddress.Loopback, 47323));
                    var recv = udp.ReceiveAsync();
                    if (await Task.WhenAny(recv, Task.Delay(sec * 1000 + 15000)) != recv) return "!диктовка ещё запускается, попробуй через минуту";
                    var j = JsonNode.Parse(Encoding.UTF8.GetString(recv.Result.Buffer))!;
                    return j["error"] is { } e ? "!" + e : ((string?)j["text"] ?? "").Trim();
                }
                catch (Exception e) when (e is SocketException { SocketErrorCode: SocketError.ConnectionReset } ||
                                          e.InnerException is SocketException { SocketErrorCode: SocketError.ConnectionReset })
                {
                    if (DateTime.Now > until)
                        return "!распознавание речи не запустилось. Причина в журнале dictate.log в папке программы";
                    status("Распознавание ещё запускается, подожди несколько секунд…");
                    await Task.Delay(2000);
                }
                catch (Exception e) { return "!" + e.Message; }
            }
        }

        void Finish()
        {
            SettingsWindow.Set("setup_done", true);
            ((App)Application.Current).RestartDictationSoon();  // language / microphone from the wizard
            Close();
        }
    }
}
