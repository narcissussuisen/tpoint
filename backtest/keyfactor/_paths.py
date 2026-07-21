# -*- coding: utf-8 -*-
"""
_paths.py — keyfactor 数据目录集中配置（2026-07-21 迁移至 F: 后建立）

测试数据库（1m 历史 CSV 等）已从 C 盘迁移到 F:/workbuddy/keyfactor_data。
所有 keyfactor 脚本统一从这里取路径，避免散落的相对路径引用。

如需临时切回本地副本，设置环境变量 KEYFACTOR_DATA_DIR 指向目标目录即可，
无需改代码。
"""
import os

KEYFACTOR_DATA_DIR = os.environ.get("KEYFACTOR_DATA_DIR", r"F:\workbuddy\keyfactor_data")
KEYFACTOR_1M_DIR = os.path.join(KEYFACTOR_DATA_DIR, "1m")

if __name__ == "__main__":
    print("KEYFACTOR_DATA_DIR =", KEYFACTOR_DATA_DIR)
    print("KEYFACTOR_1M_DIR   =", KEYFACTOR_1M_DIR)
