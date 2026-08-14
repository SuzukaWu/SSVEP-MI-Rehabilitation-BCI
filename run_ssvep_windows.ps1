param(
    [switch]$NoMI
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "项目目录：$PSScriptRoot"
Write-Host "Python：$((Get-Command python).Source)"
python -c "import sys; print('Python version:', sys.version); print('Python executable:', sys.executable)"
if ($LASTEXITCODE -ne 0) {
    throw "当前终端无法运行 python。请先 conda activate bci。"
}

$required = @(
    "stim_ssvep.py",
    "fbcca.py",
    "lsl_received_data.py",
    "动作图"
)
$missing = @()
foreach ($item in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $item))) {
        $missing += $item
    }
}
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "缺少原 SSVEP 项目文件：$($missing -join ', ')" -ForegroundColor Red
    Write-Host "本压缩包是跨平台替换包。请把这些文件从完整的气动手套 SSVEP 工程复制到当前目录。"
    exit 2
}

$configPath = Join-Path $PSScriptRoot "config.ini"
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "config_windows.example.ini") -Destination $configPath
    Write-Host "已生成 config.ini。首次运行默认 con_flag=0，请先编辑 COM 口。" -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "logs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "eeg_data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "data") | Out-Null

Write-Host "Windows 可见串口："
$ports = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object
if ($ports.Count -eq 0) {
    Write-Host "  未发现 COM 口" -ForegroundColor Yellow
} else {
    $ports | ForEach-Object { Write-Host "  $_" }
}

python -c "import numpy, scipy, pandas, serial, pylsl, psychopy; print('Python dependencies: OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "依赖检查失败。可运行：" -ForegroundColor Yellow
    Write-Host ".\安装Windows基础依赖.ps1"
    exit 3
}

$miProcess = $null
try {
    if (-not $NoMI) {
        $miScript = Join-Path $PSScriptRoot "MI参考_未修改\online_opt_plus.py"
        $miModel = Join-Path $PSScriptRoot "MI参考_未修改\opt_plus_model_bank_station_0515_0516_final.npz"
        if ((Test-Path -LiteralPath $miScript) -and (Test-Path -LiteralPath $miModel)) {
            $stdoutLog = Join-Path $PSScriptRoot "logs\mi_online.stdout.log"
            $stderrLog = Join-Path $PSScriptRoot "logs\mi_online.stderr.log"
            $miArgs = @(
                "-u",
                "`"$miScript`"",
                "--model_path", "`"$miModel`"",
                "--subject", "station",
                "--stream_type", "EEG",
                "--stream_name", "BHB-EEG",
                "--n_channels", "8",
                "--input_fs_hz", "500",
                "--step_sec", "0.5",
                "--udp_ip", "127.0.0.1",
                "--udp_port", "8889"
            )
            $miProcess = Start-Process -FilePath "python" -ArgumentList $miArgs `
                -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog `
                -PassThru -NoNewWindow
            Write-Host "MI 在线分类已启动，PID=$($miProcess.Id)"
            Write-Host "MI 日志：$stdoutLog / $stderrLog"
        } else {
            Write-Host "未找到 MI 脚本或模型，将只启动 SSVEP；反馈页显示暂无数据。" -ForegroundColor Yellow
        }
    } else {
        Write-Host "已使用 -NoMI：只启动 SSVEP。"
    }

    Write-Host "正在启动 SSVEP 自由模式动图版……"
    python -u ".\stim_ssvep.py"
    $exitCode = $LASTEXITCODE
}
finally {
    if ($null -ne $miProcess -and -not $miProcess.HasExited) {
        Stop-Process -Id $miProcess.Id -Force -ErrorAction SilentlyContinue
    }
}

exit $exitCode
