#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
人名与专有名词映射管理器

职责：
1. 维护 config/name_mapping.json 映射文件，确保在 Docker 容器及宿主机环境下持久化。
2. 提供 mappings (原文名称 -> 标准中文/规范名称) 字典及 records (包含原标题、视频链接、任务ID等审计信息)。
3. 在 AI 生成元数据前，根据原始标题检索匹配的已知映射并组装注入到 Prompt 中。
4. 在 AI 生成元数据后，提取 AI 识别出的人名/专有名词，自动追加或更新映射表。
5. 提供人工校验保护机制：人工修改或标记过的条目不会被后续 AI 自动覆盖。
"""

from __future__ import annotations

import os
import re
import json
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .utils import get_app_subdir, safe_str

logger = logging.getLogger("name_mapping_manager")

_FILE_LOCK = threading.Lock()
_MAPPING_FILE_NAME = "name_mapping.json"


def get_mapping_file_path() -> str:
    """获取映射文件的绝对路径（位于 config/name_mapping.json）。"""
    config_dir = get_app_subdir("config")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, _MAPPING_FILE_NAME)


def _get_current_time_str() -> str:
    """获取当前格式化时间字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_default_structure() -> Dict[str, Any]:
    """返回映射表的默认空结构。"""
    return {
        "_comment": (
            "人名/专有名词映射表。mappings 定义了原文名称到标准规范名称的映射。"
            "可在 mappings 中直接增删改标准名。程序在识别到新人名后会自动追加，"
            "若某条目已人工确认，可将 records 中的 manual_verified 设为 true 防止被 AI 自动更改。"
        ),
        "mappings": {},
        "records": {},
    }


def load_name_mapping_data() -> Dict[str, Any]:
    """
    安全读取映射表数据。若文件不存在则初始化空结构。
    """
    file_path = get_mapping_file_path()
    with _FILE_LOCK:
        if not os.path.isfile(file_path):
            data = _get_default_structure()
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning("初始化映射文件失败: %s", e)
            return data

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("映射文件格式非字典，将重置为默认结构: %s", file_path)
                data = _get_default_structure()
            data.setdefault("mappings", {})
            data.setdefault("records", {})
            return data
        except Exception as e:
            logger.error("读取映射文件失败 (%s): %s", file_path, e)
            return _get_default_structure()


def save_name_mapping_data(data: Dict[str, Any]) -> bool:
    """
    保存映射表数据到文件。
    """
    file_path = get_mapping_file_path()
    with _FILE_LOCK:
        try:
            temp_path = f"{file_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, file_path)
            return True
        except Exception as e:
            logger.error("保存映射文件失败 (%s): %s", file_path, e)
            return False


def get_active_mappings() -> Dict[str, str]:
    """
    获取当前所有生效的映射字典 {原文名称: 标准名称}。
    """
    data = load_name_mapping_data()
    raw_mappings = data.get("mappings", {})
    clean_mappings: Dict[str, str] = {}
    if isinstance(raw_mappings, dict):
        for k, v in raw_mappings.items():
            key = safe_str(k).strip()
            val = safe_str(v).strip()
            if key and val:
                clean_mappings[key] = val
    return clean_mappings


def get_mapping_prompt_text(original_title: str = "", description: str = "") -> str:
    """
    检索待处理文本中命中的已知人名/专有名词映射，组装成注入给 AI 的提示词上下文。
    如果文本命中已知词汇，则仅返回命中的词汇；若没有命中但总数较少（<= 30条），可提供全部参考。
    """
    mappings = get_active_mappings()
    if not mappings:
        return ""

    search_text = f"{original_title} {description}".strip().lower()
    matched: Dict[str, str] = {}

    # 1. 优先提取在原始文本中直接出现的词条（不区分大小写）
    raw_matched: Dict[str, str] = {}
    for orig_name, std_name in mappings.items():
        if orig_name.lower() in search_text or std_name.lower() in search_text:
            raw_matched[orig_name] = std_name

    # 过滤掉完全作为更长匹配词子串的较短词条（例如匹配到 '이나경' 时，避免独立将 '나경' 重复混淆注入）
    matched: Dict[str, str] = {}
    for k, v in raw_matched.items():
        k_lower = k.lower()
        total_k = search_text.count(k_lower)
        inside_longer = sum(
            search_text.count(other.lower())
            for other in raw_matched
            if other != k and k_lower in other.lower()
        )
        if total_k > inside_longer:
            matched[k] = v

    # 2. 如果没有精准命中，但映射表总数较小，提供前 30 条作为参考
    if not matched:
        if len(mappings) <= 30:
            matched = mappings
        else:
            return ""

    lines = [
        "【已知人名/专有名词映射参考】",
        "注意：若视频内容涉及以下人物或专有名词，必须严格使用对应的标准中文/规范称呼，严禁自行脑补或捏造其他译名/昵称：",
    ]
    for orig, std in matched.items():
        lines.append(f"- {orig} => {std}")

    return "\n".join(lines)


