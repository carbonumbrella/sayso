# Persistent PowerShell worker for gemma_agent.py: avoids ~0.4-0.7 s of powershell.exe startup
# (plus C# Add-Type compile) on every volume/brightness/toast/voice call.
# Protocol: one base64(UTF-8 script) per stdin line -> one "<rc> <base64(UTF-8 output)>" stdout line.
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Speech
$tts = New-Object System.Speech.Synthesis.SpeechSynthesizer
$ru = $tts.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ru-RU' } | Select-Object -First 1
if ($ru) { $tts.SelectVoice($ru.VoiceInfo.Name) }
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$toaster = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(
    '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe')
function Show-Toast([string]$title, [string]$text) {
    $x = New-Object Windows.Data.Xml.Dom.XmlDocument
    $e = [Security.SecurityElement]
    $x.LoadXml("<toast><visual><binding template=`"ToastGeneric`"><text>$($e::Escape($title))</text>" +
               "<text>$($e::Escape($text))</text></binding></visual></toast>")
    $toaster.Show([Windows.UI.Notifications.ToastNotification]::new($x))
}
function Say([string]$text) { $tts.SpeakAsyncCancelAll(); $tts.SpeakAsync($text) | Out-Null }
function SayMore([string]$text) { $tts.SpeakAsync($text) | Out-Null }  # queued after what is being said

$in = [Console]::In
while ($null -ne ($line = $in.ReadLine())) {
    $rc = 0
    try {
        $cmd = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line))
        $out = (Invoke-Expression $cmd 2>&1 | Out-String).Trim()
        if (-not $?) { $rc = 1 }
    } catch { $rc = 1; $out = $_.ToString() }
    [Console]::Out.WriteLine("$rc " + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$out)))
    [Console]::Out.Flush()
}
