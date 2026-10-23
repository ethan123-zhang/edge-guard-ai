# jetson-offline-unlock.ps1 — 离线修 Jetson Nano SD 卡（绕过 OEM 首启向导）
#
# 背景（2026-10-22）：
#   官方 JP46 镜像首启时，nv-oem-config 报
#     debconf: DbDriver "config": /var/cache/debconf/config.dat is locked by another process
#     System configuration setup wasn't completed. Please reboot device and try again.
#   向导进程直接退出 → 账号没建成 → 串口只剩那三行、按 Enter 没反应 → 板上没有可用 shell。
#   镜像本身没问题（同镜像 10-21 走到过向导），是运行期两个向导实例抢 debconf 锁。
#
# 思路：把卡插回 PC，用 usbipd 把读卡器直通进 WSL2，在真 Linux 里改 ext4：
#   1) 直接改 /etc/passwd、/etc/shadow、/etc/group 建一个已知账号（ARM 文件不能 chroot 执行）
#   2) 把 *oem-config* 单元屏蔽成 /dev/null（systemctl mask 的等价物）
#   3) 解开 serial-getty@ttyGS0 的屏蔽 —— 否则这块板起机后没人能登录（HDMI 通路是坏的）
#   4) 顺手设时区 / 主机名
#
# 前提：**板子断电、卡已经拔出来插在读卡器里**。WSL2 + usbipd-win 5.x（本机已有 5.3.0）。
#
# 用法（管理员 PowerShell）：
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\jetson-offline-unlock.ps1
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\jetson-offline-unlock.ps1 -InspectOnly
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\jetson-offline-unlock.ps1 -User ethan -Password 123456
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\jetson-offline-unlock.ps1 -BusId 1-7
#
# 日志：WSL 内 /tmp/jetson-offline.log → 拷回 firmware\jetson-offline.log

param(
    [switch]$InspectOnly,
    [string]$User = 'jetson',
    [string]$Password = 'jetson',
    [string]$BusId = '',
    [string]$Distro = '',
    [switch]$KeepAttached
)

$ErrorActionPreference = 'Continue'
# 控制台按 UTF-8 解码 WSL 输出，否则中文全是乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$Workspace = Split-Path -Parent $PSScriptRoot
$fwDir = Join-Path $Workspace 'firmware'
New-Item -ItemType Directory -Force -Path $fwDir | Out-Null
$psLog = Join-Path $fwDir 'jetson-offline.log'

function Log([string]$m) {
    $line = "{0} {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
    Write-Host $line
}

# ---------- 0. 管理员 ----------
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Log "需要管理员权限（usbipd bind 要 UAC）。正在申请提权..."
    $argList = @('-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$PSCommandPath`"")
    if ($InspectOnly) { $argList += '-InspectOnly' }
    $argList += @('-User', $User, '-Password', $Password)
    if ($BusId) { $argList += @('-BusId', $BusId) }
    if ($Distro) { $argList += @('-Distro', $Distro) }
    if ($KeepAttached) { $argList += '-KeepAttached' }
    Start-Process -FilePath (Get-Process -Id $PID).Path -Verb RunAs -ArgumentList $argList
    exit 0
}

Log "=== Jetson 离线修卡 ===  user=$User  inspectOnly=$InspectOnly"

# WSL 会继承 Windows 的 localhost 代理变量并打印警告（噪音），这里清掉本进程的副本
foreach ($n in 'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy') {
    if (Test-Path "env:$n") { Remove-Item "env:$n" -ErrorAction SilentlyContinue }
}
$env:WSL_UTF8 = '1'   # 让 wsl.exe 用 UTF-8 输出，否则 wsl -l 是乱码

