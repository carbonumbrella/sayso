using System;
using System.Collections.Generic;
using System.Linq;
using System.Speech.Synthesis;
using System.Threading.Tasks;

namespace ClawVoice
{
    /// <summary>The assistant's voices: the Windows voices (the same System.Speech the Python side speaks with through
    /// ps_host.ps1).</summary>
    public static class Voices
    {
        public static List<string> Installed()
        {
            var list = new List<string>();
            try
            {
                using var s = new SpeechSynthesizer();
                list.AddRange(s.GetInstalledVoices().Where(v => v.Enabled).Select(v => v.VoiceInfo.Name).OrderBy(n => n));
            }
            catch (Exception e) { Log.Write("voices: " + e.Message); }
            return list;
        }

        static SpeechSynthesizer? _preview;

        /// <summary>voice "" = the system voice for the text's language.</summary>
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
