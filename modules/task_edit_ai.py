#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""编辑任务页面的一键 AI 元数据生成服务。"""

import json
import logging
import os
from typing import Any, Dict, Mapping

from .ai_enhancer import (
    generate_acfun_tags,
    generate_bilibili_title_description,
    recommend_acfun_partition,
    recommend_bilibili_partition,
    recommend_partitions_aio,
    setup_task_logger,
)
from .utils import get_app_subdir, safe_str


logger = logging.getLogger(__name__)
_WORKFLOW_INPUT_LOG_LIMIT = 12000


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return safe_str(value).strip().lower() in {'true', '1', 'on', 'yes'}


def _normalize_upload_target(value: Any) -> str:
    target = safe_str(value).strip().lower()
    return target if target in {'acfun', 'bilibili', 'both'} else 'bilibili'


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    if not isinstance(value, list):
        return []
    result = []
    seen = set()
    for item in value:
        tag = safe_str(item).strip()[:20]
        lowered = tag.lower()
        if not tag or lowered in seen:
            continue
        seen.add(lowered)
        result.append(tag)
        if len(result) >= 6:
            break
    return result


def _load_source_metadata(task: Mapping[str, Any]) -> Dict[str, Any]:
    metadata_path = safe_str(task.get('metadata_json_path_local')).strip()
    if metadata_path and os.path.isfile(metadata_path):
        try:
            with open(metadata_path, 'r', encoding='utf-8') as metadata_file:
                metadata = json.load(metadata_file)
            if isinstance(metadata, dict):
                return metadata
        except Exception as exc:
            logger.warning("编辑页 AI 生成读取原始元数据失败: %s", exc)
    return {
        'webpage_url': task.get('youtube_url', ''),
        'title': task.get('video_title_original', ''),
        'description': task.get('description_original', ''),
    }


def _load_acfun_partitions() -> list:
    mapping_path = os.path.join(get_app_subdir('acfunid'), 'id_mapping.json')
    try:
        with open(mapping_path, 'r', encoding='utf-8') as mapping_file:
            mapping = json.load(mapping_file)
        return mapping if isinstance(mapping, list) else []
    except Exception as exc:
        logger.warning("编辑页 AI 生成读取 AcFun 分区失败: %s", exc)
        return []


def _build_openai_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        'OPENAI_API_KEY': config.get('OPENAI_API_KEY', ''),
        'OPENAI_BASE_URL': config.get('OPENAI_BASE_URL', ''),
        'OPENAI_MODEL_NAME': config.get('OPENAI_MODEL_NAME', 'gpt-3.5-turbo'),
        'OPENAI_THINKING_ENABLED': _as_bool(config.get('OPENAI_THINKING_ENABLED', False)),
        'OPENAI_TIMEOUT_SECONDS': config.get('OPENAI_TIMEOUT_SECONDS', 600),
        'FIXED_PARTITION_ID': config.get('FIXED_PARTITION_ID', ''),
        'FIXED_PARTITION_ID_BILIBILI': config.get('FIXED_PARTITION_ID_BILIBILI', ''),
    }


def _partition_result(selection: Any) -> Dict[str, Any]:
    selection = selection if isinstance(selection, dict) else {}
    return {
        'id': safe_str(selection.get('id')).strip(),
        'source': safe_str(selection.get('source')).strip(),
        'confidence': selection.get('confidence'),
        'reason': safe_str(selection.get('reason_summary')).strip(),
    }


