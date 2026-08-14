import os
import queue as queue_module
import time

import numpy as np
import pandas as pd
from pylsl import StreamInlet, resolve_byprop


def _wait_for_eeg_stream():
    """优先按 type=EEG 查找；兼容旧程序的 BHB-EEG/TestStream 名称。"""
    candidates = (
        ("name", "BHB-EEG"),
        ("type", "EEG"),
        ("name", "TestStream"),
    )

    while True:
        for prop, value in candidates:
            streams = resolve_byprop(prop, value, timeout=2.0)
            if streams:
                info = streams[0]
                print(
                    "找到LSL流："
                    f"name={info.name()}, type={info.type()}, "
                    f"channels={info.channel_count()}, rate={info.nominal_srate()}"
                )
                return info

        print("尚未找到EEG LSL流，2秒后继续查找……")
        time.sleep(2.0)


def _flush_inlet(inlet):
    """清掉开始记录前积压的旧样本，保证本轮文件只包含本轮数据。"""
    while True:
        chunk, _timestamps = inlet.pull_chunk(timeout=0.0, max_samples=1024)
        if not chunk:
            break


def _wait_for_first_sample(inlet):
    """只有在 LSL 流真正开始发送数据后才允许刺激程序进入第一轮。"""
    last_status_at = 0.0
    while True:
        sample, _timestamp = inlet.pull_sample(timeout=1.0)
        if sample is not None:
            print(
                "EEG LSL 已收到首个样本："
                f"channels={len(sample)}"
            )
            return

        now = time.time()
        if now - last_status_at >= 2.0:
            print("已找到 BHB-EEG 流，但尚未收到样本；请在上位机点击“开始采集”……")
            last_status_at = now


def received_data(command_queue, save_path, ready_event=None, file_ready_event=None):
    """根据 start/end 指令记录一轮 EEG，并在写盘完成后设置事件。"""
    os.makedirs(save_path, exist_ok=True)

    stream_info = _wait_for_eeg_stream()
    inlet = StreamInlet(stream_info, max_buflen=30, max_chunklen=512)

    try:
        inlet.open_stream(timeout=10.0)
    except Exception as open_error:
        print("LSL open_stream 提示：{}；继续等待实际样本。".format(open_error))

    _wait_for_first_sample(inlet)
    _flush_inlet(inlet)
    if ready_event is not None:
        ready_event.set()

    while True:
        word = command_queue.get()

        if word == "del":
            print("存储脑电程序退出")
            return
        if not str(word).startswith("start"):
            continue

        if file_ready_event is not None:
            file_ready_event.clear()

        _flush_inlet(inlet)
        eeg_data = []
        print(f"time: {time.time()}, 开始记录数据: {word}")
        stop_requested = False

        while not stop_requested:
            chunk, _timestamps = inlet.pull_chunk(timeout=0.1, max_samples=512)
            if chunk:
                eeg_data.extend(chunk)

            # 不使用 multiprocessing.Queue.empty()；它在并发场景中并不可靠。
            while True:
                try:
                    command = command_queue.get_nowait()
                except queue_module.Empty:
                    break

                if command == "end":
                    stop_requested = True
                    break
                if command == "del":
                    print("存储脑电程序退出")
                    return

        eeg_array = np.asarray(eeg_data, dtype=np.float64)
        print(
            f"time: {time.time()}, 存储数据: {word}, "
            f"shape: {eeg_array.shape}"
        )

        output_path = os.path.join(save_path, f"{word}.csv")
        temporary_path = output_path + ".tmp"

        # index=False 很重要：旧版 CSV 会额外写入索引列，导致主程序把索引当成 EEG 通道。
        pd.DataFrame(eeg_array).to_csv(temporary_path, index=False)
        os.replace(temporary_path, output_path)

        if file_ready_event is not None:
            file_ready_event.set()


if __name__ == "__main__":
    import multiprocessing
    import threading

    command_queue = multiprocessing.Queue()
    ready = threading.Event()
    saved = threading.Event()
    received_data(command_queue, "eeg_data", ready, saved)
