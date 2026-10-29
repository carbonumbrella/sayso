using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Media;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace ClawVoice
{
    /// <summary>"Тренировать имя": no model training, only Whisper transcripts. Positive rounds ("Скажи: Мурмур")
    /// collect how Whisper spells the name for this voice; negative rounds (ordinary phrases, look-alike words)
    /// drop spellings that also appear in normal speech. The result is settings "wake_names".
    /// Recording happens in dictate.py (UDP 47323), which mutes the wake listener meanwhile.</summary>
    public sealed class TrainerWindow : Window
    {
        const int Port = 47323, RecordSec = 3;
        readonly TextBlock _title, _prompt, _hint, _heard;
        readonly ProgressBar _bar;
        readonly Button _start, _save, _close;
        readonly List<string> _pos = new(), _neg = new();
        bool _running;

        public TrainerWindow()
        {
            Title = "Тренировка имени"; Width = 620; SizeToContent = SizeToContent.Height; ResizeMode = ResizeMode.NoResize;
            WindowStyle = WindowStyle.None; AllowsTransparency = true; Background = Brushes.Transparent;
            WindowStartupLocation = WindowStartupLocation.CenterScreen; Topmost = true;
            FontFamily = new FontFamily("Segoe UI Variable Text, Segoe UI"); FontSize = 14;
            Icon = System.Windows.Media.Imaging.BitmapFrame.Create(new Uri("pack://application:,,,/app.ico"));
            SetResourceReference(ForegroundProperty, "Fg");

            _title = new TextBlock { FontSize = 19, FontWeight = FontWeights.SemiBold };
            _prompt = new TextBlock { FontSize = 26, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap,
                                      Margin = new Thickness(0, 22, 0, 8), MinHeight = 70 };
            _hint = new TextBlock { FontSize = 13, TextWrapping = TextWrapping.Wrap };
            _hint.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
            _bar = new ProgressBar { Height = 4, Margin = new Thickness(0, 14, 0, 0), Maximum = 1, BorderThickness = new Thickness(0) };
            _bar.SetResourceReference(Control.ForegroundProperty, "Accent");
            _bar.SetResourceReference(Control.BackgroundProperty, "Ghost");
            _heard = new TextBlock { FontSize = 13, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 12, 0, 0) };
            _heard.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
            _start = new Button { Content = "Начать", Style = (Style)Application.Current.FindResource("Flat"), Foreground = Brushes.White };
            _start.SetResourceReference(BackgroundProperty, "Accent");
            _save = new Button { Content = "Сохранить", Style = (Style)Application.Current.FindResource("Flat"), IsEnabled = false };
            _close = new Button { Content = "Закрыть", Style = (Style)Application.Current.FindResource("Flat") };
            _start.Click += async (_, _) => await Run();
            _save.Click += (_, _) => Save();
            _close.Click += (_, _) => { _running = false; Close(); };

            var buttons = new Grid { Margin = new Thickness(0, 22, 0, 0) };
            for (int i = 0; i < 5; i++) buttons.ColumnDefinitions.Add(new ColumnDefinition { Width = i % 2 == 1 ? new GridLength(8) : new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(_start, 0); Grid.SetColumn(_save, 2); Grid.SetColumn(_close, 4);
            buttons.Children.Add(_start); buttons.Children.Add(_save); buttons.Children.Add(_close);

            var panel = new StackPanel { Margin = new Thickness(26, 22, 26, 22) };
            foreach (var c in new UIElement[] { _title, _prompt, _hint, _bar, _heard, buttons }) panel.Children.Add(c);
            Content = new Border { Style = (Style)Application.Current.FindResource("Frame"), Child = panel };
            MouseLeftButtonDown += (_, _) => { try { DragMove(); } catch { } };

            string name = Name();
            _title.Text = $"Тренировка имени «{name}»";
            _prompt.Text = "Около двух минут";
            _hint.Text = $"Я буду показывать фразы. После сигнала читай их вслух обычным голосом, у каждой есть 3 секунды. " +
                         $"Сначала имя, потом обычные фразы: так я узнаю, как распознаётся «{name}», и не спутаю его с другими словами. " +
                         "Диктовка должна быть включена.";
        }

        static string Name() => (string?)SettingsWindow.Read()["assistant_name"] ?? "Мурмур";

        static readonly string[] Ordinary =
        {
            "Сегодня хорошая погода, пойдём гулять", "Позвони маме вечером", "Мне нравится эта музыка",
            "Надо купить хлеб и молоко", "Кот мурлычет на диване", "Ну что, мур-мур, проголодался",
            "Посмотрим фильм после ужина", "Завтра рано вставать на работу",
        };

        async Task Run()
        {
            if (_running) return;
            _running = true;
            _start.IsEnabled = false; _save.IsEnabled = false;
            _pos.Clear(); _neg.Clear();
            string n = Name();
            var positives = new[] { n, $"{n}, открой блокнот", n, $"Привет, {n}", n, $"{n}, громкость сорок", n,
                                    $"{n}, сколько времени", n, $"{n}, стоп" };
            // a look-alike word for the user's own name: its first syllable doubled ("Сей-сей" for "Сейсо")
            string half = n.Length > 3 ? n[..(n.Length / 2)] : n;
            var negatives = Ordinary.Concat(new[] { $"Ну что, {half}-{half.ToLowerInvariant()}" }).ToArray();
            int total = positives.Length + negatives.Length, step = 0;
            foreach (var (phrase, positive) in positives.Select(p => (p, true)).Concat(negatives.Select(p => (p, false))))
            {
                if (!_running) return;
                step++;
                _title.Text = positive ? $"Скажи имя ({step} из {total})" : $"Прочитай фразу ({step} из {total})";
                _prompt.Text = "«" + phrase + "»";
                _hint.Text = "Приготовься…";
                await Task.Delay(1200);
                if (!_running) return;
                Beep();
                _hint.Text = "Говори";
                var rec = Record();
                var t0 = DateTime.Now;
                while (!rec.IsCompleted)
                {
                    _bar.Value = Math.Min(1, (DateTime.Now - t0).TotalSeconds / RecordSec);
                    await Task.Delay(50);
                }
                _bar.Value = 0;
                string heard = rec.Result;
                if (heard.StartsWith("!"))
                {
                    _hint.Text = "Не получилось записать: " + heard[1..] + ". Проверь, что диктовка включена.";
                    _running = false; _start.IsEnabled = true; _start.Content = "Заново";
                    return;
                }
                (positive ? _pos : _neg).Add(heard);
                _heard.Text = $"Услышала: «{heard}»";
            }
            _running = false;
            Summary();
        }

        static void Beep()
        {
            try { new SoundPlayer(Path.Combine(Stack.Dir, "beep_start.wav")).Play(); } catch { SystemSounds.Asterisk.Play(); }
        }

        static async Task<string> Record()
        {
            try
            {
                using var udp = new UdpClient(0, AddressFamily.InterNetwork);
                byte[] req = Encoding.UTF8.GetBytes($"{{\"cmd\":\"record\",\"sec\":{RecordSec}}}");
                await udp.SendAsync(req, req.Length, new IPEndPoint(IPAddress.Loopback, Port));
                var recv = udp.ReceiveAsync();
                if (await Task.WhenAny(recv, Task.Delay(RecordSec * 1000 + 15000)) != recv) return "!нет ответа от диктовки";
                var j = JsonNode.Parse(Encoding.UTF8.GetString(recv.Result.Buffer))!;
                return j["error"] is { } err ? "!" + err : ((string?)j["text"] ?? "").Trim();
            }
            catch (Exception e) { return "!" + e.Message; }
        }

        static string Norm(string s) => Regex.Replace(s.ToLowerInvariant().Replace('ё', 'е'), @"[^\p{L}\p{Nd}\s-]", " ").Trim();

        /// <summary>The name as Whisper wrote it: the first word(s) of a positive phrase, without a greeting.</summary>
        static string? Spelling(string heard, int nameWords)
        {
            var words = Norm(heard).Split(' ', StringSplitOptions.RemoveEmptyEntries).ToList();
            if (words.Count > 0 && Regex.IsMatch(words[0], "^(привет|эй|окей|ok|hey|hi)")) words.RemoveAt(0);
            if (words.Count == 0) return null;
            return string.Join(" ", words.Take(Math.Min(nameWords, words.Count)));
        }

        List<string> _variants = new();

        void Summary()
        {
            string n = Name();
            int nameWords = Norm(n).Split(' ', StringSplitOptions.RemoveEmptyEntries).Length;
            var counts = _pos.Select(h => Spelling(h, nameWords)).Where(x => x != null).GroupBy(x => x!)
                             .ToDictionary(g => g.Key, g => g.Count());
            // a spelling that also starts an ordinary phrase would wake the assistant by mistake
            var bad = counts.Keys.Where(v => _neg.Any(h => Norm(h).StartsWith(v + " ") || Norm(h) == v)).ToHashSet();
            _variants = counts.Where(kv => !bad.Contains(kv.Key) && kv.Key.Length >= 3).OrderByDescending(kv => kv.Value)
                              .Select(kv => kv.Key).ToList();
            int recognized = _pos.Count(h => _variants.Any(v => Norm(h).StartsWith(v)) || Norm(h).StartsWith(Norm(n)));
            _title.Text = "Готово";
            _prompt.Text = _variants.Count > 0 ? "Узнаю имя так: " + string.Join(", ", _variants.Take(6)) : "Имя не распознано";
            _hint.Text = $"Имя распознано в {recognized} из {_pos.Count} попыток." +
                         (bad.Count > 0 ? $" Убрала варианты, похожие на обычную речь: {string.Join(", ", bad)}." : "") +
                         (recognized < _pos.Count / 2 ? " Если узнаёт плохо, выбери имя, которое легче расслышать." : "") +
                         " Нажми «Сохранить».";
            _save.IsEnabled = _variants.Count > 0;
            _start.IsEnabled = true; _start.Content = "Ещё раз";
        }

        void Save()
        {
            string n = Name().ToLowerInvariant();
            var list = new List<string> { n };
            list.AddRange(_variants.Where(v => v != n));
            var old = (SettingsWindow.Read()["wake_names"] as JsonArray)?.Select(x => (string)x!).ToList() ?? new List<string>();
            list.AddRange(old.Where(o => !list.Contains(o) && o == "гемма"));  // this PC's old name keeps working
            SettingsWindow.Set("wake_names", new JsonArray(list.Distinct().Select(x => (JsonNode)x!).ToArray()));
            _hint.Text = "Сохранено. Новое распознавание имени работает сразу.";
            _save.IsEnabled = false;
        }
    }
}
