#!/usr/bin/env python3
from __future__ import annotations

import configparser
import importlib
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
REQUIRED_MODULES = {
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "sklearn": "scikit-learn",
    "serial": "pyserial",
    "pylsl": "pylsl",
    "psychopy": "psychopy",
}

failed = False

print("===== Python依赖 =====")
for module, package in REQUIRED_MODULES.items():
    try:
        imported = importlib.import_module(module)
        version = getattr(imported, "__version__", "unknown")
        print(f"[成功] {module}: {version}")
    except Exception as exc:
        failed = True
        print(f"[失败] {module}（安装包：{package}）: {exc}")

print("\n===== 配置 =====")
cfg = configparser.ConfigParser()
config_path = BASE / "config.ini"
if not cfg.read(config_path, encoding="utf-8"):
    failed = True
    print(f"[失败] 无法读取 {config_path}")
else:
    save_path = Path(cfg.get("localdb", "save_path")).expanduser()
    print(f"save_path={save_path}")
    print(f"con_flag={cfg.get('localdb', 'con_flag')}")
    try:
        save_path.mkdir(parents=True, exist_ok=True)
        test_file = save_path / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        print("[成功] 数据目录可写")
    except Exception as exc:
        failed = True
        print(f"[失败] 数据目录不可写: {exc}")

print("\n===== 资源文件 =====")
for name in ("stim_ssvep.py", "fbcca.py", "lsl_received_data.py", "config.ini"):
    path = BASE / name
    ok = path.is_file()
    print(f"[{'成功' if ok else '失败'}] {name}")
    failed |= not ok

image_dir = BASE / "动作图"
images = list(image_dir.glob("*.jpg")) if image_dir.is_dir() else []
print(f"[{'成功' if images else '失败'}] 动作图数量：{len(images)}")
failed |= not bool(images)

print("\n===== 显示环境 =====")
print(f"DISPLAY={os.environ.get('DISPLAY', '(未设置)')}")
print(f"XAUTHORITY={os.environ.get('XAUTHORITY', '(未设置)')}")

raise SystemExit(1 if failed else 0)
