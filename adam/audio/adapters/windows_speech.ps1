param(
    [ValidateSet('synthesize', 'play')][string]$Mode,
    [string]$Root,
    [string]$Voice
)
$ErrorActionPreference = 'Stop'
$audio = Join-Path $Root 'audio.wav'
if ($Mode -eq 'synthesize') {
    Add-Type -AssemblyName System.Speech
    $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        $speaker.SelectVoice($Voice)
        $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
        $speaker.SetOutputToWaveFile($audio, $format)
        $speaker.Speak([IO.File]::ReadAllText((Join-Path $Root 'text.txt'), [Text.Encoding]::UTF8))
    } finally {
        $speaker.Dispose()
    }
} else {
    $player = New-Object System.Media.SoundPlayer($audio)
    try {
        $player.Load()
        $player.PlaySync()
    } finally {
        $player.Stop()
        $player.Dispose()
    }
}
