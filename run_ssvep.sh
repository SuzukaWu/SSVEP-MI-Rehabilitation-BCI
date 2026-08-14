#!/usr/bin/env bash
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
set -euo pipefail

PROJECT_DIR="/home/cat/ssvep_rk3588"
CONDA_SH="/home/cat/miniconda3/etc/profile.d/conda.sh"
ENV_NAME="bci"

if [[ ! -f "$CONDA_SH" ]]; then
    echo "未找到 Conda：$CONDA_SH"
    exit 1
fi

source "$CONDA_SH"
conda activate "$ENV_NAME"

cd "$PROJECT_DIR"
export DISPLAY="${DISPLAY:-:0}"

if [[ -f /home/cat/.Xauthority ]]; then
    export XAUTHORITY="${XAUTHORITY:-/home/cat/.Xauthority}"
fi

echo "Python: $(command -v python)"
echo "DISPLAY=$DISPLAY"
echo "XAUTHORITY=${XAUTHORITY:-未设置}"

exec python stim_ssvep.py
