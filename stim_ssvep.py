# from __future__ import absolute_import, division
# SSVEP + MI 自由使用动图版（Windows / Linux 跨平台版）：无预设标签；MI 仅用于反馈显示。
import configparser
import csv
import inspect
import multiprocessing
import os
import platform
import socket
import sys
import threading
import time
from collections import deque

import numpy as np
import pandas as pd
import pyglet
import serial
from scipy.signal import cheby1, cheb1ord, resample

from psychopy import gui, visual, core, data, logging
from psychopy.constants import (NOT_STARTED, STARTED, FINISHED)
from psychopy.hardware import keyboard
from numpy import (sin, pi, )

import fbcca
from lsl_received_data import received_data
from lsl_received_data import received_data as received_data_MI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# === CJK_FONT_FORCE_BEGIN ===
# 不再写死 RK3588 的 /usr/share/fonts 路径。Windows 优先使用微软雅黑，
# Linux 继续使用 Noto Sans CJK，macOS 使用苹方。
_SYSTEM_NAME = platform.system()
if _SYSTEM_NAME == "Windows":
    CJK_FONT_NAME = "Microsoft YaHei"
    CJK_FONT_FILE = os.path.join(
        os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "msyh.ttc")
elif _SYSTEM_NAME == "Darwin":
    CJK_FONT_NAME = "PingFang SC"
    CJK_FONT_FILE = None
else:
    CJK_FONT_NAME = "Noto Sans CJK SC"
    CJK_FONT_FILE = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

if CJK_FONT_FILE and os.path.isfile(CJK_FONT_FILE):
    try:
        pyglet.font.add_file(CJK_FONT_FILE)
    except Exception as font_error:
        print("中文字体加载警告：", font_error)

# 强制所有 visual.TextStim 使用当前系统可用的中文字体。
_original_textstim_init = visual.TextStim.__init__
try:
    _textstim_parameters = inspect.signature(_original_textstim_init).parameters
except (TypeError, ValueError):
    _textstim_parameters = {}
_has_font_files = "fontFiles" in _textstim_parameters


def _cjk_textstim_init(self, *args, **kwargs):
    args = list(args)
    if len(args) >= 3:
        args[2] = CJK_FONT_NAME
    else:
        kwargs["font"] = CJK_FONT_NAME

    if _has_font_files and CJK_FONT_FILE and os.path.isfile(CJK_FONT_FILE):
        kwargs["fontFiles"] = (CJK_FONT_FILE,)

    return _original_textstim_init(self, *args, **kwargs)


visual.TextStim.__init__ = _cjk_textstim_init
# === CJK_FONT_FORCE_END ===

class MIResultReceiver:
    """后台接收 MI 程序通过 UDP 发送的 ``label,score``，不阻塞刺激刷新。"""

    def __init__(self, bind_ip='127.0.0.1', port=8889, history_size=512):
        self.bind_ip = str(bind_ip)
        self.port = int(port)
        self._history = deque(maxlen=max(8, int(history_size)))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._socket = None
        self.available = False

    def start(self):
        self._thread = threading.Thread(target=self._run, name='mi-udp-receiver')
        self._thread.daemon = True
        self._thread.start()

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket = sock
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.bind_ip, self.port))
            sock.settimeout(0.2)
            self.available = True
            print('MI UDP 接收器已启动：{}:{}'.format(self.bind_ip, self.port))
            while not self._stop_event.is_set():
                try:
                    payload, _addr = sock.recvfrom(256)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    text = payload.decode('utf-8').strip()
                    label_text, score_text = text.split(',', 1)
                    record = {
                        'label': int(label_text),
                        'score': float(score_text),
                        'received_at': time.time(),
                    }
                    with self._lock:
                        self._history.append(record)
                except Exception as parse_error:
                    print('忽略无效 MI UDP 数据：{} ({})'.format(payload, parse_error))
        except Exception as receiver_error:
            print('MI UDP 接收器启动失败，将显示“暂无数据”：{}'.format(receiver_error))
        finally:
            self.available = False
            try:
                sock.close()
            except Exception:
                pass

    def latest_between(self, start_time, end_time):
        """返回本轮刺激时间范围内收到的最新结果，避免沿用上一轮数据。"""
        start_time = float(start_time)
        end_time = float(end_time)
        with self._lock:
            for record in reversed(self._history):
                received_at = float(record['received_at'])
                if received_at > end_time:
                    continue
                if received_at >= start_time:
                    return dict(record)
                break
        return None

    def summary_between(self, start_time, end_time, threshold,
                        warmup_sec=1.8, top_fraction=0.30,
                        sustain_threshold_ratio=0.25):
        """汇总本轮 MI 强度，只改变反馈数值，不参与 SSVEP 分类。

        前 ``warmup_sec`` 秒的滑动窗口可能混入上一阶段数据，因此优先忽略。
        强度使用“最高一部分窗口的平均值”，避免大量零分把第 75 百分位
        长期压在同一范围，也避免仅使用单个最大值而被偶发尖峰支配。

        同时根据有多少窗口达到阈值的一部分，加入轻量的持续性权重：
        短暂出现较高分会升高，连续维持较高分会进一步升高。
        """
        start_time = float(start_time)
        end_time = float(end_time)
        threshold = float(threshold)
        warmup_sec = max(0.0, float(warmup_sec))
        top_fraction = float(np.clip(top_fraction, 0.10, 0.60))
        sustain_threshold_ratio = float(
            np.clip(sustain_threshold_ratio, 0.05, 1.00)
        )

        with self._lock:
            all_records = [
                dict(record)
                for record in self._history
                if start_time <= float(record['received_at']) <= end_time
            ]

        if not all_records:
            return None

        effective_start = start_time + warmup_sec
        records = [
            record for record in all_records
            if float(record['received_at']) >= effective_start
        ]
        # 本轮太短或 MI 刚启动时，避免因为预热过滤而直接显示“暂无数据”。
        if not records:
            records = all_records

        raw_scores = np.asarray(
            [float(record['score']) for record in records],
            dtype=np.float64,
        )
        labels = np.asarray(
            [int(record['label']) for record in records],
            dtype=np.int64,
        )

        # 负分表示更偏向静息模板；用于“运动想象强度”时按 0 处理。
        scores = np.maximum(raw_scores, 0.0)
        count = int(scores.size)

        top_count = int(max(
            1,
            min(count, int(np.ceil(count * top_fraction))),
        ))
        top_scores = np.sort(scores)[-top_count:]
        top_mean = float(np.mean(top_scores))

        sustain_level = float(max(0.0, threshold * sustain_threshold_ratio))
        sustain_count = int(np.count_nonzero(scores >= sustain_level))
        sustain_ratio = float(sustain_count / count)

        # 最高窗口均值决定主要强度；持续性只做 0.75～1.00 倍的温和修正。
        effective_score = float(
            top_mean * (0.75 + 0.25 * sustain_ratio)
        )

        positive_count = int(np.count_nonzero(raw_scores > threshold))

        return {
            'label': int(effective_score > threshold),
            'score': effective_score,
            'top_mean_score': top_mean,
            'top_count': top_count,
            'top_fraction': top_fraction,
            'sustain_level': sustain_level,
            'sustain_count': sustain_count,
            'sustain_ratio': sustain_ratio,
            'mean_score': float(np.mean(raw_scores)),
            'median_score': float(np.median(raw_scores)),
            'score_min': float(np.min(raw_scores)),
            'score_max': float(np.max(raw_scores)),
            'count': count,
            'positive_count': positive_count,
            'positive_ratio': float(positive_count / count),
            'received_at': float(records[-1]['received_at']),
            'raw_positive_ratio': float(np.mean(labels == 1)),
        }

    def close(self):
        self._stop_event.set()
        try:
            if self._socket is not None:
                self._socket.close()
        except Exception:
            pass


def mi_score_to_percent(score, threshold, mapping_scale=0.65):
    """把本轮有效分数展开为 0～100% 的反馈指数，而不是概率。

    旧映射把 score=0 固定显示为约 10%，导致大量低分都挤在 10～12%。
    新映射从 0% 起步，并对低分区域更敏感：
    score=0 -> 0%，score=threshold -> 约 79%，
    score=2*threshold -> 约 95%。
    """
    threshold = float(threshold)
    mapping_scale = float(mapping_scale)
    if threshold <= 0 or mapping_scale <= 0:
        return None

    nonnegative_score = max(0.0, float(score))
    percent = 100.0 * (
        1.0 - np.exp(
            -nonnegative_score / (mapping_scale * threshold)
        )
    )
    return float(np.clip(percent, 0.0, 100.0))


def responsive_wait(duration, win, drawables=None, animation_frames=None,
                    animation_fps=12.0, animation_pos=(-430, 70),
                    animation_size=(440, 440)):
    """保持原反馈时长，并循环播放识别动作的逐帧动画。

    右侧倒计时进度条已移除。动画直接复用“动作图”中已经预加载的
    动作帧，因此不会依赖 GIF 解码，也不会改变 SSVEP、MI 或手套逻辑。
    """
    drawables = list(drawables or [])
    animation_frames = list(animation_frames or [])

    # 动画帧原本用于八目标刺激；反馈期间临时移动到反馈区，结束后恢复。
    original_geometry = []
    for frame in animation_frames:
        try:
            original_geometry.append((frame, tuple(frame.pos), tuple(frame.size)))
            frame.setAutoDraw(False)
            frame.setPos(animation_pos)
            frame.setSize(animation_size)
        except Exception as geometry_error:
            print('反馈动画帧设置失败：{}'.format(geometry_error))

    wait_timer = core.Clock()
    wait_timer.reset()
    last_frame_index = -1

    try:
        while wait_timer.getTime() < duration:
            elapsed = wait_timer.getTime()
            for drawable in drawables:
                drawable.draw()

            if animation_frames:
                frame_index = int(elapsed * float(animation_fps)) % len(animation_frames)
                if frame_index != last_frame_index:
                    last_frame_index = frame_index
                animation_frames[frame_index].draw()

            win.flip()

            if keyboard.Keyboard().getKeys(keyList=["escape"]):
                core.quit()
    finally:
        for frame, old_pos, old_size in original_geometry:
            try:
                frame.setAutoDraw(False)
                frame.setPos(old_pos)
                frame.setSize(old_size)
            except Exception:
                pass

def decorator(func):
    def wrapper(*args, **kwargs):
        try:
            res = func(*args, **kwargs)
            return res
        except Exception as e:
            print("执行函数：{}，出现异常：{}".format(func.__name__, e))
    return wrapper

def startDraw(win,image,t,tThisFlipGlobal,frameN):
    image.frameNStart = frameN  # exact frame index
    image.tStart = t  # local t and not account for scr refresh
    image.tStartRefresh = tThisFlipGlobal  # on global time
    win.timeOnFlip(image, 'tStartRefresh')  # time at next scr refresh
    image.setAutoDraw(True)

def stopDraw(win,image,t,tThisFlipGlobal,frameN):
    image.tStop = t  # not accounting for scr refresh
    image.frameNStop = frameN  # exact frame index
    win.timeOnFlip(image, 'tStopRefresh')  # time at next scr refresh
    image.setAutoDraw(False)

