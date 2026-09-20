#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人名/专有名词映射表审计与修正脚本

支持模式：
1. --incremental (默认): 增量模式，只检索未经过真实核对 (manual_verified=False 或 audit_status!='verified') 的条目。
2. --full: 全量模式，检索映射表中所有的条目，用于初次全量检查或周期性全盘审计。
3. --summary: 仅输出当前映射表的核验统计与健康度报告。
4. --apply <json_file>: 批量导入核验修正结果并持久化到 config/name_mapping.json。

使用示例：
    python3 scripts/audit_name_mapping.py --incremental
    python3 scripts/audit_name_mapping.py --full
    python3 scripts/audit_name_mapping.py --summary
"""

import sys
import os
import json
import argparse

# 确保能引用项目根目录下的 modules
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from modules.name_mapping_manager import (
    load_name_mapping_data,
    get_audit_queue,
    update_audit_result,
    batch_update_audit_results,
    get_mapping_file_path,
)


def print_summary():
    data = load_name_mapping_data()
    mappings = data.get("mappings", {})
    records = data.get("records", {})

    total = len(mappings)
    verified_count = 0
    pending_count = 0

    for k in mappings:
        rec = records.get(k, {})
        is_verified = bool(rec.get("manual_verified", False))
        status = rec.get("audit_status", "verified" if is_verified else "pending")
        if is_verified and status == "verified":
            verified_count += 1
        else:
            pending_count += 1

    print("\n" + "=" * 60)
    print("【人名/专有名词映射表状态汇总】")
    print(f"映射表文件: {get_mapping_file_path()}")
    print(f"总映射词条数: {total}")
    print(f"已核对认证数 (Verified): {verified_count} ({verified_count/total*100:.1f}%)" if total else "0")
    print(f"待核验词条数 (Pending) : {pending_count} ({pending_count/total*100:.1f}%)" if total else "0")
    print("=" * 60 + "\n")
    return verified_count, pending_count


def run_audit(full: bool = False):
    mode_str = "全量检查模式 (Full Audit)" if full else "增量检查模式 (Incremental Audit)"
    print(f"\n>>> 启动人名映射表审计: {mode_str} ...")

    queue = get_audit_queue(full=full)
    if not queue:
        print("✅ 没有待核对的条目，映射表中所有条目均已完成真实核对认证！")
        return 0

    print(f"📋 共发现 {len(queue)} 条待核验条目：\n")
    for i, item in enumerate(queue, 1):
        orig = item["orig_name"]
        curr = item["current_name"]
        status = item["audit_status"]
        manual = "已人工确认" if item["manual_verified"] else "未确认"
        evidence = item.get("audit_evidence") or "无"
        titles = item.get("sample_titles", [])
        sample_title = titles[0] if titles else "无"
        urls = item.get("sample_urls", [])
        sample_url = urls[0] if urls else "无"

        print(f"[{i}/{len(queue)}] 原名: {orig} -> 当前标准名: {curr}")
        print(f"      状态: {status} ({manual}) | 依据: {evidence}")
        print(f"      样本文档: {sample_title}")
        print(f"      样本链接: {sample_url}")
        print("-" * 50)

    print(f"\n提示：可通过外部搜索（维基百科/官方资料）核实正确姓名后，使用本脚本或管理模块更新核实结果。")
    return len(queue)


def main():
    parser = argparse.ArgumentParser(description="人名映射表真实性核验工具")
    parser.add_argument("--full", action="store_true", help="执行全量检查")
    parser.add_argument("--incremental", action="store_true", help="执行增量检查（默认）")
    parser.add_argument("--summary", action="store_true", help="打印统计报告")
    parser.add_argument("--apply", type=str, help="应用包含核验修正结果的 JSON 文件")

    args = parser.parse_args()

    if args.apply:
        if not os.path.isfile(args.apply):
            print(f"❌ 找不到文件: {args.apply}")
            sys.exit(1)
        with open(args.apply, "r", encoding="utf-8") as f:
            corrections = json.load(f)
        count = batch_update_audit_results(corrections)
        print(f"✅ 成功应用并保存 {count} 条核验记录！")
        print_summary()
        sys.exit(0)

    if args.summary:
        print_summary()
        sys.exit(0)

    is_full = args.full or (not args.incremental and not args.summary)
    # 如果用户没有显式指定 full 或 incremental，默认看是否有参数
    if not args.full and args.incremental:
        is_full = False
    elif not args.full and not args.incremental:
        # 默认增量
        is_full = False

    pending_count = run_audit(full=is_full)
    print_summary()


if __name__ == "__main__":
    main()
