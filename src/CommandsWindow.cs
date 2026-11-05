using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace ClawVoice
{
    /// <summary>"Что умеет": commands.txt (written by gemma_agent.py, plugins included) as cards, one per section
    /// ("🔊 Звук и экран: громкость 40 · громче, тише"). Opens at ~90% of the screen with a scroll, so it fits a
    /// handheld at 175% scaling; the corner button switches to the full screen, the columns follow the width.</summary>
    public sealed class CommandsWindow : Window
    {
        readonly Grid _columns = new();
        readonly List<Border> _cards = new();
        int _shownColumns;

        /// <summary>The cards one by one (--render skill-cards: a picture per card).</summary>
        public IReadOnlyList<Border> Cards => _cards;

        static Style S(string key) => (Style)Application.Current.FindResource(key);

        public CommandsWindow()
        {
            Title = "Что умеет SaySo";
            Icon = System.Windows.Media.Imaging.BitmapFrame.Create(new Uri("pack://application:,,,/app.ico"));
            WindowStyle = WindowStyle.None; AllowsTransparency = true; Background = Brushes.Transparent;
            ResizeMode = ResizeMode.NoResize;
            FontFamily = new FontFamily("Segoe UI Variable Text, Segoe UI"); FontSize = 14;
            SetResourceReference(ForegroundProperty, "Fg");
            Rect wa = SystemParameters.WorkArea;
            Width = Math.Min(1180, wa.Width * 0.92);
            Height = wa.Height * 0.9;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;

            var title = new TextBlock { Text = "Что умеет SaySo", FontSize = 22, FontWeight = FontWeights.SemiBold };
            var sub = new TextBlock { Text = "Скажи фразу после кнопки команды или по имени. Точные слова не важны: «сделай потише» тоже сработает.",
                                      TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 4, 0, 0), FontSize = 13 };
            sub.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
            var heading = new StackPanel { Margin = new Thickness(0, 0, 12, 0) };
            heading.Children.Add(title); heading.Children.Add(sub);

            // round like the close button: Segoe Fluent "full screen" / "back to window"
            var icon = new TextBlock { Text = "", FontFamily = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"), FontSize = 11,
                                       HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            icon.SetResourceReference(TextBlock.ForegroundProperty, "Fg");
            var full = new Border { Width = 30, Height = 30, CornerRadius = new CornerRadius(15), Child = icon, Cursor = Cursors.Hand,
                                    Margin = new Thickness(0, 0, 8, 0), ToolTip = "На весь экран" };
            full.SetResourceReference(Border.BackgroundProperty, "Ghost");
            full.MouseLeftButtonUp += (_, e) => { ToggleFull(full, icon); e.Handled = true; };
            full.MouseLeftButtonDown += (_, e) => e.Handled = true;  // not a drag of the window
            var close = new Button { Style = S("Close") };
            close.Click += (_, _) => Close();
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Top };
            buttons.Children.Add(full); buttons.Children.Add(close);

            var header = new Grid { Margin = new Thickness(0, 0, 0, 14), Background = Brushes.Transparent };
            header.ColumnDefinitions.Add(new ColumnDefinition());
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            Grid.SetColumn(buttons, 1);
            header.Children.Add(heading); header.Children.Add(buttons);
            header.MouseLeftButtonDown += (_, _) => { if (WindowState == WindowState.Normal) try { DragMove(); } catch { } };

            foreach (var (head, note, items) in Sections()) _cards.Add(Card(head, note, items));
            var scroll = new ScrollViewer { Content = _columns, VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                                            PanningMode = PanningMode.VerticalOnly, Focusable = false };
            var root = new DockPanel { Margin = new Thickness(24, 18, 16, 18) };
            DockPanel.SetDock(header, Dock.Top);
            root.Children.Add(header); root.Children.Add(scroll);
            Content = new Border { Style = S("Frame"), Child = root };
            SizeChanged += (_, _) => Relayout(ActualWidth);
            KeyDown += (_, e) => { if (e.Key == Key.Escape) Close(); };
        }

        void ToggleFull(Border b, TextBlock icon)
        {
            WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
            bool max = WindowState == WindowState.Maximized;
            icon.Text = max ? "" : "";
            b.ToolTip = max ? "Обычный размер" : "На весь экран";
        }

        /// <summary>Cards spread over as many columns as fit (~300 px each), shortest column first.</summary>
        public void Relayout(double width)
        {
            int n = Math.Max(1, Math.Min(4, (int)((width - 60) / 300)));
            if (n == _shownColumns) return;
            _shownColumns = n;
            foreach (var c in _cards) (c.Parent as Panel)?.Children.Remove(c);
            _columns.Children.Clear();
            _columns.ColumnDefinitions.Clear();
            var cols = new List<StackPanel>();
            var weight = new List<int>();
            for (int i = 0; i < n; i++)
            {
                _columns.ColumnDefinitions.Add(new ColumnDefinition());
                var col = new StackPanel { Margin = new Thickness(0, 0, i < n - 1 ? 12 : 8, 0) };
                Grid.SetColumn(col, i);
                _columns.Children.Add(col);
                cols.Add(col);
                weight.Add(0);
            }
            foreach (var c in _cards)
            {
                int i = weight.IndexOf(weight.Min());
                cols[i].Children.Add(c);
                weight[i] += 2 + ((c.Tag as int?) ?? 1);
            }
        }

        static Border Card(string head, string note, List<string> items)
        {
            var p = new StackPanel();
            p.Children.Add(new TextBlock { Text = head, FontSize = 16, FontWeight = FontWeights.SemiBold, TextWrapping = TextWrapping.Wrap });
            if (note.Length > 0)
            {
                var n = new TextBlock { Text = note, FontSize = 12, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 2, 0, 0) };
                n.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
                p.Children.Add(n);
            }
            var list = new StackPanel { Margin = new Thickness(0, 8, 0, 0) };
            foreach (var it in items)
            {
                var row = new Grid { Margin = new Thickness(0, 2, 0, 2) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(14) });
                row.ColumnDefinitions.Add(new ColumnDefinition());
                var dot = new TextBlock { Text = "•" };
                dot.SetResourceReference(TextBlock.ForegroundProperty, "Accent");
                var t = new TextBlock { Text = it, TextWrapping = TextWrapping.Wrap, LineHeight = 20 };
                Grid.SetColumn(t, 1);
                row.Children.Add(dot); row.Children.Add(t);
                list.Children.Add(row);
            }
            p.Children.Add(list);
            var card = new Border { CornerRadius = new CornerRadius(14), Padding = new Thickness(16, 12, 16, 14), Margin = new Thickness(0, 0, 0, 12),
                                    Child = p, Tag = items.Count + (note.Length > 0 ? 1 : 0) };
            card.SetResourceReference(Border.BackgroundProperty, "Card");
            return card;
        }

        /// <summary>"🔊 Звук и экран (note): a · b · c" -> ("🔊 Звук и экран", "note", [a, b, c]).</summary>
        static IEnumerable<(string, string, List<string>)> Sections()
        {
            string[]? lines = null;
            try { lines = File.ReadAllLines(Stack.CommandsFile); }
            catch { }
            if (lines == null) { yield return ("Список появится после первого запуска диктовки", "", new List<string>()); yield break; }
            foreach (var raw in lines)
            {
                string line = raw.Trim();
                int colon = line.IndexOf(": ", StringComparison.Ordinal);
                if (colon < 0 || line.Contains("SaySo: голосовой помощник")) continue;
                string head = line[..colon].Trim(), note = "";
                var m = Regex.Match(head, @"^(.*?)\s*\((.+)\)$");
                if (m.Success) { head = m.Groups[1].Value; note = char.ToUpper(m.Groups[2].Value[0]) + m.Groups[2].Value[1..]; }
                var items = line[(colon + 2)..].Split(" · ", StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                                .Select(i => char.ToUpper(i[0]) + i[1..]).ToList();
                yield return (head, note, items);
            }
        }
    }
}
