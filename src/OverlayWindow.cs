// On-screen messages of the voice assistant (was ClawOverlay.exe, now part of the tray app).
// Protocol: UDP 127.0.0.1:47330, one UTF-8 JSON per datagram:
//   {"kind":"ok|error|info|listen|think|hide|mic|mic_off","title":"...","text":"...","ms":3500}
// mic / mic_off: a pulsing mic badge on the current card (follow-up listening), mic keeps the card up for at
// least ms. listen/think stay until the next message (safety timeout 2 min); a new message replaces the
// current one, there is no queue. The window never takes focus and lets clicks through.
// CPU: every forever animation is started only on a visible card and stopped on hide (a running WPF
// animation keeps the render thread busy even on an invisible window: ClawOverlay idled at ~2% of a core).
using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;
using System.Windows.Shapes;
using System.Windows.Threading;

namespace ClawVoice
{
    public sealed class Msg
    {
        public string Kind { get; set; } = "info";
        public string Title { get; set; } = "";
        public string Text { get; set; } = "";
        public int Ms { get; set; }
    }

    public sealed class OverlayWindow : Window
    {
        readonly Border _card, _iconBg, _micBadge;
        readonly TextBlock _icon, _title, _text;
        readonly Ellipse _spinner;
        readonly TranslateTransform _slide = new();
        readonly DispatcherTimer _hide = new();
        bool _shown;

        public OverlayWindow()
        {
            WindowStyle = WindowStyle.None;
            AllowsTransparency = true;
            Background = Brushes.Transparent;
            Topmost = true;
            ShowInTaskbar = false;
            ShowActivated = false;
            Focusable = false;
            SizeToContent = SizeToContent.WidthAndHeight;
            ResizeMode = ResizeMode.NoResize;
            FontFamily = new FontFamily("Segoe UI Variable Text, Segoe UI");
            Opacity = 0;

            _icon = new TextBlock { FontFamily = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"), FontSize = 16,
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            _spinner = new Ellipse { Width = 22, Height = 22, StrokeThickness = 2.5, StrokeDashArray = new DoubleCollection { 5, 3 },
                RenderTransformOrigin = new Point(0.5, 0.5), RenderTransform = new RotateTransform(), Visibility = Visibility.Collapsed };
            var iconGrid = new Grid { Width = 36, Height = 36 };
            _iconBg = new Border { CornerRadius = new CornerRadius(18) };
            iconGrid.Children.Add(_iconBg);
            iconGrid.Children.Add(_icon);
            iconGrid.Children.Add(_spinner);

            _title = new TextBlock { FontSize = 13, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 1) };
            _title.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
            _text = new TextBlock { FontSize = 15, TextWrapping = TextWrapping.Wrap, MaxWidth = 520,
                TextTrimming = TextTrimming.CharacterEllipsis, MaxHeight = 92 };
            _text.SetResourceReference(TextBlock.ForegroundProperty, "Fg");
            var texts = new StackPanel { VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(12, 0, 4, 0) };
            texts.Children.Add(_title);
            texts.Children.Add(_text);

            var micGlyph = new TextBlock { Text = "", FontFamily = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"),
                FontSize = 13, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            micGlyph.SetResourceReference(TextBlock.ForegroundProperty, "Accent");
            _micBadge = new Border
            {
                Width = 26, Height = 26, CornerRadius = new CornerRadius(13), Margin = new Thickness(10, 0, 0, 0),
                VerticalAlignment = VerticalAlignment.Center, Visibility = Visibility.Collapsed,
                Background = new SolidColorBrush(Color.FromArgb(0x33, 0x3E, 0x7B, 0xFA)), Child = micGlyph,
            };
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            row.Children.Add(iconGrid);
            row.Children.Add(texts);
            row.Children.Add(_micBadge);

            _card = new Border
            {
                CornerRadius = new CornerRadius(18), Padding = new Thickness(14, 11, 18, 11), Margin = new Thickness(18),
                BorderThickness = new Thickness(1), Child = row, RenderTransform = _slide, MinWidth = 260,
                Effect = new DropShadowEffect { BlurRadius = 22, ShadowDepth = 4, Opacity = 0.35 },
            };
            _card.SetResourceReference(Border.BackgroundProperty, "Bg");
            _card.SetResourceReference(Border.BorderBrushProperty, "Border");
            Content = _card;
            _hide.Tick += (_, _) => { _hide.Stop(); FadeOut(); };
            SourceInitialized += (_, _) => MakeClickThrough();
            SizeChanged += (_, _) => Place();
        }

        const int GWL_EXSTYLE = -20, WS_EX_TRANSPARENT = 0x20, WS_EX_TOOLWINDOW = 0x80, WS_EX_NOACTIVATE = 0x08000000;
        [DllImport("user32.dll")] static extern int GetWindowLong(IntPtr h, int i);
        [DllImport("user32.dll")] static extern int SetWindowLong(IntPtr h, int i, int v);
        [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);

        void MakeClickThrough()
        {
            var h = new WindowInteropHelper(this).Handle;
            SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h, GWL_EXSTYLE) | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE);
        }