# ---------- 0.5 选 WSL 发行版 ----------
# 本机默认发行版是 docker-desktop，直接 `wsl -u root` / `usbipd attach --wsl` 会打到它身上
if (-not $Distro) {
    $names = @()
    $names = @(wsl -l -q 2>$null | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $names = @($names | Where-Object { $_ -notmatch '^docker-desktop' })
    $ub = @($names | Where-Object { $_ -match 'Ubuntu' })
    if ($ub.Count -ge 1) { $Distro = $ub[0] }
    elseif ($names.Count -eq 1) { $Distro = $names[0] }
    else { Log "!! 选不出 WSL 发行版，请用 -Distro 指定。候选: $($names -join ', ')"; exit 3 }
}
Log "WSL 发行版: $Distro"

# ---------- 1. 找读卡器 ----------
$list = (usbipd list 2>&1 | Out-String)
$list -split "`r?`n" | Where-Object { $_ -match '\S' } | ForEach-Object { Log "  $_" }

if (-not $BusId) {
    # 排除 NVIDIA 板子自身（0955:7020 也带一个大容量存储功能，别误绑）
    $cands = @()
    foreach ($ln in ($list -split "`r?`n")) {
        if ($ln -match '^\s*(\d+-\d+)\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.*)$') {
            $bus = $Matches[1]; $vidpid = $Matches[2]
            $desc = ($Matches[3] -replace '\s+(Not shared|Shared|Attached)\s*$', '').Trim()
            if ($vidpid -match '^0955:') { continue }
            if ($desc -match '大容量存储|Mass Storage|Flash|Card Reader|SD') { $cands += [pscustomobject]@{ BusId = $bus; VidPid = $vidpid; Desc = $desc } }
        }
    }
    if ($cands.Count -eq 0) {
        Log "!! 没找到读卡器。请确认卡插在读卡器里、读卡器已插到 PC。"
        Log '   若读卡器没被识别成「大容量存储」，用 -BusId 手工指定（上面列表里的 BUSID）。'
        exit 4
    }
    if ($cands.Count -gt 1) {
        Log "!! 找到多个候选，用 -BusId 指定一个："
        $cands | ForEach-Object { Log ("   {0}  {1}  {2}" -f $_.BusId, $_.VidPid, $_.Desc) }
        exit 4
    }
    $BusId = $cands[0].BusId
    Log ("候选读卡器: {0}  {1}  {2}" -f $BusId, $cands[0].VidPid, $cands[0].Desc)
}

$ans = Read-Host "把 $BusId 直通给 WSL2 并开始改卡？（确认这是 Jetson 的 SD 卡，不是别的盘。输入 yes 继续）"
if ($ans -ne 'yes') { Log "已取消。"; exit 0 }

# ---------- 2. 直通到 WSL2 ----------
Log "--- usbipd detach $BusId（先清掉可能挂在别的发行版上的旧连接）---"
(usbipd detach --busid $BusId 2>&1 | Out-String).Trim() -split "`r?`n" | ForEach-Object { if ($_) { Log "  $_" } }

Log "--- usbipd bind --force $BusId ---"
(usbipd bind --force --busid $BusId 2>&1 | Out-String).Trim() -split "`r?`n" | ForEach-Object { if ($_) { Log "  $_" } }

Log "--- 启动 $Distro ---"
$null = wsl -d $Distro -u root -- true 2>&1
if ($LASTEXITCODE -ne 0) { Log "!! 发行版 $Distro 起不来（先跑 wsl -d $Distro -u root -- true 看报错）"; exit 5 }

Log "--- usbipd attach --wsl $Distro --busid $BusId ---"
(usbipd attach --wsl $Distro --busid $BusId 2>&1 | Out-String).Trim() -split "`r?`n" | ForEach-Object { if ($_) { Log "  $_" } }
Start-Sleep -Seconds 5

Log "--- $Distro 里看到的块设备 ---"
$ls = (wsl -d $Distro -u root -- lsblk -o NAME,SIZE,TRAN,TYPE,PARTLABEL 2>&1 | Out-String)
$ls -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^\s*wsl:' } | ForEach-Object { Log "  $_" }

# ---------- 3. 写 bash 脚本（LF 换行，ASCII 临时路径） ----------
$bash = @'
#!/bin/bash
# 在 WSL2 内执行：离线修 Jetson Nano SD 卡（由 jetson-offline-unlock.ps1 投递）
set -u
export LANG=C.UTF-8 LC_ALL=C.UTF-8
MODE="${1:-fix}"
JUSER="${2:-${JETSON_USER:-jetson}}"
JPASS="${3:-${JETSON_PASS:-jetson}}"
LOG=/tmp/jetson-offline.log
R=/mnt/jetson
DISK=""
command -v python3 >/dev/null 2>&1 || { echo "!! WSL 里没有 python3，先装：sudo apt install -y python3"; exit 7; }
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

echo "===== jetson-offline-unlock  mode=$MODE  user=$JUSER ====="
date
echo "----- lsblk -----"
lsblk -o NAME,SIZE,TRAN,FSTYPE,LABEL,PARTLABEL,MOUNTPOINT

# ---- 定位目标卡：优先 USB 传输 + PARTLABEL=APP；没有 TRAN 标记时退回只看 APP 标签 ----
FALLBACK=""
for d in /dev/sd?; do
  [ -b "$d" ] || continue
  if lsblk -Pno NAME,PARTLABEL "$d" 2>/dev/null | grep -q 'PARTLABEL="APP"'; then
    if [ "$(lsblk -dno TRAN "$d" 2>/dev/null | tr -d ' ')" = "usb" ]; then DISK="$d"; break; fi
    [ -z "$FALLBACK" ] && FALLBACK="$d"
  fi
done
if [ -z "$DISK" ] && [ -n "$FALLBACK" ]; then
  DISK="$FALLBACK"
  echo "（没读到 TRAN=usb，但唯一带 APP 分区标签的是 $DISK，按它处理）"
fi
if [ -z "$DISK" ]; then
  echo "!! 没找到「USB 传输 + PARTLABEL=APP」的磁盘。"
  echo "   可能原因：读卡器没直通进 WSL / 卡没插好 / 卡不是 Jetson 卡。"
  echo "--- 诊断 ---"
  echo "  [lsblk]"; lsblk -o NAME,SIZE,TRAN,TYPE
  echo "  [usb-storage 模块]"; grep -E 'usb_storage|uas|vhci_hcd' /proc/modules || echo "    (没有 usb-storage / uas / vhci_hcd 模块)"
  echo "  [scsi_host]"; ls /sys/class/scsi_host/ 2>/dev/null
  echo "  [dmesg 尾部]"; dmesg 2>/dev/null | tail -20
  echo "===== 结束（失败）====="
  exit 4
fi
PNAME=$(lsblk -Pno NAME,PARTLABEL "$DISK" | awk -F'"' '/PARTLABEL="APP"/{print $2}' | head -1)
PART="/dev/$PNAME"
DNAME=$(basename "$DISK")
echo "----- 目标: 磁盘 $DISK  根分区 $PART -----"

DSZ=$(cat /sys/block/$DNAME/size)
PSTART=$(cat /sys/block/$DNAME/$(basename "$PART")/start)
PSIZE=$(cat /sys/block/$DNAME/$(basename "$PART")/size)
echo "卡总扇区=$DSZ ($((DSZ/2/1024/1024)) GiB)  APP 起始=$PSTART  APP 扇区=$PSIZE ($((PSIZE/2/1024/1024)) GiB)"
echo "APP 之后剩余扇区=$((DSZ - PSTART - PSIZE))   (远大于 0 = 分区还没扩满，登录后再扩)"

mkdir -p "$R"
umount "$R" 2>/dev/null
if [ "$MODE" = "inspect" ]; then MOPT="-o ro"; else MOPT=""; fi
# shellcheck disable=SC2086
if ! mount $MOPT "$PART" "$R"; then echo "!! 挂载失败"; echo "===== 结束（失败）====="; exit 5; fi
echo "----- 挂载成功 -----"

# ---- 只读诊断 ----
echo ""
echo "================ 诊断 ================"
echo "--- /etc/os-release ---";        sed -n '1,4p' "$R/etc/os-release" 2>/dev/null
echo "--- /etc/nv_tegra_release ---";   head -2 "$R/etc/nv_tegra_release" 2>/dev/null
echo "--- 现有可登录账号 (uid>=1000) ---"
awk -F: '$3>=1000 && $3<65534 {print "  "$1"  uid="$3"  gid="$4"  shell="$7}' "$R/etc/passwd" 2>/dev/null
echo "--- shadow 状态（只显示是否设了密码）---"
awk -F: '/^(root|ubuntu|nvidia|jetson):/ {s=($2=="")?"EMPTY":($2~/^[!*]/?"LOCKED":"SET"); print "  "$1"  "s}' "$R/etc/shadow" 2>/dev/null
echo "--- oem-config 相关单元（/etc/systemd/system）---"
ls -l "$R/etc/systemd/system/" 2>/dev/null | grep -Ei 'oem|getty' || echo "  (无)"
echo "--- oem-config 相关单元（/lib/systemd/system）---"
ls -l "$R/lib/systemd/system/" 2>/dev/null | grep -Ei 'oem' || echo "  (无)"
echo "--- multi-user.target.wants ---"
ls "$R/etc/systemd/system/multi-user.target.wants/" 2>/dev/null | sed 's/^/  /' || echo "  (无)"
echo "--- serial getty 屏蔽情况 ---"
for f in "$R/etc/systemd/system/serial-getty@ttyGS0.service" "$R/lib/systemd/system/serial-getty@ttyGS0.service" "$R/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service"; do
  if [ -L "$f" ]; then echo "  SYMLINK $f -> $(readlink "$f")"
  elif [ -e "$f" ]; then echo "  FILE    $f"
  else echo "  absent  $f"; fi
done
echo "--- /usr/sbin/nv-oem-config（头 60 行）---"
head -60 "$R/usr/sbin/nv-oem-config" 2>/dev/null || echo "  (读不到)"
echo "--- debconf 数据库时间戳 ---"
ls -l "$R/var/cache/debconf/config.dat" 2>/dev/null || echo "  (无)"
echo "--- syslog 里 oem/debconf 相关最后 40 行 ---"
grep -Ei 'oem-config|debconf' "$R/var/log/syslog" 2>/dev/null | tail -40 || echo "  (无 syslog 或无匹配)"
echo "================ 诊断结束 ================"
echo ""

if [ "$MODE" = "inspect" ]; then
  echo "（inspect 模式：不做任何修改）"
else
  # ---- 建账号（直接改文件；ARM64 二进制不能在 x86 WSL 里 chroot 执行）----
  echo "--- 建/修账号 ---"
  export R JETSON_USER="$JUSER" JETSON_PASS="$JPASS"
  python3 - "$MODE" <<'PYEOF'
import os, sys, time, subprocess, shutil
R    = os.environ["R"]
user = os.environ["JETSON_USER"]
pw   = os.environ["JETSON_PASS"]
mode = sys.argv[1]
TAIL = time.strftime("%Y%m%d-%H%M%S")

def rd(p):
    p2 = os.path.join(R, p)
    if not os.path.exists(p2):
        return []
    with open(p2, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read().split("\n")

def wr(p, lines):
    p2 = os.path.join(R, p)
    if mode == "fix" and os.path.exists(p2):
        shutil.copy2(p2, p2 + ".bak-" + TAIL)
    out = [l for l in lines if l != ""]
    with open(p2, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(out) + "\n")

def mkhash(password):
    salt = os.urandom(8).hex()
    # 1) openssl（Ubuntu 一定有）
    try:
        r = subprocess.run(["openssl", "passwd", "-6", "-salt", salt, password],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip().startswith("$6$"):
            return r.stdout.strip(), "openssl"
    except FileNotFoundError:
        pass
    # 2) python crypt 模块
    try:
        import crypt
        h = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
        if h and h.startswith("$6$"):
            return h, "python-crypt"
    except Exception:
        pass
    # 3) perl（glibc crypt，Ubuntu 一定有 perl）
    try:
        r = subprocess.run(["perl", "-e", 'print crypt($ARGV[0], $ARGV[1])', password, "$6$" + salt + "$"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip().startswith("$6$"):
            return r.stdout.strip(), "perl-crypt"
    except FileNotFoundError:
        pass
    return None, None

passwd = [l for l in rd("etc/passwd")]
groups = [l for l in rd("etc/group")]
shadow = [l for l in rd("etc/shadow")]

users = {}
for l in passwd:
    if l and not l.startswith("#") and ":" in l:
        f = l.split(":")
        users[f[0]] = f

if user in users:
    uid, gid = int(users[user][2]), int(users[user][3])
    print("  用户 %s 已存在: uid=%d gid=%d（只重置密码）" % (user, uid, gid))
    gid = gid
else:
    used = {int(f[2]) for f in users.values() if f[2].isdigit()}
    uid = 1000
    while uid in used:
        uid += 1
    usedg = set()
    for l in groups:
        f = l.split(":")
        if len(f) > 2 and f[2].isdigit():
            usedg.add(int(f[2]))
    gid = uid
    while gid in usedg:
        gid += 1
    passwd.append("%s:x:%d:%d:Jetson Nano,,,:/home/%s:/bin/bash" % (user, uid, gid, user))
    groups.append("%s:x:%d:" % (user, gid))
    print("  新建用户 %s: uid=%d gid=%d" % (user, uid, gid))

h, tool = mkhash(pw)
if not h:
    print("  !! 生成密码哈希失败（openssl / python crypt / perl 都不可用）")
    print("  !! 中止：卡上还没有写任何账号文件，账号会没有密码 → 那样的修复没意义")
    sys.exit(2)
print("  密码哈希: %s" % tool)

days = int(time.time() // 86400)
found = False
new_sh = []
for l in shadow:
    if not l:
        continue
    f = l.split(":")
    if f and f[0] == user:
        f[1] = h
        while len(f) < 9:
            f.append("")
        f[2] = str(days); f[3] = "0"; f[4] = "99999"; f[5] = "7"
        new_sh.append(":".join(f))
        found = True
    else:
        new_sh.append(l)
if not found:
    new_sh.append("%s:%s:%d:0:99999:7:::" % (user, h, days))
shadow = new_sh

# 附加组：只加卡上真实存在的组
want = ["sudo", "adm", "dialout", "cdrom", "video", "audio", "plugdev", "i2c", "gpio", "input", "render"]
gmap = {}
for i, l in enumerate(groups):
    f = l.split(":")
    if len(f) > 3:
        gmap[f[0]] = i
added = []
for g in want:
    if g in gmap:
        i = gmap[g]
        f = groups[i].split(":")
        mem = [m for m in f[3].split(",") if m]
        if user not in mem:
            mem.append(user)
            f[3] = ",".join(mem)
            groups[i] = ":".join(f)
            added.append(g)
if added:
    print("  加入组: " + ", ".join(added))

# 家目录
if user in users:
    home_user = users[user][5] if len(users[user]) > 5 else "/home/" + user
else:
    home_user = "/home/" + user
home = os.path.join(R, home_user.lstrip("/"))
if not os.path.isdir(home):
    os.makedirs(home, exist_ok=True)
    skel = os.path.join(R, "etc/skel")
    if os.path.isdir(skel):
        subprocess.run(["cp", "-a", skel + "/.", home], check=False)
for root, dirs, files in os.walk(home):
    try:
        os.chown(root, uid, gid)
    except (OSError, AttributeError, NotImplementedError):
        pass
    for n in files:
        try:
            os.chown(os.path.join(root, n), uid, gid)
        except (OSError, AttributeError, NotImplementedError):
            pass
print("  家目录: %s" % home_user)

wr("etc/passwd", passwd)
wr("etc/group", groups)
wr("etc/shadow", shadow)
print("  已写回 passwd/shadow/group（原文件备份为 .bak-%s）" % TAIL)
PYEOF
  PYRC=$?
  if [ "$PYRC" -ne 0 ]; then
    echo "!! 建账号失败 rc=$PYRC，中止（卡上未写账号文件）"
    sync; umount "$R"
    echo "===== 结束（失败）====="
    exit 6
  fi

  # ---- 时区 / 主机名 ----
  echo "--- 时区/主机名 ---"
  ln -sf ../usr/share/zoneinfo/Asia/Shanghai "$R/etc/localtime"
  echo "Asia/Shanghai" > "$R/etc/timezone"
  echo "jetson-nano" > "$R/etc/hostname"
  if [ ! -f "$R/etc/hosts" ]; then printf '127.0.0.1\tlocalhost\n' > "$R/etc/hosts"; fi
  grep -v '^127\.0\.1\.1' "$R/etc/hosts" > "$R/etc/hosts.tmp" 2>/dev/null
  echo "127.0.1.1	jetson-nano" >> "$R/etc/hosts.tmp"
  mv "$R/etc/hosts.tmp" "$R/etc/hosts"
  echo "  tz=Asia/Shanghai  hostname=jetson-nano"

  # ---- 屏蔽 OEM 向导 ----
  echo "--- 屏蔽 oem-config 单元 ---"
  mkdir -p "$R/etc/systemd/system"
  for u in "$R"/lib/systemd/system/*oem-config*.service "$R"/lib/systemd/system/*oem-config*.target; do
    [ -e "$u" ] || continue
    b=$(basename "$u")
    ln -sf /dev/null "$R/etc/systemd/system/$b"
    echo "  masked $b"
  done

  # ---- 放开串口登录（不然这块板起机后没人能进去）----
  echo "--- 放开 serial-getty@ttyGS0 ---"
  GS="$R/etc/systemd/system/serial-getty@ttyGS0.service"
  if [ -L "$GS" ] && [ "$(readlink "$GS")" = "/dev/null" ]; then
    rm -f "$GS"; echo "  unmasked serial-getty@ttyGS0"
  fi
  if [ -e "$R/lib/systemd/system/serial-getty@.service" ]; then
    mkdir -p "$R/etc/systemd/system/getty.target.wants"
    ln -sf /lib/systemd/system/serial-getty@.service "$R/etc/systemd/system/getty.target.wants/serial-getty@ttyGS0.service"
    echo "  enabled serial-getty@ttyGS0"
  fi

  # ---- 默认跑 multi-user：这块板 HDMI 通路是坏的，桌面起不来只会拖慢/刷屏 ----
  if [ -e "$R/lib/systemd/system/multi-user.target" ]; then
    ln -sf /lib/systemd/system/multi-user.target "$R/etc/systemd/system/default.target"
    echo "  default.target -> multi-user.target（无显示，省内存）"
  fi

  # ---- 顺手把 ssh 打开（板子起来后就走远程）----
  if [ -e "$R/lib/systemd/system/ssh.service" ]; then
    mkdir -p "$R/etc/systemd/system/multi-user.target.wants"
    if [ ! -e "$R/etc/systemd/system/multi-user.target.wants/ssh.service" ]; then
      ln -sf /lib/systemd/system/ssh.service "$R/etc/systemd/system/multi-user.target.wants/ssh.service"
      echo "  enabled ssh.service"
    fi
  fi
fi

# ---- 收尾 ----
sync
echo "--- umount ---"
if umount "$R"; then echo "  ok"; else echo "  umount 失败，强制 lazy umount"; umount -l "$R"; fi
echo "--- e2fsck -fn（只读报告，不修）---"
if command -v e2fsck >/dev/null 2>&1; then
  e2fsck -fn "$PART" 2>&1 | tail -15
  echo "  e2fsck(-n) rc=${PIPESTATUS[0]}"
else
  echo "  (WSL 里没有 e2fsck，跳过)"
fi
echo "===== 结束 ====="
'@

$tmp = Join-Path $env:TEMP 'jetson-offline-unlock.sh'
[IO.File]::WriteAllText($tmp, ($bash -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
Log "bash 脚本: $tmp"

# ---------- 4. 在 WSL 里跑 ----------
$mode = if ($InspectOnly) { 'inspect' } else { 'fix' }

# 不用 wslpath：它会把 C:\ 路径里的反斜杠吃掉。自己做映射，并先确认文件可见
function ToWslPath([string]$p) {
    $q = $p -replace '\\', '/'
    if ($q -match '^([A-Za-z]):/(.*)$') { return ('/mnt/' + $Matches[1].ToLower() + '/' + $Matches[2]) }
    return $q
}
$wslPath = ToWslPath $tmp
Log "WSL 内路径: $wslPath"

$null = wsl -d $Distro -u root -- test -f "$wslPath" 2>&1
if ($LASTEXITCODE -ne 0) {
    Log "!! $Distro 里看不到 $wslPath —— 大概率 /mnt/c 没自动挂载。"
    Log "   在 WSL 里跑 'ls /mnt' 看一下；或把该文件拷到 WSL 内再跑。"
    exit 7
}

# 先在 WSL 里做 bash -n：语法有问题就根本别碰卡
$chk = (wsl -d $Distro -u root -- bash -n "$wslPath" 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) {
    $chk -split "`r?`n" | Where-Object { $_ } | ForEach-Object { Log "  $_" }
    Log "!! bash 语法检查失败，未动卡。"
    exit 8
}
Log "bash -n: 语法 OK"

Log "--- 开始执行（mode=$mode）---"
$out = (wsl -d $Distro -u root -- bash "$wslPath" $mode $User $Password 2>&1 | Out-String)
$out -split "`r?`n" | ForEach-Object { if ($_) { Log "  $_" } }

# ---------- 5. 取回日志（base64 传字节，绕开控制台编码） ----------
$b64raw = (wsl -d $Distro -u root -- base64 -w0 /tmp/jetson-offline.log 2>&1 | Out-String)
$b64 = (($b64raw -split "`r?`n") | Where-Object { $_ -match '^[A-Za-z0-9+/=]+$' } | ForEach-Object { $_.Trim() }) -join ''
if ($b64) {
    try {
        $bytes = [Convert]::FromBase64String($b64)
        [IO.File]::WriteAllBytes($psLog, $bytes)
        Log "日志已存: $psLog"
    } catch {
        Log "!! 日志 base64 解码失败：$($_.Exception.Message)"
    }
} else {
    Log "!! 没取到 WSL 侧日志（/tmp/jetson-offline.log）"
}

# ---------- 6. 归还设备 ----------
if (-not $KeepAttached) {
    Log "--- usbipd detach $BusId ---"
    (usbipd detach --busid $BusId 2>&1 | Out-String).Trim() -split "`r?`n" | ForEach-Object { if ($_) { Log "  $_" } }
}

Log "=== 完成 ==="
Log "下一步：把卡拔下来插回 Jetson 模块背面 → 串口窗口先开好 → 上电 → 应该出现 login:"
Log "账号: $User   密码: $Password   （登录后立刻 passwd 改掉）"
