#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
try:
    from PIL import Image
except ImportError:
    Image = None

def get_app_root_dir():
    """
    获取应用根目录，兼容开发环境和打包环境
    
    Returns:
        str: 应用根目录路径
    """
    if getattr(sys, 'frozen', False):
        # 在PyInstaller打包环境中
        # sys.executable 指向的是实际的可执行文件
        app_root = os.path.dirname(sys.executable)
    else:
        # 在开发环境中
        # __file__ 是当前文件的路径，需要向上两级找到项目根目录
        app_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    return app_root

def get_app_subdir(subdir_name):
    """
    获取应用子目录路径
    
    Args:
        subdir_name (str): 子目录名称，如 'config', 'logs', 'db' 等
        
    Returns:
        str: 子目录的完整路径
    """
    return os.path.join(get_app_root_dir(), subdir_name)

import re
import copy
import json
from typing import Any, Optional
from urllib.parse import urlparse

def process_cover(image_path, output_path=None, mode='crop'):
    """
    处理视频封面图片，使其适合AcFun上传要求（16:10比例）
    
    Args:
        image_path (str): 输入图片路径
        output_path (str, optional): 输出图片路径，如果不提供则覆盖原图片
        mode (str): 处理模式，'crop'表示裁剪，'pad'表示添加黑边
        
    Returns:
        str: 处理后的图片路径
    """
    if not output_path:
        output_path = image_path
        
    try:
        # 打开图片
        img = Image.open(image_path)
        width, height = img.size
        
        # 目标比例 16:10
        target_ratio = 16 / 10
        current_ratio = width / height
        
        if mode == 'crop':
            # 裁剪模式
            if current_ratio > target_ratio:
                # 图片太宽，需要裁剪宽度
                new_width = int(height * target_ratio)
                left = (width - new_width) // 2
                img = img.crop((left, 0, left + new_width, height))
            elif current_ratio < target_ratio:
                # 图片太高，需要裁剪高度
                new_height = int(width / target_ratio)
                top = (height - new_height) // 2
                img = img.crop((0, top, width, top + new_height))
        elif mode == 'pad':
            # 填充模式
            if current_ratio > target_ratio:
                # 图片太宽，需要增加高度
                new_height = int(width / target_ratio)
                new_img = Image.new('RGB', (width, new_height), (0, 0, 0))
                paste_y = (new_height - height) // 2
                new_img.paste(img, (0, paste_y))
                img = new_img
            elif current_ratio < target_ratio:
                # 图片太高，需要增加宽度
                new_width = int(height * target_ratio)
                new_img = Image.new('RGB', (new_width, height), (0, 0, 0))
                paste_x = (new_width - width) // 2
                new_img.paste(img, (paste_x, 0))
                img = new_img
        
        # 保存处理后的图片
        img.save(output_path, quality=95)
        return output_path
    except Exception as e:
        print(f"处理封面图片时出错: {str(e)}")
        return image_path 

# -----------------------------
# LLM 输出清洗与兼容辅助函数
# -----------------------------

# Pre-compiled regex patterns for strip_reasoning_thoughts (performance optimization)
_THINK_TAG_RE = re.compile(r'<\s*think\s*>.*?<\s*/\s*think\s*>', re.IGNORECASE | re.DOTALL)
_THINK_BLOCK_RE = re.compile(r'```\s*think[^\n]*\n.*?```', re.IGNORECASE | re.DOTALL)
_CODE_FENCE_RE = re.compile(r'^```[a-zA-Z0-9_-]*\s*|\s*```$', re.DOTALL)

