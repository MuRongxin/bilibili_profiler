#!/usr/bin/env python3
"""
B站弹幕发送者用户画像分析系统 — 入口脚本

用法:
    python run.py BVxxxxxxxx [--force]
    python run.py --batch videos.txt   # 批量模式：逐行读取BV号（忽略空行与 # 注释行）
    --force: 强制重新分析
"""
import sys
import os

# 将 src 目录加入模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from main import main

if __name__ == "__main__":
    main()