def parse_names_from_ai_text(text: str) -> List[Tuple[str, str]]:
    """
    从 AI 返回的文本（如 **人名** 段落或 JSON）中解析出 (原文名称, 规范名称) 列表。
    支持格式：
    - 재이 => J
    - J -> 张叡恩
    - 재이: J
    - 原文: 재이, 标准: J
    """
    if not text:
        return []

    results: List[Tuple[str, str]] = []
    lines = text.strip().splitlines()

    # 正则匹配形如 "A => B", "A -> B", "A = B", "A: B", "A：B"
    pair_re = re.compile(r"^\s*[-*•]?\s*([^=\->:：,，\n]+?)\s*(?:=>|->|=|:|：)\s*([^,，\n]+?)\s*$")

    for raw_line in lines:
        line = raw_line.strip().lstrip("-*• ")
        if not line or line in ("无", "none", "null", "暂无", "无特定人物", "无人物"):
            continue

        # 尝试 JSON 提取
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    orig = safe_str(obj.get("original") or obj.get("orig") or obj.get("name") or obj.get("原名")).strip()
                    std = safe_str(obj.get("standard") or obj.get("target") or obj.get("std") or obj.get("标准名") or obj.get("规范名")).strip()
                    if orig and std and std not in ("无", "none"):
                        results.append((orig, std))
                        continue
            except Exception:
                pass

        m = pair_re.match(line)
        if m:
            orig = m.group(1).strip()
            std = m.group(2).strip()
            # 过滤掉明显的说明或“无”
            if orig and std and std not in ("无", "none", "null", "暂无"):
                # 去除引号与首尾多余标点
                orig = orig.strip("'\"`")
                std = std.strip("'\"`")
                if orig and std:
                    results.append((orig, std))

    return results


def record_name_mappings(
    extracted_names: List[Tuple[str, str]],
    original_title: str = "",
    video_url: str = "",
    task_id: str = "",
    manual_verified: bool = False,
) -> int:
    """
    将提取到的 (原文名称, 规范名称) 记录并更新到 config/name_mapping.json。

    更新规则：
    1. mappings:
       - 若条目不存在：新增 `mappings[orig] = std`。
       - 若条目已存在但 records 中 manual_verified == True：保持人工修改的名称，不覆盖！
       - 若条目已存在且未人工校验：保留已有标准名，或允许更新。
    2. records:
       - 记录 standard_name, manual_verified, first_seen, last_seen, occurrences
       - sample_titles（保留最近最多 5 条原标题）
       - sample_urls（保留最近最多 5 条视频链接）
       - task_ids（保留最近最多 5 个任务 ID）

    Returns:
        int: 新增或更新的条目数量。
    """
    if not extracted_names:
        return 0

    data = load_name_mapping_data()
    mappings = data.setdefault("mappings", {})
    records = data.setdefault("records", {})
    now_str = _get_current_time_str()
    modified_count = 0

    for orig_name, std_name in extracted_names:
        orig = safe_str(orig_name).strip()
        std = safe_str(std_name).strip()
        if not orig or not std or std in ("无", "none", "null"):
            continue

        existing_record = records.get(orig) or {}
        is_manual = bool(existing_record.get("manual_verified", False))

        if is_manual and not manual_verified:
            # 人工确认过的条目，AI 自动记录时不修改 mappings 中的设定名称
            target_std = mappings.get(orig, std)
        else:
            # 如果是人工传入或尚未在 mappings 中，写入当前设定
            target_std = std
            mappings[orig] = target_std

        # 更新 records
        occurrences = int(existing_record.get("occurrences", 0)) + 1
        first_seen = existing_record.get("first_seen") or now_str

        sample_titles: List[str] = existing_record.get("sample_titles") or []
        if original_title and original_title not in sample_titles:
            sample_titles = [original_title] + sample_titles[:4]

        sample_urls: List[str] = existing_record.get("sample_urls") or []
        if video_url and video_url not in sample_urls:
            sample_urls = [video_url] + sample_urls[:4]

        task_ids: List[str] = existing_record.get("task_ids") or []
        if task_id and task_id not in task_ids:
            task_ids = [task_id] + task_ids[:4]

        # 保留原有的审计字段
        records[orig] = {
            "standard_name": target_std,
            "manual_verified": is_manual or manual_verified,
            "audit_status": existing_record.get("audit_status", "verified" if (is_manual or manual_verified) else "pending"),
            "audit_timestamp": existing_record.get("audit_timestamp"),
            "audit_evidence": existing_record.get("audit_evidence", ""),
            "first_seen": first_seen,
            "last_seen": now_str,
            "occurrences": occurrences,
            "sample_titles": sample_titles,
            "sample_urls": sample_urls,
            "task_ids": task_ids,
        }
        modified_count += 1

    if modified_count > 0:
        save_name_mapping_data(data)
        logger.info(
            "已更新人名/专有名词映射表: 新增/更新 %d 条 | task_id=%s",
            modified_count,
            task_id,
        )

    return modified_count


