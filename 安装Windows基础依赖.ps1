$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
Write-Host "请确认已先执行 conda activate bci。"
python -c "import sys; print(sys.executable)"
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r ".\requirements_windows_minimal.txt"
Write-Host "基础依赖安装完成。PsychoPy 请继续使用当前 bci 环境中已安装的版本。"
