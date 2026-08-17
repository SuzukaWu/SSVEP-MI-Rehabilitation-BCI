# Windows 运行说明

本项目是一个基于 **SSVEP（稳态视觉诱发电位）+ Motor Imagery（运动想象，MI）** 的实时 EEG 脑机接口实验系统。

当前版本主要面向 **Windows** 环境运行，支持：

- SSVEP 视觉刺激与在线识别
- FBCCA 分析
- Motor Imagery 在线分类
- MI 运动想象反馈
- LSL 实时 EEG 数据接收
- 气动康复手套串口控制
- 实验数据与日志自动保存

---

## 1. 项目主要文件

项目目录中应至少包含：

```text
stim_ssvep.py
fbcca.py
lsl_received_data.py
run_ssvep_windows.ps1
启动SSVEP_Windows.bat
检查Windows环境.ps1
requirements_windows_minimal.txt
config_windows.example.ini

MI参考_未修改/
动作图/
```

其中：

- `stim_ssvep.py`：SSVEP 主实验程序与反馈界面
- `fbcca.py`：SSVEP / FBCCA 识别算法
- `lsl_received_data.py`：LSL EEG 数据接收与处理
- `MI参考_未修改/online_opt_plus.py`：MI 在线分类程序
- `MI参考_未修改/opt_plus_model_bank_station_0515_0516_final.npz`：MI 模型
- `动作图/`：康复动作动画所需图片资源
- `run_ssvep_windows.ps1`：Windows 主启动脚本
- `启动SSVEP_Windows.bat`：双击启动入口

---

## 2. Python 环境

建议使用 Conda 管理 Python 环境。

先打开：

```text
Anaconda PowerShell Prompt
```

或者已经配置好 Conda 的 PowerShell。

激活安装了本项目依赖的环境。

例如：

```powershell
conda activate psyenv
```

环境名称可以根据自己的电脑实际情况修改，并不要求必须叫 `psyenv`。

进入项目目录：

```powershell
cd "<你的项目目录>"
```

例如：

```powershell
cd "D:\OneDrive\桌面\SSVEP_比6反馈完整版"
```

---

## 3. 安装 Python 依赖

项目提供：

```text
requirements_windows_minimal.txt
```

可以执行：

```powershell
python -m pip install -r .\requirements_windows_minimal.txt
```

同时请确保当前 Python 环境中可以正常使用：

```text
numpy
scipy
pandas
pyserial
pylsl
psychopy
```

可以通过项目自带的环境检查脚本进一步确认。

---

## 4. 配置文件

程序运行需要：

```text
config.ini
```

公开仓库中不会直接提供本机使用的 `config.ini`，而是提供：

```text
config_windows.example.ini
```

如果项目目录中不存在 `config.ini`，运行：

```text
run_ssvep_windows.ps1
```

时会自动根据：

```text
config_windows.example.ini
```

生成一个新的：

```text
config.ini
```

之后请根据自己的硬件修改配置。

---

## 5. 气动康复手套配置

Windows 下串口格式通常类似：

```ini
port = COM9
```

实际 COM 号请在：

```text
Windows 设备管理器 → 端口（COM 和 LPT）
```

中确认。

### 不使用气动手套

设置：

```ini
con_flag = 0
```

此时可以仅运行 EEG、SSVEP 和 MI 功能，不会连接手套串口。

### 使用气动手套

设置：

```ini
con_flag = 1
```

并确认：

```ini
port = COM9
```

已经修改为气动手套实际对应的 COM 端口。

如果串口连接失败，请检查：

- USB 串口设备是否已经连接
- 串口驱动是否安装
- COM 号是否正确
- 串口是否被其他程序占用

---

## 6. EEG 与 LSL

启动本实验之前，需要先启动 EEG 采集系统。

基本流程：

1. 启动 EEG 上位机
2. 连接 EEG 设备
3. 开始 EEG 数据采集
4. 开启 LSL 数据输出
5. 确认实验电脑能够发现 EEG LSL 流
6. 再启动本项目

当前 MI 在线分类默认使用：

```text
Stream Name: BHB-EEG
Stream Type: EEG
```

当前 Windows 启动脚本中的 MI 参数默认包括：

```text
EEG channels: 8
Input sampling rate: 500 Hz
MI update step: 0.5 s
UDP IP: 127.0.0.1
UDP Port: 8889
```

因此，在运行完整 MI 功能前，应确认 EEG 上位机已经正常输出对应的 LSL 数据流。

---

## 7. 检查 Windows 环境

启动正式实验前，可以先运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\检查Windows环境.ps1
```

该脚本会检查：

- 当前 Python 版本与路径
- Python 依赖是否可以正常导入
- 项目必要文件是否存在
- Windows 当前可见 COM 串口
- 当前能够发现的 LSL 数据流

如果 LSL 检查结果中能够看到类似：

```text
BHB-EEG
EEG
```

说明 EEG LSL 数据流已经能够被当前电脑发现。

---

## 8. 启动完整实验

确认 EEG 上位机与 LSL 已经正常工作后，在项目目录执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\run_ssvep_windows.ps1
```

启动脚本会自动：

1. 检查 Python
2. 检查项目必要文件
3. 检查 Python 依赖
4. 检查 Windows COM 串口
5. 自动创建 `config.ini`（如果不存在）
6. 自动创建实验数据与日志目录
7. 启动 MI 在线分类程序
8. 启动 SSVEP 主实验程序
9. 实验结束后关闭 MI 后台进程

---

## 9. 双击启动

也可以直接双击：

```text
启动SSVEP_Windows.bat
```

该 BAT 文件会自动调用：

```text
run_ssvep_windows.ps1
```

因此：

