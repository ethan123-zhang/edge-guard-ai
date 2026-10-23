# jetson-serial.ps1 — 通过 USB 串口（ttyGS0 / COM16）在 Jetson Nano 上执行命令
#
# 为什么需要它：这块板的 HDMI 只在你人在现场时才可用，而板子上的 tty1 会被
# docker/nvpmodel 的失败日志刷屏。更可靠的是板子自带的 **micro-USB 串口**
# （板端 /dev/ttyGS0，Windows 侧 USB Serial Device(COMx)，115200 8N1 流控 None）。
# 本脚本用 .NET SerialPort 直接读写，能拿到干净输出、能带 sudo。
#
# 前提：板子 micro-USB 数据线连着 PC，且**没有别的程序占用该串口**
#      （PuTTY 开着同一个 COM 口会打不开；先把 PuTTY 断开/关掉）。
#      板上已有一个停留在登录后的 shell（本脚本不会自己登录，见 -Login）。
#
# 用法：
#   pwsh -File scripts\jetson-serial.ps1 "hostname; uname -r"
#   pwsh -File scripts\jetson-serial.ps1 -Sudo "systemctl mask docker.service"
#   pwsh -File scripts\jetson-serial.ps1 -Port COM16 -Wait 20 -Sudo "apt-get update"
#   pwsh -File scripts\jetson-serial.ps1 -Login -User jetson -Password jetson -Cmd "id"
#
# 说明：
#   * 每次调用打开→发命令→读到 __END__ 标记→关闭；板上的 shell 会一直留着。
#   * 输出里的 CR 会被规整成 LF，去掉回显的那一行，避免日志里重复。

param(
    [Parameter(Position = 0)][string]$Cmd = '',
    [string]$Port = 'COM16',
    [int]$Wait = 15,
    [switch]$Sudo,
    [string]$SudoPassword = 'jetson',
    [switch]$Login,
    [string]$User = 'jetson',
    [string]$Password = 'jetson',
    [int]$Baud = 115200
)

$ErrorActionPreference = 'Stop'

$sp = New-Object System.IO.Ports.SerialPort $Port, $Baud, 'None', 8, 'One'
$sp.Encoding = [System.Text.Encoding]::ASCII
$sp.NewLine = "`r"

function Read-Until([string]$pattern, [int]$timeoutSec) {
    $end = (Get-Date).AddSeconds($timeoutSec)
    $sb = New-Object System.Text.StringBuilder
    while ((Get-Date) -lt $end) {
        try { $chunk = $sp.ReadExisting() } catch { $chunk = '' }
        if ($chunk) { [void]$sb.Append($chunk) }
        if ($sb.ToString() -match $pattern) { break }
        Start-Sleep -Milliseconds 100
    }
    return $sb.ToString()
}

function Clean([string]$s) {
    $s = $s -replace "`r`n", "`n" -replace "`r", "`n"
    ($s -split "`n") | Where-Object { $_ -match '\S' }
}

function Send([string]$s) { $sp.Write($s + "`r") }

try {
    $sp.Open()
} catch {
    "!! 打不开 $Port ：$($_.Exception.Message)"
    "   （最常见：PuTTY 还占着这个串口。先把它断开，或换一根数据线/一个口。）"
    exit 2
}

try {
    Start-Sleep -Milliseconds 200
    $sp.DiscardInBuffer()

    if ($Login) {
        Send ''
        $t = Read-Until 'login:' 6
        if ($t -notmatch 'login:') {
            # 可能已经在 shell 里
            if ($t -match '\$|#') { "（已经登录过，跳过登录）" }
            else { "!! 没看到 login: 提示符：`n" + (Clean $t) -join "`n"; exit 3 }
        } else {
            Send $User
            [void](Read-Until 'Password:' 5)
            Send $Password
            $t = Read-Until '(\$|#)\s*$' 12
            if ($t -match 'Login incorrect') { "!! 登录被拒"; exit 4 }
        }
    }

    Send ''
    [void](Read-Until '(\$|#)\s*$' 4)

    $body = $Cmd
    if ($Sudo) {
        # 单引号转义后交给 sh -c，避免 sudo 读不到密码
        $esc = $body -replace "'", "'\''"
        $body = "echo '$SudoPassword' | sudo -S -p '' sh -c '$esc'"
    }
    # 结束标记用变量拼出来：回显里只有 ${M}，不含真实标记，避免匹配到回显行就提前结束。
    # 注意：标记行刻意不用引号 —— 串口偶发丢字符，引号丢了 bash 会卡在续行提示符 '>'。
    $tag = 'JS' + (Get-Random -Minimum 100000 -Maximum 999999)
    Send ('M=' + $tag + '; echo ${M}BEGIN; ' + $body + '; echo ${M}END$?')
    $raw = Read-Until ($tag + 'END') $Wait

    $lines = Clean $raw
    $out = $lines | Where-Object { $_ -notmatch ($tag) -and $_ -notmatch 'echo \$\{M\}' }
    $out
} finally {
    if ($sp.IsOpen) { $sp.Close() }
}
