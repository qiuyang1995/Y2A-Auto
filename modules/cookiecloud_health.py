#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CookieCloud 凭据有效性检测 + 「按需更新」编排。

设计约定（刻意如此，别改成无脑拉取）：

- 定时任务**只做检测**。本地 cookie 有效时直接跳过，不写任何文件。
- 只有检测判定为「失效」或「本地缺失」时，才向 CookieCloud 拉取并落盘。
- 判定为「无法确定」（探测本身失败、网络问题）时**不更新**，避免把好 cookie
  换成一份来路不明的副本；这类情况只记录，等下一轮再探测。
- 本模块只负责「检测 + 决定是否更新 + 执行更新」，不写配置；状态落库由调用方
  （app.py）负责，便于脱离 Flask 环境做单元测试。
"""

import logging
import os

from .cookiecloud import (
    DEFAULT_TIMEOUT,
    PLATFORM_BILIBILI,
    PLATFORM_YOUTUBE,
    enabled_platforms,
    get_platform_spec,
    iter_platform_specs,
    resolve_platform_output_path,
    try_cookiecloud_platform_sync,
)

logger = logging.getLogger("cookiecloud_health")

# 检测结论
STATE_VALID = "valid"
STATE_INVALID = "invalid"
STATE_MISSING = "missing"
STATE_UNKNOWN = "unknown"

STATE_LABELS = {
    STATE_VALID: "有效",
    STATE_INVALID: "已失效",
    STATE_MISSING: "本地文件缺失",
    STATE_UNKNOWN: "无法判定",
}

# 判定为「需要更新」的结论
STATES_NEEDING_UPDATE = (STATE_INVALID, STATE_MISSING)
_STATES_NEEDING_UPDATE = STATES_NEEDING_UPDATE

# yt-dlp 官方测试视频，长期可用且无需登录即可解析，用来探测 YouTube 侧的
# cookie 是否被风控拦下。可通过 COOKIECLOUD_YOUTUBE_PROBE_URL 覆盖。
DEFAULT_YOUTUBE_PROBE_URL = "https://www.youtube.com/watch?v=BaW_jenozKc"


def _check_youtube_cookie(cookie_file: str, settings: dict) -> tuple[str, str]:
    """用 yt-dlp 探一次公开视频：被风控拦下→失效，其它错误→无法判定。"""
    if not cookie_file or not os.path.isfile(cookie_file):
        return STATE_MISSING, "本地 YouTube Cookies 文件不存在"

    probe_url = str(settings.get("COOKIECLOUD_YOUTUBE_PROBE_URL") or "").strip()
    if not probe_url:
        probe_url = DEFAULT_YOUTUBE_PROBE_URL

    # 延迟导入：youtube_handler 依赖链较重，且与本模块存在间接引用关系
    from .youtube_handler import (
        _find_yt_dlp_command,
        _looks_like_youtube_bot_challenge,
        test_video_availability,
    )

    yt_dlp_cmd = _find_yt_dlp_command(logger)
    available, _formats, error_msg = test_video_availability(
        probe_url, yt_dlp_cmd, cookie_file, logger,
    )
    if available:
        return STATE_VALID, "YouTube Cookies 探测通过"

    error_text = str(error_msg or "").strip()
    if _looks_like_youtube_bot_challenge(error_text):
        return STATE_INVALID, f"YouTube 判定为风控/登录失效: {error_text[:200]}"
    # 探测视频被删、网络抖动等都不该触发 cookie 更新
    return STATE_UNKNOWN, f"探测未通过但不像风控，暂不更新: {error_text[:200]}"


def _check_bilibili_cookie(cookie_file: str, settings: dict) -> tuple[str, str]:
    """走官方 nav 接口校验登录态：isLogin=false 即为失效。"""
    if not cookie_file or not os.path.isfile(cookie_file):
        return STATE_MISSING, "本地 Bilibili Cookies 文件不存在"

    from .bilibili_auth import (
        build_credential,
        load_cookie_dict,
        validate_credential,
        validate_credential_remote,
    )

    try:
        cookies = load_cookie_dict(cookie_file)
    except Exception as exc:
        return STATE_INVALID, f"Bilibili Cookies 文件无法解析: {exc}"

    credential = build_credential(cookies)
    ok, message = validate_credential(credential)
    if not ok:
        return STATE_INVALID, message

    ok, message = validate_credential_remote(credential)
    if ok:
        return STATE_VALID, message
    return STATE_INVALID, message


_PLATFORM_CHECKERS = {
    PLATFORM_YOUTUBE.key: _check_youtube_cookie,
    PLATFORM_BILIBILI.key: _check_bilibili_cookie,
}


def check_platform_cookie(
    platform,
    settings: dict | None = None,
    *,
    cookie_file: str | None = None,
) -> tuple[str, str]:
    """检测单个平台的 cookie 状态，返回 (结论, 说明)。

    结论取值见 STATE_*；任何异常都会收敛成 STATE_UNKNOWN，不向上抛。
    """
    spec = get_platform_spec(platform)
    effective = dict(settings or {})
    try:
        target = cookie_file or resolve_platform_output_path(effective, spec.key)
    except Exception as exc:
        return STATE_UNKNOWN, f"解析 {spec.label} Cookies 路径失败: {exc}"

    checker = _PLATFORM_CHECKERS.get(spec.key)
    if checker is None:  # pragma: no cover - 平台与检测器一一对应
        return STATE_UNKNOWN, f"{spec.label} 未注册有效性检测器"

    try:
        return checker(target, effective)
    except Exception as exc:
        logger.warning("检测 %s Cookies 有效性时出错: %s", spec.label, exc)
        return STATE_UNKNOWN, f"{spec.label} 有效性检测异常: {exc}"


def _preflight_error(settings: dict, require_writable: bool = True) -> str | None:
    """返回阻止整轮检测的原因；None 表示可以继续。

    require_writable=True（定时任务）时，未允许明文导出就直接跳过——检测出来也修不了。
    require_writable=False（手动巡检）时仍然执行，便于先看体检结论。
    """
    def _truthy(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")

    if not _truthy(settings.get("COOKIECLOUD_ENABLED", False)):
        return "CookieCloud 未启用"
    if require_writable and not _truthy(settings.get("COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT", False)):
        return "CookieCloud 未允许明文导出，无法写入本地文件"
    return None


def run_health_check(
    settings: dict | None = None,
    *,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session=None,
    require_writable: bool = True,
) -> dict:
    """检测各启用平台的 cookie，仅在失效时从 CookieCloud 更新。

    返回值结构（供调用方记录状态与展示）::

        {
          "checked": bool,            # 是否真的执行了检测
          "reason": str | None,       # 未执行时的原因
          "platforms": {
              "youtube": {"state", "state_label", "message", "updated", "update_message", "output_path"},
              ...
          },
          "updated": ["youtube"],     # 本轮真正更新过的平台
          "failed_updates": ["..."],  # 判定失效但更新失败的平台
          "summary": str,             # 一行摘要，便于日志/通知
        }
    """
    effective = dict(settings or {})
    result: dict = {
        "checked": False,
        "reason": None,
        "platforms": {},
        "updated": [],
        "failed_updates": [],
        "summary": "",
    }

    block_reason = _preflight_error(effective, require_writable=require_writable)
    if block_reason:
        result["reason"] = block_reason
        result["summary"] = f"跳过 Cookie 检测：{block_reason}"
        return result

    specs = enabled_platforms(effective)
    if not specs:
        result["reason"] = "未启用任何平台"
        result["summary"] = "跳过 Cookie 检测：未启用任何平台"
        return result

    result["checked"] = True
    parts: list[str] = []

    for spec in specs:
        state, message = check_platform_cookie(spec.key, effective)
        entry: dict = {
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "message": message,
            "updated": False,
            "update_message": "",
            "output_path": "",
        }

        if state in _STATES_NEEDING_UPDATE:
            logger.info(
                "%s Cookies %s（%s），尝试从 CookieCloud 更新",
                spec.label, entry["state_label"], message,
            )
            sync_ok, sync_info = try_cookiecloud_platform_sync(
                effective, spec.key, timeout=timeout, session=session,
            )
            if sync_ok and isinstance(sync_info, dict):
                entry["updated"] = True
                entry["output_path"] = sync_info.get("output_path") or ""
                new_count = sync_info.get("cookie_count")
                changed = sync_info.get("changed")
                entry["update_message"] = (
                    f"已从 CookieCloud 更新 {new_count} 条 Cookies"
                    f"{'（内容有变化）' if changed else '（内容与本地一致）'}"
                )
                result["updated"].append(spec.key)
                logger.info("%s Cookies %s", spec.label, entry["update_message"])
            else:
                entry["update_message"] = f"更新失败: {sync_info}"
                result["failed_updates"].append(spec.key)
                logger.warning("%s Cookies 更新失败: %s", spec.label, sync_info)
            parts.append(f"{spec.label}={entry['state_label']}→{entry['update_message']}")
        else:
            logger.info("%s Cookies %s，本轮跳过更新", spec.label, entry["state_label"])
            parts.append(f"{spec.label}={entry['state_label']}(跳过)")

        result["platforms"][spec.key] = entry

    if result["updated"]:
        result["summary"] = "Cookie 检测完成：" + "；".join(parts)
    else:
        result["summary"] = "Cookie 检测完成，无需更新：" + "；".join(parts)
    return result


def describe_platform_states(result: dict) -> str:
    """把检测结果压成一行可读文本（供设置页/日志使用）。"""
    platforms = (result or {}).get("platforms") or {}
    if not platforms:
        return (result or {}).get("summary") or "未执行检测"
    chunks = []
    for spec in iter_platform_specs():
        entry = platforms.get(spec.key)
        if not entry:
            continue
        tail = entry.get("update_message") or entry.get("message") or ""
        chunks.append(f"{spec.label}: {entry.get('state_label', '')}{('，' + tail) if tail else ''}")
    return "；".join(chunks)
