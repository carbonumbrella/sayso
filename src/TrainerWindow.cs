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
    /// <summary>"Тренировать имя": no model training, only Whisper transcripts. Name rounds ("Скажи: Мурмур") collect
    /// how Whisper spells the name for this voice. A spelling that is also an ordinary word would wake the assistant
    /// on normal talk: those are dropped by a built-in list of common words and Whisper's noise words, plus two
    /// look-alike phrases the user reads (reading ordinary sentences caught almost nothing). The result is settings
    /// "wake_names"; another run for the same name adds to it, so it can be trained in quiet and over music.
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
            Responsive.Attach(this, 620);  // re-centered and width-capped on rotation
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
            _prompt.Text = "Около минуты";
            _hint.Text = $"Я буду показывать фразы. После сигнала читай их вслух обычным голосом, у каждой есть 3 секунды. " +
                         $"Почти все фразы с именем: так я узнаю, как распознаётся «{name}». Если помощника часто зовёшь " +
                         "под музыку или в игре, включи их: варианты из разных прогонов складываются. Диктовка должна быть включена.";
        }

        static string Name() => (string?)SettingsWindow.Read()["assistant_name"] ?? "Сэйсо";

        // A name spelling equal to one of these would fire on ordinary talk or on Whisper's noise words
        static readonly HashSet<string> Common = new(StringComparer.Ordinal)
        {
            "сейчас", "спасибо", "пожалуйста", "смешно", "смешка", "счастье", "давай", "давайте", "сделай", "сколько",
            "скажи", "слушай", "смотри", "стоп", "хорошо", "привет", "пока", "ладно", "так", "там", "тут", "это",
            "что", "как", "где", "все", "всё", "вот", "ну", "да", "нет", "мама", "мне", "меня", "может", "можно",
            "сегодня", "завтра", "потом", "тоже", "очень", "продолжение", "субтитры", "музыка", "спокойной", "господи",
        };

        async Task Run()
        {
            if (_running) return;
            _running = true;
            _start.IsEnabled = false; _save.IsEnabled = false;
            _pos.Clear(); _neg.Clear(); _bare.Clear();
            string n = Name();
            var positives = new[] { n, $"{n}, сколько времени", n, $"Привет, {n}", n, $"{n}, громкость сорок", n,
                                    $"{n}, открой блокнот", n, $"{n}, стоп", n, $"{n}, какая погода" };
            // look-alikes read aloud: the name's first syllable doubled ("Сэй-сэй" for "Сэйсо") and a sentence that
            // starts with the most common word Whisper confuses names with
            var negatives = new[] { $"Ну что, {FirstSyllable(n)}-{FirstSyllable(n).ToLowerInvariant()}", "Сейчас пойдём гулять" };
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
                if (positive) _bare.Add(phrase == n);
                _heard.Text = $"Услышала: «{heard}»";
            }
            _running = false;
            Summary();
        }

        /// <summary>Up to the first vowel, with a following й: "Сэй" for "Сэйсо", "Ге" for "Гемма", "Му" for "Мурмур".</summary>
        static string FirstSyllable(string n)
        {
            int v = n.IndexOfAny("аеёиоуыэюяaeiouy".ToCharArray().Concat("АЕЁИОУЫЭЮЯAEIOUY".ToCharArray()).ToArray());
            if (v < 0 || v + 1 >= n.Length) return n;
            return n[..(v + 1)] + (char.ToLowerInvariant(n[v + 1]) == 'й' ? n[v + 1].ToString() : "");
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

        /// <summary>1 = the same, 0 = nothing in common: 1 - edit distance / longer length, е and э as one letter,
        /// spaces and hyphens ignored ("си сэр" is compared as "сисер").</summary>
        static double Similarity(string a, string b)
        {
            static string K(string s) => Regex.Replace(s.Replace('э', 'е'), @"[\s-]", "");
            a = K(a); b = K(b);
            if (a.Length == 0 || b.Length == 0) return 0;
            var d = new int[a.Length + 1, b.Length + 1];
            for (int i = 0; i <= a.Length; i++) d[i, 0] = i;
            for (int j = 0; j <= b.Length; j++) d[0, j] = j;
            for (int i = 1; i <= a.Length; i++)
                for (int j = 1; j <= b.Length; j++)
                    d[i, j] = Math.Min(Math.Min(d[i - 1, j] + 1, d[i, j - 1] + 1), d[i - 1, j - 1] + (a[i - 1] == b[j - 1] ? 0 : 1));
            return 1.0 - (double)d[a.Length, b.Length] / Math.Max(a.Length, b.Length);
        }

        /// <summary>The name as Whisper wrote it, without a greeting. A bare-name round is the name as a whole, also when
        /// Whisper split it ("Сей со"); in a phrase it is the part before the first comma or stop, if that part is short,
        /// else the first word(s).</summary>
        static string? Spelling(string heard, int nameWords, bool bare)
        {
            string head = bare ? heard : Regex.Split(heard, @"[,.!?;:]")[0];
            var words = Norm(head).Split(' ', StringSplitOptions.RemoveEmptyEntries).ToList();
            if (words.Count > 0 && Regex.IsMatch(words[0], "^(привет|эй|окей|ok|hey|hi)")) words.RemoveAt(0);
            if (words.Count == 0) return null;
            int take = words.Count <= nameWords + 1 ? words.Count : nameWords;
            return string.Join(" ", words.Take(take));
        }

        readonly List<bool> _bare = new();
        List<string> _variants = new();

        void Summary()
        {
            string n = Name();
            int nameWords = Norm(n).Split(' ', StringSplitOptions.RemoveEmptyEntries).Length;
            var counts = _pos.Select((h, i) => Spelling(h, nameWords, i < _bare.Count && _bare[i])).Where(x => x != null)
                             .GroupBy(x => x!).ToDictionary(g => g.Key, g => g.Count());
            // a spelling that is a common word, or that starts a look-alike phrase, would wake the assistant by mistake
            var bad = counts.Keys.Where(v => v != Norm(n) && (Common.Contains(v) || v.Split(' ').All(Common.Contains)
                                             || _neg.Any(h => Norm(h).StartsWith(v + " ") || Norm(h) == v))).ToHashSet();
            // and a spelling that has little to do with the name ("три", "пейте" for "Сэйсо", misheard over music)
            // would wake it on random words
            foreach (var v in counts.Keys.Where(v => Similarity(v, Norm(n)) < 0.65)) bad.Add(v);
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
            // the list starts with the name it was trained for: a run for the same name adds to it (quiet, then music)
            if (old.Count > 0 && old[0] == n) list.AddRange(old.Where(o => !list.Contains(o)));
            list.AddRange(old.Where(o => !list.Contains(o) && o == "гемма"));  // this PC's old name keeps working
            SettingsWindow.Set("wake_names", new JsonArray(list.Distinct().Select(x => (JsonNode)x!).ToArray()));
            _hint.Text = "Сохранено. Новое распознавание имени работает сразу.";
            _save.IsEnabled = false;
        }
    }
}
