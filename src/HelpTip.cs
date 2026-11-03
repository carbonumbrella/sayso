// A small "?" next to a setting: a tap or click opens the explanation in a popup (no hover tooltip: the Claw is a
// touch screen, and the tooltip rendered empty). Text: what it does, what happens, the load.
using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;

namespace ClawVoice
{
    public sealed class HelpTip : Border
    {
        public static readonly DependencyProperty TextProperty =
            DependencyProperty.Register(nameof(Text), typeof(string), typeof(HelpTip), new PropertyMetadata(""));

        public string Text { get => (string)GetValue(TextProperty); set => SetValue(TextProperty, value); }

        readonly Popup _popup = new() { StaysOpen = false, AllowsTransparency = true, Placement = PlacementMode.Bottom };

        public HelpTip()
        {
            Width = Height = 18;
            CornerRadius = new CornerRadius(9);
            Margin = new Thickness(6, 0, 0, 0);
            VerticalAlignment = VerticalAlignment.Center;
            Cursor = Cursors.Hand;
            SetResourceReference(BackgroundProperty, "Ghost");
            var q = new TextBlock { Text = "?", FontSize = 11, FontWeight = FontWeights.Bold,
                HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };
            q.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
            Child = q;
            _popup.PlacementTarget = this;
            // (a tap arrives as a promoted mouse click too, so one handler serves both)
            MouseLeftButtonUp += (_, e) =>
            {
                if (!_popup.IsOpen) Build();
                _popup.IsOpen = !_popup.IsOpen;
                e.Handled = true;
            };
        }

        Brush Res(string key, Brush fallback) => TryFindResource(key) as Brush ?? fallback;

        // The popup is its own tree: the window's brushes do not reach it by reference, so they are copied in here.
        void Build()
        {
            var body = new TextBlock { Text = Text, TextWrapping = TextWrapping.Wrap, MaxWidth = 380, FontSize = 12.5, LineHeight = 18,
                Foreground = Res("Fg", Brushes.White) };
            var card = new Border { CornerRadius = new CornerRadius(12), Padding = new Thickness(14, 10, 14, 12), BorderThickness = new Thickness(1),
                Margin = new Thickness(0, 6, 12, 12), Child = body,
                Background = Res("Card", new SolidColorBrush(Color.FromRgb(0x2A, 0x2A, 0x30))), BorderBrush = Res("Border", Brushes.Gray),
                Effect = new System.Windows.Media.Effects.DropShadowEffect { BlurRadius = 16, ShadowDepth = 3, Opacity = 0.35 } };
            card.MouseLeftButtonUp += (_, _) => _popup.IsOpen = false;
            _popup.Child = card;
        }
    }
}