- `.bat` 是方便使用的双击入口
- `.ps1` 是实际执行实验启动逻辑的脚本

两者功能并不重复。

---

## 10. 仅运行 SSVEP

如果暂时不希望启动 Motor Imagery 在线分类，可以运行：

```powershell
.\run_ssvep_windows.ps1 -NoMI
```

此模式下只启动 SSVEP 主实验。

适合：

- 单独测试 SSVEP
- 调试视觉刺激
- 检查 EEG / SSVEP 识别
- 暂时不运行 MI 模型

---

## 11. MI 在线分类

完整模式启动后：

```text
MI参考_未修改/online_opt_plus.py
```

会作为后台 Python 进程运行。

默认模型：

```text
MI参考_未修改/opt_plus_model_bank_station_0515_0516_final.npz
```

MI 在线分类结果通过本机 UDP 发送给 SSVEP 主程序：

```text
IP: 127.0.0.1
Port: 8889
```

SSVEP 程序接收 MI 分类结果，并在实验反馈阶段显示相应的运动想象反馈。

---

## 12. MI 日志

MI 在线程序的输出日志位于：

```text
logs\mi_online.stdout.log
logs\mi_online.stderr.log
```

如果反馈界面长时间没有 MI 数据，可以优先检查：

1. EEG 是否正在采样
2. `BHB-EEG` LSL 流是否存在
3. MI Python 程序是否正常启动
4. MI 模型文件是否存在
5. `logs\mi_online.stderr.log` 是否有异常信息

---

## 13. SSVEP 视觉刺激

当前系统使用多个不同频率的视觉刺激目标进行 SSVEP 识别。

当前目标频率为：

```text
8 Hz
9 Hz
10 Hz
11 Hz
12 Hz
13 Hz
14 Hz
15 Hz
```

SSVEP EEG 数据通过 FBCCA 方法进行分析，从而得到对应的目标识别结果。

---

## 14. 动作图片资源

项目中的：

```text
动作图/
```

不是示例图片或历史文件，而是程序运行所需的康复动作动画资源。

请保持该目录：

```text
动作图/
```

以及其中图片文件的原有目录结构和文件名。

不要随意删除或批量重命名其中的图片，否则可能导致实验反馈动画无法正常显示。

---

## 15. Windows 串口说明

Windows 串口应使用：

```text
COM3
COM9
COM10
```

这类格式。

不要使用 Linux 格式：

```text
/dev/ttyUSB0
/dev/ttyUSB1
```

实际串口请以 Windows 设备管理器显示为准。

---

## 16. 数据与日志目录

程序运行过程中可能自动创建：

```text
eeg_data/
data/
logs/
```

其中可能包含：

- EEG 原始数据
- 实验结果
- 被试实验记录
- MI 在线日志
- 临时分析结果

这些内容默认不会上传到公开 GitHub 仓库。

---

## 17. GitHub 中不会上传的本地文件

为了避免公开实验数据和本机配置，`.gitignore` 会排除：

```text
eeg_data/
data/
logs/
config.ini
```

同时还会排除：

```text
*.zip
*.rar
*.7z
*.tar
*.tar.gz
```

以及旧备份、缓存和临时文件。

因此公开仓库主要保留：

- 程序源代码
- MI 模型
- 动作资源
- Windows 启动脚本
- 示例配置
- Python 依赖列表
- 使用说明

---

## 18. 常见问题

### 找不到 Python

确认已经进入正确的 Conda 环境：

```powershell
conda activate <你的环境名称>
```

然后检查：

```powershell
python --version
```

---

### Python 依赖检查失败

执行：

```powershell
python -m pip install -r .\requirements_windows_minimal.txt
```

之后重新运行：

```powershell
.\检查Windows环境.ps1
```

---

### 找不到 BHB-EEG

确认：

1. EEG 上位机已经启动
2. EEG 设备已经连接
3. EEG 正在采样
4. LSL 输出已经开启

然后重新运行：

```powershell
.\检查Windows环境.ps1
```

查看当前可见 LSL 流。

---

### MI 反馈没有数据

检查：

```text
logs\mi_online.stdout.log
logs\mi_online.stderr.log
```

并确认：

```text
MI参考_未修改/online_opt_plus.py
```

和模型：

```text
MI参考_未修改/opt_plus_model_bank_station_0515_0516_final.npz
```

均存在。

---

### 找不到动作图片

确认：

```text
动作图/
```

目录存在，并且没有修改其中图片文件名。

---

### 气动手套无法连接

检查：

- `config.ini` 中 `con_flag` 是否为 `1`
- `port` 是否为实际 COM 口
- USB 串口设备是否连接
- Windows 串口驱动是否正常
- 串口是否被其他程序占用

如果暂时只测试脑电功能，可以设置：

```ini
con_flag = 0
```

---

## 19. 推荐启动顺序

完整实验推荐按照以下顺序进行：

```text
1. 连接 EEG 设备
       ↓
2. 启动 EEG 上位机
       ↓
3. 开始 EEG 采样
       ↓
4. 开启 / 确认 BHB-EEG LSL 流
       ↓
5. 连接气动康复手套（如果需要）
       ↓
6. 激活 Python / Conda 环境
       ↓
7. 进入项目目录
       ↓
8. 运行检查Windows环境.ps1
       ↓
9. 运行 run_ssvep_windows.ps1
       ↓
10. 开始实验
```

---

## 20. 说明

本项目目前主要用于 EEG 脑机接口与康复交互相关研究。

涉及的主要技术包括：

- EEG
- SSVEP
- FBCCA
- Motor Imagery
- Lab Streaming Layer
- PsychoPy
- 实时反馈
- 气动康复手套控制

项目仍在持续开发和优化中。