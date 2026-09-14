#!/usr/bin/env python
# -*- coding: utf-8 -*-

import base64
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlparse, urlunparse

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

COOKIECLOUD_CRYPTO_AUTO = "auto"
COOKIECLOUD_CRYPTO_LEGACY = "legacy"
COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED = "aes-128-cbc-fixed"
SUPPORTED_CRYPTO_TYPES = (
    COOKIECLOUD_CRYPTO_AUTO,
    COOKIECLOUD_CRYPTO_LEGACY,
    COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED,
)
DEFAULT_CRYPTO_TYPE = COOKIECLOUD_CRYPTO_AUTO
DEFAULT_YOUTUBE_COOKIES_PATH = "cookies/yt_cookies.txt"
DEFAULT_BILIBILI_COOKIES_PATH = "cookies/bili_cookies.json"
DEFAULT_TIMEOUT = (5, 20)
AES_BLOCK_SIZE_BITS = 128

# 输出格式：YouTube 走 yt-dlp 需要的 Netscape 格式；B 站走项目自身
# cookies/bili_cookies.json 使用的浏览器导出式 JSON 列表。
OUTPUT_FORMAT_NETSCAPE = "netscape"
OUTPUT_FORMAT_JSON_LIST = "json_list"


class CookieCloudError(RuntimeError):
    """CookieCloud 集成相关错误基类。"""


class CookieCloudConfigError(CookieCloudError):
    """配置无效。"""


class CookieCloudRequestError(CookieCloudError):
    """请求远端服务失败。"""


class CookieCloudDecryptError(CookieCloudError):
    """解密失败。"""


class CookieCloudDataError(CookieCloudError):
    """返回数据不符合预期。"""


@dataclass(frozen=True)
class CookiePlatformSpec:
    """单个平台的 CookieCloud 同步规格。

    把原先散落在各处的 YouTube 专用逻辑收敛成数据描述：域名白名单、
    开关配置键、输出路径与格式、状态记录键。新增平台只需注册一份规格，
    无需改动拉取/解密/落盘等通用流程。
    """

    key: str
    label: str
    # 该平台在 CookieCloud 里的域名白名单（按后缀匹配，含子域）
    base_domains: tuple[str, ...]
    # 单平台开关的配置键（总开关之外）
    enabled_config_key: str
    # 本地输出文件路径的配置键
    output_config_key: str
    default_output_path: str
    output_format: str
    # 同名 cookie 冲突时的优先域名
    preferred_domain: str
    # 最近一次同步状态的配置键顺序：(时间, 状态, 消息)
    status_config_keys: tuple[str, str, str]

    @property
    def empty_message(self) -> str:
        return f"CookieCloud 中未找到可用的 {self.label} Cookies。"


PLATFORM_YOUTUBE = CookiePlatformSpec(
    key="youtube",
    label="YouTube",
    base_domains=("youtube.com", "youtu.be", "google.com"),
    enabled_config_key="COOKIECLOUD_YOUTUBE_ENABLED",
    output_config_key="YOUTUBE_COOKIES_PATH",
    default_output_path=DEFAULT_YOUTUBE_COOKIES_PATH,
    output_format=OUTPUT_FORMAT_NETSCAPE,
    preferred_domain="google.com",
    status_config_keys=(
        "COOKIECLOUD_LAST_SYNC_AT",
        "COOKIECLOUD_LAST_SYNC_STATUS",
        "COOKIECLOUD_LAST_SYNC_MESSAGE",
    ),
)

PLATFORM_BILIBILI = CookiePlatformSpec(
    key="bilibili",
    label="Bilibili",
    base_domains=("bilibili.com", "bilibili.cn"),
    enabled_config_key="COOKIECLOUD_BILIBILI_ENABLED",
    output_config_key="BILIBILI_COOKIES_PATH",
    default_output_path=DEFAULT_BILIBILI_COOKIES_PATH,
    output_format=OUTPUT_FORMAT_JSON_LIST,
    preferred_domain="bilibili.com",
    status_config_keys=(
        "COOKIECLOUD_BILIBILI_LAST_SYNC_AT",
        "COOKIECLOUD_BILIBILI_LAST_SYNC_STATUS",
        "COOKIECLOUD_BILIBILI_LAST_SYNC_MESSAGE",
    ),
)

