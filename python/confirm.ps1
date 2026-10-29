# Modern confirmation dialog for gemma_agent.py (WPF, borderless, rounded, follows the system theme).
# Input: -B64 <base64 UTF-8 JSON {title, message, detail, ok, cancel, danger, timeout}>.
# Exit code: 0 = confirmed, 1 = cancelled / closed / timed out.
# Keys: Enter = confirm, Esc = cancel (ClawTweaks maps gamepad A/B to Enter/Esc in desktop mode).
param([Parameter(Mandatory = $true)][string]$B64, [string]$Preview, [string]$Theme)
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase

$a = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($B64)) | ConvertFrom-Json
$timeout = if ($a.timeout) { [int]$a.timeout } else { 20 }

$light = $false
if ($Theme) { $light = $Theme -eq 'light' } else { try { $light = (Get-ItemPropertyValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' AppsUseLightTheme) -eq 1 } catch {} }
if ($light) { $bg = '#F7F7F8'; $fg = '#1B1B1F'; $sub = '#5E5E66'; $card = '#FFFFFF'; $border = '#E3E3E8'; $ghost = '#ECECF0' }
else        { $bg = '#202024'; $fg = '#F3F3F5'; $sub = '#A4A4AE'; $card = '#2A2A30'; $border = '#38383F'; $ghost = '#34343B' }
$accent = if ($a.danger) { '#E5484D' } else { '#3E7BFA' }
$icon = if ($a.danger) { [char]0xE7BA } else { [char]0xE9CE }   # Segoe Fluent: Warning / Help

[xml]$xaml = @"
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        WindowStyle="None" AllowsTransparency="True" Background="Transparent" ResizeMode="NoResize"
        SizeToContent="WidthAndHeight" WindowStartupLocation="CenterScreen" Topmost="True"
        ShowInTaskbar="False" FontFamily="Segoe UI Variable Text, Segoe UI" Opacity="0">
  <Window.Resources>
    <Style x:Key="Btn" TargetType="Button">
      <Setter Property="Height" Value="48"/><Setter Property="MinWidth" Value="150"/>
      <Setter Property="FontSize" Value="15"/><Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/><Setter Property="Focusable" Value="False"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="b" CornerRadius="12" Background="{TemplateBinding Background}" Padding="22,0">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="b" Property="Opacity" Value="0.88"/></Trigger>
              <Trigger Property="IsPressed" Value="True"><Setter TargetName="b" Property="Opacity" Value="0.72"/></Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>
  <Border Margin="24" CornerRadius="20" Background="$bg" BorderBrush="$border" BorderThickness="1" Width="520">
    <Border.Effect><DropShadowEffect BlurRadius="32" ShadowDepth="6" Opacity="0.35" Color="Black"/></Border.Effect>
    <StackPanel Margin="28,26,28,24">
      <StackPanel Orientation="Horizontal">
        <Border Width="40" Height="40" CornerRadius="20" Background="$accent" Opacity="0.15"/>
        <TextBlock Text="$icon" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="20"
                   Foreground="$accent" Margin="-30,0,0,0" VerticalAlignment="Center"/>
        <TextBlock x:Name="Title" FontSize="20" FontWeight="SemiBold" Foreground="$fg"
                   Margin="16,0,0,0" VerticalAlignment="Center" TextWrapping="Wrap" MaxWidth="400"/>
      </StackPanel>
      <TextBlock x:Name="Msg" FontSize="15" Foreground="$sub" Margin="0,16,0,0" TextWrapping="Wrap"/>
      <Border x:Name="DetailBox" CornerRadius="10" Background="$card" BorderBrush="$border" BorderThickness="1"
              Margin="0,14,0,0" Padding="14,10">
        <TextBlock x:Name="Detail" FontFamily="Cascadia Mono, Consolas" FontSize="13" Foreground="$fg" TextWrapping="Wrap"/>
      </Border>
      <Grid Margin="0,22,0,0">
        <Grid.ColumnDefinitions><ColumnDefinition/><ColumnDefinition Width="12"/><ColumnDefinition/></Grid.ColumnDefinitions>
        <Button x:Name="No" Grid.Column="0" Style="{StaticResource Btn}" Background="$ghost" Foreground="$fg"/>
        <Button x:Name="Yes" Grid.Column="2" Style="{StaticResource Btn}" Background="$accent" Foreground="White"/>
      </Grid>
      <Border Height="3" CornerRadius="2" Background="$ghost" Margin="0,18,0,0">
        <Border x:Name="Bar" Height="3" CornerRadius="2" Background="$accent" HorizontalAlignment="Left"/>
      </Border>
      <TextBlock x:Name="Hint" FontSize="12" Foreground="$sub" Margin="0,8,0,0" HorizontalAlignment="Center"/>
    </StackPanel>
  </Border>
</Window>
"@

$w = [Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
$n = { param($name) $w.FindName($name) }
(& $n 'Title').Text = $a.title
(& $n 'Msg').Text = $a.message
if ($a.detail) { (& $n 'Detail').Text = $a.detail } else { (& $n 'DetailBox').Visibility = 'Collapsed' }
(& $n 'Yes').Content = if ($a.ok) { $a.ok } else { 'Выполнить' }
(& $n 'No').Content = if ($a.cancel) { $a.cancel } else { 'Отмена' }

$script:result = 1
$close = { param($r) $script:result = $r; $w.Close() }
(& $n 'Yes').Add_Click({ & $close 0 })
(& $n 'No').Add_Click({ & $close 1 })
$w.Add_MouseLeftButtonDown({ try { $w.DragMove() } catch {} })
$w.Add_KeyDown({
    param($s, $e)
    if ($e.Key -eq 'Return' -or $e.Key -eq 'Space') { & $close 0 }
    elseif ($e.Key -eq 'Escape') { & $close 1 }
})

$bar = & $n 'Bar'; $hint = & $n 'Hint'
$start = Get-Date
$timer = New-Object Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(50)
$timer.Add_Tick({
    $left = $timeout - ((Get-Date) - $start).TotalSeconds
    if ($left -le 0) { $timer.Stop(); & $close 1; return }
    $bar.Width = [Math]::Max(0, 462 * $left / $timeout)
    $hint.Text = "Enter или A: выполнить   Esc или B: отмена   Автоотмена через {0:N0} с" -f [Math]::Ceiling($left)
})
$w.Add_ContentRendered({
    $w.Activate() | Out-Null
    $w.BeginAnimation([Windows.Window]::OpacityProperty,
        (New-Object Windows.Media.Animation.DoubleAnimation 0, 1, ([TimeSpan]::FromMilliseconds(160))))
    $timer.Start()
})
if ($Preview) {
    # Render to PNG without showing the window (for visual checks).
    $hint.Text = "Enter или A: выполнить   Esc или B: отмена   Автоотмена через $timeout с"
    $bar.Width = 462 * 0.7
    $root = $w.Content; $w.Content = $null
    $holder = New-Object Windows.Controls.Border; $holder.Background = [Windows.Media.Brushes]::DimGray
    $holder.Child = $root
    $holder.Measure([Windows.Size]::new([double]::PositiveInfinity, [double]::PositiveInfinity))
    $holder.Arrange([Windows.Rect]::new($holder.DesiredSize)); $holder.UpdateLayout()
    $bmp = New-Object Windows.Media.Imaging.RenderTargetBitmap ([int]$holder.ActualWidth), ([int]$holder.ActualHeight), 96, 96, ([Windows.Media.PixelFormats]::Pbgra32)
    $bmp.Render($holder)
    $enc = New-Object Windows.Media.Imaging.PngBitmapEncoder
    $enc.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($bmp))
    $fs = [IO.File]::Create($Preview); $enc.Save($fs); $fs.Close()
    exit 2
}
$w.ShowDialog() | Out-Null
exit $script:result
