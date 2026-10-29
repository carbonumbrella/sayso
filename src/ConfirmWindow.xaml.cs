using System;
using System.Text.Json;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Threading;

namespace ClawVoice
{
    public sealed class ConfirmRequest
    {
        public string Title { get; set; } = "";
        public string Message { get; set; } = "";
        public string Detail { get; set; } = "";
        public string Ok { get; set; } = "Выполнить";
        public string Cancel { get; set; } = "Отмена";
        public bool Danger { get; set; } = true;
        public int Timeout { get; set; } = 20;
    }

    /// <summary>Confirmation for gemma_agent.confirm() (was confirm.ps1: a new PowerShell + WPF per dialog).
    /// Enter/Space = yes, Esc = no (ClawTweaks maps gamepad A/B to them), auto-cancel after Timeout.</summary>
    public partial class ConfirmWindow : Window
    {
        readonly DispatcherTimer _timer = new() { Interval = TimeSpan.FromMilliseconds(50) };
        readonly DateTime _start = DateTime.Now;
        readonly int _timeout;
        public bool Yes { get; private set; }

        public ConfirmWindow(ConfirmRequest r)
        {
            InitializeComponent();
            _timeout = Math.Max(3, r.Timeout);
            var accent = Theme.Get(r.Danger ? "Danger" : "Accent");
            TitleText.Text = r.Title;
            Msg.Text = r.Message;
            if (string.IsNullOrEmpty(r.Detail)) DetailBox.Visibility = Visibility.Collapsed; else Detail.Text = r.Detail;
            Icon.Text = r.Danger ? "" : "";
            Icon.Foreground = accent;
            IconBg.Background = accent;
            Bar.Background = accent;
            YesBtn.Background = accent;
            YesBtn.Content = string.IsNullOrEmpty(r.Ok) ? "Выполнить" : r.Ok;
            NoBtn.Content = string.IsNullOrEmpty(r.Cancel) ? "Отмена" : r.Cancel;
            YesBtn.Click += (_, _) => Close(true);
            NoBtn.Click += (_, _) => Close(false);
            MouseLeftButtonDown += (_, _) => { try { DragMove(); } catch { } };
            KeyDown += (_, e) =>
            {
                if (e.Key is Key.Return or Key.Space) Close(true);
                else if (e.Key == Key.Escape) Close(false);
            };
            _timer.Tick += (_, _) =>
            {
                double left = _timeout - (DateTime.Now - _start).TotalSeconds;
                if (left <= 0) { Close(false); return; }
                Bar.Width = Math.Max(0, 462 * left / _timeout);
                Hint.Text = $"Enter или A: выполнить   Esc или B: отмена   Автоотмена через {Math.Ceiling(left):0} с";
            };
            ContentRendered += (_, _) =>
            {
                Activate();
                BeginAnimation(OpacityProperty, new DoubleAnimation(0, 1, TimeSpan.FromMilliseconds(160)));
                _timer.Start();
            };
            Closed += (_, _) => _timer.Stop();
        }


        void Close(bool yes)
        {
            Yes = yes;
            _timer.Stop();
            Close();
        }
    }
}