        void Place()
        {
            var wa = SystemParameters.WorkArea;
            Left = wa.Left + (wa.Width - ActualWidth) / 2;
            Top = wa.Top + 6;
        }

        /// <summary>Create the HWND and run one layout pass at startup, so the first message shows instantly.</summary>
        public void Prewarm()
        {
            Show();
            UpdateLayout();
            Hide();
        }

        static DoubleAnimation Pulse(double to) =>
            new(1, to, TimeSpan.FromSeconds(0.6)) { AutoReverse = true, RepeatBehavior = RepeatBehavior.Forever };

        void StopAnimations()
        {
            _spinner.RenderTransform.BeginAnimation(RotateTransform.AngleProperty, null);
            _iconBg.BeginAnimation(OpacityProperty, null);
            _micBadge.BeginAnimation(OpacityProperty, null);
        }

        public void ShowMessage(Msg m)
        {
            if (m.Kind is "mic" or "mic_off")
            {
                bool on = m.Kind == "mic";
                if (on && !_shown)
                {
                    ShowMessage(new Msg { Kind = "listen", Title = "Gemma", Text = "Слушаю…", Ms = m.Ms });
                    return;  // the listen card pulses itself; no badge on a card that only says "Слушаю…"
                }
                _micBadge.Visibility = on && _shown ? Visibility.Visible : Visibility.Collapsed;
                _micBadge.BeginAnimation(OpacityProperty, on && _shown ? Pulse(0.3) : null);
                if (on && m.Ms > 0 && (!_hide.IsEnabled || _hide.Interval.TotalMilliseconds < m.Ms))
                {
                    _hide.Stop();
                    _hide.Interval = TimeSpan.FromMilliseconds(m.Ms);
                    _hide.Start();
                }
                return;
            }
            _micBadge.Visibility = Visibility.Collapsed;
            _micBadge.BeginAnimation(OpacityProperty, null);
            if (m.Kind == "hide")
            {
                _hide.Stop();
                if (_shown) FadeOut();
                return;
            }
            var (glyph, key) = m.Kind switch
            {
                "ok" => ("", "Ok"),
                "error" => ("", "Danger"),
                "listen" => ("", "Accent"),
                "think" => ("", "Accent"),
                _ => ("", "Accent"),
            };
            var accent = ((SolidColorBrush)Theme.Get(key)).Color;
            _icon.Text = glyph;
            _icon.Foreground = new SolidColorBrush(accent);
            _iconBg.Background = new SolidColorBrush(Color.FromArgb(0x2E, accent.R, accent.G, accent.B));
            _spinner.Stroke = new SolidColorBrush(accent);
            _spinner.Visibility = m.Kind == "think" ? Visibility.Visible : Visibility.Collapsed;
            _icon.Visibility = m.Kind == "think" ? Visibility.Collapsed : Visibility.Visible;
            _spinner.RenderTransform.BeginAnimation(RotateTransform.AngleProperty, m.Kind == "think"
                ? new DoubleAnimation(0, 360, TimeSpan.FromSeconds(0.9)) { RepeatBehavior = RepeatBehavior.Forever } : null);
            _iconBg.BeginAnimation(OpacityProperty, m.Kind == "listen" ? Pulse(0.35) : null);

            _title.Text = m.Title ?? "";
            _title.Visibility = string.IsNullOrEmpty(m.Title) ? Visibility.Collapsed : Visibility.Visible;
            _text.Text = m.Text ?? "";

            bool sticky = m.Kind is "listen" or "think";
            int ms = m.Ms > 0 ? m.Ms : sticky ? 120000 : Math.Clamp(2600 + 45 * (m.Text?.Length ?? 0), 2600, 9000);
            _hide.Stop();
            _hide.Interval = TimeSpan.FromMilliseconds(ms);
            _hide.Start();

            if (!IsVisible) Show();
            UpdateLayout();
            Place();
            // stay above other topmost windows (game overlays, task manager) without activating
            SetWindowPos(new WindowInteropHelper(this).Handle, new IntPtr(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010);
            if (!_shown)
            {
                _shown = true;
                BeginAnimation(OpacityProperty, new DoubleAnimation(1, TimeSpan.FromMilliseconds(140)));
                _slide.BeginAnimation(TranslateTransform.YProperty,
                    new DoubleAnimation(-14, 0, TimeSpan.FromMilliseconds(180)) { EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut } });
            }
        }

        void FadeOut()
        {
            _shown = false;
            StopAnimations();  // at once: nothing pulses on a fading card
            var a = new DoubleAnimation(0, TimeSpan.FromMilliseconds(160));
            a.Completed += (_, _) =>
            {
                if (_shown) return;
                _micBadge.Visibility = Visibility.Collapsed;
                Hide();
                BeginAnimation(OpacityProperty, null);  // release the finished animation clock
                Opacity = 0;
            };
            BeginAnimation(OpacityProperty, a);
        }
    }
}