def generate_edit_page_metadata(
    task: Mapping[str, Any],
    config: Mapping[str, Any],
    current_fields: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """生成编辑表单字段但不写数据库，由用户确认后再保存。"""
    current_fields = dict(current_fields or {})
    task_id = safe_str(task.get('id') or 'unknown').strip()
    task_logger = setup_task_logger(f'{task_id}_edit')
    openai_config = _build_openai_config(config)
    upload_target = _normalize_upload_target(task.get('upload_target'))
    task_logger.info(
        "编辑页AI自动生成开始 | task_id=%s | upload_target=%s | model=%s",
        task_id,
        upload_target,
        safe_str(openai_config.get('OPENAI_MODEL_NAME')),
    )
    if not safe_str(openai_config.get('OPENAI_API_KEY')).strip():
        task_logger.warning("编辑页AI自动生成终止 | task_id=%s | OpenAI API 密钥未配置", task_id)
        return {'success': False, 'message': 'OpenAI API 密钥未配置'}

    title_limit = 80 if upload_target == 'bilibili' else 50
    description_limit = 2000 if upload_target == 'bilibili' else 1000
    source_metadata = _load_source_metadata(task)
    current_tags = _normalize_tags(current_fields.get('tags'))
    current_metadata = {
        'youtube_url': task.get('youtube_url', ''),
        'upload_target': upload_target,
        'video_title_original': task.get('video_title_original', ''),
        'description_original': task.get('description_original', ''),
        'video_title_current': current_fields.get('title', ''),
        'description_current': current_fields.get('description', ''),
        'tags_current': current_tags,
    }
    try:
        input_text = json.dumps({
            'source_metadata': source_metadata,
            'current_metadata': current_metadata,
        }, ensure_ascii=False, default=safe_str)
    except Exception:
        input_text = safe_str(current_metadata)
    if len(input_text) <= _WORKFLOW_INPUT_LOG_LIMIT:
        task_logger.info("编辑页AI自动生成输入数据\n%s", input_text)
    else:
        task_logger.info(
            "编辑页AI自动生成输入数据已省略 | input_chars=%d | log_limit=%d",
            len(input_text),
            _WORKFLOW_INPUT_LOG_LIMIT,
        )

    task_logger.info("编辑页AI阶段开始 | stage=标题和描述")
    generated = generate_bilibili_title_description(
        source_metadata,
        current_metadata=current_metadata,
        openai_config=openai_config,
        task_id=f"{task.get('id', 'unknown')}_edit",
        title_limit=title_limit,
        description_limit=description_limit,
    )
    task_logger.info(
        "编辑页AI阶段结束 | stage=标题和描述 | result=\n%s",
        json.dumps(generated, ensure_ascii=False, default=safe_str),
    )
    if not generated.get('success'):
        task_logger.warning("编辑页AI自动生成失败 | stage=标题和描述")
        return {
            'success': False,
            'message': generated.get('error_message') or 'AI 未能生成有效的标题和描述',
        }

    title = safe_str(generated.get('title')).strip()
    description = safe_str(generated.get('description')).strip()
    task_logger.info("编辑页AI阶段开始 | stage=标签")
    tags = _normalize_tags(generate_acfun_tags(
        title,
        description,
        openai_config=openai_config,
        task_id=f"{task.get('id', 'unknown')}_edit",
    ))
    task_logger.info(
        "编辑页AI阶段结束 | stage=标签 | result=\n%s",
        json.dumps(tags, ensure_ascii=False),
    )

    common_partition_kwargs = {
        'title_original': safe_str(task.get('video_title_original')),
        'description_original': safe_str(task.get('description_original')),
        'title_translated': title,
        'description_translated': description,
        'tags': tags,
        'openai_config': openai_config,
        'task_id': f"{task.get('id', 'unknown')}_edit",
        'cover_path': task.get('cover_path_local', ''),
        'include_cover_for_ai': _as_bool(config.get('RECOMMEND_PARTITION_WITH_COVER', False)),
    }
    partitions: Dict[str, Dict[str, Any]] = {}
    warnings = []
    if not tags:
        warnings.append('AI 未返回有效标签')

    task_logger.info("编辑页AI阶段开始 | stage=分区推荐 | upload_target=%s", upload_target)
    try:
        if upload_target == 'both':
            from .bilibili_zones import get_zone_list_sub

            selections = recommend_partitions_aio(
                title,
                description,
                acfun_id_mapping_data=_load_acfun_partitions(),
                bilibili_zone_data=get_zone_list_sub(),
                **common_partition_kwargs,
            )
            partitions = {
                platform: _partition_result(selections.get(platform))
                for platform in ('acfun', 'bilibili')
            }
        elif upload_target == 'bilibili':
            from .bilibili_zones import get_zone_list_sub

            partitions['bilibili'] = _partition_result(recommend_bilibili_partition(
                title,
                description,
                get_zone_list_sub(),
                **common_partition_kwargs,
            ))
        else:
            partitions['acfun'] = _partition_result(recommend_acfun_partition(
                title,
                description,
                _load_acfun_partitions(),
                **common_partition_kwargs,
            ))
    except Exception as exc:
        logger.exception("编辑页 AI 分区推荐失败")
        task_logger.exception("编辑页AI阶段失败 | stage=分区推荐")
        warnings.append(f'分区推荐失败：{safe_str(exc)}')

    task_logger.info(
        "编辑页AI阶段结束 | stage=分区推荐 | result=\n%s",
        json.dumps(partitions, ensure_ascii=False, default=safe_str),
    )

    for platform in ('acfun', 'bilibili'):
        if platform in partitions and not partitions[platform].get('id'):
            warnings.append(f'{platform} 未匹配到有效分区')

    result = {
        'success': True,
        'complete': not warnings,
        'title': title,
        'description': description,
        'tags': tags,
        'partitions': partitions,
        'warnings': warnings,
    }
    task_logger.info(
        "编辑页AI自动生成结束 | task_id=%s | complete=%s | result=\n%s",
        task_id,
        result['complete'],
        json.dumps(result, ensure_ascii=False, default=safe_str),
    )
    return result
