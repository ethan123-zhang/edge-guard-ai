<#
.SYNOPSIS
  只读探测磁盘镜像（.img）的分区布局与 ext4 真实占用。
  不需要 Linux、不需要管理员权限、不挂载、不写盘。

.DESCRIPTION
  用途：判断一个厂商镜像「能不能缩到 32G 卡」。关键不是 .img 文件的长度
  （Jetson/Nano 镜像的最后一个大分区总是被撑满到整张卡），而是那个 ext4
  文件系统里真正存了多少数据 —— 这个数直接写在 ext4 超级块里，读得到。

  对 Jetson 系列：APP 分区是唯一的大分区，从 14 MiB 开始，一直到最后。

.EXAMPLE
  .\probe-img-usage.ps1 -ImagePath E:\RM\出厂镜像\xxx.img

.EXAMPLE
  # 没有参数时默认探测仓库里的官方镜像
  .\probe-img-usage.ps1
#>
[CmdletBinding()]
param(
    [string]$ImagePath = "$PSScriptRoot\..\firmware\sd-blob-b01.img",
    # 目标卡容量（十进制 GB，卡上印的数字）
    [int]$TargetCardGB = 32
)

$ErrorActionPreference = 'Stop'
$GiB = 1GB

function Read-At {
    param($fs, [long]$offset, [int]$count)
    $buf = New-Object byte[] $count
    $fs.Position = $offset
    $read = 0
    while ($read -lt $count) {
        $n = $fs.Read($buf, $read, $count - $read)
        if ($n -le 0) { break }
        $read += $n
    }
    , $buf
}

function Get-GptPartitions {
    param($fs, [long]$fileLen)
    $hdr = Read-At $fs 512 512
    if ([System.Text.Encoding]::ASCII.GetString($hdr, 0, 8) -ne 'EFI PART') { return $null }
    $entryLba = [BitConverter]::ToUInt64($hdr, 72)
    $count = [BitConverter]::ToUInt32($hdr, 80)
    $esize = [BitConverter]::ToUInt32($hdr, 84)
    if ($count -gt 512 -or $esize -lt 128) { return $null }
    $raw = Read-At $fs ($entryLba * 512) ($count * $esize)
    $list = @()
    for ($i = 0; $i -lt $count; $i++) {
        $o = $i * $esize
        $first = [BitConverter]::ToUInt64($raw, $o + 32)
        $last = [BitConverter]::ToUInt64($raw, $o + 40)
        if ($first -eq 0 -and $last -eq 0) { continue }
        if ($last -lt $first) { continue }
        $name = ''
        if ($esize -gt 56) {
            $name = [System.Text.Encoding]::Unicode.GetString($raw, $o + 56, $esize - 56).Trim([char]0).Trim()
        }
        $list += [pscustomobject]@{
            Index = $i; Name = $name
            Start = [long]$first * 512
            Size = [long]($last - $first + 1) * 512
        }
    }
    , $list
}

function Get-MbrPartitions {
    param($fs)
    $mbr = Read-At $fs 0 512
    if ($mbr[510] -ne 0x55 -or $mbr[511] -ne 0xAA) { return $null }
    $list = @()
    for ($i = 0; $i -lt 4; $i++) {
        $o = 446 + $i * 16
        $type = $mbr[$o + 4]
        $first = [BitConverter]::ToUInt32($mbr, $o + 8)
        $sectors = [BitConverter]::ToUInt32($mbr, $o + 12)
        if ($type -eq 0 -or $sectors -eq 0) { continue }
        $list += [pscustomobject]@{
            Index = $i; Name = ('type=0x{0:X2}' -f $type)
            Start = [long]$first * 512
            Size = [long]$sectors * 512
        }
    }
    , $list
}

# ext4/3/2 超级块：分区起点 +1024 字节，magic 0xEF53 在偏移 56
function Get-Ext4Info {
    param($fs, [long]$partStart, [long]$partSize)
    if ($partSize -lt 2048) { return $null }
    $sb = Read-At $fs ($partStart + 1024) 1024
    if ($sb.Length -lt 1024) { return $null }
    if ([BitConverter]::ToUInt16($sb, 56) -ne 0xEF53) { return $null }

    $logBs = [BitConverter]::ToUInt32($sb, 24)
    if ($logBs -gt 6) { return $null }
    $blockSize = [long]1024 -shl [int]$logBs

    $blocksLo = [BitConverter]::ToUInt32($sb, 4)
    $freeLo = [BitConverter]::ToUInt32($sb, 12)
    $blocksHi = [BitConverter]::ToUInt32($sb, 336)
    $freeHi = [BitConverter]::ToUInt32($sb, 344)
    $blocks = [long]$blocksLo -bor ([long]$blocksHi -shl 32)
    $free = [long]$freeLo -bor ([long]$freeHi -shl 32)
    if ($blocks -le 0 -or $free -gt $blocks) { return $null }

    [pscustomobject]@{
        BlockSize = $blockSize
        TotalBytes = $blocks * $blockSize
        FreeBytes = $free * $blockSize
        UsedBytes = ($blocks - $free) * $blockSize
        Label = [System.Text.Encoding]::ASCII.GetString($sb, 120, 16).Trim([char]0).Trim()
    }
}

# ---------- 主流程 ----------
if (-not (Test-Path -LiteralPath $ImagePath)) { throw "镜像不存在: $ImagePath" }
$file = Get-Item -LiteralPath $ImagePath
$fs = [System.IO.File]::Open($file.FullName, 'Open', 'Read', 'ReadWrite')