def strip_reasoning_thoughts(text):
    """
    屏蔽/移除思考模型产出的思考内容，仅保留最终答案。
    - 兼容 DeepSeek 的 <think>...</think> 标签
    - 兼容 ```think ...``` 代码块形式

    Args:
        text (str): 原始模型输出

    Returns:
        str: 已移除思考内容的纯净文本
    """
    try:
        if not isinstance(text, str):
            return text

        cleaned = text

        # 移除 <think>...</think>（大小写不敏感，跨行匹配）
        cleaned = _THINK_TAG_RE.sub('', cleaned)

        # 移除 ```think ...``` 样式的思考内容代码块（仅当语言标记包含 think 时）
        cleaned = _THINK_BLOCK_RE.sub('', cleaned)

        # 去除多余空白
        cleaned = cleaned.strip()
        return cleaned
    except Exception:
        return text


def strip_code_fences(text):
    """移除 Markdown 代码块围栏。"""
    try:
        if not isinstance(text, str):
            return text
        cleaned = text.strip()
        if cleaned.startswith('```'):
            cleaned = _CODE_FENCE_RE.sub('', cleaned)
        return cleaned.strip()
    except Exception:
        return text

def safe_str(value, default=''):
    """
    将任意值安全转换为字符串，如果为 None 则返回默认值（默认为空字符串）。

    Args:
        value: 可能为 None 或其他类型的值
        default: 当 value 为 None 或空时返回的默认字符串

    Returns:
        str: 安全的字符串表示
    """
    try:
        if value is None:
            return default
        # 如果已经是字符串，直接返回（保持原样）
        if isinstance(value, str):
            return value
        # 否则尝试转换为字符串
        return str(value)
    except Exception:
        return default


def _extract_all_balanced_json_blocks(text, start_char='{', end_char='}'):
    """提取文本中所有平衡的 JSON 字符串块。"""
    blocks = []
    i = 0
    while i < len(text):
        if text[i] == start_char:
            start = i
            depth = 0
            in_string = False
            escaped = False
            found_end = False
            for j in range(start, len(text)):
                char = text[j]
                if escaped:
                    escaped = False
                    continue
                if char == '\\':
                    escaped = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if char == start_char:
                    depth += 1
                elif char == end_char:
                    depth -= 1
                    if depth == 0:
                        blocks.append(text[start:j + 1])
                        i = j + 1
                        found_end = True
                        break
            if not found_end:
                i += 1
        else:
            i += 1
    return blocks


def extract_json_from_text(text, expected_type=None):
    """从文本中提取 JSON，兼容 reasoning/代码块/包裹文本。"""
    raw = strip_code_fences(strip_reasoning_thoughts(safe_str(text))).strip()
    if not raw:
        return None

    candidates = [raw]
    for start_char, end_char in (('{', '}'), ('[', ']')):
        blocks = _extract_all_balanced_json_blocks(raw, start_char, end_char)
        for block in blocks:
            if block and block not in candidates:
                candidates.append(block)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if expected_type is not None and not isinstance(parsed, expected_type):
            continue
        # 过滤模型镜像反射用户输入的伪 JSON（即包含入参键但缺乏实际输出键）
        if isinstance(parsed, dict):
            is_input_mirror = ('target_platform' in parsed or 'source_metadata' in parsed) and not any(
                k in parsed for k in ('title', 'description', 'tags', 'keywords', 'summary', 'video_title', 'bilibili_title', 'desc', 'intro')
            )
            if is_input_mirror:
                continue
        return parsed
    return None


def get_chat_message_text(message) -> str:
    """提取 chat.completions message 的纯文本内容。"""
    if message is None:
        return ''

    content = getattr(message, 'content', None)
    if isinstance(content, list):
        parts = []
        for segment in content:
            if isinstance(segment, dict):
                parts.append(safe_str(segment.get('text')))
            else:
                parts.append(safe_str(getattr(segment, 'text', '')))
        text = ''.join(parts)
    else:
        text = safe_str(content) or safe_str(getattr(message, 'reasoning_content', ''))

    return strip_code_fences(strip_reasoning_thoughts(text)).strip()


