SSVEP RK3588 部署版
====================

目录目标：
  /home/cat/ssvep_rk3588

本迁移版只做跨平台阻塞修复：
1. Windows 图片路径“动作图\\...”改为 Linux 路径“动作图/...”
2. config.ini 的 F:\ 与 COM 端口改为 Linux 示例
3. 首次测试关闭串口：con_flag = 0
4. 删除未使用且压缩包缺失的 lsl_received_data_MI 导入
5. LSL 接收器依次兼容 type=EEG、BHB-EEG、TestStream
6. 自动创建 eeg_data 与 data 目录
7. 关闭逐帧 print，避免影响刺激时序

SSH中：
  source /home/cat/miniconda3/etc/profile.d/conda.sh
  conda activate bci
  cd /home/cat/ssvep_rk3588
  python check_env.py

仅首次显示授权可能需要在RK3588本地桌面终端执行：
  xhost +SI:localuser:cat

随后可在SSH中启动：
  /home/cat/ssvep_rk3588/run_ssvep.sh

注意：
- 运行前需先启动EEG上位机并产生LSL流。
- 初次测试 con_flag=0，不会访问串口。
- 正式启用串口前，先用 ls -l /dev/ttyUSB* /dev/ttyACM* 确认真实端口。
- 当前刺激频率计算按60Hz刷新率编写。运行后记录输出的 frameRate。
