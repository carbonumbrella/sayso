using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using Microsoft.Win32;

namespace ClawVoice
{
    /// <summary>Keeps a fixed-design window usable when the screen rotates: the Claw in portrait is only ~690 DIP
    /// wide, and the 900-wide window ran off the screen. Width is capped to the work area; when it is narrower
    /// than the design, two-column page grids (column, 24, column) stack into one column and a one-row tab bar
    /// becomes two rows; the height is capped and the scroller scrolls; the window is re-centered. Runs on show
    /// and on every display change (rotation, resolution, scale).</summary>
    public static class Responsive
    {
        [System.Runtime.InteropServices.DllImport("user32.dll")] static extern bool AllowSetForegroundWindow(int processId);

        /// <summary>A second launch that only signals the running copy hands it the right to take the foreground.</summary>
        public static void AllowForeground() => AllowSetForegroundWindow(-1);

        /// <summary>Shows a tray app's window in front of everything, also over the full-screen Xbox shell (a plain
        /// Show + Activate from the background can stay behind it). Topmost only until the user leaves the window.</summary>
        public static void BringToFront(Window w)
        {
            w.Show();
            if (w.WindowState == WindowState.Minimized) w.WindowState = WindowState.Normal;
            w.Topmost = true;
            w.Activate();
            EventHandler? off = null;
            off = (_, _) => { w.Topmost = false; w.Deactivated -= off; };
            w.Deactivated += off;
        }

        public static void Attach(Window w, double designWidth, ScrollViewer? scroller = null, UniformGrid? tabs = null)
        {
            var moved = new List<(Grid grid, UIElement child)>();
            bool? narrowNow = null;

            void Apply()
            {
                Rect wa = SystemParameters.WorkArea;
                bool narrow = wa.Width < designWidth;
                w.Width = Math.Min(designWidth, wa.Width);
                if (narrow != narrowNow)
                {
                    narrowNow = narrow;
                    if (narrow) Stack(w, moved); else Unstack(moved);
                    if (tabs != null) tabs.Rows = narrow ? 2 : 1;
                }
                w.MaxHeight = wa.Height;
                if (scroller != null)
                {
                    // the window's chrome around the scroller (title, tabs, margins) keeps its height
                    scroller.MaxHeight = double.PositiveInfinity;
                    w.UpdateLayout();
                    double chrome = Math.Max(0, w.ActualHeight - scroller.ActualHeight);
                    scroller.MaxHeight = Math.Max(200, wa.Height - chrome);
                }
                w.UpdateLayout();
                w.Left = wa.Left + Math.Max(0, (wa.Width - w.ActualWidth) / 2);
                w.Top = wa.Top + Math.Max(0, (wa.Height - w.ActualHeight) / 2);
            }

            void Later() => w.Dispatcher.BeginInvoke(new Action(Apply), System.Windows.Threading.DispatcherPriority.Loaded);
            EventHandler onDisplay = (_, _) => Later();
            w.Loaded += (_, _) => Later();
            w.IsVisibleChanged += (_, e) => { if ((bool)e.NewValue) Later(); };
            SystemEvents.DisplaySettingsChanged += onDisplay;
            w.Closed += (_, _) => SystemEvents.DisplaySettingsChanged -= onDisplay;
        }

        /// <summary>Applies the narrow/wide layout now, without a window on screen (--render previews).</summary>
        public static void Preview(Window w, bool narrow, UniformGrid? tabs = null)
        {
            if (narrow) Stack(w, new List<(Grid, UIElement)>());
            if (tabs != null) tabs.Rows = narrow ? 2 : 1;
        }

        static void Stack(DependencyObject root, List<(Grid grid, UIElement child)> moved)
        {
            foreach (Grid g in Grids(root))
            {
                if (g.ColumnDefinitions.Count != 3 || g.RowDefinitions.Count != 0) continue;
                var mid = g.ColumnDefinitions[1].Width;
                if (!mid.IsAbsolute || mid.Value != 24) continue;  // only page-level two-column grids
                var right = new List<UIElement>();
                foreach (UIElement c in g.Children) if (Grid.GetColumn(c) == 2) right.Add(c);
                if (right.Count == 0) continue;
                g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                g.RowDefinitions.Add(new RowDefinition { Height = new GridLength(8) });  // the gap the columns had
                g.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                g.ColumnDefinitions[1].Width = new GridLength(0);
                g.ColumnDefinitions[2].Width = new GridLength(0);
                foreach (var c in right) { Grid.SetColumn(c, 0); Grid.SetRow(c, 2); moved.Add((g, c)); }
            }
        }

        static void Unstack(List<(Grid grid, UIElement child)> moved)
        {
            var grids = new HashSet<Grid>();
            foreach (var (g, c) in moved) { Grid.SetColumn(c, 2); Grid.SetRow(c, 0); grids.Add(g); }
            foreach (var g in grids)
            {
                g.RowDefinitions.Clear();
                g.ColumnDefinitions[1].Width = new GridLength(24);
                g.ColumnDefinitions[2].Width = new GridLength(1, GridUnitType.Star);
            }
            moved.Clear();
        }

        static IEnumerable<Grid> Grids(DependencyObject root)
        {
            foreach (object o in LogicalTreeHelper.GetChildren(root))
            {
                if (o is not DependencyObject d) continue;
                if (d is Grid g) yield return g;
                foreach (var inner in Grids(d)) yield return inner;
            }
        }
    }
}
