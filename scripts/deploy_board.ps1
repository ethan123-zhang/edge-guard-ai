# =============================================================================
# 哨兵管家 · 一键部署到 Jetson 板端（本机 Windows → 板端工作目录）
# -----------------------------------------------------------------------------
# 做什么（顺序就是脚本的执行顺序）：
#   1. 本机前置检查：ssh / scp / tar 在不在，vision/、scripts/ 在不在；
#   2. 板端连通性：cd 进工作目录，打印 pwd / hostname / python3 -V；
#   3. 本机打包：vision/ + scripts/ → tar.gz（不传 logs/ datasets/ snapshots/
#      vision/face_db/ .git/ __pycache__），同时按包内清单算 sha256；
#   4. 上传：scp tar → 板端核对 tar 自己的 sha256 → 解包 → scp 清单；
#   5. 校验：板端 `sha256sum -c .board-sync.sha256` 逐个核对，不一致立即失败；
#   6. 自检：python3 scripts/check_backend.py --profile board（必须 exit 0）
#      → python3 -m vision.run_tests（必须 OK）。
#
# 为什么是 tar + scp 而不是 rsync：Windows 自带 OpenSSH 有 scp、系统有 tar(bsdtar)，
# 但没有 rsync；仓库里 scripts/make_board_pkg.ps1 也是同一套打法，保持一致。
#
# 认证：板端已配免密公钥，所有 ssh/scp 都带 BatchMode=yes（强制非交互）。
#       **脚本里没有也不接受密码**；一旦板端开始要密码，这里会直接失败而不是挂着等输入。
#
# 用法：
#   pwsh -File scripts\deploy_board.ps1              # 同步 + 校验 + 自检 + 单测
#   pwsh -File scripts\deploy_board.ps1 -DryRun      # 只打印计划，不写板端
#   pwsh -File scripts\deploy_board.ps1 -SkipTests   # 跳过最后的单测（check_backend 照跑）
#   pwsh -File scripts\deploy_board.ps1 -Force       # 即使清单一致也重新上传
#   pwsh -File scripts\deploy_board.ps1 -Host 192.168.1.9 -WorkDir /home/jetson/other
#
# 退出码：0 = 成功；1 = 连接/打包/上传/sha256 校验失败；2 = check_backend 不通过；
#         3 = 单测不通过。
#
# 幂等：可以反复跑。板端清单与本次一致时跳过上传（要强制重传用 -Force）；
#       同步是**覆盖式**，不删除板端多余文件（不用 rsync --delete 是有意的：
#       板端有 material-eval/、snapshots/ 这类跑出来的产物，误删代价大）。
# =============================================================================
[CmdletBinding()]
param(
    # PowerShell 的 $Host 是只读自动变量，所以参数名取 RemoteHost、别名 Host，
    # 命令行上照样写 -Host。
    [Alias('Host')]
    [string]$RemoteHost = '10.77.156.4',
    [string]$User = 'jetson',
    [string]$WorkDir = '/home/jetson/sentinel-quality-20261106',
    [switch]$SkipTests,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$Repo       = Split-Path -Parent $PSScriptRoot
$VisionDir  = Join-Path $Repo 'vision'
$ScriptsDir = Join-Path $Repo 'scripts'
$Target     = "${User}@${RemoteHost}"
$SshOpts = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10')
$script:TmpDir  = $null

# 板端路径两种形态：
#   shellDir —— 塞进 ssh 命令串里用双引号包，~ 在双引号里不展开，所以换成 $HOME；
#   scpDir   —— scp 的远端路径支持 ~ 展开，保持原样。
if ($WorkDir.StartsWith('~')) {
    $scpDir   = $WorkDir.TrimEnd('/')
    $shellDir = '$HOME' + $WorkDir.Substring(1).TrimEnd('/')
} else {
    $scpDir   = $WorkDir.TrimEnd('/')
    $shellDir = $WorkDir.TrimEnd('/')
}

# ---------------------------------------------------------------- 输出小工具

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Write-Pass([string]$Text) {
    Write-Host "  $Text" -ForegroundColor Green
}

function Write-Info([string]$Text) {
    Write-Host "  $Text"
}

function Write-Note([string]$Text) {
    Write-Host "  $Text" -ForegroundColor Yellow
}

function Clear-TmpDir {
    if ($script:TmpDir -and (Test-Path -LiteralPath $script:TmpDir)) {
        Remove-Item -Recurse -Force -LiteralPath $script:TmpDir -ErrorAction SilentlyContinue
    }
}

function Fail([string]$Message, [int]$Code = 1) {
    Clear-TmpDir
    Write-Host ''
    Write-Host "[deploy] 失败：$Message" -ForegroundColor Red
    exit $Code
}

function Show-Tail($Lines, [int]$Count = 30) {
    $all = @($Lines)
    $start = [Math]::Max(0, $all.Count - $Count)
    Write-Host ("  --- 末尾 " + ($all.Count - $start) + " 行输出 ---") -ForegroundColor DarkGray
    for ($i = $start; $i -lt $all.Count; $i++) { Write-Host ("  " + $all[$i]) }
}

# ---------------------------------------------------------------- 调用封装

# 跑一条远端 shell 命令，返回 @{ Code; Lines; Text }。
# stderr 合并进输出（unittest 把结果写在 stderr，必须一起收）。
function Invoke-Remote {
    param([Parameter(Mandatory)][string]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & ssh @SshOpts $Target $Command 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{
        Code  = $code
        Lines = @($lines)
        Text  = (@($lines) -join "`n")
    }
}

# 跑本机原生程序（tar / scp），同样把 stderr 合并进 Lines。
function Invoke-Native {
    param([Parameter(Mandatory)][string]$Exe, [Parameter(Mandatory)][string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $Exe @Arguments 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return [pscustomobject]@{
        Code  = $code
        Lines = @($lines)
        Text  = (@($lines) -join "`n")
    }
}

# ============================================================================
Write-Host '哨兵管家 · 部署到 Jetson 板端' -ForegroundColor White
Write-Host ("  本机：{0}" -f $Repo)
Write-Host ("  板端：{0}:{1}" -f $Target, $WorkDir)
if ($DryRun)    { Write-Host '  模式：-DryRun（只打印计划，不写板端）' }
if ($SkipTests) { Write-Host '  模式：-SkipTests（跳过最后的 vision.run_tests）' }
if ($Force)     { Write-Host '  模式：-Force（清单一致也重新上传）' }

# ---------------------------------------------------------------- 1. 本机前置
Write-Step '1/6 本机前置检查'
foreach ($exe in @('ssh', 'scp', 'tar')) {
    $cmd = Get-Command $exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Fail ("本机找不到 " + $exe + "。Windows 10/11 自带 OpenSSH 客户端和 tar；" +
              "在「设置 → 应用 → 可选功能」里装 OpenSSH 客户端，或换一台机器跑。")
    }
    Write-Pass ("找到 " + $cmd.Name + " → " + $cmd.Source)
}
foreach ($dir in @($VisionDir, $ScriptsDir)) {
    if (-not (Test-Path -LiteralPath $dir)) { Fail ("本机缺目录：" + $dir) }
}

# ---------------------------------------------------------------- 2. 板端连通
Write-Step '2/6 板端连通性'
$cd = 'cd "' + $shellDir + '"'
$probe = Invoke-Remote ($cd + ' && echo PWD=$(pwd) && hostname && python3 -V')
if ($probe.Code -ne 0) {
    Show-Tail $probe.Lines 10
    Fail ("连不上板端或工作目录不存在：" + $Target + ":" + $WorkDir +
          "`n  按顺序排查：`n    1) 板子开机且与本机同一网段（ping " + $RemoteHost + "）；" +
          "`n    2) 免密公钥还有效：ssh -o BatchMode=yes " + $Target + " 'echo ok'；" +
          "`n    3) -WorkDir 是不是板端实际目录（默认 " + $WorkDir + "）。")
}
$probe.Lines | Select-Object -First 3 | ForEach-Object { Write-Pass $_ }

# ---------------------------------------------------------------- 3. 本机打包
Write-Step '3/6 本机打包（vision/ + scripts/）'
$script:TmpDir = Join-Path ([IO.Path]::GetTempPath()) ('board-sync-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $script:TmpDir | Out-Null
$tarPath      = Join-Path $script:TmpDir 'board-sync.tar.gz'
$manifestPath = Join-Path $script:TmpDir '.board-sync.sha256'

# 同步范围就是 vision/ + scripts/；logs/ datasets/ snapshots/ vision/face_db/ .git/
# 根本不在范围内，这里再显式排除一遍是防呆（-C 用错/将来加目录时不至于把产物传上板）。
$excludes = @('*__pycache__*', '*.pyc', 'vision/face_db/*')
$tarArgs = @('-czf', $tarPath, '-C', $Repo)
foreach ($pattern in $excludes) { $tarArgs += ('--exclude=' + $pattern) }
$tarArgs += @('vision', 'scripts')

$tarRes = Invoke-Native 'tar' $tarArgs
if ($tarRes.Code -ne 0 -or -not (Test-Path -LiteralPath $tarPath)) {
    Show-Tail $tarRes.Lines 20
    Fail ("打包失败（tar exit " + $tarRes.Code + "）。" +
          "本机 tar 必须是 Windows 自带的 bsdtar（scripts/make_board_pkg.ps1 用的是同一套）。")
}
$tarMB = [Math]::Round((Get-Item -LiteralPath $tarPath).Length / 1MB, 2)

# 包内清单 = 真正会落到板端的文件；sha256 清单直接由它生成，避免「清单和实际内容不一致」。
$listRes = Invoke-Native 'tar' @('-tzf', $tarPath)
if ($listRes.Code -ne 0) {
    Show-Tail $listRes.Lines 20
    Fail '读不出 tar 内容清单（tar -tzf 失败）'
}
$entries = @(
    $listRes.Lines |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.EndsWith('/') } |
        ForEach-Object { $_ -replace '^\./', '' } |
        Sort-Object -Unique
)
if ($entries.Count -eq 0) { Fail '打包结果是空的：vision/、scripts/ 里没有文件？' }

$stray = @($entries | Where-Object { -not ($_ -like 'vision/*' -or $_ -like 'scripts/*') })
if ($stray.Count -gt 0) {
    Fail ("包内出现了不在同步范围里的路径（-C 没生效？）：" + ($stray | Select-Object -First 5))
}
$leak = @($entries | Where-Object { $_ -match '__pycache__' -or $_ -match '\.pyc$' -or $_ -match '^vision/face_db/' })
if ($leak.Count -gt 0) {
    Fail ("排除规则没生效，包里出现了不该上板的文件：" + ($leak | Select-Object -First 5) +
          "（别继续，否则会把板端的 pyc/人脸库覆盖掉）")
}

$sb = New-Object System.Text.StringBuilder
foreach ($entry in $entries) {
    $localPath = Join-Path $Repo ($entry -replace '/', [IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $localPath -PathType Leaf)) {
        Fail ("清单里的文件本机找不到：" + $localPath)
    }
    $hash = (Get-FileHash -LiteralPath $localPath -Algorithm SHA256).Hash.ToLower()
    [void]$sb.Append($hash + '  ' + $entry + "`n")
}
[IO.File]::WriteAllText($manifestPath, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
$fileCount = $entries.Count
Write-Pass ("打包完成：" + $fileCount + " 个文件，" + $tarMB + " MB")

# 板端是否已经是这份内容：清单文件哈希一致 + 板端逐个 sha256 -c 都过 → 可跳过上传。
$localManifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLower()
$remoteHash = ''
$fastCode = $null
$skipUpload = $false
if (-not $Force) {
    $fast = Invoke-Remote ($cd + ' && sha256sum -c --status .board-sync.sha256 && sha256sum .board-sync.sha256')
    $fastCode = $fast.Code
    if ($fast.Code -eq 0) {
        $remoteHash = @($fast.Text -split '\s+' | Where-Object { $_ }) | Select-Object -First 1
        if ($remoteHash -eq $localManifestHash) { $skipUpload = $true }
    }
}
if ($skipUpload) {
    Write-Pass '板端清单与本次完全一致（sha256 已全部通过）→ 跳过上传'
} elseif ($Force) {
    Write-Info '板端需要上传（-Force：无条件重传）'
} else {
    $remoteHint = if ($remoteHash) { $remoteHash.Substring(0, 12) } elseif ($fastCode -eq 0) { '清单读取失败' } else { '无清单/板端文件已漂移' }
    Write-Info ('板端需要上传（本机清单 ' + $localManifestHash.Substring(0, 12) + ' / 板端 ' + $remoteHint + '）')
}

# ---------------------------------------------------------------- DryRun 出口
if ($DryRun) {
    Write-Step 'DryRun：接下来真跑会做的事'
    Write-Info ("同步范围：" + $fileCount + " 个文件，" + $tarMB + " MB（vision/ + scripts/）")
    Write-Info ("上传：" + $(if ($skipUpload) { '跳过（清单一致）' } else { 'scp tar → 板端核 tar 哈希 → 解包 → scp 清单' }))
    Write-Info '校验：板端 sha256sum -c .board-sync.sha256'
    Write-Info ('自检：' + $cd + ' && python3 scripts/check_backend.py --profile board')
    if (-not $SkipTests) {
        Write-Info ('单测：' + $cd + ' && python3 -m vision.run_tests')
    }
    Write-Info ('包内前 5 个文件：' + (($entries | Select-Object -First 5) -join '，'))
    Write-Note 'DryRun 没有改板端任何文件（只读了一次板端清单）'
    Clear-TmpDir
    exit 0
}

# ---------------------------------------------------------------- 4. 上传 + 解包
Write-Step '4/6 上传 + 解包'
if (-not $skipUpload) {
    $remoteTar = $scpDir + '/.board-sync.tar.gz'
    $scpArgs = $SshOpts + @($tarPath, ($Target + ':' + $remoteTar))
    $upload = Invoke-Native 'scp' $scpArgs
    if ($upload.Code -ne 0) {
        Show-Tail $upload.Lines 10
        Fail ("上传失败（scp exit " + $upload.Code + "）。" +
              "查网络是否断、板端家目录是否满了（板端 df -h ~）。" +
              "板端可能残留 .board-sync.tar.gz，下次会覆盖。")
    }

    # 传完先核 tar 自己的哈希：被截断/串到别的板子时在解包前就拦住。
    $wantTarHash = (Get-FileHash -LiteralPath $tarPath -Algorithm SHA256).Hash.ToLower()
    $gotTar = Invoke-Remote ($cd + ' && sha256sum .board-sync.tar.gz')
    $gotTarHash = @($gotTar.Text -split '\s+' | Where-Object { $_ }) | Select-Object -First 1
    if ($gotTar.Code -ne 0 -or $gotTarHash -ne $wantTarHash) {
        Fail ("板端收到的 tar 哈希不一致（上传被截断，或连到了别的板子）：`n" +
              "    本机 " + $wantTarHash + "`n    板端 " + $gotTarHash)
    }

    $extract = Invoke-Remote ($cd + ' && tar -xzf .board-sync.tar.gz && rm -f .board-sync.tar.gz')
    if ($extract.Code -ne 0) {
        Show-Tail $extract.Lines 20
        Fail ("解包失败（板端 tar exit " + $extract.Code + "）。" +
              "常见原因：板端磁盘满（df -h ~）、tar 包被改过。")
    }
    Write-Pass '上传 + 解包完成（vision/ + scripts/）'

    $remoteManifest = $scpDir + '/.board-sync.sha256'
    $manifestArgs = $SshOpts + @($manifestPath, ($Target + ':' + $remoteManifest))
    $manifestUp = Invoke-Native 'scp' $manifestArgs
    if ($manifestUp.Code -ne 0) {
        Show-Tail $manifestUp.Lines 10
        Fail ("sha256 清单上传失败（scp exit " + $manifestUp.Code + "），无法继续校验。")
    }
}

# ---------------------------------------------------------------- 5. sha256 校验
Write-Step '5/6 板端 sha256 清单校验'
$verify = Invoke-Remote ($cd + ' && sha256sum -c .board-sync.sha256')
$okCount = 0
$failedLines = @()
foreach ($line in $verify.Lines) {
    if ($line -match ':\s+OK$') { $okCount++ }
    elseif ($line -match ':\s+FAILED') { $failedLines += $line }
}
if ($verify.Code -ne 0 -or $failedLines.Count -gt 0 -or $okCount -ne $fileCount) {
    $failedLines | Select-Object -First 10 | ForEach-Object { Write-Host ("  " + $_) -ForegroundColor Red }
    Show-Tail $verify.Lines 10
    Fail ("sha256 校验不通过：通过 " + $okCount + "/" + $fileCount + "，失败 " + $failedLines.Count + " 个。" +
          "先别跑自检——查清是上传中断、板端磁盘满，还是有人手改过板端文件。")
}
Write-Pass ("sha256 校验通过：" + $okCount + "/" + $fileCount + " 个文件一致")

# ---------------------------------------------------------------- 6. 板端自检
Write-Step '6/6 板端自检'
# 板端 TensorRT/numpy 的固定环境（每次 ssh 都是新 shell，必须显式带上）。
$envPrefix = 'export OPENBLAS_CORETYPE=ARMV8 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 && '

$backend = Invoke-Remote ($cd + ' && ' + $envPrefix + 'python3 scripts/check_backend.py --profile board')
$backend.Lines | ForEach-Object { Write-Info $_ }
if ($backend.Code -ne 0) {
    Show-Tail $backend.Lines 30
    Fail ("check_backend 未通过（exit " + $backend.Code + "）：实际后端不是 TensorRT。" +
          "`n  最常见原因：engine 与当前 TensorRT 版本/源模型对不上（换板、升级 JetPack、模型换过），" +
          "`n  这时 auto 后端会静默退 CPU（17.6 fps → 3.7 fps）。" +
          "`n  下一步：ssh 到板端 cd " + $WorkDir + " 后跑 python3 scripts/ensure_engines.py --check 看原因，" +
          "`n  再去掉 --check 重跑，让它按 sidecar 记的 profile 重建。", 2)
}
Write-Pass 'check_backend 通过（检测与识别都在 TensorRT 上）'

$ranLine = ''
$okLine = ''
if ($SkipTests) {
    Write-Note '按 -SkipTests 跳过 python3 -m vision.run_tests'
} else {
    $tests = Invoke-Remote ($cd + ' && ' + $envPrefix + 'python3 -m vision.run_tests')
    $ranLine = @($tests.Lines | Where-Object { $_ -match '^Ran \d+ tests' }) | Select-Object -Last 1
    $okLine = @($tests.Lines | Where-Object { $_ -match '^OK' }) | Select-Object -Last 1
    if ($tests.Code -ne 0 -or -not $okLine) {
        Show-Tail $tests.Lines 30
        Fail ("vision.run_tests 未通过（exit " + $tests.Code + "）。上面是末尾 30 行；" +
              "本机可先跑 py -3.12 -m vision.run_tests 对照。", 3)
    }
    Write-Pass ('单测通过：' + $ranLine + ' ' + $okLine)
}

# ---------------------------------------------------------------- 收尾
Write-Step '部署完成'
Write-Pass ("板端：" + $Target + ':' + $WorkDir)
Write-Pass ("同步：" + $fileCount + " 个文件（vision/ + scripts/），sha256 全部一致" +
            $(if ($skipUpload) { '（本次跳过上传）' } else { '' }))
Write-Pass '后端：检测 + 识别均走 TensorRT（check_backend exit 0）'
if (-not $SkipTests) {
    Write-Pass ('单测：' + $ranLine + ' ' + $okLine)
}
Write-Note '提示：engine 与 TensorRT 版本/设备绑定；换板或升级 JetPack 后先跑 python3 scripts/ensure_engines.py --check'
Clear-TmpDir
exit 0