_PLATFORM_SPECS: dict[str, CookiePlatformSpec] = {
    PLATFORM_YOUTUBE.key: PLATFORM_YOUTUBE,
    PLATFORM_BILIBILI.key: PLATFORM_BILIBILI,
}

SUPPORTED_PLATFORMS = tuple(_PLATFORM_SPECS.keys())
DEFAULT_PLATFORM = PLATFORM_YOUTUBE.key


def iter_platform_specs() -> tuple[CookiePlatformSpec, ...]:
    return tuple(_PLATFORM_SPECS.values())


def get_platform_spec(platform: Any) -> CookiePlatformSpec:
    key = str(platform or "").strip().lower() or DEFAULT_PLATFORM
    spec = _PLATFORM_SPECS.get(key)
    if spec is None:
        raise CookieCloudConfigError(f"不支持的 CookieCloud 平台: {platform}")
    return spec


def normalize_platform(platform: Any) -> str:
    return get_platform_spec(platform).key


def _get_app_root_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _coerce_text(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_cookie_field(value: Any) -> str:
    return str(value or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def normalize_crypto_type(value: Any) -> str:
    normalized = _coerce_text(value).lower() or DEFAULT_CRYPTO_TYPE
    if normalized not in SUPPORTED_CRYPTO_TYPES:
        return DEFAULT_CRYPTO_TYPE
    return normalized


def normalize_server_url(server_url: Any) -> str:
    text = _coerce_text(server_url)
    if not text:
        raise CookieCloudConfigError("请先填写 CookieCloud 服务地址。")

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CookieCloudConfigError("CookieCloud 服务地址格式无效，仅支持 http/https。")

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        (parsed.path or "").rstrip("/"),
        "",
        "",
        "",
    ))


def validate_cookiecloud_settings(settings: dict[str, Any] | None, require_enabled: bool = True) -> dict[str, Any]:
    normalized = dict(settings or {})
    enabled = _as_bool(normalized.get("COOKIECLOUD_ENABLED", False))
    if require_enabled and not enabled:
        raise CookieCloudConfigError("请先启用 CookieCloud。")

    server_url = normalize_server_url(normalized.get("COOKIECLOUD_SERVER_URL", ""))
    cc_user = _coerce_text(normalized.get("COOKIECLOUD_UUID", ""))
    cc_key = _coerce_text(normalized.get("COOKIECLOUD_PASSWORD", ""))
    if not cc_user:
        raise CookieCloudConfigError("请先填写 CookieCloud UUID。")
    if not cc_key:
        raise CookieCloudConfigError("请先填写 CookieCloud 密码。")

    result: dict[str, Any] = {
        "COOKIECLOUD_ENABLED": enabled,
        "COOKIECLOUD_SERVER_URL": server_url,
        "COOKIECLOUD_UUID": cc_user,
        "COOKIECLOUD_PASSWORD": cc_key,
        "COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT": _as_bool(
            normalized.get("COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT", False)
        ),
        "COOKIECLOUD_CRYPTO_TYPE": normalize_crypto_type(
            normalized.get("COOKIECLOUD_CRYPTO_TYPE", DEFAULT_CRYPTO_TYPE)
        ),
    }

    # 各平台的启用开关与输出路径（保持 YOUTUBE_COOKIES_PATH 老键名不变）
    for spec in iter_platform_specs():
        default_enabled = spec.key == DEFAULT_PLATFORM
        result[spec.enabled_config_key] = _as_bool(
            normalized.get(spec.enabled_config_key, default_enabled)
        )
        result[spec.output_config_key] = (
            _coerce_text(normalized.get(spec.output_config_key, "")) or spec.default_output_path
        )

    return result


def is_platform_enabled(settings: dict[str, Any] | None, platform: Any) -> bool:
    spec = get_platform_spec(platform)
    return _as_bool((settings or {}).get(spec.enabled_config_key, False))


def enabled_platforms(settings: dict[str, Any] | None) -> tuple[CookiePlatformSpec, ...]:
    return tuple(spec for spec in iter_platform_specs() if is_platform_enabled(settings, spec.key))


