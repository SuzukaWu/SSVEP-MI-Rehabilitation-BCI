#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Online opt-plus score-map template CCA inference from an LSL EEG stream."""

from __future__ import annotations

import argparse
import socket
import time
from collections import deque
from dataclasses import dataclass
from math import gcd
from pathlib import Path
from typing import Sequence, Tuple

import numpy as np
import scipy.linalg
import scipy.signal


@dataclass(frozen=True)
class OnlineConfig:
    model_path: str
    subject: str = ""
    stream_type: str = "EEG"
    stream_name: str = ""
    n_channels: int = 0
    input_fs_hz: int = 500
    step_sec: float = 0.5
    udp_ip: str = "127.0.0.1"
    udp_port: int = 8889


def _as_str(value: np.ndarray) -> str:
    return str(np.asarray(value).item())


def _resolve_first_eeg_stream(stream_type: str, stream_name: str):
    import pylsl

    all_streams = []
    if stream_name and hasattr(pylsl, "resolve_byprop"):
        streams = pylsl.resolve_byprop("name", stream_name, timeout=3.0)
        if streams:
            return streams[0]
    if hasattr(pylsl, "resolve_byprop"):
        streams = pylsl.resolve_byprop("type", stream_type, timeout=3.0)
        if streams:
            return streams[0]
    if hasattr(pylsl, "resolve_stream"):
        streams = pylsl.resolve_stream("type", stream_type)
        if streams:
            return streams[0]
    if hasattr(pylsl, "resolve_streams"):
        all_streams = pylsl.resolve_streams(wait_time=3.0)
        for info in all_streams:
            if stream_name and info.name() == stream_name:
                return info
        for info in all_streams:
            if info.type() == stream_type:
                return info

    visible = ", ".join(f"{info.name()}:{info.type()}" for info in all_streams) or "none"
    raise RuntimeError(f"未找到 LSL 流: type={stream_type}, name={stream_name or '*'}, visible={visible}")


def _wait_for_eeg_stream(stream_type: str, stream_name: str):
    last_status_at = 0.0
    print(f"Waiting for LSL stream: type={stream_type}, name={stream_name or '*'}", flush=True)
    while True:
        try:
            info = _resolve_first_eeg_stream(stream_type, stream_name)
            print(f"Found LSL stream: name={info.name()}, type={info.type()}", flush=True)
            return info
        except RuntimeError as exc:
            now = time.time()
            if now - last_status_at >= 2.0:
                print(f"{exc}；继续等待上位机创建 LSL 流...", flush=True)
                last_status_at = now
            time.sleep(0.5)


def _read_inlet_chunk(inlet, n_channels: int, timeout: float = 0.2) -> np.ndarray:
    chunk, _ts = inlet.pull_chunk(timeout=float(timeout), max_samples=512)
    if not chunk:
        return np.zeros((int(n_channels), 0), dtype=np.float32)
    arr = np.asarray(chunk, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] < int(n_channels):
        return np.zeros((int(n_channels), 0), dtype=np.float32)
    return np.asarray(arr[:, : int(n_channels)].T, dtype=np.float32)


