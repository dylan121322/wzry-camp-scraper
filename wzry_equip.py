#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
王者荣耀全量装备宝典抓取器 — 独立版
====================================
数据源: 官网"装备宝典"(游戏内置页面)背后的腾讯云 COS 公开数据, 无需登录:
  列表: https://wuji-1254960240.file.myqcloud.com/smoba_weapon/pages/p{1~6}.json
  详情: https://wuji-1254960240.file.myqcloud.com/smoba_weapon_detail/{equipment_id}.json

用法:
  python3 wzry_equip.py              # 抓取全部装备详情
  python3 wzry_equip.py --out DIR    # 指定输出目录(默认 ./data)
  python3 wzry_equip.py --list-only  # 只抓装备列表, 不抓详情

输出:
  data/equips_full.json — 全量(126件): 属性/被动主动/合成配方/适合英雄/攻略/图标
  data/equips_full.csv  — 扁平表格

依赖: 仅 Python 标准库 (urllib/ssl/json)。certifi 可选(CA链兜底)。
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 复用主脚本的抓取实现(本文件与 wzry_camp_scraper.py 同目录部署)
from wzry_camp_scraper import scrape_equips  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="王者荣耀全量装备宝典抓取器(COS公开数据, 无需登录)")
    ap.add_argument("--out", type=str, default=None, help="输出目录 (默认: 脚本同目录 data/)")
    args = ap.parse_args()
    out_dir = os.path.abspath(args.out) if args.out else os.path.join(HERE, "data")
    os.makedirs(out_dir, exist_ok=True)
    rows = scrape_equips(out_dir)
    print(f"\n完成: {len(rows)} 件装备 -> {out_dir}/equips_full.json + .csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