try {
    $parts = Get-GptPartitions $fs $file.Length
    $scheme = 'GPT'
    if (-not $parts) { $parts = Get-MbrPartitions $fs; $scheme = 'MBR' }
    if (-not $parts) { throw '既不是 GPT 也不是 MBR 分区表 —— 这不是整盘镜像？' }

    Write-Host ''
    Write-Host "镜像 : $($file.FullName)" -ForegroundColor Cyan
    Write-Host ("大小 : {0:N2} GiB ({1:N1} GB)" -f ($file.Length / $GiB), ($file.Length / 1e9))
    Write-Host "分区表: $scheme，共 $($parts.Count) 个分区"
    Write-Host ''
    Write-Host ('{0,-4} {1,-12} {2,10} {3,10} {4,10} {5,10} {6,8} {7}' -f 'Idx', 'Name', 'StartGiB', 'SizeGiB', 'FsGiB', 'UsedGiB', 'Use%', 'FS')
    Write-Host ('-' * 92)

    $big = $null
    foreach ($p in $parts) {
        $ext = Get-Ext4Info $fs $p.Start $p.Size
        $fsName = '?'
        $fsTotal = $null; $used = $null; $free = $null; $pct = $null
        if ($ext) {
            $fsName = 'ext4'
            $fsTotal = $ext.TotalBytes; $used = $ext.UsedBytes; $free = $ext.FreeBytes
            if ($fsTotal -gt 0) { $pct = 100.0 * $used / $fsTotal }
        }
        elseif ($p.Size -gt 0 -and $p.Size -le 512MB) { $fsName = '(小分区)' }

        Write-Host ('{0,-4} {1,-12} {2,10:N3} {3,10:N3} {4,10:N2} {5,10:N2} {6,7:N1}% {7}' -f `
            $p.Index, $p.Name, ($p.Start / $GiB), ($p.Size / $GiB),
            ($(if ($fsTotal) { $fsTotal / $GiB } else { 0 })), ($(if ($used) { $used / $GiB } else { 0 })),
            ($(if ($pct -ne $null) { $pct } else { 0 })), $fsName)

        if ($ext -and (-not $big -or $p.Size -gt $big.Size)) {
            $big = [pscustomobject]@{ Part = $p; Ext = $ext }
        }
    }

    if (-not $big) { Write-Host "`n没找到可读的 ext4 大分区，无法估算。" -ForegroundColor Yellow; return }

    $usedGiB = $big.Ext.UsedBytes / $GiB
    $totalGiB = $big.Ext.TotalBytes / $GiB

    # 32G 卡的可用空间：标称 32GB ≈ 29.8 GiB，留 1 MiB 给备份 GPT
    $cardGiB = $TargetCardGB * 1e9 / $GiB
    $maxAppGiB = $cardGiB - 0.02

    # 可缩目标：已用 + 1 GiB 余量（首启还会自动扩到整卡，这里只要"装得下"）
    $targetGiB = [math]::Ceiling(($usedGiB + 1) * 2) / 2
    $slackGiB = $maxAppGiB - $targetGiB

    Write-Host ''
    Write-Host '── 结论 ──' -ForegroundColor Cyan
    Write-Host ("大分区 '{0}'：文件系统 {1:N2} GiB 里真正存了 {2:N2} GiB（占 {3:N0}%），空闲 {4:N2} GiB" -f `
        $big.Part.Name, $totalGiB, $usedGiB, (100.0 * $usedGiB / $totalGiB), ($big.Ext.FreeBytes / $GiB))
    Write-Host ("{0} GB 卡可用约 {1:N2} GiB → 该分区最多能占 {2:N2} GiB" -f $TargetCardGB, $cardGiB, $maxAppGiB)
    Write-Host ("需要把文件系统缩到 ≈ {0:N2} GiB（已用 {1:N2} + 1.0 余量），余量 {2:N2} GiB" -f $targetGiB, $usedGiB, $slackGiB)

    if ($slackGiB -ge 1.5) {
        Write-Host ("`n✅ 可行：缩到 {0:N2} GiB 后能装进 {1} GB 卡，且留了 {2:N1} GiB 余量。" -f $targetGiB, $TargetCardGB, $slackGiB) -ForegroundColor Green
    }
    elseif ($slackGiB -ge 0) {
        Write-Host ("`n⚠️ 勉强：能装下但余量只有 {0:N1} GiB。删掉 swapfile / apt 缓存 / cuda samples 会更稳。" -f $slackGiB) -ForegroundColor Yellow
    }
    else {
        $need = $usedGiB - ($maxAppGiB - 1.0)
        # 64GB 卡实际可用 57.6~59.6 GiB（各品牌差异大），超过 56 GiB 就别赌 64G
        $rec = if (($usedGiB + 2) -le 56) { 64 } else { 128 }
        Write-Host ("`n❌ 装不下：至少要在镜像里删掉 {0:N1} GiB 文件，才够缩进 {1} GB 卡。" -f $need, $TargetCardGB) -ForegroundColor Red
        Write-Host ("   或者换 {0} GB 卡（该分区是按 {1:N0} GiB 撑满的）。" -f $rec, $totalGiB) -ForegroundColor Red
    }
    Write-Host ''
}
finally {
    $fs.Dispose()
}
