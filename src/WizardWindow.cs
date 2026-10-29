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
                        heard.Text = "Говори…";
                        string r = await Record(4);
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
                    bool npu = (bool?)_hw["npu"] == true && Directory.Exists(Path.Combine(Stack.Dir, "gemma-3-4b-it-int4-cw-ov-intel"));
                    var prov = (string?)(s["llm"] as JsonObject)?["provider"] ?? (npu ? "npu" : "none");
                    var mseg = new UniformGrid { Rows = 1 };
                    foreach (var (title, code, enabled) in new[] { ("Без модели", "none", true), ("На NPU", "npu", npu), ("Свой сервер", "server", true) })
                    {
                        var rb = new RadioButton { Content = title, GroupName = "wprov", Style = S("Seg"), IsEnabled = enabled, IsChecked = prov == code };
                        rb.Click += (_, _) => SettingsWindow.Set("llm", new JsonObject
                            { ["provider"] = code, ["url"] = code == "server" ? "http://127.0.0.1:11434" : "http://127.0.0.1:8081", ["model"] = "" });
                        mseg.Children.Add(rb);
                    }
                    _body.Children.Add(Section("МОДЕЛЬ"));
                    _body.Children.Add(new Border { Style = S("SegBox"), Child = mseg });
                    var note = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 12, Margin = new Thickness(4, 8, 0, 0),
                        Text = npu ? "На этом компьютере есть NPU и модель Gemma: она будет работать локально."
                                   : "Gemma на NPU недоступна здесь. Свой сервер (Ollama, LM Studio) и облако настраиваются на вкладке «Модель»." };
                    note.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
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

        static async Task<string> Record(int sec)
        {
            try
            {
                using var udp = new UdpClient(0, AddressFamily.InterNetwork);
                byte[] req = Encoding.UTF8.GetBytes($"{{\"cmd\":\"record\",\"sec\":{sec}}}");
                await udp.SendAsync(req, req.Length, new IPEndPoint(IPAddress.Loopback, 47323));
                var recv = udp.ReceiveAsync();
                if (await Task.WhenAny(recv, Task.Delay(sec * 1000 + 15000)) != recv) return "!диктовка ещё запускается, попробуй через минуту";
                var j = JsonNode.Parse(Encoding.UTF8.GetString(recv.Result.Buffer))!;
                return j["error"] is { } e ? "!" + e : ((string?)j["text"] ?? "").Trim();
            }
            catch (Exception e) { return "!" + e.Message; }
        }

        void Finish()
        {
            SettingsWindow.Set("setup_done", true);
            ((App)Application.Current).RestartDictationSoon();  // language / microphone from the wizard
            Close();
        }
    }
}