def extract_chat_message_json(message, expected_type=dict):
    """优先读取 message.parsed，失败时从文本中提取 JSON。"""
    parsed = getattr(message, 'parsed', None)
    if expected_type is None:
        if isinstance(parsed, (dict, list)):
            return parsed
    elif isinstance(parsed, expected_type):
        return parsed

    return extract_json_from_text(
        get_chat_message_text(message),
        expected_type=expected_type,
    )


_THINKING_FALLBACK_WARNED_SCENES = set()
_THINKING_FALLBACK_WARNED_SCENES_MAX = 128
_KNOWN_UNSUPPORTED_THINKING_PARAM = set()
_KNOWN_UNSUPPORTED_THINKING_PARAM_MAX = 256


def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _mask_base_url(base_url):
    text = safe_str(base_url).strip()
    if not text:
        return 'unknown'
    try:
        parsed = urlparse(text)
        if parsed.scheme and parsed.hostname:
            if parsed.port:
                return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
            return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        pass
    return 'configured-endpoint'


def _is_thinking_param_unsupported_error(exc):
    text = safe_str(exc).lower()
    # 排除与思考参数无关的地理位置、区域、网络、配额或鉴权错误，避免误判
    if any(k in text for k in ('location', 'region', 'country', 'geoblock', 'blocked', 'quota', 'credit', 'unauthorized', 'permission', 'failed_precondition')):
        return False

    signals = (
        'unknown parameter',
        'unrecognized parameter',
        'unrecognized request argument',
        'unsupported parameter',
        'parameter is unsupported',
        'invalid parameter',
        'extra_body',
        'reasoning_effort',
        'thinking',
        'not permitted',
        'invalid json payload received',
    )
    return any(sig in text for sig in signals)


def _invoke_chat_create_with_rate_limit_retry(
    client,
    kwargs,
    logger=None,
    max_retries=5,
    is_thinking_probe=False,
):
    """底层调用 AI 接口，支持失败时自动重试，每次重试间隔 5-10s，最多重试 max_retries 次（默认 5 次）。
    如果是思考参数试探请求且模型确实不支持思考参数，则立即抛出异常以便快速降级，不进行重试。
    """
    import random
    import time
    import re
    import logging

    log = logger or logging.getLogger('ai_client')
    attempts = 0
    while True:
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            # 若处于思考参数试探阶段且明确是不支持思考参数，立即向上抛出以触发降级，不浪费时间重试
            if is_thinking_probe and _is_thinking_param_unsupported_error(exc):
                raise

            err_text = safe_str(exc)
            exc_name = type(exc).__name__

            # 若是每日配额耗尽（如 Google Free Tier 每日 20 次限制: GenerateRequestsPerDay），重试必定失败，立即向上抛出以快速触发模型故障转移
            if 'perday' in err_text.lower() or 'generaterequestsperday' in err_text.lower():
                log.warning(
                    "模型已耗尽今日免费配额 (PerDay Quota Exceeded)，跳过本地重试，立即触发备用模型切换: %s: %s",
                    exc_name,
                    err_text[:120],
                )
                raise

            attempts += 1

            if attempts <= max_retries:
                # 随机 5-10 秒等待时间，防止固定间隔重试以及多任务并发重试冲突
                wait_sec = round(random.uniform(5.0, 10.0), 2)
                # 若服务端明确返回了 429 retry-after 时间且更长，则在合理范围内采用该时间
                match = re.search(r'retry\s+(?:in\s+)?(\d+(?:\.\d+)?)\s*s', err_text, re.IGNORECASE)
                if match:
                    header_wait = float(match.group(1))
                    if header_wait <= 15.0:
                        wait_sec = max(wait_sec, header_wait)

                log.warning(
                    "调用 AI 接口失败 (原因: %s: %s)，将在 %.1f 秒后进行第 %d/%d 次重试...",
                    exc_name,
                    err_text,
                    wait_sec,
                    attempts,
                    max_retries,
                )
                time.sleep(wait_sec)
                continue

            log.error(
                "调用 AI 接口已达最大重试次数 (%d次)，最终失败: %s: %s",
                max_retries,
                exc_name,
                err_text,
            )
            raise


