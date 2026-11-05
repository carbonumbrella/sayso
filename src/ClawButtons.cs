using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace ClawVoice
{
    /// <summary>MSI Claw back buttons in ClawTweaks: M1 = the dictation hotkey (held), M2 = the command hotkey.
    /// Hidden where ClawTweaks is not installed. Shows the state, writes on a press (sayso_cli claw-buttons-set)
    /// and asks first when M1/M2 already do something else. Used by the wizard and the Settings hotkeys.
    /// ClawTweaks gives each new game profile its own gamepad profile with M1/M2 blank: the switch "claw_buttons_auto"
    /// lets dictate.py fill those (claw_buttons.fill_new_game_profiles). It turns on when the wizard sets the buttons.</summary>
    public sealed class ClawButtonsPanel : StackPanel
    {
        readonly bool _wizard;
        readonly TextBlock _text = new() { TextWrapping = TextWrapping.Wrap, FontSize = 12, Margin = new Thickness(4, 0, 0, 0) };
        readonly Button _set = new() { Content = "Настроить задние кнопки M1 и M2", Height = 30, FontSize = 12,
                                       Margin = new Thickness(0, 6, 0, 0), HorizontalAlignment = HorizontalAlignment.Left,
                                       Padding = new Thickness(14, 0, 14, 0) };
        readonly Button _replace = new() { Content = "Заменить", Height = 30, FontSize = 12, Padding = new Thickness(14, 0, 14, 0) };
        readonly Button _cancel = new() { Content = "Отмена", Height = 30, FontSize = 12, Padding = new Thickness(14, 0, 14, 0),
                                          Margin = new Thickness(8, 0, 0, 0) };
        readonly StackPanel _ask = new() { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 6, 0, 0),
                                           Visibility = Visibility.Collapsed };
        readonly CheckBox _auto = new() { VerticalAlignment = VerticalAlignment.Center };
        readonly Grid _autoRow = new() { Margin = new Thickness(4, 10, 0, 0), Visibility = Visibility.Collapsed };
        readonly TextBlock _autoNote = new() { TextWrapping = TextWrapping.Wrap, FontSize = 12, Margin = new Thickness(0, 2, 0, 0) };
        JsonObject _state = new();

        /// <summary>A key combination that never breaks across lines ("Ctrl+Alt+" / "W").</summary>
        static string Keys(JsonNode? n) => ((string?)n ?? "").Replace("+", "+⁠");

        static bool AutoOn => (bool?)SettingsWindow.Read()["claw_buttons_auto"] == true;

        public ClawButtonsPanel(bool wizard = false)
        {
            _wizard = wizard;
            Visibility = Visibility.Collapsed;
            Margin = new Thickness(0, 12, 0, 0);
            var flat = (Style)Application.Current.FindResource("Flat");
            _set.Style = flat; _replace.Style = flat; _cancel.Style = flat;
            _ask.Children.Add(_replace); _ask.Children.Add(_cancel);

            // Settings only: the switch for the gamepad profiles of new games
            _auto.Style = (Style)Application.Current.FindResource("Switch");
            var title = new TextBlock { Text = "Добавлять в профили игр", FontSize = 13, FontWeight = FontWeights.SemiBold };
            var tip = new HelpTip { Text = "Когда создаёшь в ClawTweaks профиль игры, на вкладке «Геймпад» для неё автоматически появляется " +
                "отдельный профиль геймпада, и M1 и M2 в нём пустые: в этой игре кнопки диктовки и команды не работают. " +
                "Если переключатель включён, SaySo при запуске игры записывает M1 и M2 в её профиль геймпада, и в новый, и в созданный раньше. " +
                "Только если обе кнопки там пустые, назначенные не трогает. Если выключен, назначьте M1 и M2 в профиле " +
                "геймпада игры или удали этот профиль геймпада: тогда в игре действуют кнопки общего профиля." };
            var head = new StackPanel { Orientation = Orientation.Horizontal };
            head.Children.Add(title); head.Children.Add(tip);
            var left = new StackPanel();
            left.Children.Add(head); left.Children.Add(_autoNote);
            _autoNote.SetResourceReference(TextBlock.ForegroundProperty, "Sub");
            _autoRow.ColumnDefinitions.Add(new ColumnDefinition());
            _autoRow.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            Grid.SetColumn(_auto, 1);
            _autoRow.Children.Add(left); _autoRow.Children.Add(_auto);
            _auto.Click += (_, _) => { SettingsWindow.Set("claw_buttons_auto", _auto.IsChecked == true); AutoNote(); };

            Children.Add(_text); Children.Add(_set); Children.Add(_ask);
            if (!_wizard) Children.Add(_autoRow);
            _set.Click += (_, _) =>
            {
                if ((bool?)_state["free"] == true) { _ = Write(); return; }
                var now = _state["now"] as JsonObject;
                Say((bool?)_state["one_button"] == true
                    ? $"Сейчас на M1: {now?["M1"]}. Заменить на кнопку SaySo?"
                    : $"Сейчас на M1: {now?["M1"]}, на M2: {now?["M2"]}. Заменить на диктовку и команду SaySo?", "Fg");
                _set.Visibility = Visibility.Collapsed;
                _ask.Visibility = Visibility.Visible;
            };
            _replace.Click += (_, _) => _ = Write();
            _cancel.Click += (_, _) => _ = Refresh();
            _ = Refresh();
        }

        void Say(string text, string brush)
        {
            _text.Text = text;
            _text.SetResourceReference(TextBlock.ForegroundProperty, brush);
        }

        void AutoNote()
        {
            bool one = (bool?)_state["one_button"] == true;
            _autoNote.Text = _auto.IsChecked == true
                ? (one ? "Сочетание назначится на M1 в пустых профилях геймпада игр" : "Сочетания назначатся на M1 и M2 в пустых профилях геймпада игр")
                : (one ? "В профилях геймпада игр M1 останется без назначения" : "В профилях геймпада игр M1 и M2 останутся без назначения");
        }

        /// <summary>Reads ClawTweaks again (after a hotkey change in Settings).</summary>
        public async Task Refresh()
        {
            _state = await Task.Run(() => Stack.Cli("claw-buttons", null, 15000));
            _ask.Visibility = Visibility.Collapsed;
            if ((bool?)_state["installed"] != true) { Visibility = Visibility.Collapsed; return; }
            Visibility = Visibility.Visible;
            var want = _state["want"] as JsonObject;
            bool one = (bool?)_state["one_button"] == true;  // only M1: M2 is the user's again
            bool reachable = (bool?)_state["reachable"] == true;
            _autoRow.Visibility = reachable ? Visibility.Visible : Visibility.Collapsed;
            _auto.IsChecked = AutoOn;
            AutoNote();
            string games = _wizard ? " и в пустые профили геймпада игр" : "";
            if (!reachable)
            {
                Say("ClawTweaks не отвечает: откройте его один раз, потом здесь можно будет настроить задние кнопки.", "Sub");
                _set.Visibility = Visibility.Collapsed;
            }
            else if ((bool?)_state["same"] == true)
            {
                Say(one ? $"Задняя кнопка M1 в общем профиле ClawTweaks настроена ({Keys(want?["M1"])}): коротко команда, удержание диктовка."
                        : $"Задние кнопки в общем профиле ClawTweaks настроены: M1 диктовка ({Keys(want?["M1"])}), " +
                          $"M2 команда ({Keys(want?["M2"])}).", "Sub");
                _set.Visibility = Visibility.Collapsed;
            }
            else
            {
                Say((one ? $"M1 станет кнопкой SaySo ({Keys(want?["M1"])}): коротко команда, удержание диктовка. "
                         : $"M1 станет диктовкой ({Keys(want?["M1"])}, держать), M2 командой ({Keys(want?["M2"])}). ") +
                    $"Запишется в общий профиль ClawTweaks{games}.", "Sub");
                _set.Visibility = Visibility.Visible;
            }
        }

        async Task Write()
        {
            _set.IsEnabled = _replace.IsEnabled = false;
            Say("Записываю в ClawTweaks…", "Sub");
            var r = await Task.Run(() => Stack.Cli("claw-buttons-set", null, 20000));
            _set.IsEnabled = _replace.IsEnabled = true;
            if (r["error"] is JsonNode e)
            {
                _ask.Visibility = Visibility.Collapsed;
                _set.Visibility = Visibility.Visible;
                Say("Не получилось: " + e, "Danger");
                return;
            }
            if (_wizard && (bool?)r["ok"] == true) SettingsWindow.Set("claw_buttons_auto", true);  // the wizard's "yes" covers new games too
            await Refresh();
            if ((bool?)r["ok"] != true) Say("ClawTweaks принял запись, но кнопки не совпали. Проверьте их в ClawTweaks.", "Danger");
        }
    }
}
