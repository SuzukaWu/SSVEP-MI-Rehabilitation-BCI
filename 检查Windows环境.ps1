$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
Write-Host "=== Python ==="
python -c "import sys; print(sys.version); print(sys.executable)"
Write-Host "=== 依赖 ==="
python -c "import numpy, scipy, pandas, serial, pylsl, psychopy; print('OK')"
Write-Host "=== 项目文件 ==="
@('stim_ssvep.py','fbcca.py','lsl_received_data.py','config.ini','动作图') | ForEach-Object {
    $ok = Test-Path -LiteralPath (Join-Path $PSScriptRoot $_)
    Write-Host ("{0,-24} {1}" -f $_, $(if($ok){'OK'}else{'MISSING'}))
}
Write-Host "=== COM 口 ==="
[System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object
Write-Host "=== LSL 可见流（5 秒） ==="
python -c "from pylsl import resolve_streams; s=resolve_streams(wait_time=5); print([(x.name(), x.type(), x.channel_count(), x.nominal_srate()) for x in s])"
