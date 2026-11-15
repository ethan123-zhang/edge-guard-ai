# 生成固定话术 wav（16 kHz 单声道 PCM），给板端口播播报用。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\make_voice_phrases.ps1 [-OutDir voice\phrases]
param(
    [string]$OutDir = "voice\phrases",
    [string]$VoiceName = "Microsoft Huihui Desktop"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech

$phrases = [ordered]@{
    "greeting_unknown"     = "您好，家里现在没人，请问您找哪位？"
    "greeting_known"       = "您好，又见面了。今天来是有什么事吗？"
    "purpose_ack"          = "好的，已经帮您记下了，我会转告家里人的。"
    "ask_caller"           = "请问您来是有什么事？"
    "farewell"             = "好的，我先记下您来过，再见。"
    "handoff"              = "好的，已经帮您转告家里人了，再见。"
    "not_heard"            = "不好意思，我没听清，您再说一遍？"
    "refusal_open_door"    = "抱歉，我不能为您开门。我只能帮您把话带给家里人。"
    "refusal_info_probe"   = "这个我不方便说。您有什么事，我可以帮您转达。"
    "refusal_impersonation"= "抱歉，我没法确认您的身份，所以不能按您说的做。您可以把事情留给我转达。"
    "refusal_manipulation" = "这个我不能这么做。您可以说您来的目的，我如实转告。"
    "refusal_system_probe" = "这个我就不聊了。请问您今天来是有什么事吗？"
    "refusal_promise"      = "这个我没法保证。您的话我一定会转达到。"
}

$out = Join-Path (Get-Location) $OutDir
New-Item -ItemType Directory -Force -Path $out | Out-Null

# 16 kHz / 16 bit / 单声道 —— 板端 Vosk 同采样率，播放也不挑
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)

foreach ($key in $phrases.Keys) {
    $path = Join-Path $out "$key.wav"
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoice($VoiceName)
    $synth.SetOutputToWaveFile($path, $format)
    $synth.Speak($phrases[$key])
    $synth.Dispose()
    $size = (Get-Item $path).Length
    Write-Output ("{0,-24} {1,8} bytes  {2}" -f $key, $size, $phrases[$key])
}

Write-Output "输出目录：$out"