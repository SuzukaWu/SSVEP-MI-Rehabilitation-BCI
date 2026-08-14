#!/usr/bin/env bash
set -euo pipefail

export LANG=C.UTF-8
export LC_ALL=C.UTF-8

SSVEP_DIR="/home/cat/ssvep_rk3588"
MI_SCRIPT="/home/cat/MI-online/cca-online/online_opt_plus.py"
MI_MODEL="/home/cat/MI-online/cca-online/models/opt_plus_model_bank_station_0515_0516_final.npz"
CONDA_SH="/home/cat/miniconda3/etc/profile.d/conda.sh"
ENV_NAME="bci"
LOG_DIR="${SSVEP_DIR}/logs"
MI_LOG="${LOG_DIR}/mi_online.log"

for required in "$CONDA_SH" "$MI_SCRIPT" "$MI_MODEL" "${SSVEP_DIR}/stim_ssvep.py"; do
    if [[ ! -f "$required" ]]; then
        echo "缺少文件：$required" >&2
        exit 1
    fi
done

# PsychoPy 必须连接 RK3588 图形桌面。SSH 会话中 DISPLAY 为空时直接给出提示，
# 不再强行设置 :0，以免出现 Cannot connect to None / X 授权失败。
if [[ -z "${DISPLAY:-}" ]]; then
    echo "当前终端没有图形桌面 DISPLAY。" >&2
    echo "请在 RK3588 本机桌面终端中运行本脚本。" >&2
    exit 1
fi

source "$CONDA_SH"
conda activate "$ENV_NAME"
mkdir -p "$LOG_DIR"
cd "$SSVEP_DIR"

MI_PID=""
cleanup() {
    if [[ -n "${MI_PID}" ]] && kill -0 "${MI_PID}" 2>/dev/null; then
        kill "${MI_PID}" 2>/dev/null || true
        wait "${MI_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# MI 分类算法和模型均不修改，只把原有 label,score 发往本机 UDP 8889。
python -u "$MI_SCRIPT" \
    --model_path "$MI_MODEL" \
    --subject station \
    --stream_type EEG \
    --stream_name BHB-EEG \
    --n_channels 8 \
    --input_fs_hz 500 \
    --step_sec 0.5 \
    --udp_ip 127.0.0.1 \
    --udp_port 8889 \
    >"$MI_LOG" 2>&1 &
MI_PID=$!

echo "MI 在线分类已启动，PID=${MI_PID}"
echo "MI 日志：${MI_LOG}"
echo "正在启动 SSVEP 自由模式动图版……"

python "$SSVEP_DIR/stim_ssvep.py"
