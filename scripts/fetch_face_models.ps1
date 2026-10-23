# 下载 YuNet / SFace 模型到 vision\models\
#
# ⚠️ 这个脚本要在**你自己的 PowerShell**里跑（不要通过 agent 跑）：
#    agent 的网络走沙箱代理，模型站会被 403 挡掉。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_face_models.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\fetch_face_models.ps1 -Force
#
# 下完告诉我（或直接 `python scripts\vision_selftest.py`），我这边切 YuNet/SFace 验证。

param([switch]$Force)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$modelDir = Join-Path $root "vision\models"
New-Item -ItemType Directory -Force -Path $modelDir | Out-Null

$models = @(
    @{
        Name = "face_detection_yunet_2023mar.onnx"
        MinBytes = 150000
        Urls = @(
            "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
            "https://cdn.jsdelivr.net/gh/opencv/opencv_zoo@main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
            "https://ghproxy.net/https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
        )
    },
    @{
        Name = "face_recognition_sface_2021dec.onnx"
        MinBytes = 30000000
        Urls = @(
            "https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
            "https://ghproxy.net/https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
            "https://cdn.jsdelivr.net/gh/opencv/opencv_zoo@main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
        )
    }
)

foreach ($model in $models) {
    $dest = Join-Path $modelDir $model.Name
    if ((Test-Path $dest) -and -not $Force) {
        $size = (Get-Item $dest).Length
        if ($size -ge $model.MinBytes) {
            Write-Host "[skip] $($model.Name) 已存在（$([math]::Round($size/1MB,1)) MB）" -ForegroundColor DarkGray
            continue
        }
        Write-Host "[warn] $($model.Name) 大小不对（$size B），重新下" -ForegroundColor Yellow
    }

    $ok = $false
    foreach ($url in $model.Urls) {
        try {
            Write-Host "[get ] $($model.Name) ← $url" -ForegroundColor Cyan
            Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 180
            $size = (Get-Item $dest).Length
            if ($size -lt $model.MinBytes) {
                throw "文件太小（$size B），可能被网关换成了错误页"
            }
            $ok = $true
            break
        } catch {
            Write-Host "       失败：$($_.Exception.Message)" -ForegroundColor DarkYellow
            Remove-Item -Force -ErrorAction SilentlyContinue $dest
        }
    }

    if (-not $ok) {
        Write-Host "[fail] $($model.Name) 三个地址都没成功 —— 用浏览器手动下，另存到：" -ForegroundColor Red
        Write-Host "       $dest" -ForegroundColor Red
        continue
    }

    $hash = (Get-FileHash -Algorithm SHA256 -Path $dest).Hash.ToLower()
    Write-Host "[ok  ] $($model.Name)  $([math]::Round((Get-Item $dest).Length/1MB,2)) MB" -ForegroundColor Green
    Write-Host "       sha256 = $hash" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "下一步：python scripts\vision_selftest.py" -ForegroundColor Green