def stim():
    n=1
    multiprocessing.freeze_support()

    # 无论从 VS Code、PowerShell 还是双击脚本启动，都以程序目录为工作目录。
    _thisDir = BASE_DIR
    os.chdir(_thisDir)

    cf = configparser.ConfigParser()
    config_path = os.path.join(_thisDir, 'config.ini')
    if not os.path.isfile(config_path):
        raise FileNotFoundError(
            '未找到 config.ini。请把 config_windows.example.ini 复制为 config.ini，'
            '再填写手套 COM 口。当前目录：{}'.format(_thisDir))
    cf.read(config_path, encoding='utf-8-sig')
    if not cf.has_section('localdb'):
        raise RuntimeError('config.ini 缺少 [localdb] section：{}'.format(config_path))

    save_path = os.path.expandvars(os.path.expanduser(
        cf.get('localdb', 'save_path', fallback='./eeg_data').strip()))
    # 旧 RK3588 配置中的 /home/cat/... 不能在 Windows 上直接使用。
    if os.name == 'nt' and (save_path.startswith('/home/') or save_path.startswith('/usr/')):
        print('检测到 Linux 保存路径，Windows 下自动改为项目内 eeg_data：{}'.format(save_path))
        save_path = os.path.join(_thisDir, 'eeg_data')
    elif not os.path.isabs(save_path):
        save_path = os.path.abspath(os.path.join(_thisDir, save_path))
    os.makedirs(save_path, exist_ok=True)

    port = cf.get('localdb', 'port', fallback='').strip()
    con_flag = cf.getint('localdb', 'con_flag', fallback=0)

    # Windows 串口名应为 COM3/COM4 等；配置为 auto 时只在“恰好一个端口”时自动选择。
    if con_flag and os.name == 'nt':
        try:
            from serial.tools import list_ports
            available_ports = [item.device for item in list_ports.comports()]
        except Exception:
            available_ports = []
        if port.lower() == 'auto':
            if len(available_ports) == 1:
                port = available_ports[0]
                print('自动选择气动手套串口：{}'.format(port))
            else:
                raise RuntimeError(
                    'port=auto 需要系统中恰好一个串口；当前串口：{}。'
                    '请在 config.ini 中明确填写 COM 口。'.format(
                        ', '.join(available_ports) if available_ports else '未发现'))
        elif port.startswith('/dev/'):
            raise RuntimeError(
                'config.ini 仍是 Linux 串口 {}。Windows 请改为 COM3/COM4 等。'
                '当前可见串口：{}'.format(
                    port, ', '.join(available_ports) if available_ports else '未发现'))
        elif available_ports and port not in available_ports:
            raise RuntimeError(
                '配置的串口 {} 当前不存在。Windows 可见串口：{}'.format(
                    port, ', '.join(available_ports)))

    # 开启 EEG 记录线程，并等待它真正连接到正在发送样本的 LSL 流。
    # 原程序启动线程后立即进入闪烁，第一轮结束时采集线程可能尚未就绪，
    # 从而写出 0 行 CSV，随后 scipy.resample(空数组) 导致程序退出。
    queue = multiprocessing.Queue()
    eeg_capture_ready = threading.Event()
    eeg_file_ready = threading.Event()
    print('正在启动 EEG LSL 采集线程……')
    process = threading.Thread(
        target=received_data,
        args=(queue, save_path, eeg_capture_ready, eeg_file_ready),
        name='ssvep-eeg-recorder',
    )
    process.daemon = True
    process.start()

    print('等待 BHB-EEG 流开始发送样本……')
    if not eeg_capture_ready.wait(timeout=60.0):
        queue.put('del')
        raise RuntimeError(
            '60 秒内没有收到 BHB-EEG 样本。请先在 BHB-EEGSuite 中连接头环、'
            '进入 EEG 模式并点击“开始采集”，确认实时波形正在刷新后再运行本程序。'
        )
    print('EEG LSL 采集线程已就绪。')

    # time of stimulation（支持 0.5 秒步进，因此使用 float）
    trial_dura = float(cf.get('localdb', 'trial_dura'))
    t_stim = float(cf.get('localdb', 't_stim'))

    # MI 结果只用于反馈页面，不参与 SSVEP 分类、手套控制或时序。
    mi_feedback_enabled = cf.getboolean('mi_feedback', 'enabled', fallback=True)
    mi_udp_ip = cf.get('mi_feedback', 'udp_ip', fallback='127.0.0.1')
    mi_udp_port = cf.getint('mi_feedback', 'udp_port', fallback=8889)
    mi_threshold = cf.getfloat(
        'mi_feedback', 'threshold', fallback=0.013461242301178974)
    mi_warmup_sec = cf.getfloat(
        'mi_feedback', 'warmup_sec', fallback=1.8)
    # 以下参数只控制运动想象强度反馈，不参与 SSVEP 分类。
    mi_top_fraction = cf.getfloat(
        'mi_feedback', 'top_fraction', fallback=0.30)
    mi_sustain_threshold_ratio = cf.getfloat(
        'mi_feedback', 'sustain_threshold_ratio', fallback=0.25)
    mi_mapping_scale = cf.getfloat(
        'mi_feedback', 'mapping_scale', fallback=0.65)
    mi_receiver = MIResultReceiver(mi_udp_ip, mi_udp_port)
    if mi_feedback_enabled:
        mi_receiver.start()

    # 保留原程序中的反应时间与分析时间设置。
    reaction_time = max(0.0, trial_dura - t_stim)

    print('数据保存目录：', save_path)
    print('手套串口：', port if con_flag else '已关闭')
    print(trial_dura)
    print(t_stim)
    if con_flag:
        try:
            ser = serial.Serial(port, 9600, timeout=10)
        except serial.SerialException as serial_error:
            mi_receiver.close()
            queue.put('del')
            raise RuntimeError(
                '无法打开气动手套串口 {}：{}。请检查设备管理器中的 COM 口、'
                '串口是否被其他程序占用，以及 config.ini。'.format(port, serial_error))
    psychopyVersion = '3.2.4'
    expName = 'tello_control'  # from the Builder filename that created this script
    expInfo = {
        'participant': '',
        'session': '001',
    }
    dlg = gui.DlgFromDict(dictionary=expInfo, sortKeys=False, title=expName)
    if dlg.OK == False:
        queue.put("del")
        mi_receiver.close()
        print("线程已关闭")
        core.quit()  # user pressed cancel

    run_mode = '自由使用'
    print('运行模式：自由使用')
    expInfo['date'] = data.getDateStr()  # add a simple timestamp
    expInfo['expName'] = expName

    # Data file name stem = absolute path + name; later add .psyexp, .csv, .log, etc
    filename = _thisDir + os.sep + u'data/%s_%s_%s' % (expInfo['participant'], expName, expInfo['date'])

    # save a log file for detail verbose info
    # logFile = logging.LogFile(filename + '.log', level=logging.EXP)
    # logging.console.setLevel(logging.WARNING)  # this outputs to the screen, not a file

    endExpNow = False  # flag for 'escape' or other condition => quit the exp
    frameTolerance = 0.001  # how close to onset before 'same' frame

    # Start Code - component code to be run before the window creation

    # Setup the Window
    win = visual.Window(
        size=[2560, 1600], fullscr=True, screen=0,
        winType='pyglet', allowGUI=False, allowStencil=False,
        monitor='testMonitor', color=[-1.000, -1.000, -1.000], colorSpace='rgb',
        blendMode='avg', useFBO=True,
        units='height')
    # 测量显示器实际刷新率。频闪计算必须使用这个值，不能固定写死为 60。
    expInfo['frameRate'] = win.getActualFrameRate()
    if expInfo['frameRate'] is not None:
        refresh_hz = float(expInfo['frameRate'])
    else:
        refresh_hz = 60.0
        print('警告：无法自动测量刷新率，暂按 60 Hz 运行。')
    frameDur = 1.0 / refresh_hz

    actual_width = float(win.size[0])
    actual_height = float(win.size[1])
    # 将原来的 1920×1080 设计等比例放大并完整放入当前屏幕。
    # 2560×1600 时 scale=4/3，刺激区约为 2400×1440，避免拉伸变形。
    layout_scale = min(actual_width / 1920.0, actual_height / 1080.0)
    print('实际屏幕：{}×{}'.format(int(actual_width), int(actual_height)))
    print('实际刷新率：{:.3f} Hz'.format(refresh_hz))
    print('界面缩放比例：{:.4f}'.format(layout_scale))

    # create a default keyboard (e.g. to check for escape)
    defaultKeyboard = keyboard.Keyboard()

    # “正在识别”只在原有写盘同步完成后、真正读取与算法计算期间显示。
    recognizing_text = visual.TextStim(
        win=win, name='recognizing_text', text='正在识别……', font='Arial',
        units='pix', pos=(0, 0), height=68, wrapWidth=1400, ori=0,
        color='white', colorSpace='rgb', opacity=1,
        languageStyle='LTR', depth=-30.0)


    # 综合反馈页：左侧播放识别动作动画，MI 结果显示稳健模型支持指数。
    feedback_bg = visual.Rect(
        win=win, name='feedback_bg', units='pix', width=1740, height=930,
        pos=(0, 0), lineColor=[-0.4, -0.4, -0.4], fillColor=[-0.9, -0.9, -0.9],
        opacity=1.0, depth=-20.0)
    feedback_image = visual.ImageStim(
        win=win, name='feedback_image', image=os.path.join('动作图', '0.jpg'),
        units='pix', pos=(-430, 70), size=(440, 440), depth=-21.0)
    feedback_title = visual.TextStim(
        win=win, name='feedback_title', text='', font='Arial', units='pix',
        pos=(250, 250), height=58, wrapWidth=1000, color='white',
        colorSpace='rgb', opacity=1, languageStyle='LTR', depth=-22.0)
    feedback_detail = visual.TextStim(
        win=win, name='feedback_detail', text='', font='Arial', units='pix',
        pos=(250, 120), height=40, wrapWidth=1000, color='white',
        colorSpace='rgb', opacity=1, languageStyle='LTR', depth=-22.0)
    mi_degree_text = visual.TextStim(
        win=win, name='mi_degree_text', text='', font='Arial', units='pix',
        pos=(250, -80), height=42, wrapWidth=1000, color='white',
        colorSpace='rgb', opacity=1, languageStyle='LTR', depth=-22.0)
    mi_degree_bg = visual.Rect(
        win=win, name='mi_degree_bg', units='pix', width=520, height=42,
        pos=(250, -155), lineColor='white', fillColor=[-0.55, -0.55, -0.55],
        opacity=1.0, depth=-22.0)
    mi_degree_fill = visual.Rect(
        win=win, name='mi_degree_fill', units='pix', width=0, height=36,
        pos=(-10, -155), lineColor=None, fillColor='green', opacity=1.0,
        depth=-23.0)
    feedback_stats = visual.TextStim(
        win=win, name='feedback_stats', text='', font='Arial', units='pix',
        pos=(250, -310), height=34, wrapWidth=1100, color='white',
        colorSpace='rgb', opacity=1, languageStyle='LTR', depth=-22.0)

    def configure_feedback_page(feedback):
        """配置自由使用反馈，显示整轮稳健汇总后的 MI 模型支持指数。"""
        predicted = int(feedback['predicted'])
        feedback_image.setImage(os.path.join('动作图', '{}.jpg'.format(predicted - 1)))
        feedback_title.setText('识别出的手势：{}'.format(order_lst[predicted - 1]))
        feedback_title.setColor('white')
        feedback_detail.setText('')
        feedback_detail.setColor('white')
        feedback_stats.setText('')

        mi_percent = feedback.get('mi_percent')
        if mi_percent is None:
            mi_degree_text.setText('运动想象意图程度：暂无数据')
            mi_degree_fill.width = 0
            mi_degree_fill.pos = (-10, -155)
        else:
            mi_percent = min(max(float(mi_percent), 0.0), 100.0)
            mi_degree_text.setText(
                '运动想象意图程度：{:.0f}%'.format(mi_percent))
            fill_width = 520.0 * mi_percent / 100.0
            mi_degree_fill.width = fill_width
            mi_degree_fill.pos = (250.0 - 260.0 + fill_width / 2.0, -155)

        # 保留控件但不显示任何附加统计文字。
        feedback_stats.setText('')

        # 动画正常时不绘制静态 feedback_image；若动作帧缺失则由调用端加入它。
        return [
            feedback_bg, feedback_title, feedback_detail,
            mi_degree_text, mi_degree_bg, mi_degree_fill, feedback_stats,
        ]

    def append_session_log(row):
        """记录自由使用结果；日志失败不终止实验。"""
        log_path = os.path.join(save_path, 'ssvep_free_session_log.csv')
        try:
            os.makedirs(save_path, exist_ok=True)
            write_header = not os.path.exists(log_path) or os.path.getsize(log_path) == 0
            fieldnames = [
                'timestamp', 'mode', 'trial_index',
                'predicted', 'predicted_name',
                'used_t_stim', 'used_trial_dura',
                'mi_received', 'mi_label', 'mi_score', 'mi_percent',
                'mi_received_at'
            ]
            with open(log_path, 'a', newline='', encoding='utf-8-sig') as log_file:
                writer = csv.DictWriter(log_file, fieldnames=fieldnames)
                if write_header:
                    writer.writeheader()
                writer.writerow(row)
        except Exception as log_error:
            print('会话日志写入失败：{}'.format(log_error))

    # Initialize components for Routine "instr"
    instrClock = core.Clock()
    text = visual.TextStim(win=win, name='text',
                           text='脑机接口\n\n手部康复训练\n\n按"空格"继续\n\n可随时按"ESC"退出',
                           font='Arial',
                           units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                           color='white', colorSpace='rgb', opacity=1,
                           languageStyle='LTR',
                           depth=0.0)
    i0=1
    key_resp = keyboard.Keyboard()
    # instr Begin Experiment
    Freq = np.array([8.00, 9.00, 10.00, 11.00, 12.00, 13.00, 14.00, 15.00])
    Phas = np.array([0, 0.15, 0.3, 0.45, 0.60, 0.9, 0.8, 0])

    varpy = [600 * layout_scale, 90 * layout_scale]

    # 原始刺激布局按当前屏幕等比例缩放。
    screen_long = 1800.0 * layout_scale
    screen_width = 1080.0 * layout_scale
    x0 = screen_long * 0 / 4 - screen_long / 2
    x1 = screen_long * 1 / 4 - screen_long / 2
    x2 = screen_long * 2 / 4 - screen_long / 2
    x3 = screen_long * 3 / 4 - screen_long / 2
    x4 = screen_long * 4 / 4 - screen_long / 2
    # x5 = screen_long * 2 / 8 - screen_long / 2

    y0 = screen_width * 0 / 4 - screen_width / 2
    y1 = screen_width * 1 / 4 - screen_width / 2
    y2 = screen_width * 2 / 4 - screen_width / 2
    y3 = screen_width * 3 / 4 - screen_width / 2
    y4 = screen_width * 4 / 4 - screen_width / 2
    # y5 = screen_width * 6 / 8 - screen_width / 2

    mylocation = [
        [(x0 + x1) / 2, (y3 + y4) / 2 - 25 * layout_scale],  ##上升
        [x2, (y3 + y4) / 2-25],  ##前进
        [(x0 + x1) / 2, (y0 + y1) / 2],  ##起飞
        [(x0 + x1) / 2, y2],  ##左转
        # [x5, y5],                        ##悬停
        [(x3 + x4) / 2, y2],  ##右转
        [(x3 + x4) / 2, (y0 + y1) / 2],  ##降落
        [x2, (y0 + y1) / 2],  ##后退
        [(x3 + x4) / 2, (y3 + y4) / 2 - 25 * layout_scale]  ##下降
    ]

    size_w = 300.0 * layout_scale
    size_h = 300.0 * layout_scale
    order_lst = ['握拳', '比1', '比2', '比3', '比4', '比5', '比6', '特殊3']
    # Initialize components for Routine "cue"
    cueClock = core.Clock()
    command_0 = visual.TextStim(win=win, name='text',
                                text='脑机接口\n\n手部康复训练\n\n按"空格"继续\n\n可随时按"ESC"退出',
                                font='Arial',
                                units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                color='white', colorSpace='rgb', opacity=1,
                                languageStyle='LTR',
                                depth=0.0)
    polygon_0 = visual.ImageStim(win=win, name='polygon_trial_0', image='动作图/0.jpg',units='pix')
    order_0 = visual.TextStim(win=win, name='text',
                              text=order_lst[0],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=0.0)

    polygon_1 = visual.ImageStim(win=win, name='polygon_trial_1', image='动作图/1.jpg',units='pix')
    order_1 = visual.TextStim(win=win, name='text',
                              text=order_lst[1],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=-1.0)

    polygon_2 = visual.ImageStim(win=win, name='polygon_trial_2', image='动作图/2.jpg',units='pix')
    order_2 = visual.TextStim(win=win, name='text',
                              text=order_lst[2],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=-2.0)

    polygon_3 = visual.ImageStim(win=win, name='polygon_trial_3', image='动作图/3.jpg',units='pix')
    order_3 = visual.TextStim(win=win, name='text',
                              text=order_lst[3],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=-3.0)

    polygon_4 = visual.ImageStim(win=win, name='polygon_trial_4', image='动作图/4.jpg',units='pix')
    order_4 = visual.TextStim(win=win, name='text',
                              text=order_lst[4],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=-5.0)

    polygon_5 = visual.ImageStim(win=win, name='polygon_trial_5', image='动作图/5.jpg',units='pix')
    order_5 = visual.TextStim(win=win, name='text',
                              text=order_lst[5],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=-6.0)

    polygon_6 = visual.ImageStim(win=win, name='polygon_trial_6', image='动作图/6.jpg',units='pix')
    order_6 = visual.TextStim(win=win, name='text',
                              text=order_lst[6],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=-7.0)

    polygon_7 = visual.ImageStim(win=win, name='polygon_trial_7', image='动作图/7.jpg',units='pix')
    order_7 = visual.TextStim(win=win, name='text',
                              text=order_lst[7],
                              font='Arial',
                              units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                              color='white', colorSpace='rgb', opacity=1,
                              languageStyle='LTR',
                              depth=-8.0)
    # ------------
    loop_id = -1

    # Initialize components for Routine "trial"
    trialClock = core.Clock()
    screen_long = 1.77
    screen_width = 1
    x0 = screen_long * 0 / 4 - screen_long / 2
    x1 = screen_long * 1 / 4 - screen_long / 2
    x2 = screen_long * 2 / 4 - screen_long / 2
    x3 = screen_long * 3 / 4 - screen_long / 2
    x4 = screen_long * 4 / 4 - screen_long / 2
    # x5 = screen_long * 2 / 8 - screen_long / 2

    y0 = screen_width * 0 / 4 - screen_width / 2
    y1 = screen_width * 1 / 4 - screen_width / 2
    y2 = screen_width * 2 / 4 - screen_width / 2
    y3 = screen_width * 3 / 4 - screen_width / 2
    y4 = screen_width * 4 / 4 - screen_width / 2
    # y5 = screen_width * 6 / 8 - screen_width / 2

    mylocation1 = [
        [(x0 + x1) / 2, (y3 + y4) / 2],  ##上升
        [x2, (y3 + y4) / 2],  ##前进
        [(x0 + x1) / 2, (y0 + y1) / 2],  ##起飞
        [(x0 + x1) / 2, y2],  ##左转
        # [x5, y5],                        ##悬停
        [(x3 + x4) / 2, y2],  ##右转
        [(x3 + x4) / 2, (y0 + y1) / 2],  ##降落
        [x2, (y0 + y1) / 2],  ##后退
        [(x3 + x4) / 2, (y3 + y4) / 2]  ##下降
    ]

    size_w1 = 0.5
    size_h1 = 0.5
    polygon_trial_0 = visual.ImageStim(win=win, name='polygon_trial_0', image='动作图/0.jpg',units='pix',pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h),depth=1)
    polygon_trial_1 = visual.ImageStim(win=win, name='polygon_trial_1', image='动作图/1.jpg',units='pix',pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h),depth=1)
    polygon_trial_2 = visual.ImageStim(win=win, name='polygon_trial_2', image='动作图/2.jpg',units='pix',pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h),depth=1)
    polygon_trial_3 = visual.ImageStim(win=win, name='polygon_trial_3', image='动作图/3.jpg',units='pix',pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h),depth=1)
    polygon_trial_4 = visual.ImageStim(win=win, name='polygon_trial_4', image='动作图/4.jpg',units='pix',pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h),depth=1)
    polygon_trial_5 = visual.ImageStim(win=win, name='polygon_trial_5', image='动作图/5.jpg',units='pix',pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h),depth=1)
    polygon_trial_6 = visual.ImageStim(win=win, name='polygon_trial_6', image='动作图/6.jpg',units='pix',pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h),depth=1)
    polygon_trial_7 = visual.ImageStim(win=win, name='polygon_trial_7', image='动作图/7.jpg',units='pix',pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h),depth=1)
    polygon_trial_01 = visual.ImageStim(win=win, name='polygon_trial_01', image='动作图/11.jpg',units='pix',pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_02 = visual.ImageStim(win=win, name='polygon_trial_02', image='动作图/12.jpg',units='pix',pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_03 = visual.ImageStim(win=win, name='polygon_trial_03', image='动作图/13.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_04 = visual.ImageStim(win=win, name='polygon_trial_04', image='动作图/14.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_05 = visual.ImageStim(win=win, name='polygon_trial_05', image='动作图/15.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_06 = visual.ImageStim(win=win, name='polygon_trial_06', image='动作图/16.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_07 = visual.ImageStim(win=win, name='polygon_trial_07', image='动作图/17.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_08 = visual.ImageStim(win=win, name='polygon_trial_08', image='动作图/18.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_09 = visual.ImageStim(win=win, name='polygon_trial_09', image='动作图/19.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_010 = visual.ImageStim(win=win, name='polygon_trial_10', image='动作图/110.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_011 = visual.ImageStim(win=win, name='polygon_trial_11', image='动作图/111.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_012 = visual.ImageStim(win=win, name='polygon_trial_12', image='动作图/112.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_013 = visual.ImageStim(win=win, name='polygon_trial_13', image='动作图/113.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_014 = visual.ImageStim(win=win, name='polygon_trial_14', image='动作图/114.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_015 = visual.ImageStim(win=win, name='polygon_trial_15', image='动作图/115.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_016 = visual.ImageStim(win=win, name='polygon_trial_16', image='动作图/116.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_017 = visual.ImageStim(win=win, name='polygon_trial_01', image='动作图/117.jpg',units='pix',pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_018 = visual.ImageStim(win=win, name='polygon_trial_02', image='动作图/118.jpg',units='pix',pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_019 = visual.ImageStim(win=win, name='polygon_trial_03', image='动作图/119.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_020 = visual.ImageStim(win=win, name='polygon_trial_04', image='动作图/120.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_021 = visual.ImageStim(win=win, name='polygon_trial_05', image='动作图/121.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_022 = visual.ImageStim(win=win, name='polygon_trial_06', image='动作图/122.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_023 = visual.ImageStim(win=win, name='polygon_trial_07', image='动作图/123.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_024 = visual.ImageStim(win=win, name='polygon_trial_08', image='动作图/124.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_025 = visual.ImageStim(win=win, name='polygon_trial_09', image='动作图/125.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_026 = visual.ImageStim(win=win, name='polygon_trial_10', image='动作图/126.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_027 = visual.ImageStim(win=win, name='polygon_trial_11', image='动作图/127.jpg',units='pix', pos=(mylocation[0][0], mylocation[0][1]),size=(size_w, size_h))
    polygon_trial_11 = visual.ImageStim(win=win, name='polygon_trial_11', image='动作图/21.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_12 = visual.ImageStim(win=win, name='polygon_trial_12', image='动作图/22.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_13 = visual.ImageStim(win=win, name='polygon_trial_13', image='动作图/23.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_14 = visual.ImageStim(win=win, name='polygon_trial_14', image='动作图/24.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_15 = visual.ImageStim(win=win, name='polygon_trial_15', image='动作图/25.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_16 = visual.ImageStim(win=win, name='polygon_trial_16', image='动作图/26.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_17 = visual.ImageStim(win=win, name='polygon_trial_17', image='动作图/27.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_18 = visual.ImageStim(win=win, name='polygon_trial_18', image='动作图/28.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_19 = visual.ImageStim(win=win, name='polygon_trial_19', image='动作图/29.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_110 = visual.ImageStim(win=win, name='polygon_trial_110', image='动作图/210.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_111 = visual.ImageStim(win=win, name='polygon_trial_111', image='动作图/211.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_112 = visual.ImageStim(win=win, name='polygon_trial_112', image='动作图/212.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_113 = visual.ImageStim(win=win, name='polygon_trial_113', image='动作图/213.jpg', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_114 = visual.ImageStim(win=win, name='polygon_trial_11', image='动作图/214.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_115 = visual.ImageStim(win=win, name='polygon_trial_12', image='动作图/215.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_116 = visual.ImageStim(win=win, name='polygon_trial_13', image='动作图/216.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_117 = visual.ImageStim(win=win, name='polygon_trial_14', image='动作图/217.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_118 = visual.ImageStim(win=win, name='polygon_trial_15', image='动作图/218.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_119 = visual.ImageStim(win=win, name='polygon_trial_16', image='动作图/219.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_120 = visual.ImageStim(win=win, name='polygon_trial_17', image='动作图/220.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_121 = visual.ImageStim(win=win, name='polygon_trial_18', image='动作图/221.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_122 = visual.ImageStim(win=win, name='polygon_trial_19', image='动作图/222.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_123 = visual.ImageStim(win=win, name='polygon_trial_110', image='动作图/223.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_124 = visual.ImageStim(win=win, name='polygon_trial_111', image='动作图/224.jpg',units='pix', pos=(mylocation[1][0], mylocation[1][1]),size=(size_w, size_h))
    polygon_trial_21 = visual.ImageStim(win=win, name='polygon_trial_21', image='动作图/31.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_22 = visual.ImageStim(win=win, name='polygon_trial_22', image='动作图/32.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_23 = visual.ImageStim(win=win, name='polygon_trial_23', image='动作图/33.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_24 = visual.ImageStim(win=win, name='polygon_trial_24', image='动作图/34.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_25 = visual.ImageStim(win=win, name='polygon_trial_25', image='动作图/35.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_26 = visual.ImageStim(win=win, name='polygon_trial_26', image='动作图/36.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_27 = visual.ImageStim(win=win, name='polygon_trial_27', image='动作图/37.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_28 = visual.ImageStim(win=win, name='polygon_trial_28', image='动作图/38.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_29 = visual.ImageStim(win=win, name='polygon_trial_29', image='动作图/39.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_210 = visual.ImageStim(win=win, name='polygon_trial_210', image='动作图/310.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_211 = visual.ImageStim(win=win, name='polygon_trial_211', image='动作图/311.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_212 = visual.ImageStim(win=win, name='polygon_trial_212', image='动作图/312.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_213 = visual.ImageStim(win=win, name='polygon_trial_213', image='动作图/313.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_214 = visual.ImageStim(win=win, name='polygon_trial_214', image='动作图/314.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_215 = visual.ImageStim(win=win, name='polygon_trial_215', image='动作图/315.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_216 = visual.ImageStim(win=win, name='polygon_trial_216', image='动作图/316.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_217 = visual.ImageStim(win=win, name='polygon_trial_217', image='动作图/317.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_218 = visual.ImageStim(win=win, name='polygon_trial_218', image='动作图/318.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_219 = visual.ImageStim(win=win, name='polygon_trial_219', image='动作图/319.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_220 = visual.ImageStim(win=win, name='polygon_trial_220', image='动作图/320.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_221 = visual.ImageStim(win=win, name='polygon_trial_221', image='动作图/321.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_222 = visual.ImageStim(win=win, name='polygon_trial_222', image='动作图/322.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_223 = visual.ImageStim(win=win, name='polygon_trial_223', image='动作图/323.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_224 = visual.ImageStim(win=win, name='polygon_trial_224', image='动作图/324.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_225 = visual.ImageStim(win=win, name='polygon_trial_225', image='动作图/325.jpg',units='pix', pos=(mylocation[2][0], mylocation[2][1]),size=(size_w, size_h))
    polygon_trial_31 = visual.ImageStim(win=win, name='polygon_trial_31', image='动作图/41.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_32 = visual.ImageStim(win=win, name='polygon_trial_32', image='动作图/42.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_33 = visual.ImageStim(win=win, name='polygon_trial_33', image='动作图/43.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_34 = visual.ImageStim(win=win, name='polygon_trial_34', image='动作图/44.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_35 = visual.ImageStim(win=win, name='polygon_trial_35', image='动作图/45.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_36 = visual.ImageStim(win=win, name='polygon_trial_36', image='动作图/46.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_37 = visual.ImageStim(win=win, name='polygon_trial_37', image='动作图/47.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_38 = visual.ImageStim(win=win, name='polygon_trial_38', image='动作图/48.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_39 = visual.ImageStim(win=win, name='polygon_trial_39', image='动作图/49.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_310 = visual.ImageStim(win=win, name='polygon_trial_310', image='动作图/410.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_311 = visual.ImageStim(win=win, name='polygon_trial_311', image='动作图/411.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_312 = visual.ImageStim(win=win, name='polygon_trial_312', image='动作图/412.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_313 = visual.ImageStim(win=win, name='polygon_trial_313', image='动作图/413.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_314 = visual.ImageStim(win=win, name='polygon_trial_314', image='动作图/414.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_315 = visual.ImageStim(win=win, name='polygon_trial_315', image='动作图/415.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_316 = visual.ImageStim(win=win, name='polygon_trial_316', image='动作图/416.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_317 = visual.ImageStim(win=win, name='polygon_trial_317', image='动作图/417.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_318 = visual.ImageStim(win=win, name='polygon_trial_318', image='动作图/418.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_319 = visual.ImageStim(win=win, name='polygon_trial_319', image='动作图/419.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_320 = visual.ImageStim(win=win, name='polygon_trial_320', image='动作图/420.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_321 = visual.ImageStim(win=win, name='polygon_trial_321', image='动作图/421.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_322 = visual.ImageStim(win=win, name='polygon_trial_322', image='动作图/422.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_323 = visual.ImageStim(win=win, name='polygon_trial_323', image='动作图/423.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_324 = visual.ImageStim(win=win, name='polygon_trial_324', image='动作图/424.jpg',units='pix', pos=(mylocation[3][0], mylocation[3][1]),size=(size_w, size_h))
    polygon_trial_41 = visual.ImageStim(win=win, name='polygon_trial_41', image='动作图/51.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_42 = visual.ImageStim(win=win, name='polygon_trial_42', image='动作图/52.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_43 = visual.ImageStim(win=win, name='polygon_trial_43', image='动作图/53.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_44 = visual.ImageStim(win=win, name='polygon_trial_44', image='动作图/54.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_45 = visual.ImageStim(win=win, name='polygon_trial_45', image='动作图/55.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_46 = visual.ImageStim(win=win, name='polygon_trial_46', image='动作图/56.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_47 = visual.ImageStim(win=win, name='polygon_trial_47', image='动作图/57.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_48 = visual.ImageStim(win=win, name='polygon_trial_48', image='动作图/58.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_49 = visual.ImageStim(win=win, name='polygon_trial_49', image='动作图/59.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_410 = visual.ImageStim(win=win, name='polygon_trial_410', image='动作图/510.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_411 = visual.ImageStim(win=win, name='polygon_trial_411', image='动作图/511.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_412 = visual.ImageStim(win=win, name='polygon_trial_412', image='动作图/512.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_413 = visual.ImageStim(win=win, name='polygon_trial_413', image='动作图/513.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_414 = visual.ImageStim(win=win, name='polygon_trial_414', image='动作图/514.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_415 = visual.ImageStim(win=win, name='polygon_trial_415', image='动作图/515.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_416 = visual.ImageStim(win=win, name='polygon_trial_416', image='动作图/516.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_417 = visual.ImageStim(win=win, name='polygon_trial_417', image='动作图/517.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_418 = visual.ImageStim(win=win, name='polygon_trial_418', image='动作图/518.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_419 = visual.ImageStim(win=win, name='polygon_trial_419', image='动作图/519.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_420 = visual.ImageStim(win=win, name='polygon_trial_420', image='动作图/520.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_421 = visual.ImageStim(win=win, name='polygon_trial_421', image='动作图/521.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_422 = visual.ImageStim(win=win, name='polygon_trial_422', image='动作图/522.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_423 = visual.ImageStim(win=win, name='polygon_trial_423', image='动作图/523.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_424 = visual.ImageStim(win=win, name='polygon_trial_424', image='动作图/524.jpg',units='pix', pos=(mylocation[4][0], mylocation[4][1]),size=(size_w, size_h))
    polygon_trial_51 = visual.ImageStim(win=win, name='polygon_trial_51', image='动作图/61.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_52 = visual.ImageStim(win=win, name='polygon_trial_52', image='动作图/62.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_53 = visual.ImageStim(win=win, name='polygon_trial_53', image='动作图/63.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_54 = visual.ImageStim(win=win, name='polygon_trial_54', image='动作图/64.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_55 = visual.ImageStim(win=win, name='polygon_trial_55', image='动作图/65.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_56 = visual.ImageStim(win=win, name='polygon_trial_56', image='动作图/66.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_57 = visual.ImageStim(win=win, name='polygon_trial_57', image='动作图/67.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_58 = visual.ImageStim(win=win, name='polygon_trial_58', image='动作图/68.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_59 = visual.ImageStim(win=win, name='polygon_trial_59', image='动作图/69.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_510 = visual.ImageStim(win=win, name='polygon_trial_510', image='动作图/610.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_511 = visual.ImageStim(win=win, name='polygon_trial_511', image='动作图/611.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_512 = visual.ImageStim(win=win, name='polygon_trial_512', image='动作图/612.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_513 = visual.ImageStim(win=win, name='polygon_trial_513', image='动作图/613.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_514 = visual.ImageStim(win=win, name='polygon_trial_514', image='动作图/614.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_515 = visual.ImageStim(win=win, name='polygon_trial_515', image='动作图/615.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_516 = visual.ImageStim(win=win, name='polygon_trial_516', image='动作图/616.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_517 = visual.ImageStim(win=win, name='polygon_trial_517', image='动作图/617.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_518 = visual.ImageStim(win=win, name='polygon_trial_518', image='动作图/618.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_519 = visual.ImageStim(win=win, name='polygon_trial_519', image='动作图/619.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_520 = visual.ImageStim(win=win, name='polygon_trial_520', image='动作图/620.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_521 = visual.ImageStim(win=win, name='polygon_trial_521', image='动作图/621.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_522 = visual.ImageStim(win=win, name='polygon_trial_522', image='动作图/622.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_523 = visual.ImageStim(win=win, name='polygon_trial_523', image='动作图/623.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_524 = visual.ImageStim(win=win, name='polygon_trial_524', image='动作图/624.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_525 = visual.ImageStim(win=win, name='polygon_trial_525', image='动作图/625.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_526 = visual.ImageStim(win=win, name='polygon_trial_526', image='动作图/626.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_527 = visual.ImageStim(win=win, name='polygon_trial_527', image='动作图/627.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_528 = visual.ImageStim(win=win, name='polygon_trial_528', image='动作图/628.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_529 = visual.ImageStim(win=win, name='polygon_trial_529', image='动作图/629.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_530 = visual.ImageStim(win=win, name='polygon_trial_530', image='动作图/630.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_531 = visual.ImageStim(win=win, name='polygon_trial_531', image='动作图/631.jpg',units='pix', pos=(mylocation[6][0], mylocation[6][1]),size=(size_w, size_h))
    polygon_trial_61 = visual.ImageStim(win=win, name='polygon_trial_61', image='动作图/71.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_62 = visual.ImageStim(win=win, name='polygon_trial_62', image='动作图/72.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_63 = visual.ImageStim(win=win, name='polygon_trial_63', image='动作图/73.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_64 = visual.ImageStim(win=win, name='polygon_trial_64', image='动作图/74.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_65 = visual.ImageStim(win=win, name='polygon_trial_65', image='动作图/75.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_66 = visual.ImageStim(win=win, name='polygon_trial_66', image='动作图/76.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_67 = visual.ImageStim(win=win, name='polygon_trial_67', image='动作图/77.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_68 = visual.ImageStim(win=win, name='polygon_trial_68', image='动作图/78.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_69 = visual.ImageStim(win=win, name='polygon_trial_69', image='动作图/79.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_610 = visual.ImageStim(win=win, name='polygon_trial_610', image='动作图/710.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_611 = visual.ImageStim(win=win, name='polygon_trial_611', image='动作图/711.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_612 = visual.ImageStim(win=win, name='polygon_trial_612', image='动作图/712.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_613 = visual.ImageStim(win=win, name='polygon_trial_613', image='动作图/713.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_614 = visual.ImageStim(win=win, name='polygon_trial_614', image='动作图/714.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_615 = visual.ImageStim(win=win, name='polygon_trial_615', image='动作图/713.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_616 = visual.ImageStim(win=win, name='polygon_trial_616', image='动作图/714.jpg',units='pix', pos=(mylocation[5][0], mylocation[5][1]),size=(size_w, size_h))
    polygon_trial_71 = visual.ImageStim(win=win, name='polygon_trial_71', image='动作图/81.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_72 = visual.ImageStim(win=win, name='polygon_trial_72', image='动作图/82.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_73 = visual.ImageStim(win=win, name='polygon_trial_73', image='动作图/83.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_74 = visual.ImageStim(win=win, name='polygon_trial_74', image='动作图/84.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_75 = visual.ImageStim(win=win, name='polygon_trial_75', image='动作图/85.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_76 = visual.ImageStim(win=win, name='polygon_trial_76', image='动作图/86.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_77 = visual.ImageStim(win=win, name='polygon_trial_77', image='动作图/87.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_78 = visual.ImageStim(win=win, name='polygon_trial_78', image='动作图/88.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_79 = visual.ImageStim(win=win, name='polygon_trial_79', image='动作图/89.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_710 = visual.ImageStim(win=win, name='polygon_trial_710', image='动作图/810.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_711 = visual.ImageStim(win=win, name='polygon_trial_711', image='动作图/811.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_712 = visual.ImageStim(win=win, name='polygon_trial_712', image='动作图/812.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_713 = visual.ImageStim(win=win, name='polygon_trial_713', image='动作图/813.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_714 = visual.ImageStim(win=win, name='polygon_trial_714', image='动作图/814.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_715 = visual.ImageStim(win=win, name='polygon_trial_715', image='动作图/815.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_716 = visual.ImageStim(win=win, name='polygon_trial_716', image='动作图/816.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_717 = visual.ImageStim(win=win, name='polygon_trial_717', image='动作图/817.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_718 = visual.ImageStim(win=win, name='polygon_trial_718', image='动作图/818.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_719 = visual.ImageStim(win=win, name='polygon_trial_719', image='动作图/819.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_720 = visual.ImageStim(win=win, name='polygon_trial_720', image='动作图/820.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_721 = visual.ImageStim(win=win, name='polygon_trial_721', image='动作图/821.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_722 = visual.ImageStim(win=win, name='polygon_trial_722', image='动作图/822.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_723 = visual.ImageStim(win=win, name='polygon_trial_723', image='动作图/823.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_724 = visual.ImageStim(win=win, name='polygon_trial_724', image='动作图/824.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    polygon_trial_725 = visual.ImageStim(win=win, name='polygon_trial_725', image='动作图/825.jpg',units='pix', pos=(mylocation[7][0], mylocation[7][1]),size=(size_w, size_h))
    
    trial_0 = [
    polygon_trial_01, polygon_trial_02, polygon_trial_03, polygon_trial_04,
    polygon_trial_05, polygon_trial_06, polygon_trial_07, polygon_trial_08,
    polygon_trial_09, polygon_trial_010, polygon_trial_011, polygon_trial_012,
    polygon_trial_013, polygon_trial_014, polygon_trial_015, polygon_trial_016,
    polygon_trial_017, polygon_trial_018, polygon_trial_019, polygon_trial_020,
    polygon_trial_021, polygon_trial_022, polygon_trial_023, polygon_trial_024,
    polygon_trial_025, polygon_trial_026, polygon_trial_027]
    trial_1 = [
    polygon_trial_11, polygon_trial_12, polygon_trial_13, polygon_trial_14,
    polygon_trial_15, polygon_trial_16, polygon_trial_17, polygon_trial_18,
    polygon_trial_19, polygon_trial_110, polygon_trial_111, polygon_trial_112,
    polygon_trial_113,polygon_trial_114, polygon_trial_115, polygon_trial_116, polygon_trial_117,
    polygon_trial_118, polygon_trial_119, polygon_trial_120, polygon_trial_121,
    polygon_trial_122, polygon_trial_123, polygon_trial_124]
    trial_2 = [
    polygon_trial_21, polygon_trial_22, polygon_trial_23, polygon_trial_24,
    polygon_trial_25, polygon_trial_26, polygon_trial_27, polygon_trial_28,
    polygon_trial_29, polygon_trial_210, polygon_trial_211, polygon_trial_212,
    polygon_trial_213, polygon_trial_214, polygon_trial_215, polygon_trial_216,
    polygon_trial_217, polygon_trial_218, polygon_trial_219, polygon_trial_220,
    polygon_trial_221, polygon_trial_222, polygon_trial_223, polygon_trial_224,
    polygon_trial_225]
    trial_3 = [
    polygon_trial_31, polygon_trial_32, polygon_trial_33, polygon_trial_34,
    polygon_trial_35, polygon_trial_36, polygon_trial_37, polygon_trial_38,
    polygon_trial_39, polygon_trial_310, polygon_trial_311, polygon_trial_312,
    polygon_trial_313, polygon_trial_314, polygon_trial_315, polygon_trial_316,
    polygon_trial_317, polygon_trial_318, polygon_trial_319, polygon_trial_320,
    polygon_trial_321, polygon_trial_322, polygon_trial_323, polygon_trial_324,]
    trial_4 = [
    polygon_trial_41, polygon_trial_42, polygon_trial_43, polygon_trial_44,
    polygon_trial_45, polygon_trial_46, polygon_trial_47, polygon_trial_48,
    polygon_trial_49, polygon_trial_410, polygon_trial_411, polygon_trial_412,
    polygon_trial_413, polygon_trial_414, polygon_trial_415, polygon_trial_416,
    polygon_trial_417, polygon_trial_418, polygon_trial_419, polygon_trial_420,
    polygon_trial_421, polygon_trial_422, polygon_trial_423, polygon_trial_424]
    # 第6目标（比5/全掌伸展）：使用原第7组帧反向播放，从握拳过渡到张手。
    trial_5 = [
    polygon_trial_616, polygon_trial_615, polygon_trial_614, polygon_trial_613,
    polygon_trial_612, polygon_trial_611, polygon_trial_610, polygon_trial_69,
    polygon_trial_68, polygon_trial_67, polygon_trial_66, polygon_trial_65,
    polygon_trial_64, polygon_trial_63, polygon_trial_62, polygon_trial_61]
    # 第7目标（比6）：使用 external_program2 / 动作图(2) 的 31 帧新动画。
    trial_6 = [
    polygon_trial_51, polygon_trial_52, polygon_trial_53, polygon_trial_54,
    polygon_trial_55, polygon_trial_56, polygon_trial_57, polygon_trial_58,
    polygon_trial_59, polygon_trial_510, polygon_trial_511, polygon_trial_512,
    polygon_trial_513, polygon_trial_514, polygon_trial_515, polygon_trial_516,
    polygon_trial_517, polygon_trial_518, polygon_trial_519, polygon_trial_520,
    polygon_trial_521, polygon_trial_522, polygon_trial_523, polygon_trial_524,
    polygon_trial_525, polygon_trial_526, polygon_trial_527, polygon_trial_528,
    polygon_trial_529, polygon_trial_530, polygon_trial_531]
    trial_7 = [
    polygon_trial_71, polygon_trial_72, polygon_trial_73, polygon_trial_74,
    polygon_trial_75, polygon_trial_76, polygon_trial_77, polygon_trial_78,
    polygon_trial_79, polygon_trial_710, polygon_trial_711, polygon_trial_712,
    polygon_trial_713, polygon_trial_714, polygon_trial_715, polygon_trial_716,
    polygon_trial_717, polygon_trial_718, polygon_trial_719, polygon_trial_720,
    polygon_trial_721, polygon_trial_722, polygon_trial_723, polygon_trial_724,
    polygon_trial_725]
    # 八类动作的逐帧动画。反馈页直接循环播放对应组。
    feedback_animation_groups = [
        trial_0, trial_1, trial_2, trial_3,
        trial_4, trial_5, trial_6, trial_7,
    ]
    polygon_trial_0 = trial_0[0]
    polygon_trial_1 = trial_1[0]
    polygon_trial_2 = trial_2[0]
    polygon_trial_3 = trial_3[0]
    polygon_trial_4 = trial_4[0]
    polygon_trial_5 = trial_5[0]
    polygon_trial_6 = trial_6[0]
    polygon_trial_7 = trial_7[0]
    order_trial_0 = visual.TextStim(win=win, name='text',
                                    text=order_lst[0],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=0.0)
    order_trial_1 = visual.TextStim(win=win, name='text',
                                    text=order_lst[1],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=-1.0)
    order_trial_2 = visual.TextStim(win=win, name='text',
                                    text=order_lst[2],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=-2.0)
    order_trial_3 = visual.TextStim(win=win, name='text',
                                    text=order_lst[3],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=-3.0)

    # polygon_trial_4 = visual.Rect(
    #     win=win, name='polygon_trial_4', units='pix',
    #     width=[1.0, 1.0][0], height=[1.0, 1.0][1],
    #     ori=0, pos=[0, 0],
    #     lineWidth=1, lineColor=[1, 1, 1], lineColorSpace='rgb',
    #     fillColor=1.0, fillColorSpace='rgb',
    #     opacity=1, depth=-4.0, interpolate=True)
    # order_trial_4 = visual.TextStim(win=win, name='text',
    #                                 text='悬停',
    #                                 font='Arial',
    #                                 units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
    #                                 color='white', colorSpace='rgb', opacity=1,
    #                                 languageStyle='LTR',
    #                                 depth=-4.0)
    order_trial_4 = visual.TextStim(win=win, name='text',
                                    text=order_lst[4],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=-5.0)
    order_trial_5 = visual.TextStim(win=win, name='text',
                                    text=order_lst[5],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=-6.0)
    order_trial_6 = visual.TextStim(win=win, name='text',
                                    text=order_lst[6],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=-7.0)
    order_trial_7 = visual.TextStim(win=win, name='text',
                                    text=order_lst[7],
                                    font='Arial',
                                    units='pix', pos=(0, 0), height=50, wrapWidth=None, ori=0,
                                    color='white', colorSpace='rgb', opacity=1,
                                    languageStyle='LTR',
                                    depth=-8.0)

    # Create some handy timers
    globalClock = core.Clock()  # to track the time since experiment started
    routineTimer = core.CountdownTimer()  # to track time remaining of each (non-slip) routine

    # ------Prepare to start Routine "instr"-------
    # update component parameters for each repeat
    key_resp.keys = []
    key_resp.rt = []
    # keep track of which components have finished
    instrComponents = [text, key_resp]
    for thisComponent in instrComponents:
        thisComponent.tStart = None
        thisComponent.tStop = None
        thisComponent.tStartRefresh = None
        thisComponent.tStopRefresh = None
        if hasattr(thisComponent, 'status'):
            thisComponent.status = NOT_STARTED
    # reset timers
    t = 0
    _timeToFirstFrame = win.getFutureFlipTime(clock="now")
    instrClock.reset(-_timeToFirstFrame)  # t0 is time of first possible flip
    frameN = -1
    continueRoutine = True

    # tello = Tello()

    # decorator(tello.connect)()
    # decorator(tello.set_speed)(10)  # 设置 tello 速度

    # -------Run Routininstre ""-------
    # queue1.put("start-2")
    while continueRoutine:

        # get current time
        t = instrClock.getTime()
        tThisFlip = win.getFutureFlipTime(clock=instrClock)
        tThisFlipGlobal = win.getFutureFlipTime(clock=None)
        frameN = frameN + 1  # number of completed frames (so 0 is the first frame)
        # update/draw components on each frame

        # *text* updates
        if text.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
            # keep track of start time/frame for later
            text.frameNStart = frameN  # exact frame index
            text.tStart = t  # local t and not account for scr refresh
            text.tStartRefresh = tThisFlipGlobal  # on global time
            win.timeOnFlip(text, 'tStartRefresh')  # time at next scr refresh
            text.setAutoDraw(True)

        # *key_resp* updates
        waitOnFlip = False
        if key_resp.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
            # keep track of start time/frame for later
            key_resp.famreNStart = frameN  # exact frame index
            key_resp.tStart = t  # local t and not account for scr refresh
            key_resp.tStartRefresh = tThisFlipGlobal  # on global time
            win.timeOnFlip(key_resp, 'tStartRefresh')  # time at next scr refresh
            key_resp.status = STARTED
            # keyboard checking is just starting
            win.callOnFlip(key_resp.clearEvents, eventType='keyboard')  # clear events on next screen flip
        if key_resp.status == STARTED and not waitOnFlip:
            theseKeys = key_resp.getKeys(keyList=['space'], waitRelease=False)
            if len(theseKeys):
                theseKeys = theseKeys[0]  # at least one key was pressed

                # check for quit:
                if "escape" == theseKeys:
                    endExpNow = True
                # a response ends the routine
                continueRoutine = False

        # check for quit (typically the Esc key)
        # if endExpNow or defaultKeyboard.getKeys(keyList=["escape"]):
        #     core.quit()
        #     decorator(tello.land)()
 
            # check if all components have finished
        if not continueRoutine:  # a component has requested a forced-end of Routine
            break
        continueRoutine = False  # will revert to True if at least one component still running
        for thisComponent in instrComponents:
            if hasattr(thisComponent, "status") and thisComponent.status != FINISHED:
                continueRoutine = True
                break  # at least one component has not yet finished

        # refresh the screen
        if continueRoutine:  # don't flip if this routine is over or we'll get a blank screen
            win.flip()

    # -------Ending Routine "instr"-------
    for thisComponent in instrComponents:
        if hasattr(thisComponent, "setAutoDraw"):
            thisComponent.setAutoDraw(False)
    # the Routine "instr" was not non-slip safe, so reset the non-slip timer
    routineTimer.reset()

    # set up handler to look after randomisation of conditions etc
    trials = data.TrialHandler(nReps=1000000, method='sequential',
                               extraInfo=expInfo, originPath=-1,
                               trialList=[None],
                               seed=None, name='trials')

    thisTrial = trials.trialList[0]  # so we can initialise stimuli with some values
    # abbreviate parameter names if possible (e.g. rgb = thisTrial.rgb)
    if thisTrial != None:
        for paramName in thisTrial:
            exec('{} = thisTrial[paramName]'.format(paramName))
    result = 0
    trial_index = 0
    for thisTrial in trials:
        trial_index += 1

        trial_t_stim = float(t_stim)
        trial_trial_dura = float(trial_dura)
        cue_message = '自由使用模式：请自由选择想做的手势'
        restim = visual.TextStim(win, cue_message, font='Arial',
                                 units='pix', pos=(0, 0), height=50, wrapWidth=1500, ori=0,
                                 color='white', colorSpace='rgb', opacity=1,
                                 languageStyle='LTR', depth=-17.0)
        # if ifbegin:
        #     time.sleep(5)
        #     ser.write(b'4')
        # ifbegin=True
        # currentLoop = trials
        # abbreviate parameter names if possible (e.g. rgb = thisTrial.rgb)
        if thisTrial != None:
            for paramName in thisTrial:
                exec('{} = thisTrial[paramName]'.format(paramName))

        # ------Prepare to start Routine "cue"-------
        routineTimer.add(1.000000)
        # update component parameters for each repeat
        polygon_0.setPos((mylocation[0][0], mylocation[0][1]))
        order_0.setPos((mylocation[0][0], mylocation[0][1]))
        polygon_0.setSize((size_w, size_h))

        polygon_1.setPos((mylocation[1][0], mylocation[1][1]))
        order_1.setPos((mylocation[1][0], mylocation[1][1]))
        polygon_1.setSize((size_w, size_h))

        polygon_2.setPos((mylocation[2][0], mylocation[2][1]))
        order_2.setPos((mylocation[2][0], mylocation[2][1]))
        polygon_2.setSize((size_w, size_h))

        polygon_3.setPos((mylocation[3][0], mylocation[3][1]))
        order_3.setPos((mylocation[3][0], mylocation[3][1]))
        polygon_3.setSize((size_w, size_h))

        polygon_4.setPos((mylocation[4][0], mylocation[4][1]))
        order_4.setPos((mylocation[4][0], mylocation[4][1]))
        polygon_4.setSize((size_w, size_h))

        polygon_5.setPos((mylocation[5][0], mylocation[5][1]))
        order_5.setPos((mylocation[5][0], mylocation[5][1]))
        polygon_5.setSize((size_w, size_h))

        polygon_6.setPos((mylocation[6][0], mylocation[6][1]))
        order_6.setPos((mylocation[6][0], mylocation[6][1]))
        polygon_6.setSize((size_w, size_h))

        polygon_7.setPos((mylocation[7][0], mylocation[7][1]))
        order_7.setPos((mylocation[7][0], mylocation[7][1]))
        polygon_7.setSize((size_w, size_h))

        # polygon_8.setPos((mylocation[8][0], mylocation[8][1]))
        # order_8.setPos((mylocation[8][0], mylocation[8][1]))
        # polygon_8.setSize((size_w, size_h))


        # keep track of which components have finished
        # cueComponents = [polygon_0, polygon_1, polygon_2, polygon_3, polygon_4, polygon_5, polygon_6, polygon_7, polygon_8]
        cueComponents = [polygon_0, polygon_1, polygon_2, polygon_3, polygon_4, polygon_5, polygon_6, polygon_7]

        for thisComponent in cueComponents:
            thisComponent.tStart = None
            thisComponent.tStop = None
            thisComponent.tStartRefresh = None
            thisComponent.tStopRefresh = None
            if hasattr(thisComponent, 'status'):
                thisComponent.status = NOT_STARTED
        # reset timers
        t = 0
        _timeToFirstFrame = win.getFutureFlipTime(clock="now")
        cueClock.reset(-_timeToFirstFrame)  # t0 is time of first possible flip
        frameN = -1
        continueRoutine = True
        i0=0
        # -------Run Routine "cue"-------
        while continueRoutine:
            # ------------ NEW ADD

            # get current time
            t = cueClock.getTime()
            tThisFlip = win.getFutureFlipTime(clock=cueClock)
            tThisFlipGlobal = win.getFutureFlipTime(clock=None)
            frameN = frameN + 1  # number of completed frames (so 0 is the first frame)
            # update/draw components on each frame

            # *polygon_0* updates
            if polygon_0.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # keep track of start time/frame for later
                polygon_0.frameNStart = frameN  # exact frame index
                polygon_0.tStart = t  # local t and not account for scr refresh
                polygon_0.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_0, 'tStartRefresh')  # time at next scr refresh
                polygon_0.setAutoDraw(True)
                restim.setAutoDraw(True)
                print(polygon_0.status)
                # print('frameN1:%d'%frameN)
            if polygon_0.status == STARTED:
                # print('frameN2:%d' % frameN)
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_0.tStartRefresh + 1.0 - frameTolerance:
                    # keep track of stop time/frame for later
                    polygon_0.tStop = t  # not accounting for scr refresh
                    polygon_0.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_0, 'tStopRefresh')  # time at next scr refresh
                    polygon_0.setAutoDraw(False)
                    # restim.setAutoDraw(False)
                    print('frameN3:%d' % frameN)

            # *polygon_1* updates
            if polygon_1.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # print('frameN4:%d' % frameN)
                # keep track of start time/frame for later
                polygon_1.frameNStart = frameN  # exact frame index
                polygon_1.tStart = t  # local t and not account for scr refresh
                polygon_1.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_1, 'tStartRefresh')  # time at next scr refresh
                polygon_1.setAutoDraw(True)
            if polygon_1.status == STARTED:
                # print('frameN5:%d' % frameN)
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_1.tStartRefresh + 1.0 - frameTolerance:
                    # print('frameN6:%d' % frameN)
                    # keep track of stop time/frame for later
                    polygon_1.tStop = t  # not accounting for scr refresh
                    polygon_1.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_1, 'tStopRefresh')  # time at next scr refresh
                    polygon_1.setAutoDraw(False)

            # *polygon_2* updates
            if polygon_2.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # print('frameN7:%d' % frameN)
                # keep track of start time/frame for later
                polygon_2.frameNStart = frameN  # exact frame index
                polygon_2.tStart = t  # local t and not account for scr refresh
                polygon_2.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_2, 'tStartRefresh')  # time at next scr refresh
                polygon_2.setAutoDraw(True)
            if polygon_2.status == STARTED:
                # print('frameN8:%d' % frameN)
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_2.tStartRefresh + 1.0 - frameTolerance:
                    # print('frameN9:%d' % frameN)
                    # keep track of stop time/frame for later
                    polygon_2.tStop = t  # not accounting for scr refresh
                    polygon_2.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_2, 'tStopRefresh')  # time at next scr refresh
                    polygon_2.setAutoDraw(False)

            # *polygon_3* updates
            if polygon_3.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # keep track of start time/frame for later
                polygon_3.frameNStart = frameN  # exact frame index
                polygon_3.tStart = t  # local t and not account for scr refresh
                polygon_3.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_3, 'tStartRefresh')  # time at next scr refresh
                polygon_3.setAutoDraw(True)
            if polygon_3.status == STARTED:
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_3.tStartRefresh + 1.0 - frameTolerance:
                    # keep track of stop time/frame for later
                    polygon_3.tStop = t  # not accounting for scr refresh
                    polygon_3.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_3, 'tStopRefresh')  # time at next scr refresh
                    polygon_3.setAutoDraw(False)

            # *polygon_4* updates
            if polygon_4.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # keep track of start time/frame for later
                polygon_4.frameNStart = frameN  # exact frame index
                polygon_4.tStart = t  # local t and not account for scr refresh
                polygon_4.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_4, 'tStartRefresh')  # time at next scr refresh
                polygon_4.setAutoDraw(True)
            if polygon_4.status == STARTED:
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_4.tStartRefresh + 1.0 - frameTolerance:
                    # keep track of stop time/frame for later
                    polygon_4.tStop = t  # not accounting for scr refresh
                    polygon_4.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_4, 'tStopRefresh')  # time at next scr refresh
                    polygon_4.setAutoDraw(False)

            # *polygon_5* updates
            if polygon_5.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # keep track of start time/frame for later
                polygon_5.frameNStart = frameN  # exact frame index
                polygon_5.tStart = t  # local t and not account for scr refresh
                polygon_5.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_5, 'tStartRefresh')  # time at next scr refresh
                polygon_5.setAutoDraw(True)
            if polygon_5.status == STARTED:
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_5.tStartRefresh + 1.0 - frameTolerance:
                    # keep track of stop time/frame for later
                    polygon_5.tStop = t  # not accounting for scr refresh
                    polygon_5.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_5, 'tStopRefresh')  # time at next scr refresh
                    polygon_5.setAutoDraw(False)

            # *polygon_6* updates
            if polygon_6.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # keep track of start time/frame for later
                polygon_6.frameNStart = frameN  # exact frame index
                polygon_6.tStart = t  # local t and not account for scr refresh
                polygon_6.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_6, 'tStartRefresh')  # time at next scr refresh
                polygon_6.setAutoDraw(True)
            if polygon_6.status == STARTED:
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_6.tStartRefresh + 1.0 - frameTolerance:
                    # keep track of stop time/frame for later
                    polygon_6.tStop = t  # not accounting for scr refresh
                    polygon_6.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_6, 'tStopRefresh')  # time at next scr refresh
                    polygon_6.setAutoDraw(False)

            # *polygon_7* updates
            if polygon_7.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                # keep track of start time/frame for later
                polygon_7.frameNStart = frameN  # exact frame index
                polygon_7.tStart = t  # local t and not account for scr refresh
                polygon_7.tStartRefresh = tThisFlipGlobal  # on global time
                win.timeOnFlip(polygon_7, 'tStartRefresh')  # time at next scr refresh
                polygon_7.setAutoDraw(True)
            if polygon_7.status == STARTED:
                # is it time to stop? (based on global clock, using actual start)
                if tThisFlipGlobal > polygon_7.tStartRefresh + 1.0 - frameTolerance:
                    # keep track of stop time/frame for later
                    polygon_7.tStop = t  # not accounting for scr refresh
                    polygon_7.frameNStop = frameN  # exact frame index
                    win.timeOnFlip(polygon_7, 'tStopRefresh')  # time at next scr refresh
                    polygon_7.setAutoDraw(False)

            # check for quit (typically the Esc key)
            if endExpNow or defaultKeyboard.getKeys(keyList=["escape"]):
                print(1111111111111)
                queue.put("end")
                time.sleep(0.5)
                # time.sleep(30)
                queue.put("del")
                time.sleep(0.5)
                # process.terminate()
                # process.wait()
                print("线程已关闭")
                core.quit()
                # sys.exit()

            # check if all components have finished
            if not continueRoutine:  # a component has requested a forced-end of Routine
                break
            continueRoutine = False  # will revert to True if at least one component still running
            for thisComponent in cueComponents:
                if hasattr(thisComponent, "status") and thisComponent.status != FINISHED:
                    continueRoutine = True
                    break  # at least one component has not yet finished

            # refresh the screen
            if continueRoutine:  # don't flip if this routine is over or we'll get a blank screen
                win.flip()
                currentLoop = trials

        # -------Ending Routine "cue"-------
        for thisComponent in cueComponents:
            if hasattr(thisComponent, "setAutoDraw"):
                thisComponent.setAutoDraw(False)
        restim.setAutoDraw(False)

        # ------Prepare to start Routine "trial"-------
        # update component parameters for each repeat
        polygon_trial_0.setPos((mylocation[0][0], mylocation[0][1]))
        order_trial_0.setPos((mylocation[0][0], mylocation[0][1]))
        polygon_trial_0.setSize((size_w, size_h))

        polygon_trial_1.setPos((mylocation[1][0], mylocation[1][1]))
        order_trial_1.setPos((mylocation[1][0], mylocation[1][1]))
        polygon_trial_1.setSize((size_w, size_h))

        polygon_trial_2.setPos((mylocation[2][0], mylocation[2][1]))
        order_trial_2.setPos((mylocation[2][0], mylocation[2][1]))
        polygon_trial_2.setSize((size_w, size_h))

        polygon_trial_3.setPos((mylocation[3][0], mylocation[3][1]))
        order_trial_3.setPos((mylocation[3][0], mylocation[3][1]))
        polygon_trial_3.setSize((size_w, size_h))

        polygon_trial_4.setPos((mylocation[4][0], mylocation[4][1]))
        order_trial_4.setPos((mylocation[4][0], mylocation[4][1]))
        polygon_trial_4.setSize((size_w, size_h))

        polygon_trial_5.setPos((mylocation[5][0], mylocation[5][1]))
        order_trial_5.setPos((mylocation[5][0], mylocation[5][1]))
        polygon_trial_5.setSize((size_w, size_h))

        polygon_trial_6.setPos((mylocation[6][0], mylocation[6][1]))
        order_trial_6.setPos((mylocation[6][0], mylocation[6][1]))
        polygon_trial_6.setSize((size_w, size_h))

        polygon_trial_7.setPos((mylocation[7][0], mylocation[7][1]))
        order_trial_7.setPos((mylocation[7][0], mylocation[7][1]))
        polygon_trial_7.setSize((size_w, size_h))

        # polygon_trial_8.setPos((mylocation[8][0], mylocation[8][1]))
        # order_trial_8.setPos((mylocation[8][0], mylocation[8][1]))
        # polygon_trial_8.setSize((size_w, size_h))

        # seleclist2 = [polygon_trial_0, polygon_trial_1, polygon_trial_2, polygon_trial_3, polygon_trial_4, polygon_trial_5,
        #               polygon_trial_6,
        #               polygon_trial_7, polygon_trial_8]
        # keep track of which components have finished
        # trialComponents = [polygon_trial_0, polygon_trial_1, polygon_trial_2, polygon_trial_3, polygon_trial_4,
        #                    polygon_trial_5, polygon_trial_6, polygon_trial_7, polygon_trial_8]
        polygon_trial_0=trial_0[0]
        trialComponents = [trial_0[0], polygon_trial_1, polygon_trial_2, polygon_trial_3, polygon_trial_4,
                           polygon_trial_5, polygon_trial_6, polygon_trial_7]
        for thisComponent in trialComponents:
            thisComponent.tStart = None
            thisComponent.tStop = None
            thisComponent.tStartRefresh = None
            thisComponent.tStopRefresh = None
            if hasattr(thisComponent, 'status'):
                thisComponent.status = NOT_STARTED
        # reset timers
        t = 0
        _timeToFirstFrame = win.getFutureFlipTime(clock="now")
        trialClock.reset(-_timeToFirstFrame)  # t0 is time of first possible flip
        frameN = -1
        continueRoutine = True

        trial_mi_start = time.time()
        eeg_file_ready.clear()
        queue.put("start-1")
        begin_time = time.time()
        # -------Run Routine "trial"-------
        flag_start=True
        idx_num = [27,24,25,24,24,16,31,25]
        idx_flag = [0,0,0,0,0,0,0,0]
        count = 0
        while continueRoutine:
            # get current time
            count+=1
            if count==5:
                count=0
                # old_polygon_trial_0= trial_0[idx_flag[0]]
                # trial_0[idx_flag[0]].setAutoDraw(False)
                for k in range(8):
                    if k == 0:
                        trial_0[idx_flag[0]].setAutoDraw(False)
                        trial_1[idx_flag[1]].setAutoDraw(False)
                        trial_2[idx_flag[2]].setAutoDraw(False)
                        trial_3[idx_flag[3]].setAutoDraw(False)
                        trial_4[idx_flag[4]].setAutoDraw(False)
                        trial_5[idx_flag[5]].setAutoDraw(False)
                        trial_6[idx_flag[6]].setAutoDraw(False)
                        trial_7[idx_flag[7]].setAutoDraw(False)
                    idx_flag[k] +=1
                    if idx_flag[k]==idx_num[k]:
                        idx_flag[k]=1
                    # trial_0[idx_flag[0]].setAutoDraw(False)
                trialComponents = [ trial_0[idx_flag[0]], trial_1[idx_flag[1]], trial_2[idx_flag[2]], trial_3[idx_flag[3]], 
                                trial_4[idx_flag[4]],trial_5[idx_flag[5]],trial_6[idx_flag[6]], trial_7[idx_flag[7]]]
            t = trialClock.getTime()
            tThisFlip = win.getFutureFlipTime(clock=trialClock)
            tThisFlipGlobal = win.getFutureFlipTime(clock=None)
            frameN = frameN + 1  # number of completed frames (so 0 is the first frame)
            # update/draw   +components on each frame
            restim.setAutoDraw(False)
            
            # *polygon_trial_0* updates
            if flag_start==True:
                tStart=t
                StartRefresh = tThisFlipGlobal
                flag_start = False
            if polygon_trial_0.status == NOT_STARTED and tThisFlip >= 0.0 - frameTolerance:
                startDraw(win,polygon_trial_0,t,tThisFlipGlobal,frameN)
                startDraw(win,polygon_trial_1,t,tThisFlipGlobal,frameN)
                startDraw(win,polygon_trial_2,t,tThisFlipGlobal,frameN)
                startDraw(win,polygon_trial_3,t,tThisFlipGlobal,frameN)
                startDraw(win,polygon_trial_4,t,tThisFlipGlobal,frameN)
                startDraw(win,polygon_trial_5,t,tThisFlipGlobal,frameN)
                startDraw(win,polygon_trial_6,t,tThisFlipGlobal,frameN)
                startDraw(win,polygon_trial_7,t,tThisFlipGlobal,frameN)
                    # Image_trial_0.setAutoDraw(False)


            # Amp = (sin(2 * pi * Freq * frameN / 60 + Phas) - 0.5) * 2
            Amp = sin(2 * pi * Freq * frameN / refresh_hz + Phas)
            Amp = np.sign(Amp)
            # i0+=1
            i0+=1
            # 不在逐帧循环中打印，避免终端 I/O 干扰刺激刷新时序。
            for idx in range(8):
                if Amp[idx]==1:
                    trialComponents[idx].setAutoDraw(True)
                    trialComponents[idx].status = STARTED
                else:
                    trialComponents[idx].setAutoDraw(False)
                    trialComponents[idx].status = STARTED
                # is it time to stop? (based on global clock, using actual start)
            if tThisFlipGlobal > StartRefresh + trial_trial_dura - frameTolerance:
                # keep track of stop time/frame for later
                # polygon_trial_0.tStop = t  # not accounting for scr refresh
                # polygon_trial_0.frameNStop = frameN  # exact frame index
                stopDraw(win,polygon_trial_0,t,tThisFlipGlobal,frameN)
                stopDraw(win,polygon_trial_1,t,tThisFlipGlobal,frameN)
                stopDraw(win,polygon_trial_2,t,tThisFlipGlobal,frameN)
                stopDraw(win,polygon_trial_3,t,tThisFlipGlobal,frameN)
                stopDraw(win,polygon_trial_4,t,tThisFlipGlobal,frameN)
                stopDraw(win,polygon_trial_5,t,tThisFlipGlobal,frameN)
                stopDraw(win,polygon_trial_6,t,tThisFlipGlobal,frameN)
                stopDraw(win,polygon_trial_7,t,tThisFlipGlobal,frameN)
                for idx in range(8):
                    trialComponents[idx].status = FINISHED
            # check for quit (typically the Esc key)
            if endExpNow or defaultKeyboard.getKeys(keyList=["escape"]):
                print(1111111111111)
                queue.put("end")
                time.sleep(0.5)
                queue.put("del")
                time.sleep(0.5)
                # time.sleep(30)
                # process.terminate()
                print("线程已关闭")
                core.quit()
                # sys.exit()
            # check if all components have finished
            if not continueRoutine:  # a component has requested a forced-end of Routine
                break
            continueRoutine = False  # will revert to True if at least one component still running
            for thisComponent in trialComponents:
                if hasattr(thisComponent, "status") and thisComponent.status != FINISHED:
                    continueRoutine = True
                    break  # at least one component has not yet finished
            # refresh the screen
            # print(continueRoutine)
            if continueRoutine:  # don't flip if this routine is over or we'll get a blank screen
                win.flip()
        # 彻底关闭所有闪烁图像，避免残留的 AutoDraw 刺激覆盖反馈页面。
        for stimulus_group in (trial_0, trial_1, trial_2, trial_3,
                               trial_4, trial_5, trial_6, trial_7):
            for stimulus_image in stimulus_group:
                stimulus_image.setAutoDraw(False)
        win.flip()

        trial_mi_end = time.time()
        queue.put("end")
        # queue1.put("end")
        print("show trial spend time: ", time.time() - begin_time)

        # 等待采集线程完成本轮 CSV 写入，避免主线程读到旧文件或半写文件。
        if not eeg_file_ready.wait(timeout=8.0):
            raise RuntimeError(
                'EEG 采集线程在 8 秒内没有完成本轮数据写入。请检查 BHB 上位机'
                '是否仍在采集、LSL 流是否中断。'
            )

        # 写盘完成后才进入真正的数据读取与 FBCCA 计算。
        recognizing_text.draw()
        win.flip()

        def resample_eeg_data(x, resample_fs):
            """
            重新采样 eeg 数据
            resample_fs 重新采样的频率
            """
            resample_list = []
            for channel in range(x.shape[0]):
                resample_list.append(resample(x[channel], resample_fs))
            return np.array(resample_list)

        eeg_csv_path = os.path.join(save_path, "start-1.csv")
        try:
            np_array = pd.read_csv(eeg_csv_path).to_numpy()
        except pd.errors.EmptyDataError as csv_error:
            raise RuntimeError(
                '本轮 EEG 文件为空，无法执行 SSVEP 分析。请确认 BHB-EEGSuite '
                '仍处于“开始采集”状态。文件：{}'.format(eeg_csv_path)
            ) from csv_error

        if np_array.ndim != 2 or np_array.shape[0] == 0:
            raise RuntimeError(
                '本轮没有收到任何 EEG 采样，已停止分析以避免对空数组重采样。'
                '请确认 BHB 实时波形正在刷新，并重新启动本程序。'
            )
        if np_array.shape[1] < 8:
            raise RuntimeError(
                'LSL 数据只有 {} 列，但 FBCCA 至少需要 8 个 EEG 通道。'.format(
                    np_array.shape[1])
            )
        if np_array.shape[1] > 8:
            print(
                '检测到 {} 个 LSL 通道；FBCCA 使用前 8 个通道，其余通道忽略。'.format(
                    np_array.shape[1])
            )

        data_len = np_array.shape[0]

        # 仅取末尾 trial_t_stim 秒用于识别；前面的 reaction_time 留给大脑反应。
        print("识别数据形状:{}".format(np_array.shape))
        required_samples = int(round(trial_t_stim * 500))
        if data_len < required_samples:
            print("警告：本轮仅收到 {} 个采样点，少于期望的 {} 个".format(
                data_len, required_samples))
        start_index = max(0, data_len - required_samples)
        np_array = np_array[start_index:data_len, 0:8]

        print("识别数据形状:{}".format(np_array.shape))
        np_array = np_array.transpose(1, 0)
        # 下采样成 250 Hz
        resampled_samples = int(round(250 * trial_t_stim))
        np_array = resample_eeg_data(np_array, resampled_samples)
        print("识别数据形状:{}".format(np_array.shape))
        result = int(fbcca.fbcca_classify(np_array, resampled_samples))
        if result not in range(1, 9):
            raise ValueError("FBCCA 返回了无效类别：{}".format(result))

        # MI 只作为反馈信息，不参与 SSVEP 识别、手套动作或时长调整。
        # 使用最高一部分窗口的平均分，并用持续窗口比例进行轻量修正。
        mi_result = (
            mi_receiver.summary_between(
                trial_mi_start,
                trial_mi_end,
                threshold=mi_threshold,
                warmup_sec=mi_warmup_sec,
                top_fraction=mi_top_fraction,
                sustain_threshold_ratio=mi_sustain_threshold_ratio,
            )
            if mi_feedback_enabled else None
        )
        mi_percent = None
        if mi_result is not None:
            mi_result['threshold'] = float(mi_threshold)
            mi_percent = mi_score_to_percent(
                mi_result['score'],
                mi_threshold,
                mapping_scale=mi_mapping_scale,
            )
            print(
                'MI 本轮强度：有效窗口={count}，最高{top_fraction:.0%}均值='
                '{top_mean_score:.6f}，持续窗口={sustain_count}/{count}'
                '（门槛={sustain_level:.6f}），有效分数={score:.6f}，'
                '原始范围=[{score_min:.6f}, {score_max:.6f}]，'
                '强度={percent:.1f}%'.format(
                    percent=mi_percent,
                    **mi_result
                )
            )

        print('result:', result, order_lst[result - 1],
              'mode: 自由使用（无真实标签，不计算正确率）')

        order2action = {1:b'\xA8', 2:b'\xA1',3:b'\xA2',4:b'\xA3',5:b'\xA4',6:b'\xA5',7:b'\xA6',8:b'\xA7'}
        action = order2action[result]
        if con_flag:
            ser.write(action)

        feedback_payload = {
            'predicted': result,
            'mi_percent': mi_percent,
            'mi_result': mi_result,
        }
        feedback_drawables = configure_feedback_page(feedback_payload)
        feedback_animation = feedback_animation_groups[result - 1]

        # 保留 external_program2 的动作等待时长：普通动作 7 秒，第1动作（握拳）14 秒。
        # 反馈期间循环播放对应动作动画，不再显示右侧绿色倒计时条。
        feedback_duration = 14.0 if result == 1 else 7.0
        if not feedback_animation:
            feedback_drawables.insert(1, feedback_image)
        responsive_wait(
            feedback_duration, win,
            drawables=feedback_drawables,
            animation_frames=feedback_animation,
            animation_fps=12.0)

        append_session_log({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'mode': run_mode,
            'trial_index': trial_index,
            'predicted': result,
            'predicted_name': order_lst[result - 1],
            'used_t_stim': '{:.1f}'.format(trial_t_stim),
            'used_trial_dura': '{:.1f}'.format(trial_trial_dura),
            'mi_received': int(mi_result is not None),
            'mi_label': '' if mi_result is None else mi_result['label'],
            'mi_score': '' if mi_result is None else '{:.6f}'.format(mi_result['score']),
            'mi_percent': '' if mi_percent is None else '{:.1f}'.format(mi_percent),
            'mi_received_at': '' if mi_result is None else '{:.6f}'.format(mi_result['received_at']),
        })

        # -------Ending Routine "trial"-------

        for thisComponent in trialComponents:
            if hasattr(thisComponent, "setAutoDraw"):
                thisComponent.setAutoDraw(False)

        # the Routine "trial" was not non-slip safe, so reset the non-slip timer
        routineTimer.reset()

    # 实际运行持续到用户按 ESC；TrialHandler 仅提供足够大的循环上限。

    # Flip one final time so any remaining win.callOnFlip()
    # and win.timeOnFlip() tasks get executed before quitting
    win.flip()

    # these shouldn't be strictly necessary (should auto-save)
    logging.flush()
    mi_receiver.close()
    win.close()
    queue.put("end")
    time.sleep(0.5)
    queue.put("del")
    time.sleep(0.5)
    # time.sleep(30)
    # process.terminate()
    print("线程已关闭")
    core.quit()
    # decorator(tello.land)()

if __name__ == '__main__':
    stim()