def build_cookiecloud_get_url(server_url: str, uuid_value: str, crypto_type: str = DEFAULT_CRYPTO_TYPE) -> str:
    normalized_server_url = normalize_server_url(server_url)
    safe_uuid = quote(_coerce_text(uuid_value), safe="")
    base_url = f"{normalized_server_url}/get/{safe_uuid}"
    normalized_crypto_type = normalize_crypto_type(crypto_type)
    if normalized_crypto_type == COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED:
        return f"{base_url}?crypto_type={COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED}"
    return base_url


def fetch_cookiecloud_payload(
    server_url: str,
    uuid_value: str,
    *,
    crypto_type: str = DEFAULT_CRYPTO_TYPE,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    request_url = build_cookiecloud_get_url(server_url, uuid_value, crypto_type=crypto_type)
    requester = session or requests
    try:
        response = requester.get(request_url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CookieCloudRequestError("CookieCloud 服务请求失败，请检查服务地址与网络连通性。") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise CookieCloudDataError("CookieCloud 返回了无法解析的 JSON 数据。") from exc

    if not isinstance(payload, dict):
        raise CookieCloudDataError("CookieCloud 返回数据格式无效。")
    if not payload.get("encrypted") and not payload.get("cookie_data"):
        raise CookieCloudDataError("CookieCloud 未返回可用的加密 Cookie 数据。")
    return payload


def _derive_cryptojs_key(cc_user: str, cc_key: str) -> bytes:
    """CookieCloud protocol key derivation — MD5 is mandated by the wire format, not used for security."""
    # Construct key input per CookieCloud protocol spec: MD5("{uuid}-{password}")[:16]
    raw = f"{cc_user}-{cc_key}".encode("utf-8")
    md5_hash = hashlib.md5(raw, usedforsecurity=False)  # noqa: S324 — protocol requirement
    return md5_hash.hexdigest()[:16].encode("utf-8")


def _evp_md5_derive(data: bytes, salt: bytes, key_len: int, iv_len: int) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey using MD5 — protocol-mandated KDF, not used for password storage.

    This implements the OpenSSL key derivation function required by the CookieCloud
    wire format. MD5 is mandated by the protocol specification and cannot be replaced.
    The input `data` is already-derived key material (not a raw password).
    """
    total_length = key_len + iv_len
    derived = b""
    block = b""
    while len(derived) < total_length:
        block = hashlib.md5(block + data + salt, usedforsecurity=False).digest()  # noqa: S324
        derived += block
    return derived[:key_len], derived[key_len:total_length]


def _derive_preview_compat_key_seed(cc_user: str, cc_key: str) -> bytes:
    salt = cc_user.encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha256",
        cc_key.encode("utf-8"),
        salt,
        200000,
        dklen=16,
    )


def _preview_compat_pbkdf2_key_iv(key_material: bytes, salt: bytes, key_len: int, iv_len: int) -> tuple[bytes, bytes]:
    total_length = key_len + iv_len
    derived = hashlib.pbkdf2_hmac("sha256", key_material, salt, 200000, dklen=total_length)
    return derived[:key_len], derived[key_len:total_length]


def _aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(AES_BLOCK_SIZE_BITS).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _decrypt_legacy_payload(ciphertext: str, cc_user: str, cc_key: str) -> bytes:
    decoded = base64.b64decode(ciphertext)
    if len(decoded) < 17 or decoded[:8] != b"Salted__":
        raise CookieCloudDecryptError("CookieCloud legacy 密文格式无效。")
    salt = decoded[8:16]
    body = decoded[16:]
    seed = _derive_cryptojs_key(cc_user, cc_key)
    key, iv = _evp_md5_derive(bytes(seed), salt, key_len=32, iv_len=16)
    return _aes_cbc_decrypt(body, key, iv)


def _decrypt_fixed_iv_payload(ciphertext: str, cc_user: str, cc_key: str) -> bytes:
    decoded = base64.b64decode(ciphertext)
    key = _derive_cryptojs_key(cc_user, cc_key)
    fixed_iv = b"\x00" * 16
    return _aes_cbc_decrypt(decoded, bytes(key), fixed_iv)


def _decrypt_legacy_payload_preview_compat(ciphertext: str, cc_user: str, cc_key: str) -> bytes:
    decoded = base64.b64decode(ciphertext)
    if len(decoded) < 17 or decoded[:8] != b"Salted__":
        raise CookieCloudDecryptError("CookieCloud legacy 密文格式无效。")
    salt = decoded[8:16]
    body = decoded[16:]
    seed = _derive_preview_compat_key_seed(cc_user, cc_key)
    key, iv = _preview_compat_pbkdf2_key_iv(seed, salt, key_len=32, iv_len=16)
    return _aes_cbc_decrypt(body, key, iv)


def _decrypt_fixed_iv_payload_preview_compat(ciphertext: str, cc_user: str, cc_key: str) -> bytes:
    decoded = base64.b64decode(ciphertext)
    key = _derive_preview_compat_key_seed(cc_user, cc_key)
    fixed_iv = b"\x00" * 16
    return _aes_cbc_decrypt(decoded, key, fixed_iv)


def _iter_decryptors_for_candidate(candidate: str):
    if candidate == COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED:
        return (
            _decrypt_fixed_iv_payload,
            _decrypt_fixed_iv_payload_preview_compat,
        )
    return (
        _decrypt_legacy_payload,
        _decrypt_legacy_payload_preview_compat,
    )


def _resolve_crypto_candidates(requested_crypto_type: str, payload: dict[str, Any] | None) -> list[str]:
    requested = normalize_crypto_type(requested_crypto_type)
    payload_crypto_type = normalize_crypto_type((payload or {}).get("crypto_type", ""))
    candidates: list[str] = []

    def _push(value: str):
        normalized = normalize_crypto_type(value)
        if normalized == COOKIECLOUD_CRYPTO_AUTO:
            return
        if normalized not in candidates:
            candidates.append(normalized)

    if requested == COOKIECLOUD_CRYPTO_AUTO:
        _push(payload_crypto_type)
        _push(COOKIECLOUD_CRYPTO_LEGACY)
        _push(COOKIECLOUD_CRYPTO_AES_128_CBC_FIXED)
    else:
        _push(requested)
        _push(payload_crypto_type)

    if not candidates:
        candidates.append(COOKIECLOUD_CRYPTO_LEGACY)
    return candidates


def decrypt_cookiecloud_payload(
    payload: dict[str, Any],
    cc_user: str,
    cc_key: str,
    *,
    crypto_type: str = DEFAULT_CRYPTO_TYPE,
) -> tuple[dict[str, Any], str]:
    if not isinstance(payload, dict):
        raise CookieCloudDataError("CookieCloud 返回数据格式无效。")

    if isinstance(payload.get("cookie_data"), dict):
        return payload, normalize_crypto_type(payload.get("crypto_type", crypto_type))

    ciphertext = payload.get("encrypted")
    if not isinstance(ciphertext, str) or not ciphertext.strip():
        raise CookieCloudDataError("CookieCloud 未返回可用的加密 Cookie 数据。")

    last_error: Exception | None = None
    for candidate in _resolve_crypto_candidates(crypto_type, payload):
        for decryptor in _iter_decryptors_for_candidate(candidate):
            try:
                decrypted = decryptor(ciphertext, cc_user, cc_key)
                parsed = json.loads(decrypted.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise CookieCloudDataError("CookieCloud 解密后的数据不是对象。")
                return parsed, candidate
            except Exception as exc:  # pragma: no cover - 自动回退需要保留宽口径
                last_error = exc
                continue

    raise CookieCloudDecryptError("CookieCloud 凭据无效或解密失败，请检查 UUID、密码与加密算法。") from last_error


def _iter_cookie_items(cookie_payload: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    cookie_data = cookie_payload.get("cookie_data")
    if isinstance(cookie_data, dict):
        for bucket, items in cookie_data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    yield str(bucket or ""), item
        return

    if isinstance(cookie_data, list):
        for item in cookie_data:
            if isinstance(item, dict):
                yield "", item


def is_platform_related_domain(platform: Any, domain: str) -> bool:
    spec = get_platform_spec(platform)
    normalized = _sanitize_cookie_field(domain).lower().lstrip(".")
    if not normalized:
        return False
    for base_domain in spec.base_domains:
        if normalized == base_domain:
            return True
        if normalized.endswith(f".{base_domain}"):
            return True
    return False


def _matches_preferred_domain(domain: str, spec: CookiePlatformSpec) -> bool:
    normalized = _sanitize_cookie_field(domain).lower().lstrip(".")
    if not normalized:
        return False
    if normalized == spec.preferred_domain:
        return True
    return normalized.endswith(f".{spec.preferred_domain}")


def _normalize_cookie_domain(domain: str, host_only: bool) -> tuple[str, str]:
    cleaned = _sanitize_cookie_field(domain).lstrip(".").lower()
    if not cleaned:
        return "", "FALSE"
    if host_only:
        return cleaned, "FALSE"
    return f".{cleaned}", "TRUE"


def _sanitize_cookie_value(value: Any) -> str:
    return _sanitize_cookie_field(value)


def _extract_expiration(item: dict[str, Any]) -> int:
    for key in ("expirationDate", "expires", "expiry", "expiration"):
        raw_value = item.get(key)
        if raw_value in (None, ""):
            continue
        try:
            return max(0, int(float(str(raw_value).strip())))
        except (TypeError, ValueError, OverflowError):
            continue
    return 0


def build_youtube_netscape_cookies(cookie_payload: dict[str, Any]) -> tuple[str, int]:
    lines_by_key: dict[tuple[str, str, str], str] = {}
    for bucket, item in _iter_cookie_items(cookie_payload):
        domain = _sanitize_cookie_field(item.get("domain")) or _sanitize_cookie_field(bucket)
        if not domain or not is_platform_related_domain(PLATFORM_YOUTUBE.key, domain):
            continue

        name = _sanitize_cookie_field(item.get("name"))
        if not name:
            continue

        path = _sanitize_cookie_field(item.get("path")) or "/"
        host_only = _as_bool(item.get("hostOnly", False))
        normalized_domain, include_subdomains = _normalize_cookie_domain(domain, host_only)
        if not normalized_domain:
            continue

        secure = "TRUE" if _as_bool(item.get("secure", False)) else "FALSE"
        expires = _extract_expiration(item)
        value = _sanitize_cookie_value(item.get("value", ""))
        line = "\t".join([
            normalized_domain,
            include_subdomains,
            path,
            secure,
            str(expires),
            name,
            value,
        ])
        lines_by_key[(normalized_domain, path, name)] = line

    if not lines_by_key:
        raise CookieCloudDataError(PLATFORM_YOUTUBE.empty_message)

    header = [
        "# Netscape HTTP Cookie File",
        "# Generated by Y2A-Auto CookieCloud integration.",
    ]
    body = [lines_by_key[key] for key in sorted(lines_by_key)]
    content = "\n".join(header + body) + "\n"
    return content, len(body)


def build_bilibili_cookies_json(cookie_payload: dict[str, Any]) -> tuple[str, int]:
    """把 CookieCloud 的 B 站 cookie 输出成项目自身的 JSON 列表格式。

    形状与 `bilibili_auth.save_credential_to_file()` 写出的文件一致
    （`[{"name", "value", "domain", "path"}]`），因此 bilibili_auth 的解析层
    （`_parse_cookies_text` 的 list 分支）无需任何改动即可读取。
    同名 cookie 冲突时优先保留 `.bilibili.com` 下的那份，保证 SESSDATA /
    bili_jct / DedeUserID 不会取到 `.bilibili.cn` 上的旧值。
    """
    spec = PLATFORM_BILIBILI
    chosen: dict[str, tuple[int, str, str, str]] = {}
    for bucket, item in _iter_cookie_items(cookie_payload):
        domain = _sanitize_cookie_field(item.get("domain")) or _sanitize_cookie_field(bucket)
        if not domain or not is_platform_related_domain(spec.key, domain):
            continue

        name = _sanitize_cookie_field(item.get("name"))
        if not name:
            continue

        rank = 0 if _matches_preferred_domain(domain, spec) else 1
        existing = chosen.get(name)
        if existing is not None and existing[0] <= rank:
            continue
        chosen[name] = (
            rank,
            _sanitize_cookie_field(domain).lower().lstrip("."),
            _sanitize_cookie_field(item.get("path")) or "/",
            _sanitize_cookie_value(item.get("value", "")),
        )

    if not chosen:
        raise CookieCloudDataError(spec.empty_message)

    items = [
        {"name": name, "value": entry[3], "domain": entry[1], "path": entry[2]}
        for name, entry in sorted(chosen.items(), key=lambda kv: (kv[1][1], kv[0]))
    ]
    return json.dumps(items, ensure_ascii=False, indent=2) + "\n", len(items)


_PLATFORM_SERIALIZERS: dict[str, Callable[[dict[str, Any]], tuple[str, int]]] = {
    PLATFORM_YOUTUBE.key: build_youtube_netscape_cookies,
    PLATFORM_BILIBILI.key: build_bilibili_cookies_json,
}


def build_platform_cookies(platform: Any, cookie_payload: dict[str, Any]) -> tuple[str, int]:
    spec = get_platform_spec(platform)
    serializer = _PLATFORM_SERIALIZERS.get(spec.key)
    if serializer is None:  # pragma: no cover - 规格与序列化器一一对应
        raise CookieCloudConfigError(f"平台 {spec.label} 未注册输出序列化器。")
    return serializer(cookie_payload)


def resolve_cookie_output_path(path_value: Any, default_relative_path: str = DEFAULT_YOUTUBE_COOKIES_PATH) -> str:
    raw_path = _coerce_text(path_value) or default_relative_path
    app_root = os.path.realpath(_get_app_root_dir())
    resolved = os.path.realpath(raw_path) if os.path.isabs(raw_path) else os.path.realpath(os.path.join(app_root, raw_path))
    try:
        common_path = os.path.commonpath([app_root, resolved])
    except ValueError as exc:
        raise CookieCloudConfigError("Cookie 输出路径无效。") from exc
    if common_path != app_root:
        raise CookieCloudConfigError("Cookie 输出路径必须位于项目目录内。")
    return resolved


def resolve_platform_output_path(
    settings: dict[str, Any] | None,
    platform: Any,
    output_path: str | None = None,
) -> str:
    spec = get_platform_spec(platform)
    raw_path = (
        _coerce_text(output_path)
        or _coerce_text((settings or {}).get(spec.output_config_key, ""))
        or spec.default_output_path
    )
    return resolve_cookie_output_path(raw_path, default_relative_path=spec.default_output_path)


def make_display_path(path_value: str) -> str:
    app_root = os.path.realpath(_get_app_root_dir())
    resolved = os.path.realpath(path_value)
    try:
        common_path = os.path.commonpath([app_root, resolved])
    except ValueError:
        return os.path.basename(resolved)
    if common_path != app_root:
        return os.path.basename(resolved)
    return os.path.relpath(resolved, app_root).replace("\\", "/")


def _fetch_and_build_platform_cookies(
    settings: dict[str, Any] | None,
    platform: Any,
    *,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    spec = get_platform_spec(platform)
    normalized = validate_cookiecloud_settings(settings, require_enabled=True)
    cc_user = str(normalized["COOKIECLOUD_UUID"])
    cc_key = str(normalized["COOKIECLOUD_PASSWORD"])
    payload = fetch_cookiecloud_payload(
        normalized["COOKIECLOUD_SERVER_URL"],
        cc_user,
        crypto_type=normalized["COOKIECLOUD_CRYPTO_TYPE"],
        timeout=timeout,
        session=session,
    )
    decrypted, crypto_type_used = decrypt_cookiecloud_payload(
        payload,
        cc_user,
        cc_key,
        crypto_type=normalized["COOKIECLOUD_CRYPTO_TYPE"],
    )
    content, cookie_count = build_platform_cookies(spec.key, decrypted)
    return {
        "platform": spec.key,
        "platform_label": spec.label,
        "content": content,
        "cookie_count": cookie_count,
        "crypto_type_used": crypto_type_used,
    }


def test_cookiecloud_platform_sync(
    settings: dict[str, Any] | None,
    platform: Any = DEFAULT_PLATFORM,
    *,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """联网校验配置并在内存中解析指定平台的 cookies，不写本地文件。"""
    return _fetch_and_build_platform_cookies(settings, platform, timeout=timeout, session=session)


def test_cookiecloud_youtube_sync(
    settings: dict[str, Any] | None,
    *,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    return test_cookiecloud_platform_sync(
        settings, PLATFORM_YOUTUBE.key, timeout=timeout, session=session,
    )


def _write_cookie_file(target_path: str, cookie_text: str) -> None:
    """Write cookie content to a local file.

    This is intentional plaintext storage of browser cookies (not passwords)
    after the user has explicitly opted in via COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT.
    The file is consumed by yt-dlp (Netscape) or by the platform SDK (JSON).
    """
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8", newline="\n") as file_obj:
        file_obj.write(cookie_text)


def _write_cookie_file_if_changed(target_path: str, cookie_text: str) -> bool:
    """内容无变化时跳过写入，避免无意义刷新挂载盘上的文件。返回是否真的写入。"""
    try:
        if os.path.exists(target_path):
            with open(target_path, "r", encoding="utf-8") as file_obj:
                if file_obj.read() == cookie_text:
                    return False
    except OSError:
        pass
    _write_cookie_file(target_path, cookie_text)
    return True


def sync_cookiecloud_to_platform_file(
    settings: dict[str, Any] | None,
    platform: Any = DEFAULT_PLATFORM,
    *,
    output_path: str | None = None,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    spec = get_platform_spec(platform)
    normalized = validate_cookiecloud_settings(settings, require_enabled=True)
    if not normalized.get("COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT", False):
        raise CookieCloudConfigError(
            "CookieCloud 立即拉取需要显式勾选\u201c允许明文导出\u201d后，才会把 Cookies 写入本地文件。"
        )
    result = _fetch_and_build_platform_cookies(
        normalized, spec.key, timeout=timeout, session=session,
    )
    target_path = resolve_platform_output_path(normalized, spec.key, output_path)
    changed = _write_cookie_file_if_changed(target_path, result["content"])
    result.update({
        "output_path": target_path,
        "output_path_display": make_display_path(target_path),
        "changed": changed,
    })
    return result


def sync_cookiecloud_to_youtube_file(
    settings: dict[str, Any] | None,
    *,
    output_path: str | None = None,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    return sync_cookiecloud_to_platform_file(
        settings,
        PLATFORM_YOUTUBE.key,
        output_path=output_path,
        timeout=timeout,
        session=session,
    )


def try_cookiecloud_platform_sync(
    settings: dict[str, Any] | None,
    platform: Any = DEFAULT_PLATFORM,
    *,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> tuple[bool, dict[str, Any] | str]:
    """尝试通过 CookieCloud 同步指定平台的 cookies 到本地文件。

    用于 cookie 失效时的自动恢复：拉取远端 cookies 并写入本地文件，
    成功后返回 (True, result_dict)，失败返回 (False, error_msg)。
    所有异常内部捕获，不会向调用方抛出。

    仅在 CookieCloud 已启用、对应平台已启用且允许明文导出时执行。
    """
    try:
        spec = get_platform_spec(platform)
        effective_config = dict(settings or {})
        if not _as_bool(effective_config.get("COOKIECLOUD_ENABLED", False)):
            return False, "CookieCloud 未启用"
        if not is_platform_enabled(effective_config, spec.key):
            return False, f"CookieCloud 未启用 {spec.label} 平台"
        if not _as_bool(effective_config.get("COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT", False)):
            return False, "CookieCloud 未允许明文导出"
        result = sync_cookiecloud_to_platform_file(
            effective_config, spec.key, timeout=timeout, session=session,
        )
        return True, result
    except Exception:
        return False, "CookieCloud 同步失败"


def try_cookiecloud_youtube_sync(
    settings: dict[str, Any] | None,
    *,
    timeout: tuple[int, int] = DEFAULT_TIMEOUT,
    session: requests.Session | None = None,
) -> tuple[bool, dict[str, Any] | str]:
    return try_cookiecloud_platform_sync(
        settings, PLATFORM_YOUTUBE.key, timeout=timeout, session=session,
    )
