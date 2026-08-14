# SSVEP + MI 自由模式动图版：Windows 适配版

本包已处理 Windows 移植时最常见的问题：

- 不再强制读取 Linux 字体路径 `/usr/share/fonts/...`；
- Windows 自动使用 `Microsoft YaHei`；
- `config.ini` 始终从脚本所在目录读取；
- Linux 保存路径在 Windows 下自动回退到项目内 `eeg_data`；
- 串口改用 `COM3`、`COM4` 等 Windows 名称，并提供清晰报错；
- 提供 PowerShell/BAT 启动脚本，可同时启动 MI 在线分类和 SSVEP；
- “识别出的手势”标题已按要求从 y=310 下移到 y=250。

## 重要：这是跨平台替换包，不含原工程的大量资源

压缩包中没有重复打包原 SSVEP 工程里的以下内容：

- `fbcca.py`
- `lsl_received_data.py`
- `动作图` 文件夹
- 你实际使用的 `config.ini`

请把本包内容放入/合并到**完整的 SSVEP 工程目录**。运行目录至少应包含：

```text
stim_ssvep.py
fbcca.py
lsl_received_data.py
config.ini
动作图MI参考_未修改run_ssvep_windows.ps1
```

## 第一次配置

1. 打开 Anaconda PowerShell Prompt 或 VS Code PowerShell。
2. 激活环境：

```powershell
conda activate bci
```

3. 进入项目目录：

```powershell
cd "C:\你的路径\SSVEP_MI自由模式动图版_Windows"
```

4. 若没有 `config.ini`，执行启动脚本时会从 `config_windows.example.ini` 自动生成。
5. 打开 `config.ini`，把：

```ini
port = COM3
con_flag = 0
```

改成设备管理器中气动手套对应的 COM 口。确认串口无误后，再将 `con_flag` 改为 `1`。

## 安装基础依赖

在已经激活 `bci` 环境的 PowerShell 中执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\安装Windows基础依赖.ps1
```

这会通过清华 PyPI 镜像安装 `numpy/scipy/pandas/pyserial/pylsl`。PsychoPy 继续使用你的 bci 环境已有版本。

## 检查环境

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\检查Windows环境.ps1
```

该脚本会列出 Python 路径、依赖、缺失项目文件、COM 口和当前可见 LSL 流。

## 启动

先启动 BHB-EEG Station，连接头环并确认已经创建 `BHB-EEG` LSL 流，然后执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.un_ssvep_windows.ps1
```

也可以双击：

```text
启动SSVEP_Windows.bat
```

只测试 SSVEP、不启动 MI：

```powershell
.un_ssvep_windows.ps1 -NoMI
```

## 常见问题

### 找不到 `/usr/share/fonts/...`

Windows 适配版已删除这个硬编码路径。若仍出现该报错，说明运行的不是本包中的 `stim_ssvep.py`。

### 找不到 `fbcca` 或 `lsl_received_data`

说明只解压了替换包，没有与完整原工程合并。把对应 `.py` 文件复制到 `stim_ssvep.py` 同一目录。

### 找不到动作图片

把完整原工程中的 `动作图` 文件夹复制到当前目录，目录名保持不变。

### 串口报错

Windows 串口必须写成 `COM3`、`COM4` 等，不能写 `/dev/ttyUSB0`。在设备管理器中查看实际端口，并确保串口没有被其他程序占用。

### MI 显示“暂无数据”

查看：

```text
logs\mi_online.stdout.log
logs\mi_online.stderr.log
```

确认 `BHB-EEG` LSL 流存在，且 MI 脚本和模型都在 `MI参考_未修改` 目录。