def openai_chat_create_with_thinking_control(
    client,
    create_kwargs,
    thinking_enabled=False,
    logger=None,
    scene_name='unknown',
    max_retries=5,
):
    """统一 chat.completions 请求，支持“尝试关闭思考 + 自动降级 + 自动重试”策略。"""
    if _coerce_bool(thinking_enabled, default=False):
        return _invoke_chat_create_with_rate_limit_retry(
            client, create_kwargs, logger=logger, max_retries=max_retries, is_thinking_probe=False
        )

    model_name = safe_str((create_kwargs or {}).get('model'), default='unknown').strip()
    endpoint_label = _mask_base_url(getattr(client, 'base_url', None))
    client_base_url = safe_str(getattr(client, 'base_url', '')).lower()
    model_lower = model_name.lower()

    unsupported_key = f"{endpoint_label}:{model_name}"
    if unsupported_key in _KNOWN_UNSUPPORTED_THINKING_PARAM:
        return _invoke_chat_create_with_rate_limit_retry(
            client, create_kwargs, logger=logger, max_retries=max_retries, is_thinking_probe=False
        )

    is_gemini = 'googleapis.com' in client_base_url or 'gemini' in model_lower
    is_openai_reasoning = any(k in model_lower for k in ('o1-', 'o3-', 'o4-')) or model_lower in ('o1', 'o3', 'o4')

    disabled_kwargs = copy.deepcopy(create_kwargs or {})
    if is_gemini or is_openai_reasoning:
        # Google Gemini OpenAI 接口及 OpenAI o系列支持 reasoning_effort 控制思考预算
        disabled_kwargs['reasoning_effort'] = 'low'
    else:
        # Anthropic 或支持 extra_body.thinking 的服务商
        extra_body = disabled_kwargs.get('extra_body')
        if not isinstance(extra_body, dict):
            extra_body = {}
        extra_body = copy.deepcopy(extra_body)
        thinking_body = extra_body.get('thinking')
        if not isinstance(thinking_body, dict):
            thinking_body = {}
        thinking_body.update({'type': 'disabled', 'enabled': False})
        extra_body['thinking'] = thinking_body
        disabled_kwargs['extra_body'] = extra_body

    try:
        return _invoke_chat_create_with_rate_limit_retry(
            client, disabled_kwargs, logger=logger, max_retries=max_retries, is_thinking_probe=True
        )
    except Exception as exc:
        if not _is_thinking_param_unsupported_error(exc):
            raise

        _KNOWN_UNSUPPORTED_THINKING_PARAM.add(unsupported_key)
        if len(_KNOWN_UNSUPPORTED_THINKING_PARAM) > _KNOWN_UNSUPPORTED_THINKING_PARAM_MAX:
            _KNOWN_UNSUPPORTED_THINKING_PARAM.clear()

        scene = safe_str(scene_name, default='unknown')
        warn_key = f"{scene}:{model_name}:{endpoint_label}"
        if logger:
            if warn_key not in _THINKING_FALLBACK_WARNED_SCENES:
                logger.warning(
                    "模型不支持 thinking 控制参数，已降级为普通请求"
                )
                _THINKING_FALLBACK_WARNED_SCENES.add(warn_key)
                if len(_THINKING_FALLBACK_WARNED_SCENES) > _THINKING_FALLBACK_WARNED_SCENES_MAX:
                    _THINKING_FALLBACK_WARNED_SCENES.clear()
            else:
                logger.debug(
                    "thinking 控制参数不受支持，继续普通请求"
                )
        return _invoke_chat_create_with_rate_limit_retry(
            client, create_kwargs, logger=logger, max_retries=max_retries, is_thinking_probe=False
        )

