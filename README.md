# SSVEP-MI-Rehabilitation-BCI

A real-time EEG-based brain-computer interface rehabilitation system integrating **SSVEP**, **Motor Imagery (MI)**, real-time feedback, and pneumatic rehabilitation glove control.

基于 **SSVEP + 运动想象（Motor Imagery）EEG** 的实时脑机接口康复实验系统，支持视觉刺激、在线脑电分析、运动想象反馈以及气动康复手套控制。

---

## Overview

This project combines two EEG-based brain-computer interface paradigms:

- **SSVEP (Steady-State Visual Evoked Potential)** for visual target selection
- **Motor Imagery (MI)** for online motor intention estimation

The system receives real-time EEG data through **Lab Streaming Layer (LSL)**, performs online analysis, provides visual feedback, and can optionally control a pneumatic rehabilitation glove.

The current version is primarily designed for **Windows**.

---

## Features

- Real-time SSVEP visual stimulation
- FBCCA-based SSVEP recognition
- 8-target SSVEP paradigm
- Online Motor Imagery classification
- Real-time MI intention feedback
- LSL-based EEG acquisition
- UDP communication between MI and the main experiment
- Pneumatic rehabilitation glove control
- Windows PowerShell launcher
- Optional SSVEP-only mode
- Automatic experiment data and log directories
- Local EEG data excluded from the public repository

---

## SSVEP Paradigm

The current system uses eight visual stimulation frequencies:

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

EEG signals are analyzed using **Filter Bank Canonical Correlation Analysis (FBCCA)** to estimate the attended visual target.

---

## Motor Imagery

The Motor Imagery module performs online EEG classification using the model included in:

```text
MI参考_未修改/
```

Main MI files:

```text
MI参考_未修改/
├─ online_opt_plus.py
└─ opt_plus_model_bank_station_0515_0516_final.npz
```

The online MI process communicates with the main SSVEP program through local UDP.

Default communication parameters:

```text
IP:   127.0.0.1
Port: 8889
```

The current launcher is configured to receive EEG from an LSL stream with:

```text
Stream Name: BHB-EEG
Stream Type: EEG
```

---

## System Workflow

```text
EEG Device
    │
    ▼
EEG Acquisition Software
    │
    ▼
Lab Streaming Layer (LSL)
    │
    ├──────────────► SSVEP / FBCCA Analysis
    │
    └──────────────► Motor Imagery Classification
                           │
                           ▼
                      UDP Feedback
                           │
                           ▼
                    Main Experiment UI
                           │
                           ▼
              Pneumatic Rehabilitation Glove
                    (optional)
```

---

## Project Structure

```text
SSVEP-MI-Rehabilitation-BCI/
│
├─ stim_ssvep.py
│  Main SSVEP experiment and feedback interface
│
├─ fbcca.py
│  FBCCA-based SSVEP recognition
│
├─ lsl_received_data.py
│  EEG acquisition through Lab Streaming Layer
│
├─ MI参考_未修改/
│  Motor Imagery online classification and model
│
├─ 动作图/
│  Rehabilitation movement animation resources
│
├─ config_windows.example.ini
│  Example configuration file
│
├─ requirements_windows_minimal.txt
│  Python dependency list
│
├─ run_ssvep_windows.ps1
│  Main Windows launcher
│
├─ 启动SSVEP_Windows.bat
│  Double-click launcher for Windows
│
├─ 检查Windows环境.ps1
│  Environment / COM / LSL diagnostic script
│
├─ WINDOWS运行说明.md
│  Detailed Windows usage instructions
│
├─ .gitignore
└─ .gitattributes
```

---

## Requirements

The current version is mainly tested under Windows.

Main software dependencies include:

```text
Python
NumPy
SciPy
Pandas
scikit-learn
pyserial
pylsl
PsychoPy
```

Install the project dependencies with:

```powershell
python -m pip install -r .\requirements_windows_minimal.txt
```

A Conda environment is recommended.

---

## Configuration

The repository provides an example configuration file:

```text
config_windows.example.ini
```

The local experiment configuration is:

```text
config.ini
```

If `config.ini` does not exist, the Windows launcher can create it from the example configuration.

### Pneumatic glove

To run the system without the rehabilitation glove:

```ini
con_flag = 0
```

To enable glove control:

```ini
con_flag = 1
```

Then configure the correct Windows serial port, for example:

```ini
port = COM9
```

The actual COM port depends on the connected hardware.

---

## EEG / LSL Setup

Before starting the experiment:

1. Connect the EEG device.
2. Start the EEG acquisition software.
3. Start EEG sampling.
4. Enable the LSL EEG stream.
5. Confirm that the experiment computer can discover the stream.

You can check the current Windows environment with:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\检查Windows环境.ps1
```

The script checks Python, required dependencies, project files, available COM ports, and visible LSL streams.

---

## Running the Experiment

### Full SSVEP + MI mode

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\run_ssvep_windows.ps1
```

You can also double-click:

```text
启动SSVEP_Windows.bat
```

The launcher starts the MI online process and the main SSVEP experiment.

---

### SSVEP-only mode

To run SSVEP without Motor Imagery:

```powershell
.\run_ssvep_windows.ps1 -NoMI
```

This mode is useful for testing visual stimulation and SSVEP recognition independently.

---

## MI Logs

Motor Imagery runtime logs are written to:

```text
logs\mi_online.stdout.log
logs\mi_online.stderr.log
```

If MI feedback is unavailable, check:

- whether EEG acquisition is running;
- whether the `BHB-EEG` LSL stream is visible;
- whether the MI model exists;
- whether `mi_online.stderr.log` contains an error.

---

## Rehabilitation Animation Resources

The directory:

```text
动作图/
```

contains rehabilitation movement animation frames used by the experiment.

These files are runtime resources and should not be deleted or renamed.

---

## Experimental Data

The following local directories and configuration files are intentionally excluded from the public repository:

```text
eeg_data/
data/
logs/
config.ini
```

This prevents experimental EEG recordings, local logs, and machine-specific configuration from being accidentally published.

Archive and backup files are also excluded through `.gitignore`.

---

## Recommended Startup Order

```text
Connect EEG device
        ↓
Start EEG acquisition software
        ↓
Start EEG sampling
        ↓
Enable / verify BHB-EEG LSL stream
        ↓
Connect pneumatic glove (optional)
        ↓
Activate Python / Conda environment
        ↓
Run 检查Windows环境.ps1
        ↓
Run run_ssvep_windows.ps1
        ↓
Start experiment
```

---

## Documentation

Detailed Windows instructions are available in:

```text
WINDOWS运行说明.md
```

---

## Research Scope

This project is intended for research on:

- Brain-Computer Interfaces
- EEG signal processing
- SSVEP
- Motor Imagery
- Neurorehabilitation
- Real-time EEG feedback
- Human-machine interaction
- Pneumatic rehabilitation devices

The project is still under active development and optimization.

---

## Disclaimer

This software is intended for **research and educational purposes only**.

It is not a certified medical device and should not be used for clinical diagnosis or treatment without appropriate validation, ethical approval, and regulatory compliance.

---

## Contributing

Issues, suggestions, and contributions are welcome.

If you find this project useful, consider giving the repository a ⭐ **Star**.

---

## License

No open-source license has been specified yet.

A license may be added in a future release.