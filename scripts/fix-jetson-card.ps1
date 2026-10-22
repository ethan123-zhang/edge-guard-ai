# fix-jetson-card.ps1 — 把 Jetson SD 卡挂进 WSL2，做 ext4 体检/修复
#
# ⚠️ 2026-10-21 实测结论：**这条路对 SD 卡（USB 可移动介质）走不通**。
#    `wsl --mount` 只接受可离线的固定盘，读卡器会报
#    `Wsl/Service/AttachDisk/MountDisk/HCS/ERROR_INVALID_DRIVE`；
#    而 `Set-Disk -IsOffline $true` 对可移动介质直接 `Not Supported`。
#    本脚本保留，仅用于**固定盘**（NVMe/SATA/USB 硬盘盒）场景，或将来 WSL 支持后复用。
#    要修 Jetson 的卡：优先「重烧」，其次「板端 initramfs / U-Boot 单用户」（见 docs/04 §2.0）。
#
# 必须以管理员身份运行（wsl --mount 需要）。用法：
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\fix-jetson-card.ps1           # 只读体检（安全）
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\fix-jetson-card.ps1 -Fix      # 体检后自动修
#
# 设计要点：
#   * 只碰 ext4 的 APP 分区（镜像里是 p1，12.75 GiB），绝不写 p2–p14 的启动固件分区；
#   * 默认用 e2fsck -fn（只读、不修改），先看清楚再决定；
#   * 日志走 ASCII 路径 /mnt/e/dsh-jetson-card.log，最后拷回工作区，便于中文路径不出问题。

param(
    [int]$DiskNumber = 2,
    [switch]$Fix,
    [string]$Workspace = 'E:\哨兵管家'
)

$ErrorActionPreference = 'Continue'
$logDir = Join-Path $Workspace 'firmware'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'card-repair.log'
Remove-Item $log -ErrorAction SilentlyContinue

function Log([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Log "=== Jetson 卡体检开始 === 管理员=$isAdmin  Fix=$Fix"
if (-not $isAdmin) { Log "不是管理员，wsl --mount 会失败。请以管理员身份重跑。"; exit 5 }

# ---------- 1. 确认目标磁盘 ----------
try {
    $d = Get-Disk -Number $DiskNumber -ErrorAction Stop
} catch {
    Log "找不到磁盘 $DiskNumber：$($_.Exception.Message)"
    Log "当前磁盘列表："
    Get-Disk | ForEach-Object { Log ("  #{0} {1} {2:N2} GB {3} {4}" -f $_.Number, $_.FriendlyName, ($_.Size/1GB), $_.PartitionStyle, $_.BusType) }
    exit 3
}
Log ("磁盘 #{0}  {1}  {2:N2} GB  {3}  Bus={4}" -f $d.Number, $d.FriendlyName, ($d.Size/1GB), $d.PartitionStyle, $d.BusType)
if ($d.Size -lt 20GB -or $d.Size -gt 64GB) { Log "大小不像 32G 卡，中止（防止挂错盘）"; exit 3 }

Log "--- wsl --version ---"
(wsl --version 2>&1 | Out-String).Trim() -split "`r?`n" | ForEach-Object { Log $_ }

# ---------- 2. 写 bash 体检脚本（ASCII 路径，避免中文进 WSL） ----------
$bash = @'
#!/bin/bash
set -u
OUT=/mnt/e/dsh-jetson-card.log
: > "$OUT"
exec > >(tee -a "$OUT") 2>&1
MODE="${1:-check}"
echo "===== 模式: $MODE ====="
echo "----- lsblk -----"
lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTLABEL
PART=$(lsblk -pno NAME,FSTYPE | awk '$2=="ext4"{print $1; exit}')
if [ -z "$PART" ]; then echo "!! 没有找到 ext4 分区，无法继续"; exit 4; fi
echo "----- 目标分区: $PART -----"
echo "----- dumpe2fs -h -----"
dumpe2fs -h "$PART" 2>/dev/null | grep -Ei 'filesystem state|block count|free blocks|inode count|free inodes|last mounted|volume name|block size|filesystem features' || true
echo "----- e2fsck -fn（只读体检，不改任何东西）-----"
e2fsck -fn "$PART"
rc=$?
echo "e2fsck(-n) 退出码=$rc   （0=干净, 4=有错误未修, 8=致命）"
if [ "$MODE" = "fix" ] && [ "$rc" -ne 0 ]; then
  echo "----- e2fsck -fy（开始修复）-----"
  e2fsck -fy "$PART"
  echo "e2fsck(-y) 退出码=$?"
  echo "----- 复验 -----"
  e2fsck -fn "$PART"
  echo "复验退出码=$?"
fi
echo "===== 结束 ====="
'@
$bashPath = Join-Path $env:TEMP 'jetson-card-check.sh'
[IO.File]::WriteAllText($bashPath, ($bash -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
Log "bash 脚本: $bashPath"

# ---------- 3. 挂载（裸盘） ----------
$dev = "\\.\PHYSICALDRIVE$DiskNumber"
Log "挂载 $dev 到 WSL2（--bare）..."
$m = (wsl --mount $dev --bare 2>&1 | Out-String).Trim()
Log ($m -replace "`r?`n", " | ")
if ($LASTEXITCODE -ne 0) {
    Log "挂载失败 exit=$LASTEXITCODE。尝试先关闭 WSL 再试一次..."
    wsl --shutdown 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    $m = (wsl --mount $dev --bare 2>&1 | Out-String).Trim()
    Log ($m -replace "`r?`n", " | ")
    if ($LASTEXITCODE -ne 0) { Log "仍然失败，改用板端 initramfs 方案"; exit 2 }
}

# ---------- 4. 在 WSL 里体检 ----------
$mode = if ($Fix) { 'fix' } else { 'check' }
$wslScript = (wsl -u root -- wslpath -a "$bashPath" 2>&1 | Out-String).Trim()
Log "WSL 内脚本路径: $wslScript"
$out = (wsl -u root -- bash $wslScript $mode 2>&1 | Out-String)
$out -split "`r?`n" | ForEach-Object { if ($_) { Log "  $_" } }

# ---------- 5. 卸载 + 收日志 ----------
Log "卸载 $dev ..."
$u = (wsl --unmount $dev 2>&1 | Out-String).Trim()
Log ($u -replace "`r?`n", " | ")

if (Test-Path 'E:\dsh-jetson-card.log') {
    Copy-Item 'E:\dsh-jetson-card.log' (Join-Path $logDir 'card-check.log') -Force
    Log "WSL 原始日志已拷到 firmware\card-check.log"
}
Log "=== 结束 ==="