def get_audit_queue(full: bool = False) -> List[Dict[str, Any]]:
    """
    获取待核验的人名/专有名词条目列表。

    Args:
        full (bool): 若为 True，返回全量条目；若为 False，仅返回未核验条目（manual_verified 为 False 或 audit_status != 'verified'）。

    Returns:
        List[Dict[str, Any]]: 包含条目详细信息的列表。
    """
    data = load_name_mapping_data()
    mappings = data.get("mappings", {})
    records = data.get("records", {})
    queue: List[Dict[str, Any]] = []

    for orig_name, std_name in mappings.items():
        rec = records.get(orig_name, {})
        is_verified = bool(rec.get("manual_verified", False))
        audit_status = rec.get("audit_status", "verified" if is_verified else "pending")

        if full or (not is_verified or audit_status != "verified"):
            queue.append({
                "orig_name": orig_name,
                "current_name": std_name,
                "manual_verified": is_verified,
                "audit_status": audit_status,
                "audit_timestamp": rec.get("audit_timestamp"),
                "audit_evidence": rec.get("audit_evidence", ""),
                "occurrences": rec.get("occurrences", 1),
                "sample_titles": rec.get("sample_titles", []),
                "sample_urls": rec.get("sample_urls", []),
                "task_ids": rec.get("task_ids", []),
            })

    return queue


def update_audit_result(
    orig_name: str,
    standard_name: str,
    audit_status: str = "verified",
    audit_evidence: str = "",
    manual_verified: bool = True,
) -> bool:
    """
    更新单个人名条目的真实核验结果。

    Args:
        orig_name: 原始韩文/外文名称
        standard_name: 真实核对后的标准中文/规范名称
        audit_status: 审计状态 ('verified', 'flagged', 'pending')
        audit_evidence: 真实核验依据（如维基百科官方汉字正名、官方SNS资料等）
        manual_verified: 是否锁定防止后续被 AI 再次篡改
    """
    data = load_name_mapping_data()
    mappings = data.setdefault("mappings", {})
    records = data.setdefault("records", {})

    orig = safe_str(orig_name).strip()
    std = safe_str(standard_name).strip()
    if not orig or not std:
        return False

    now_str = _get_current_time_str()
    mappings[orig] = std

    rec = records.setdefault(orig, {})
    rec["standard_name"] = std
    rec["manual_verified"] = manual_verified
    rec["audit_status"] = audit_status
    rec["audit_timestamp"] = now_str
    rec["audit_evidence"] = audit_evidence
    if "first_seen" not in rec:
        rec["first_seen"] = now_str
    rec["last_seen"] = now_str

    success = save_name_mapping_data(data)
    if success:
        logger.info(
            "已记录人名核验结果: [%s] -> [%s] (status=%s, evidence=%s)",
            orig,
            std,
            audit_status,
            audit_evidence,
        )
    return success


def batch_update_audit_results(results: Dict[str, Dict[str, Any]]) -> int:
    """
    批量更新人名条目核验结果并原子持久化保存。

    Args:
        results: {orig_name: {"standard_name": "...", "audit_status": "...", "audit_evidence": "..."}}

    Returns:
        int: 成功更新的条目数量。
    """
    if not results:
        return 0

    data = load_name_mapping_data()
    mappings = data.setdefault("mappings", {})
    records = data.setdefault("records", {})
    now_str = _get_current_time_str()
    count = 0

    for orig_name, info in results.items():
        orig = safe_str(orig_name).strip()
        if not orig or not isinstance(info, dict):
            continue

        std = safe_str(info.get("standard_name") or mappings.get(orig)).strip()
        if not std:
            continue

        status = info.get("audit_status", "verified")
        evidence = info.get("audit_evidence", "")
        verified = info.get("manual_verified", True)

        mappings[orig] = std
        rec = records.setdefault(orig, {})
        rec["standard_name"] = std
        rec["manual_verified"] = verified
        rec["audit_status"] = status
        rec["audit_timestamp"] = now_str
        rec["audit_evidence"] = evidence
        if "first_seen" not in rec:
            rec["first_seen"] = now_str
        rec["last_seen"] = now_str
        count += 1

    if count > 0:
        save_name_mapping_data(data)
        logger.info("批量更新人名核验结果完成，共更新 %d 条记录", count)

    return count

