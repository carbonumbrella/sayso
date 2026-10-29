using System;
using System.Collections.Generic;
using System.Linq;
using System.Speech.Synthesis;
using System.Threading.Tasks;

namespace ClawVoice
{
    /// <summary>Windows voices (the same System.Speech the Python side speaks with through ps_host.ps1).</summary>
    public static class Voices
    {
        public static List<string> Installed()
        {
            try
            {
                using var s = new SpeechSynthesizer();
                return s.GetInstalledVoices().Where(v => v.Enabled)
                        .Select(v => v.VoiceInfo.Name).OrderBy(n => n).ToList();
            }
            catch (Exception e) { Log.Write("voices: " + e.Message); return new List<string>(); }
        }

        static SpeechSynthesizer? _preview;

        public static void Preview(string voice, int rate, int volume, string text)
        {
            Task.Run(() =>
            {
                try
                {
                    _preview?.SpeakAsyncCancelAll();
                    _preview ??= new SpeechSynthesizer();
                    if (voice.Length > 0) _preview.SelectVoice(voice);
                    _preview.Rate = Math.Clamp(rate, -10, 10);
                    _preview.Volume = Math.Clamp(volume, 0, 100);
                    _preview.SpeakAsync(text);
                }
                catch (Exception e) { Log.Write("voice preview: " + e.Message); }
            });
        }
    }
}
