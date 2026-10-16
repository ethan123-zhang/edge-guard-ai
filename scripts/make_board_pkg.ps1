# ============================================================================
# 重新打包板端最小包（含 llm_demo 提示词补丁）—— Windows 端，不依赖 WSL
# ----------------------------------------------------------------------------
# 做法：以现有 board-pkg\rkllm-board-v1.3.1.tar.gz 为基础（里面已有官方源码与
#       runtime 二进制），只替换/新增需要改的文件，重新打包。
#       这样避免了重新克隆 773M 仓库、也绕开 WSL 沙箱限制。
#
# 用法：pwsh -File scripts\make_board_pkg.ps1
# ============================================================================
$ErrorActionPreference = 'Stop'

$Repo    = Split-Path -Parent $PSScriptRoot          # E:\哨兵管家
$PkgDir  = Join-Path $Repo 'board-pkg'
$OldTar  = Join-Path $PkgDir 'rkllm-board-v1.3.1.tar.gz'
$Name    = 'rkllm-board-v1.3.1'
$Stage   = Join-Path $env:TEMP "repack_$Name"
$NewTar  = Join-Path $PkgDir 'rkllm-board-v1.3.1-v2.tar.gz'

if (-not (Test-Path $OldTar)) { throw "找不到基础包: $OldTar" }

Write-Host '=== 1. 清理并解压基础包 ===' -ForegroundColor Cyan
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null
tar -xzf $OldTar -C $Stage
$Root = Join-Path $Stage $Name
if (-not (Test-Path $Root)) { throw "解压后找不到 $Name 目录" }

Write-Host '=== 2. 替换 llm_demo.cpp 为补丁版 ===' -ForegroundColor Cyan
$srcDst = Join-Path $Root 'examples\rkllm_api_demo\deploy\src\llm_demo.cpp'
$srcNew = Join-Path $PkgDir 'src\llm_demo.cpp'
Copy-Item $srcNew $srcDst -Force
$patched = Select-String -Path $srcDst -Pattern 'system_prompt_file' -Quiet
if (-not $patched) { throw '覆盖后仍找不到补丁标志，中止' }
Write-Host '  OK 已覆盖，且确认含补丁标志'

Write-Host '=== 3. 加入 prompts 与复验脚本 ===' -ForegroundColor Cyan
$promptDir = Join-Path $Root 'prompts'
New-Item -ItemType Directory -Force -Path $promptDir | Out-Null
Copy-Item (Join-Path $PkgDir 'prompts\system-prompt.txt')          $promptDir -Force
Copy-Item (Join-Path $PkgDir 'prompts\system-prompt.template.txt') $promptDir -Force
Copy-Item (Join-Path $PkgDir 'verify-persona.sh')                  $Root -Force
Write-Host '  OK prompts/ + verify-persona.sh'

Write-Host '=== 4. 替换 build-on-board.sh（含补丁自检）===' -ForegroundColor Cyan
Copy-Item (Join-Path $PkgDir 'build-on-board.sh') (Join-Path $Root 'build-on-board.sh') -Force
Write-Host '  OK'

Write-Host '=== 5. 清掉本机编译产物，保持包干净 ===' -ForegroundColor Cyan
foreach ($junk in 'build', 'examples\rkllm_api_demo\deploy\install') {
    $p = Join-Path $Root $junk
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

Write-Host '=== 6. 重新打包 ===' -ForegroundColor Cyan
if (Test-Path $NewTar) { Remove-Item -Force $NewTar }
Push-Location $Stage
try { tar -czf $NewTar $Name } finally { Pop-Location }

Write-Host ''
Write-Host '=== 结果 ===' -ForegroundColor Green
Get-Item $NewTar | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,2)} } | Format-Table -AutoSize

Write-Host '=== 包内结构 ===' -ForegroundColor Cyan
tar -tzf $NewTar

Write-Host ''
Write-Host '=== 关键内容自检 ===' -ForegroundColor Cyan
Write-Host '--- llm_demo.cpp 含补丁的行数 ---'
$tmpCheck = Join-Path $env:TEMP 'check_demo.cpp'
tar -xzf $NewTar -C $env:TEMP -O "$Name/examples/rkllm_api_demo/deploy/src/llm_demo.cpp" > $tmpCheck
(Select-String -Path $tmpCheck -Pattern 'system_prompt_file').Count
Write-Host '--- 提示词首行 ---'
$tmpPrompt = Join-Path $env:TEMP 'check_prompt.txt'
tar -xzf $NewTar -C $env:TEMP -O "$Name/prompts/system-prompt.txt" > $tmpPrompt
Get-Content $tmpPrompt -TotalCount 1

Write-Host ''
Write-Host "下一步：把 $NewTar 传到板子（VSCode 拖拽或 scp），解压后跑 ./build-on-board.sh 再跑 ./verify-persona.sh" -ForegroundColor Yellow