def _resample_eeg(x: np.ndarray, input_fs_hz: int, target_fs_hz: int) -> np.ndarray:
    if int(input_fs_hz) == int(target_fs_hz):
        return np.asarray(x, dtype=np.float32)
    up = int(target_fs_hz)
    down = int(input_fs_hz)
    g = gcd(up, down)
    return np.asarray(scipy.signal.resample_poly(x, up // g, down // g, axis=1), dtype=np.float32)


# 注意：此函数保持原模型训练时的实现，以确保现有阈值和模板仍兼容。
# 未重新训练模型前，不应单独替换 CCA 数学实现。
def _cca_similarity(x: np.ndarray, template: np.ndarray, reg: float) -> float:
    X = np.asarray(x, dtype=np.float64).T
    Y = np.asarray(template, dtype=np.float64).T
    X = X - np.mean(X, axis=0, keepdims=True)
    Y = Y - np.mean(Y, axis=0, keepdims=True)
    n = int(X.shape[0])
    p = int(X.shape[1])
    denom = float(max(1, n - 1))
    Sxx = (X.T @ X) / denom + float(reg) * np.eye(p)
    Syy = (Y.T @ Y) / denom + float(reg) * np.eye(p)
    Sxy = (X.T @ Y) / denom
    mid = scipy.linalg.solve(Syy, Sxy.T, assume_a="pos", check_finite=False)
    mat = scipy.linalg.solve(Sxx, Sxy @ mid, assume_a="pos", check_finite=False)
    eigvals = scipy.linalg.eigh(0.5 * (mat + mat.T), eigvals_only=True, check_finite=False)
    return float(np.sqrt(max(0.0, min(1.0, float(np.max(eigvals)) if eigvals.size else 0.0))))


def _shifted_views(x: np.ndarray, template: np.ndarray, shift_samples: int) -> Tuple[np.ndarray, np.ndarray] | None:
    n = int(x.shape[1])
    shift = int(shift_samples)
    if abs(shift) >= n - 2:
        return None
    if shift > 0:
        xx, tt = x[:, shift:n], template[:, : n - shift]
    elif shift < 0:
        s = -shift
        xx, tt = x[:, : n - s], template[:, s:n]
    else:
        xx, tt = x, template
    if int(xx.shape[1]) <= int(xx.shape[0]) + 2:
        return None
    return xx, tt


def _template_similarity(x: np.ndarray, template: np.ndarray, shifts: Sequence[int], reg: float, shift_agg: str, top_k: int) -> float:
    scores = []
    for shift in shifts:
        views = _shifted_views(x, template, int(shift))
        if views is not None:
            scores.append(_cca_similarity(views[0], views[1], reg))
    if not scores:
        return _cca_similarity(x, template, reg)
    arr = np.asarray(scores, dtype=np.float64)
    if str(shift_agg).lower() == "topk":
        k = int(max(1, min(int(top_k), arr.size)))
        return float(np.mean(np.sort(arr)[-k:]))
    return float(np.max(arr))


class OptPlusModel:
    def __init__(self, path: str, subject: str = "") -> None:
        blob = np.load(path, allow_pickle=False)
        self.path = path
        self.subjects = np.asarray(blob["subjects"]).astype(str)
        idx = 0
        if subject:
            matches = np.where(self.subjects == str(subject))[0]
            if matches.size == 0:
                raise ValueError(f"模型中没有 subject={subject}; available={self.subjects.tolist()}")
            idx = int(matches[0])
        self.subject = str(self.subjects[idx])
        self.fs = int(round(float(blob["fs"])))
        self.window_length_sec = float(blob["window_length_sec"])
        self.filter_banks = np.asarray(blob["filter_banks"], dtype=np.float64)
        self.channel_names = np.asarray(blob["channel_names"]).astype(str).tolist()
        self.cca_reg = float(blob["cca_reg"])
        self.filter_order = int(blob["filter_order"])
        self.fusion = _as_str(blob["fusion"])
        self.top_k = int(blob["top_k"])
        self.feature_mode = _as_str(blob["feature_mode"])
        self.baseline_correct = int(blob["baseline_correct"])
        self.template_shift_sec = np.asarray(blob["template_shift_sec"], dtype=np.float64)
        self.shift_samples = [int(round(float(v) * self.fs)) for v in self.template_shift_sec.tolist()]
        self.shift_agg = _as_str(blob["shift_agg"])
        self.shift_top_k = int(blob["shift_top_k"])
        self.rest_template = np.asarray(blob["rest_templates"][idx], dtype=np.float64)
        self.feet_template = np.asarray(blob["feet_templates"][idx], dtype=np.float64)
        self.weights = np.asarray(blob["weights"][idx], dtype=np.float64)
        self.directions = np.asarray(blob["directions"][idx], dtype=np.float64)
        self.threshold = float(np.asarray(blob["thresholds"], dtype=np.float64)[idx])
        self.filters = [
            scipy.signal.butter(self.filter_order, [float(lo), float(hi)], btype="band", fs=float(self.fs), output="sos")
            for lo, hi in self.filter_banks.tolist()
        ]

    @property
    def n_channels(self) -> int:
        return int(self.rest_template.shape[1])

    @property
    def window_samples(self) -> int:
        return int(round(self.window_length_sec * float(self.fs)))

    def _filter_segment(self, segment: np.ndarray, sos: np.ndarray) -> np.ndarray:
        y = np.asarray(scipy.signal.sosfiltfilt(sos, segment, axis=1), dtype=np.float64)
        if self.feature_mode == "waveform":
            return y
        analytic = scipy.signal.hilbert(y, axis=1)
        log_power = np.log(np.abs(analytic) ** 2 + 1e-12)
        if self.baseline_correct:
            log_power = log_power - np.mean(log_power, axis=1, keepdims=True)
        return np.asarray(log_power, dtype=np.float64)

    def score_map(self, segment: np.ndarray) -> np.ndarray:
        n_times = int(self.feet_template.shape[0])
        out = np.zeros((n_times, len(self.filters)), dtype=np.float64)
        for b, sos in enumerate(self.filters):
            x_band = self._filter_segment(segment, sos)
            rest_score = _template_similarity(x_band, self.rest_template[b], self.shift_samples, self.cca_reg, self.shift_agg, self.shift_top_k)
            for t in range(n_times):
                feet_score = _template_similarity(x_band, self.feet_template[t, b], self.shift_samples, self.cca_reg, self.shift_agg, self.shift_top_k)
                out[t, b] = float(feet_score) - float(rest_score)
        return out

    def predict(self, segment: np.ndarray) -> Tuple[int, float]:
        score_map = self.score_map(segment)
        adjusted = score_map * self.directions
        if self.fusion == "max":
            score = float(np.max(adjusted))
        elif self.fusion == "topk":
            flat = adjusted.reshape(-1)
            k = int(max(1, min(flat.size, self.top_k)))
            score = float(np.mean(np.sort(flat)[-k:]))
        elif self.fusion == "weighted":
            score = float(np.sum(adjusted * self.weights))
        else:
            score = float(np.mean(adjusted))
        return (1 if score > self.threshold else 0), score


def _send_result(sock: socket.socket, ip: str, port: int, label: int, score: float) -> None:
    sock.sendto(f"{label},{score:.6f}".encode("utf-8"), (ip, int(port)))


def run_online(cfg: OnlineConfig) -> None:
    from pylsl import StreamInlet

    model = OptPlusModel(cfg.model_path, cfg.subject)
    info = _wait_for_eeg_stream(cfg.stream_type, cfg.stream_name)

    stream_channels = int(info.channel_count())
    requested_channels = int(cfg.n_channels or model.n_channels)
    if requested_channels < model.n_channels:
        raise RuntimeError(
            f"配置只读取 {requested_channels} 个通道，但模型要求 "
            f"{model.n_channels} 个通道。"
        )
    if stream_channels < requested_channels:
        raise RuntimeError(
            f"LSL 流只有 {stream_channels} 个通道，但程序配置为读取 "
            f"{requested_channels} 个通道。"
        )
    n_channels = requested_channels

    stream_fs = float(info.nominal_srate())
    effective_input_fs_hz = (
        int(round(stream_fs)) if stream_fs > 0 else int(cfg.input_fs_hz)
    )
    if stream_fs > 0 and abs(stream_fs - float(cfg.input_fs_hz)) > 1.0:
        print(
            f"警告：命令行采样率为 {cfg.input_fs_hz} Hz，但 LSL 报告为 "
            f"{stream_fs:.2f} Hz；自动使用 {effective_input_fs_hz} Hz。",
            flush=True,
        )

    inlet = StreamInlet(info, max_chunklen=256)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw_window_samples = int(
        round(model.window_length_sec * float(effective_input_fs_hz))
    )
    step_samples = int(round(float(cfg.step_sec) * float(effective_input_fs_hz)))
    buffer: deque[np.ndarray] = deque(
        maxlen=max(raw_window_samples * 2, raw_window_samples + step_samples)
    )
    last_infer_count = 0
    sample_count = 0
    last_buffer_status_at = 0.0
    last_empty_chunk_status_at = 0.0

    print(f"Loaded opt-plus model: {cfg.model_path}, subject={model.subject}")
    print(
        f"LSL inlet: name={info.name()}, type={info.type()}, "
        f"stream_channels={stream_channels}, online_channels={n_channels}, "
        f"model_channels={model.n_channels}, "
        f"nominal_srate={stream_fs:.2f} Hz, "
        f"effective_srate={effective_input_fs_hz} Hz"
    )
    print(
        f"window={model.window_length_sec:.2f}s, step={cfg.step_sec:.2f}s, "
        f"UDP={cfg.udp_ip}:{cfg.udp_port}"
    )

    while True:
        chunk = _read_inlet_chunk(inlet, n_channels=n_channels)
        if chunk.shape[1] == 0:
            now_t = time.time()
            if now_t - last_empty_chunk_status_at >= 2.0:
                print("Waiting for EEG samples from LSL inlet...", flush=True)
                last_empty_chunk_status_at = now_t
            time.sleep(0.02)
            continue
        for i in range(chunk.shape[1]):
            buffer.append(chunk[: model.n_channels, i].copy())
        sample_count += int(chunk.shape[1])
        if len(buffer) < raw_window_samples or sample_count - last_infer_count < step_samples:
            now_t = time.time()
            if now_t - last_buffer_status_at >= 1.0:
                print(f"Buffering EEG samples: {len(buffer)}/{raw_window_samples}", flush=True)
                last_buffer_status_at = now_t
            continue
        last_infer_count = sample_count
        raw = np.asarray(buffer, dtype=np.float32)[-raw_window_samples:].T
        seg = _resample_eeg(raw, effective_input_fs_hz, model.fs)
        if seg.shape[1] > model.window_samples:
            seg = seg[:, -model.window_samples :]
        pred, score = model.predict(seg)
        _send_result(sock, cfg.udp_ip, cfg.udp_port, pred, score)
        print(
            f"pred={pred} score={score:.6f} thr={model.threshold:.6f} "
            f"margin={score - model.threshold:.6f} raw_mean={float(np.mean(raw)):.3f} raw_std={float(np.std(raw)):.3f}",
            flush=True,
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Online opt-plus score-map template CCA inference")
    station_root = Path(__file__).resolve().parents[1]
    p.add_argument("--model_path", type=str, default=str(station_root / "models" / "opt_plus_model_bank_station_0515_0516_final.npz"))
    p.add_argument("--subject", type=str, default="")
    p.add_argument("--stream_type", type=str, default="EEG")
    p.add_argument("--stream_name", type=str, default="BHB-EEG")
    p.add_argument("--n_channels", type=int, default=0)
    p.add_argument("--input_fs_hz", type=int, default=500)
    p.add_argument("--step_sec", type=float, default=0.5)
    p.add_argument("--udp_ip", type=str, default="127.0.0.1")
    p.add_argument("--udp_port", type=int, default=8889)
    return p


def main() -> None:
    args = _build_arg_parser().parse_args()
    run_online(OnlineConfig(**vars(args)))


if __name__ == "__main__":
    main()
