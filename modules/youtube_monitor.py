#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import logging
import sqlite3
import datetime
from datetime import datetime, timedelta, timezone
import socket
import ssl
from typing import Optional, Dict, List, Any, Union, Tuple
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC
import httplib2
import threading
import time
from urllib.parse import quote, urlsplit
import re
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from logging.handlers import RotatingFileHandler
from modules.task_manager import add_task
from .config_manager import load_config
from .utils import get_app_subdir


def parse_daily_time_points(time_points_str):
    """解析形如 '16:00, 17:00' 或 '06:00,10:00,18:30' 的每日定点时间列表 [(16, 0), (17, 0)]"""
    result = []
    if not time_points_str:
        return result
    for raw in str(time_points_str).replace('，', ',').split(','):
        item = raw.strip()
        if not item:
            continue
        parts = item.split(':')
        if len(parts) == 2:
            try:
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    result.append((h, m))
            except ValueError:
                pass
    return result


def _generate_iter_pages(page, total_pages, left_edge=2, left_current=2, right_current=2, right_edge=2):
    """生成带省略号的分页页码列表，例如 [1, 2, None, 5, 6, 7, 8, None, 19, 20]"""
    pages = []
    last = 0
    for num in range(1, total_pages + 1):
        if num <= left_edge or \
           (page - left_current <= num <= page + right_current) or \
           num > total_pages - right_edge:
            if last + 1 != num:
                pages.append(None)
            pages.append(num)
            last = num
    return pages


def normalize_search_keywords(keywords: str) -> str:
    """规整 YouTube 搜索关键词。
    YouTube Search API 中空格优先级高于竖线 |（空格为隐式 AND），
    当用户使用 | 连接多个多词短语时（如 '아이브 직캠|IVE fancam'），
    未加引号的多词短语会被引擎错误解析为超长跨词 AND 关系。
    本函数将全角 ｜ 转换为半角 |，并自动为每个包含空格且未被引号包裹的短语添加双引号。
    """
    if not keywords or not isinstance(keywords, str):
        return ""
    
    cleaned = keywords.replace('｜', '|').strip()
    if not cleaned:
        return ""
        
    if '|' in cleaned:
        tokens = [t.strip() for t in cleaned.split('|')]
        normalized_tokens = []
        for t in tokens:
            if not t:
                continue
            is_quoted = (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'"))
            if any(c.isspace() for c in t) and not is_quoted:
                normalized_tokens.append(f'"{t}"')
            else:
                normalized_tokens.append(t)
        return '|'.join(normalized_tokens)
    
    return cleaned


def setup_youtube_monitor_logger():
    """设置YouTube监控专用日志"""
    logger = logging.getLogger('Y2A-Auto.YouTube-Monitor')
    
    # 如果已经设置过处理器，直接返回
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    
    # 创建logs目录
    logs_dir = get_app_subdir('logs')
    os.makedirs(logs_dir, exist_ok=True)
    
    # 文件处理器 - 使用轮转日志
    log_file = os.path.join(logs_dir, 'youtube_monitor.log')
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.INFO)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('%(asctime)s - YouTube监控 - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_youtube_monitor_logger()

API_INIT_STATUS_DIRECT_READY = 'direct_ready'
API_INIT_STATUS_PROXY_READY = 'proxy_ready'
API_INIT_STATUS_MISSING_API_KEY = 'missing_api_key'
API_INIT_STATUS_INIT_FAILED = 'init_failed'


def get_api_init_status_message(status_code: Optional[str]) -> str:
    messages = {
        API_INIT_STATUS_DIRECT_READY: 'YouTube API 初始化成功，当前为直连模式',
        API_INIT_STATUS_PROXY_READY: 'YouTube API 初始化成功，独立代理已启用',
        API_INIT_STATUS_MISSING_API_KEY: 'YouTube API 密钥未配置，请先在设置页完成接入。',
        API_INIT_STATUS_INIT_FAILED: 'YouTube API 初始化失败，请检查 API 密钥、代理配置与网络连通性。',
    }
    if status_code is None:
        return 'YouTube API 未初始化，请检查设置。'
    return messages.get(status_code, 'YouTube API 未初始化，请检查设置。')


MONITOR_CONFIG_FIELD_DEFAULTS: Dict[str, Any] = {
    'name': None,
    'enabled': True,
    'monitor_type': 'youtube_search',
    'channel_mode': 'latest',
    'region_code': 'US',
    'category_id': '0',
    'time_period': 7,
    'max_results': 10,
    'min_view_count': 0,
    'min_like_count': 0,
    'min_comment_count': 0,
    'keywords': '',
    'exclude_keywords': '',
    'channel_ids': '',
    'channel_keywords': '',
    'exclude_channel_ids': '',
    'min_duration': 0,
    'max_duration': 0,
    'schedule_type': 'manual',
    'schedule_interval': 120,
    'schedule_time_points': '',
    'order_by': 'viewCount',
    'start_date': '',
    'end_date': '',
    'latest_days': 7,
    'latest_max_results': 20,
    'rate_limit_requests': 100,
    'rate_limit_window': 60,
    'auto_add_to_tasks': False,
    'historical_progress_date': '',
    'historical_offset': 0,
    'video_types': 'video,short,live',
}

MONITOR_CONFIG_DB_FIELDS: Tuple[str, ...] = tuple(MONITOR_CONFIG_FIELD_DEFAULTS.keys())

DEFAULT_AUTO_ENQUEUE_TIME_POINTS = '13:00, 14:00, 15:00, 16:00, 17:00, 18:00, 19:00, 20:00, 21:00, 22:00, 23:00'

AUTO_ENQUEUE_FIELD_DEFAULTS: Dict[str, Any] = {
    'enabled': False,
    'schedule_type': 'daily_times',
    'schedule_time_points': DEFAULT_AUTO_ENQUEUE_TIME_POINTS,
    'schedule_interval': 60,
    'batch_count': 2,
    'order_by': 'view_count',
    'filter_config_id': 0,
    'last_run_time': '',
    'last_run_status': '',
    'updated_time': '',
}


class YouTubeMonitor:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.youtube: Optional[Any] = None
        self.youtube_http: Optional[httplib2.Http] = None
        self.scheduler = BackgroundScheduler()
        self.db_path = os.path.join(get_app_subdir('db'), 'youtube_monitor.db')
        self._last_fetch_had_errors = False
        self._api_proxy_enabled = False
        self._last_api_init_error: Optional[str] = None
        self._init_database()
        self._init_youtube_api()
        
    def _init_database(self):
        """初始化数据库"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        # 检查数据库是否为新建
        is_new_database = not os.path.exists(self.db_path)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 监控配置表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitor_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT 1,
                    region_code TEXT DEFAULT 'US',
                    category_id TEXT DEFAULT '0',
                    time_period INTEGER DEFAULT 7,
                    max_results INTEGER DEFAULT 10,
                    min_view_count INTEGER DEFAULT 0,
                    min_like_count INTEGER DEFAULT 0,
                    min_comment_count INTEGER DEFAULT 0,
                    keywords TEXT DEFAULT '',
                    exclude_keywords TEXT DEFAULT '',
                    channel_ids TEXT DEFAULT '',
                    exclude_channel_ids TEXT DEFAULT '',
                    min_duration INTEGER DEFAULT 0,
                    max_duration INTEGER DEFAULT 0,
                    schedule_type TEXT DEFAULT 'manual',
                    schedule_interval INTEGER DEFAULT 120,
                    order_by TEXT DEFAULT 'viewCount',
                    start_date TEXT DEFAULT '',
                    rate_limit_requests INTEGER DEFAULT 20,
                    rate_limit_window INTEGER DEFAULT 60,
                    last_run_time TEXT,
                    created_time TEXT DEFAULT (datetime('now', 'localtime')),
                    updated_time TEXT DEFAULT (datetime('now', 'localtime'))
                )
            ''')
            
            # 为现有表添加新字段（如果不存在）
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN order_by TEXT DEFAULT 'viewCount'")
            except sqlite3.OperationalError:
                pass  # 字段已存在
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN start_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN rate_limit_requests INTEGER DEFAULT 100")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN rate_limit_window INTEGER DEFAULT 60")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN auto_add_to_tasks BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN schedule_time_points TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            # 添加新的监控类型字段
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN monitor_type TEXT DEFAULT 'youtube_search'")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN channel_mode TEXT DEFAULT 'latest'")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN channel_keywords TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN end_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN latest_days INTEGER DEFAULT 7")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN latest_max_results INTEGER DEFAULT 20")
            except sqlite3.OperationalError:
                pass
            
            # 添加可监控内容类型字段（逗号分隔: video,short,live）
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN video_types TEXT DEFAULT 'video,short,live'")
            except sqlite3.OperationalError:
                pass
            
            # 添加历史搬运进度记录字段
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN historical_progress_date TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            
            # 添加当前时间段处理偏移量字段
            try:
                cursor.execute("ALTER TABLE monitor_configs ADD COLUMN historical_offset INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            # 监控历史表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitor_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id INTEGER,
                    video_id TEXT NOT NULL,
                    video_type TEXT,
                    video_title TEXT,
                    channel_title TEXT,
                    view_count INTEGER,
                    like_count INTEGER,
                    comment_count INTEGER,
                    duration TEXT,
                    published_at TEXT,
                    added_to_tasks BOOLEAN DEFAULT 0,
                    thumbnail_url TEXT,
                    run_time TEXT DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY (config_id) REFERENCES monitor_configs (id)
                )
            ''')
            
            # 为历史表新增 video_type 字段（向后兼容）
            try:
                cursor.execute("ALTER TABLE monitor_history ADD COLUMN video_type TEXT")
            except sqlite3.OperationalError:
                pass

            # 为历史表新增 thumbnail_url 字段（向后兼容）
            try:
                cursor.execute("ALTER TABLE monitor_history ADD COLUMN thumbnail_url TEXT")
            except sqlite3.OperationalError:
                pass

            # 为历史表新增 run_id 字段关联监控执行批次（向后兼容）
            try:
                cursor.execute("ALTER TABLE monitor_history ADD COLUMN run_id INTEGER")
            except sqlite3.OperationalError:
                pass

            # 监控执行历史表 (记录每一次监控运行的元数据、漏斗数据与状态)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id INTEGER,
                    trigger_type TEXT DEFAULT 'scheduled',
                    status TEXT DEFAULT 'running',
                    start_time TEXT DEFAULT (datetime('now', 'localtime')),
                    end_time TEXT,
                    duration_seconds REAL DEFAULT 0,
                    fetched_count INTEGER DEFAULT 0,
                    filtered_count INTEGER DEFAULT 0,
                    new_count INTEGER DEFAULT 0,
                    added_count INTEGER DEFAULT 0,
                    error_message TEXT DEFAULT '',
                    FOREIGN KEY (config_id) REFERENCES monitor_configs (id)
                )
            ''')

            # 定时入队任务配置表 (单例记录 id=1)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitor_auto_enqueue_config (
                    id INTEGER PRIMARY KEY,
                    enabled BOOLEAN DEFAULT 0,
                    schedule_type TEXT DEFAULT 'daily_times',
                    schedule_time_points TEXT DEFAULT '13:00, 14:00, 15:00, 16:00, 17:00, 18:00, 19:00, 20:00, 21:00, 22:00, 23:00',
                    schedule_interval INTEGER DEFAULT 60,
                    batch_count INTEGER DEFAULT 2,
                    order_by TEXT DEFAULT 'view_count',
                    filter_config_id INTEGER DEFAULT 0,
                    last_run_time TEXT DEFAULT '',
                    last_run_status TEXT DEFAULT '',
                    updated_time TEXT DEFAULT (datetime('now', 'localtime'))
                )
            ''')
            cursor.execute("SELECT id FROM monitor_auto_enqueue_config WHERE id = 1")
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO monitor_auto_enqueue_config (
                        id, enabled, schedule_type, schedule_time_points, schedule_interval,
                        batch_count, order_by, filter_config_id, last_run_time, last_run_status, updated_time
                    ) VALUES (1, 0, 'daily_times', ?, 60, 2, 'view_count', 0, '', '', datetime('now', 'localtime'))
                ''', (DEFAULT_AUTO_ENQUEUE_TIME_POINTS,))
            
            conn.commit()
        
        # 如果是新数据库或表为空，尝试从配置文件恢复
        self._restore_configs_from_files()
        self._restore_auto_enqueue_config_from_file()
    
    def _restore_configs_from_files(self):
        """从配置文件恢复监控配置到数据库"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            
            if not os.path.exists(config_dir):
                logger.info("配置文件目录不存在，跳过恢复")
                return
            
            # 检查数据库中是否已有配置
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM monitor_configs')
                existing_count = cursor.fetchone()[0]
                
                if existing_count > 0:
                    logger.info(f"数据库中已有 {existing_count} 个配置，跳过恢复")
                    return
            
            # 扫描配置文件
            config_files = []
            for filename in os.listdir(config_dir):
                if filename.startswith('monitor_config_') and filename.endswith('.json'):
                    config_files.append(filename)
            
            if not config_files:
                logger.info("未找到配置文件，跳过恢复")
                return
            
            logger.info(f"发现 {len(config_files)} 个配置文件，开始恢复到数据库")
            
            restored_count = 0
            for filename in sorted(config_files):
                try:
                    config_file_path = os.path.join(config_dir, filename)
                    with open(config_file_path, 'r', encoding='utf-8') as f:
                        config_data = json.load(f)
                    
                    # 提取配置ID
                    original_config_id = config_data.get('config_id')
                    if not original_config_id:
                        logger.warning(f"配置文件 {filename} 缺少config_id，跳过")
                        continue
                    
                    # 移除不需要插入数据库的字段
                    config_data.pop('config_id', None)
                    config_data.pop('created_time', None)
                    
                    # 恢复到数据库，保持原有ID
                    restored_id = self._restore_single_config(config_data, original_config_id)
                    if restored_id:
                        restored_count += 1
                        logger.info(f"恢复配置: {config_data.get('name', '未命名')} (ID: {original_config_id} -> {restored_id})")
                    
                except Exception as e:
                    logger.error(f"恢复配置文件 {filename} 失败: {str(e)}")
                    continue
            
            if restored_count > 0:
                logger.info(f"成功恢复 {restored_count} 个监控配置")
                
                # 启动已启用的自动调度配置
                self._restart_restored_schedules()
            else:
                logger.warning("没有成功恢复任何配置")
                
        except Exception as e:
            logger.error(f"从配置文件恢复失败: {str(e)}")
    
    def _restore_single_config(self, config_data, target_id):
        """恢复单个配置到数据库，尝试保持原有ID"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 检查目标ID是否可用
                cursor.execute('SELECT id FROM monitor_configs WHERE id = ?', (target_id,))
                if cursor.fetchone():
                    logger.warning(f"ID {target_id} 已存在，使用自动分配的ID")
                    target_id = None

                if target_id:
                    config_id = self._insert_monitor_config_record(cursor, config_data, target_id=target_id)
                else:
                    config_id = self._insert_monitor_config_record(cursor, config_data)
                
                conn.commit()
                return config_id
                
        except Exception as e:
            logger.error(f"恢复单个配置失败: {str(e)}")
            return None
    
    def _restart_restored_schedules(self):
        """重新启动已恢复配置的自动调度"""
        try:
            configs = self.get_monitor_configs()
            restored_schedules = 0
            
            for config in configs:
                if config['enabled'] and config['schedule_type'] == 'auto':
                    self._schedule_monitor(config['id'], config['schedule_interval'])
                    restored_schedules += 1
            
            if restored_schedules > 0:
                logger.info(f"重新启动了 {restored_schedules} 个自动调度任务")
                
                # 启动调度器
                if not self.scheduler.running:
                    self.scheduler.start()
                    
        except Exception as e:
            logger.error(f"重新启动调度任务失败: {str(e)}")
    
    def _normalize_proxy_url(self, proxy_url: str) -> str:
        """标准化代理地址，缺失协议时默认使用 HTTP。"""
        normalized = str(proxy_url or '').strip()
        if not normalized:
            return ''
        if '://' not in normalized:
            normalized = f'http://{normalized}'
        return normalized

    def _build_proxy_url_with_auth(self, proxy_url: str, username: str, password: str) -> str:
        """根据用户名密码构造带认证信息的代理 URL。"""
        normalized = self._normalize_proxy_url(proxy_url)
        if not normalized:
            return ''

        if username and password:
            protocol, rest = normalized.split('://', 1)
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}"
            return f"{protocol}://{auth}@{rest}"
        return normalized

    def _resolve_api_proxy_url(self, runtime_config: Dict[str, Any]) -> Optional[str]:
        """从配置中解析监控 API 独立代理。"""
        if not runtime_config.get('YOUTUBE_API_PROXY_ENABLED', False):
            return None

        proxy_url = self._build_proxy_url_with_auth(
            str(runtime_config.get('YOUTUBE_API_PROXY_URL', '') or ''),
            str(runtime_config.get('YOUTUBE_API_PROXY_USERNAME', '') or '').strip(),
            str(runtime_config.get('YOUTUBE_API_PROXY_PASSWORD', '') or '').strip(),
        )
        if not proxy_url:
            return None

        parsed = urlsplit(proxy_url)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("YouTube 监控 API 代理地址无效，请填写包含主机名的 http:// 或 socks5:// 地址")

        return proxy_url

    def _get_api_init_success_status(self) -> str:
        return API_INIT_STATUS_PROXY_READY if self._api_proxy_enabled else API_INIT_STATUS_DIRECT_READY

    def _build_youtube_http(self, runtime_config: Dict[str, Any]) -> httplib2.Http:
        """构造用于 YouTube Data API 的 HTTP transport。"""
        http_timeout = socket.getdefaulttimeout()
        if http_timeout is None:
            http_timeout = DEFAULT_HTTP_TIMEOUT_SEC

        proxy_url = self._resolve_api_proxy_url(runtime_config)
        self._api_proxy_enabled = bool(proxy_url)

        if proxy_url:
            return httplib2.Http(
                timeout=http_timeout,
                proxy_info=lambda method: httplib2.proxy_info_from_url(proxy_url, method=method),
            )

        return httplib2.Http(timeout=http_timeout, proxy_info=None)

    def _init_youtube_api(self, runtime_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """初始化 YouTube API，并显式控制是否走独立代理。"""
        if runtime_config is None:
            config = dict(load_config() or {})
        else:
            config = dict(runtime_config)
        self.api_key = str(config.get('YOUTUBE_API_KEY') or self.api_key or '').strip()
        self.youtube = None
        self.youtube_http = None
        self._api_proxy_enabled = False
        self._last_api_init_error = None

        if not self.api_key:
            logger.info("YouTube API密钥未配置，跳过监控 API 初始化")
            self._last_api_init_error = API_INIT_STATUS_MISSING_API_KEY
            return False, API_INIT_STATUS_MISSING_API_KEY

        try:
            self.youtube_http = self._build_youtube_http(config)
            self.youtube = build(
                'youtube',
                'v3',
                developerKey=self.api_key,
                http=self.youtube_http,
            )
            status_code = self._get_api_init_success_status()
            self._last_api_init_error = None
            if status_code == API_INIT_STATUS_PROXY_READY:
                logger.info('YouTube API 初始化成功，独立代理已启用')
                return True, API_INIT_STATUS_PROXY_READY
            logger.info('YouTube API 初始化成功，当前为直连模式')
            return True, API_INIT_STATUS_DIRECT_READY
        except Exception:
            self.youtube = None
            self.youtube_http = None
            self._last_api_init_error = API_INIT_STATUS_INIT_FAILED
            logger.error('YouTube API 初始化失败，请检查 API 密钥、代理配置与网络连通性。')
            return False, API_INIT_STATUS_INIT_FAILED

    def reload_api_client(self, runtime_config: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """根据当前配置重建监控 API 客户端。"""
        return self._init_youtube_api(runtime_config)

    def set_api_key(self, api_key: str) -> Tuple[bool, str]:
        """兼容旧调用：仅设置 API 密钥并按当前配置重建客户端。"""
        current_config = dict(load_config() or {})
        current_config['YOUTUBE_API_KEY'] = api_key
        return self._init_youtube_api(current_config)

    def _format_run_error_message(self, error: Exception) -> str:
        """把网络层错误转换为更可操作的监控提示。"""
        if isinstance(error, HttpError):
            return f"监控失败: {str(error)}"

        error_text = str(error).lower()
        network_markers = (
            'timed out',
            'timeout',
            'connection refused',
            'network is unreachable',
            'temporary failure',
            'name or service not known',
            'nodename nor servname',
            'proxy',
            'getaddrinfo',
            'unable to find the server',
            '11001',
            '10060',
        )
        is_network_error = isinstance(error, ssl.SSLError) or (
            isinstance(error, (TimeoutError, socket.timeout, httplib2.HttpLib2Error, OSError))
            and any(marker in error_text for marker in network_markers)
        )

        if is_network_error:
            if self._api_proxy_enabled:
                return (
                    "监控失败：YouTube Data API 网络不可达或请求超时。"
                    "请检查“YouTube 监控 API”代理配置、代理容器状态与目标地址连通性。"
                )
            return (
                "监控失败：YouTube Data API 网络不可达或请求超时。"
                "当前未启用“YouTube 监控 API”代理，请在设置中单独配置该代理，"
                "确认服务器可直连 YouTube Data API，或暂时关闭监控功能。"
            )

        return f"监控失败: {str(error)}"

    def _collect_monitor_config_values(
        self,
        config_data: Dict[str, Any],
        field_default_overrides: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        """按统一字段顺序生成监控配置 SQL 参数，避免多处手写字段列表。"""
        defaults = dict(MONITOR_CONFIG_FIELD_DEFAULTS)
        if field_default_overrides:
            defaults.update(field_default_overrides)
        return [
            config_data.get(field, defaults[field])
            for field in MONITOR_CONFIG_DB_FIELDS
        ]

    def _insert_monitor_config_record(
        self,
        cursor: sqlite3.Cursor,
        config_data: Dict[str, Any],
        target_id: Optional[int] = None,
        field_default_overrides: Optional[Dict[str, Any]] = None,
    ) -> int:
        """插入监控配置记录，可选保留指定 ID 用于恢复旧配置。"""
        fields = list(MONITOR_CONFIG_DB_FIELDS)
        values = self._collect_monitor_config_values(config_data, field_default_overrides)

        if target_id is not None:
            fields.insert(0, 'id')
            values.insert(0, target_id)

        columns_sql = ', '.join(fields)
        placeholders_sql = ', '.join(['?'] * len(fields))
        cursor.execute(
            f'INSERT INTO monitor_configs ({columns_sql}) VALUES ({placeholders_sql})',
            tuple(values)
        )
        if target_id is not None:
            return target_id

        lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise RuntimeError('插入监控配置记录失败：未获取到新记录 ID')
        return lastrowid

    def _update_monitor_config_record(
        self,
        cursor: sqlite3.Cursor,
        config_id: int,
        config_data: Dict[str, Any],
        field_default_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        """按统一字段顺序更新监控配置记录。"""
        assignments_sql = ', '.join(f'{field} = ?' for field in MONITOR_CONFIG_DB_FIELDS)
        values = self._collect_monitor_config_values(config_data, field_default_overrides)
        cursor.execute(
            f"UPDATE monitor_configs SET {assignments_sql}, updated_time = datetime('now', 'localtime') WHERE id = ?",
            tuple(values + [config_id])
        )
    
    def create_monitor_config(self, config_data):
        """创建监控配置"""
        logger.info(f"开始创建监控配置: {config_data.get('name', '未命名')}")
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                config_id = self._insert_monitor_config_record(cursor, config_data)
                conn.commit()
                
                # 如果为频道监控的持续跟进最新模式，则在创建时将基准时间设为当前时间
                try:
                    is_channel_monitor = bool(config_data.get('channel_ids') and str(config_data.get('channel_ids')).strip())
                    if config_data.get('channel_mode') == 'latest' and is_channel_monitor:
                        cursor.execute(
                            "UPDATE monitor_configs SET last_run_time = datetime('now', 'localtime') WHERE id = ?",
                            (config_id,)
                        )
                        conn.commit()
                        logger.info("创建配置：已为持续跟进最新模式设置基准时间为当前时间")
                except Exception as e:
                    logger.warning(f"设置最新跟进基准时间失败: {str(e)}")
                
                logger.info(f"监控配置已创建，ID: {config_id}, 名称: {config_data.get('name')}")
            
            # 保存配置到文件
            self._save_config_to_file(config_id, config_data)
            
            # 如果是自动调度，添加到调度器
            if config_data.get('schedule_type') == 'auto':
                logger.info(f"配置 {config_id} 启用自动调度，间隔: {config_data.get('schedule_interval', 120)}分钟")
                self._schedule_monitor(config_id, config_data.get('schedule_interval', 120))
            
            return config_id
        except Exception as e:
                logger.error(f"创建监控配置失败: {str(e)}")
                raise

    def _save_config_to_file(self, config_id, config_data):
        """保存配置到文件"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            os.makedirs(config_dir, exist_ok=True)

            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")

            # 防止路径遍历攻击：验证路径在config目录内
            config_file_real = os.path.realpath(config_file)
            config_dir_real = os.path.realpath(config_dir)
            if not config_file_real.startswith(config_dir_real + os.sep):
                logger.error(f"配置文件路径不在config目录内，拒绝保存: {config_id}")
                return

            # 添加配置ID到数据中
            config_data_with_id = config_data.copy()
            config_data_with_id['config_id'] = config_id
            config_data_with_id['created_time'] = datetime.now().isoformat()

            with open(config_file_real, 'w', encoding='utf-8') as f:
                json.dump(config_data_with_id, f, ensure_ascii=False, indent=2)

            logger.info(f"监控配置已保存到文件: {config_file_real}")
        except Exception as e:
            logger.error(f"保存配置文件失败: {str(e)}")

    def _load_config_from_file(self, config_id):
        """从文件加载配置"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")

            # 防止路径遍历攻击：验证路径在config目录内
            config_file_real = os.path.realpath(config_file)
            config_dir_real = os.path.realpath(config_dir)
            if not config_file_real.startswith(config_dir_real + os.sep):
                logger.error(f"配置文件路径不在config目录内，拒绝加载: {config_id}")
                return None

            if os.path.exists(config_file_real):
                with open(config_file_real, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载配置文件失败: {str(e)}")
        return None

    def _delete_config_file(self, config_id):
        """删除配置文件"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            config_file = os.path.join(config_dir, f"monitor_config_{config_id}.json")

            # 防止路径遍历攻击：验证路径在config目录内
            config_file_real = os.path.realpath(config_file)
            config_dir_real = os.path.realpath(config_dir)
            if not config_file_real.startswith(config_dir_real + os.sep):
                logger.error(f"配置文件路径不在config目录内，拒绝删除: {config_id}")
                return

            if os.path.exists(config_file_real):
                os.remove(config_file_real)
                logger.info(f"配置文件已删除: {config_file_real}")
        except Exception as e:
            logger.error(f"删除配置文件失败: {str(e)}")

    def get_monitor_configs(self):
        """获取所有监控配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM monitor_configs ORDER BY created_time DESC')

            columns = [description[0] for description in cursor.description]
            configs = []
            for row in cursor.fetchall():
                config = dict(zip(columns, row))
                configs.append(config)

            return configs

    def get_monitor_config(self, config_id):
        """获取指定监控配置"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM monitor_configs WHERE id = ?', (config_id,))

            row = cursor.fetchone()
            if row:
                columns = [description[0] for description in cursor.description]
                return dict(zip(columns, row))
            return None
    
    def update_monitor_config(self, config_id, config_data):
        """更新监控配置"""
        logger.info(f"更新监控配置，ID: {config_id}")
        
        try:
            # 获取原有配置
            old_config = self.get_monitor_config(config_id)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 检查历史搬运模式下时间范围是否发生变化
                should_reset_offset = False
                if (config_data.get('channel_mode') == 'historical' and old_config and 
                    old_config.get('channel_mode') == 'historical'):
                    # 检查开始日期或结束日期是否变化
                    old_start = old_config.get('start_date', '')
                    old_end = old_config.get('end_date', '')
                    new_start = config_data.get('start_date', '')
                    new_end = config_data.get('end_date', '')
                    
                    if old_start != new_start or old_end != new_end:
                        should_reset_offset = True
                        logger.info(f"检测到历史搬运模式时间范围变化，将重置偏移量：{old_start}-{old_end} → {new_start}-{new_end}")
                
                # 如果需要重置偏移量，将其设为0
                if should_reset_offset:
                    config_data['historical_offset'] = 0

                update_field_defaults = {
                    'historical_offset': old_config.get('historical_offset', 0) if old_config and not should_reset_offset else 0,
                    'video_types': old_config.get('video_types', 'video,short,live') if old_config else 'video,short,live',
                }
                self._update_monitor_config_record(cursor, config_id, config_data, update_field_defaults)
                
                conn.commit()
                
                # 如果从其他模式切换为持续跟进最新模式，则将基准时间设为当前时间
                try:
                    became_latest = (
                        config_data.get('channel_mode') == 'latest' and 
                        (not old_config or old_config.get('channel_mode') != 'latest')
                    )
                    is_channel_monitor = bool(config_data.get('channel_ids') and str(config_data.get('channel_ids')).strip())
                    if became_latest and is_channel_monitor:
                        cursor.execute(
                            "UPDATE monitor_configs SET last_run_time = datetime('now', 'localtime') WHERE id = ?",
                            (config_id,)
                        )
                        conn.commit()
                        logger.info("切换为持续跟进最新模式：已设置基准时间为当前时间")
                except Exception as e:
                    logger.warning(f"更新最新跟进基准时间失败: {str(e)}")
                
                if should_reset_offset:
                    logger.info(f"历史搬运偏移量已重置为0")
                
                logger.info(f"监控配置已更新: {config_data.get('name')} (ID: {config_id})")
            
            # 保存配置到文件
            self._save_config_to_file(config_id, config_data)
            
            # 更新调度
            logger.info(f"更新调度设置: {config_data.get('schedule_type', 'manual')}")
            self._update_schedule(config_id, config_data)
                
        except Exception as e:
            logger.error(f"更新监控配置失败，ID: {config_id}, 错误: {str(e)}")
            raise
    
    def delete_monitor_config(self, config_id):
        """删除监控配置"""
        logger.info(f"开始删除监控配置，ID: {config_id}")
        
        try:
            # 获取配置信息用于日志
            config = self.get_monitor_config(config_id)
            config_name = config['name'] if config else f"ID-{config_id}"
            
            # 移除调度
            self._remove_schedule(config_id)
            
            # 删除配置文件
            self._delete_config_file(config_id)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 获取历史记录数量
                cursor.execute('SELECT COUNT(*) FROM monitor_history WHERE config_id = ?', (config_id,))
                history_count = cursor.fetchone()[0]
                
                cursor.execute('DELETE FROM monitor_configs WHERE id = ?', (config_id,))
                cursor.execute('DELETE FROM monitor_history WHERE config_id = ?', (config_id,))
                conn.commit()
                
                logger.info(f"监控配置已删除: {config_name} (ID: {config_id}), 同时删除了 {history_count} 条历史记录")
                
        except Exception as e:
            logger.error(f"删除监控配置失败，ID: {config_id}, 错误: {str(e)}")
            raise
    
    def _start_monitor_run(self, config_id: int, trigger_type: str = 'scheduled') -> Optional[int]:
        """创建监控运行记录并返回 run_id"""
        start_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO monitor_runs (
                        config_id, trigger_type, status, start_time,
                        fetched_count, filtered_count, new_count, added_count, error_message
                    ) VALUES (?, ?, 'running', ?, 0, 0, 0, 0, '')
                ''', (config_id, trigger_type, start_time_str))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"创建监控运行记录失败: {e}")
            return None

    def _finalize_monitor_run(self, run_id: Optional[int], status: str, start_timestamp: float = 0.0,
                              fetched_count: int = 0, filtered_count: int = 0,
                              new_count: int = 0, added_count: int = 0,
                              error_message: str = ''):
        """更新监控运行批次状态与漏斗数据"""
        if not run_id:
            return
        duration_seconds = round(max(0.0, time.time() - start_timestamp), 2) if start_timestamp > 0 else 0.0
        end_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE monitor_runs
                    SET status = ?,
                        end_time = ?,
                        duration_seconds = ?,
                        fetched_count = ?,
                        filtered_count = ?,
                        new_count = ?,
                        added_count = ?,
                        error_message = ?
                    WHERE id = ?
                ''', (
                    status, end_time_str, duration_seconds,
                    fetched_count, filtered_count, new_count, added_count,
                    error_message, run_id
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"更新监控运行记录(ID: {run_id})失败: {e}")

    def run_monitor(self, config_id: int, trigger_type: str = 'scheduled') -> Tuple[bool, str]:
        """执行监控任务"""
        logger.info(f"开始执行监控任务，配置ID: {config_id}, 触发方式: {trigger_type}")
        start_ts = time.time()
        run_id = self._start_monitor_run(config_id, trigger_type=trigger_type)

        # 添加调试日志 - 检查 YouTube API 对象状态
        logger.debug(f"YouTube API 对象状态: {type(self.youtube)}, 值: {self.youtube}")
        if not self.youtube:
            init_status = self._last_api_init_error or API_INIT_STATUS_INIT_FAILED
            if init_status == API_INIT_STATUS_MISSING_API_KEY:
                init_message = 'YouTube API 密钥未配置，请先在设置页完成接入。'
            elif init_status == API_INIT_STATUS_INIT_FAILED:
                init_message = 'YouTube API 初始化失败，请检查 API 密钥、代理配置与网络连通性。'
            else:
                init_message = 'YouTube API 未初始化，请检查设置。'
            logger.error("YouTube API未初始化: %s", init_message)
            self._finalize_monitor_run(run_id, 'failed', start_ts, error_message=init_message)
            return False, f"监控失败：{init_message}"
        
        config = self.get_monitor_config(config_id)
        if not config:
            logger.error(f"监控配置不存在: {config_id}")
            self._finalize_monitor_run(run_id, 'failed', start_ts, error_message='监控配置不存在')
            return False, "监控配置不存在"
        
        logger.info(f"执行监控配置: {config['name']} (ID: {config_id})")
        logger.info(f"监控类型: {config.get('monitor_type', 'youtube_search')}, "
                   f"频道模式: {config.get('channel_mode', 'latest')}")
        
        videos = []
        filtered_videos = []
        processed_count = 0
        added_count = 0
        try:
            # 获取视频
            logger.info("开始获取视频数据...")
            # 每次运行前重置错误标记
            self._last_fetch_had_errors = False
            videos = self._fetch_trending_videos(config)
            logger.info(f"获取到 {len(videos)} 个视频")
            
            # 筛选视频
            logger.info("开始筛选视频...")
            filtered_videos = self._filter_videos(videos, config)
            logger.info(f"筛选后剩余 {len(filtered_videos)} 个视频")
            
            # 历史搬运模式需要考虑偏移量
            if config.get('channel_mode') == 'historical':
                current_offset = config.get('historical_offset', 0)
                if current_offset > 0:
                    logger.info(f"历史搬运模式，跳过前 {current_offset} 个视频")
                    filtered_videos = filtered_videos[current_offset:]
                    logger.info(f"应用偏移量后剩余 {len(filtered_videos)} 个视频")
            
            # 保存到历史记录
            auto_add_enabled = config.get('auto_add_to_tasks', False)
            
            # 获取添加到任务队列的数量限制
            # 所有模式都使用rate_limit_requests来控制每次添加的视频数量
            max_add_to_tasks = config.get('rate_limit_requests', 20) if auto_add_enabled else 0
            
            logger.info(f"开始处理视频，自动添加到任务队列: {'是' if auto_add_enabled else '否'}")
            if auto_add_enabled:
                logger.info(f"本次最大添加到任务队列数量: {max_add_to_tasks}")
            
            max_reached_logged = False
            for video in filtered_videos:
                # 检查是否已经处理过
                if not self._is_video_processed(video['id'], config_id):
                    # 检查是否还能添加到任务队列
                    should_add_to_tasks = auto_add_enabled and added_count < max_add_to_tasks
                    
                    # 始终保存到历史记录，传递 run_id
                    self._save_video_history(video, config_id, auto_add_to_tasks=should_add_to_tasks, run_id=run_id)
                    processed_count += 1
                    
                    if should_add_to_tasks:
                        added_count += 1
                        logger.info(f"视频已添加到任务队列 ({added_count}/{max_add_to_tasks}): {video['title']}")
                    else:
                        if auto_add_enabled:
                            logger.info(f"视频已保存到历史记录: {video['title']}")
                        else:
                            logger.info(f"视频已保存到历史记录（未添加到任务队列）: {video['title']}")
                    
                    # 如果启用了自动添加且达到上限，后续视频继续保存到历史记录（未添加到任务队列），不再跳出循环丢弃
                    if auto_add_enabled and added_count >= max_add_to_tasks and not max_reached_logged:
                        logger.info(f"已达到本次添加到任务队列上限 {max_add_to_tasks}，后续视频仅保存至监控记录供后续定时入队使用")
                        max_reached_logged = True
                else:
                    logger.debug(f"视频已处理过，跳过: {video['title']}")
            
            # 更新最后运行时间（若本次抓取存在错误则跳过，避免漏掉新视频）
            if not self._last_fetch_had_errors:
                self._update_last_run_time(config_id)
            else:
                logger.warning("本次抓取存在错误，跳过更新last_run_time以避免漏掉新视频")
            
            logger.info(f"监控任务完成 - 配置: {config['name']}, "
                       f"处理新视频: {processed_count}, 添加到任务队列: {added_count}")
            
            # 更新历史搬运进度（如果是历史模式）
            if config.get('channel_mode') == 'historical':
                # 获取应用偏移量前的完整筛选结果
                original_filtered = self._filter_videos(videos, config)
                self._update_historical_progress(config_id, original_filtered, added_count)
            
            self._finalize_monitor_run(
                run_id=run_id,
                status='success',
                start_timestamp=start_ts,
                fetched_count=len(videos),
                filtered_count=len(filtered_videos),
                new_count=processed_count,
                added_count=added_count,
                error_message=''
            )
            return True, f"监控完成，处理了 {processed_count} 个新视频，添加了 {added_count} 个到任务队列"
            
        except Exception as e:
            logger.error(f"监控任务执行失败 - 配置: {config['name']} (ID: {config_id}), 错误: {str(e)}")
            err_msg = self._format_run_error_message(e)
            self._finalize_monitor_run(
                run_id=run_id,
                status='failed',
                start_timestamp=start_ts,
                fetched_count=len(videos),
                filtered_count=len(filtered_videos),
                new_count=processed_count,
                added_count=added_count,
                error_message=err_msg
            )
            return False, err_msg
    
    def _fetch_trending_videos(self, config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """获取视频"""
        try:
            # 设置时间范围
            published_after: Optional[str] = None
            published_before: Optional[str] = None
            
            # 历史搬运模式的智能时间推进
            if config.get('channel_mode') == 'historical' and config.get('start_date'):
                published_after, published_before, current_offset = self._get_historical_time_range(config)
            elif config.get('start_date'):
                # 如果设置了开始日期，使用开始日期
                start_date = datetime.strptime(config['start_date'], '%Y-%m-%d')
                published_after = start_date.isoformat() + 'Z'
                logger.info(f"使用开始日期: {config['start_date']}")
                
                # 检查是否设置了结束日期
                if config.get('end_date'):
                    end_date = datetime.strptime(config['end_date'], '%Y-%m-%d')
                    # 结束日期加一天，确保包含当天的视频
                    end_date = end_date + timedelta(days=1)
                    published_before = end_date.isoformat() + 'Z'
                    logger.info(f"使用结束日期: {config['end_date']}")
            else:
                # 否则根据模式计算
                is_channel_monitor = bool(config.get('channel_ids') and str(config.get('channel_ids')).strip())
                if config.get('channel_mode') == 'latest' and is_channel_monitor:
                    # 持续跟进最新（频道监控）：从开启/上次运行时间开始算
                    last_run_str = config.get('last_run_time')
                    if last_run_str:
                        # 数据库中存储的是本地时间（YYYY-MM-DD HH:MM:SS），需转为 UTC 供 YouTube API 使用
                        try:
                            last_run_dt = datetime.strptime(last_run_str, '%Y-%m-%d %H:%M:%S')
                            published_after = last_run_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                            logger.info(f"最新跟进模式：使用上次运行时间为基准(UTC): {published_after}")
                        except Exception:
                            # 解析失败则退回到当前时间
                            published_after = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
                            logger.info("最新跟进模式：无法解析上次运行时间，使用当前时间为基准")
                    else:
                        # 首次运行：从当前时间开始，不处理历史视频
                        published_after = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
                        logger.info("最新跟进模式：首次运行，仅从当前时间开始跟进新发布的视频")
                else:
                    # 其他模式或全局检索：手动执行必须使用显式配置的时间段；
                    # 只有自动调度的 latest 模式才按调度间隔估算搜索窗口。
                    is_auto_schedule = str(config.get('schedule_type', 'manual')).strip().lower() == 'auto'
                    if config.get('channel_mode') == 'latest' and is_auto_schedule:
                        interval_hours = config.get('schedule_interval', 120) / 60 * 2
                        days = max(1, interval_hours / 24)  # 至少1天
                        logger.info(f"使用动态时间段: 最近 {days:.1f} 天（基于调度间隔 {config.get('schedule_interval', 120)} 分钟）")
                    else:
                        days = config.get('time_period', 7)
                        if config.get('channel_mode') == 'latest' and not is_auto_schedule:
                            logger.info(f"手动执行：忽略调度间隔，使用配置时间段: 最近 {days} 天")
                        else:
                            logger.info(f"使用时间段: 最近 {days} 天")
                    published_after = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
            
            # 如果指定了频道，优先使用频道搜索
            if config.get('channel_ids') and config['channel_ids'].strip():
                logger.info(f"使用频道监控模式，频道数量: {len([ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()])}")
                if published_after is not None:
                    return self._fetch_channel_videos(config, published_after, published_before)
                else:
                    logger.error("published_after 为 None，无法获取频道视频")
                    return []
            else:
                logger.info(f"使用YouTube搜索模式，关键词: {config.get('keywords', '无')}")
                if published_after is not None:
                    return self._fetch_search_videos(config, published_after, published_before)
                else:
                    logger.error("published_after 为 None，无法搜索视频")
                    return []
                
        except HttpError as e:
            logger.error(f"YouTube API错误: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"获取视频数据失败: {str(e)}")
            raise
    
    def _fetch_search_videos(self, config: Dict[str, Any], published_after: str, published_before: Optional[str] = None) -> List[Dict[str, Any]]:
        """通过搜索获取视频"""
        # 确保 YouTube API 对象可用
        if not self.youtube:
            logger.error("YouTube API 未初始化")
            return []
            
        # 构建搜索参数
        search_params = {
            'part': 'id,snippet',
            'type': 'video',
            'order': config.get('order_by', 'viewCount'),
            'publishedAfter': published_after,
            'maxResults': min(config['max_results'] * 2, 50),  # 获取更多结果用于筛选
            'regionCode': config['region_code']
        }

        # 根据所选类型决定是否在搜索阶段过滤直播
        selected_types = str(config.get('video_types', 'video,short,live')).split(',')
        selected_types = [t.strip() for t in selected_types if t.strip()]
        only_live = set(selected_types) == {'live'}
        # 注意：eventType 是「把搜索范围限制到直播事件」的过滤器，
        # 官方语义为 completed = Only include completed broadcasts。
        # 因此只能在「只要直播」时设置 eventType=live；
        # 若在排除直播时设置 eventType=completed，会把普通投稿一并排除，
        # 导致搜索结果几乎为空（实测 28 关键词仅返回 1 条）。
        # 非直播类型统一交由详情阶段的 _detect_video_type 判定，搜索阶段不加 eventType。
        if only_live:
            search_params['eventType'] = 'live'

        # 添加结束日期限制
        if published_before:
            search_params['publishedBefore'] = published_before
        
        # 添加关键词搜索
        if config.get('keywords'):
            raw_keywords = config['keywords']
            normalized_keywords = normalize_search_keywords(raw_keywords)
            if normalized_keywords != raw_keywords:
                logger.info(f"关键词已自动规整: '{raw_keywords}' -> '{normalized_keywords}'")
            search_params['q'] = normalized_keywords
        
        # 添加分类过滤
        if config['category_id'] and config['category_id'] != '0':
            search_params['videoCategoryId'] = config['category_id']
        
        # 不再按 videoDuration 分批搜索，统一一次搜索后在详情阶段精确分类
        search_batches = [dict(search_params)]

        all_video_ids = []
        for idx, sp in enumerate(search_batches, 1):
            logger.debug(f"准备执行搜索请求({idx}/{len(search_batches)})，参数: {sp}")
            search_request = self.youtube.search().list(**sp)
            search_response = self._execute_with_retry(search_request, 'search.list')
            if not search_response or 'items' not in search_response:
                logger.error(f"搜索响应异常: {search_response}")
                continue
            ids = [item['id']['videoId'] for item in search_response['items'] if 'id' in item and 'videoId' in item['id']]
            all_video_ids.extend(ids)

        # 去重并限制数量
        video_ids = []
        seen = set()
        for vid in all_video_ids:
            if vid not in seen:
                seen.add(vid)
                video_ids.append(vid)
            if len(video_ids) >= min(config['max_results'] * 2, 50):
                break
        
        if not video_ids:
            return []
        
        # 获取视频详细信息（带重试）
        logger.debug(f"准备执行视频详情请求，YouTube API 对象: {type(self.youtube)}")
        videos_request = self.youtube.videos().list(
            part='id,snippet,statistics,contentDetails,liveStreamingDetails',
            id=','.join(video_ids)
        )
        logger.debug(f"视频详情请求已创建: {type(videos_request)}")
        videos_response = self._execute_with_retry(videos_request, 'videos.list')
        
        # 添加调试日志 - 检查视频响应
        logger.debug(f"视频响应类型: {type(videos_response)}, 值: {videos_response}")
        if videos_response is None:
            logger.error("视频响应为 None")
            return []
        
        if 'items' not in videos_response:
            logger.error(f"视频响应中缺少 'items' 字段，响应内容: {videos_response}")
            return []
        
        return videos_response['items']
    
    def _fetch_channel_videos(self, config: Dict[str, Any], published_after: str, published_before: Optional[str] = None) -> List[Dict[str, Any]]:
        """从指定频道获取视频"""
        all_videos = []
        channel_ids = [ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()]
        
        # 实现请求速率限制
        request_count = 0
        max_requests = config.get('rate_limit_requests', 4)
        # 使用调度间隔作为时间窗口（转换为秒）
        request_window = config.get('schedule_interval', 120) * 60
        
        # 根据频道模式调整获取策略
        channel_mode = config.get('channel_mode', 'latest')
        logger.info(f"开始处理 {len(channel_ids)} 个频道，模式: {channel_mode}，请求限制: {max_requests}/{config.get('schedule_interval', 120)}分钟")
        
        had_error = False
        for i, channel_id in enumerate(channel_ids, 1):
            if request_count >= max_requests:
                logger.warning(f"达到请求限制 {max_requests}/{config.get('schedule_interval', 120)}分钟，跳过剩余 {len(channel_ids) - i + 1} 个频道")
                break
                
            try:
                logger.info(f"处理频道 {i}/{len(channel_ids)}: {channel_id}")
                
                if channel_mode == 'search':
                    # 频道内搜索模式
                    videos = self._fetch_channel_search_videos(channel_id, config, published_after, published_before)
                    request_count += 2  # 估算搜索请求数
                else:
                    # 历史搬运和最新跟进模式都使用播放列表方式
                    videos = self._fetch_channel_playlist_videos(channel_id, config, published_after, published_before)
                    request_count += 3  # 频道信息 + 播放列表 + 视频详情
                
                all_videos.extend(videos)
                logger.info(f"频道 {channel_id} 获取到 {len(videos)} 个视频")
                
                # 简单的速率限制
                if request_count >= max_requests:
                    break
                    
            except Exception as e:
                logger.error(f"获取频道 {channel_id} 视频失败: {str(e)}")
                had_error = True
                continue
        
        logger.info(f"频道视频获取完成，总计 {len(all_videos)} 个视频，使用了 {request_count} 个API请求")
        # 记录本次获取是否出现错误，供上层决定是否更新last_run_time
        self._last_fetch_had_errors = had_error
        return all_videos
    
    def _fetch_channel_search_videos(self, channel_id: str, config: Dict[str, Any], published_after: str, published_before: Optional[str] = None) -> List[Dict[str, Any]]:
        """在指定频道内搜索视频"""
        # 确保 YouTube API 对象可用
        if not self.youtube:
            logger.error("YouTube API 未初始化")
            return []
            
        try:
            raw_keywords = config.get('channel_keywords', '')
            keywords = normalize_search_keywords(raw_keywords) if raw_keywords else ''
            if keywords and keywords != raw_keywords:
                logger.info(f"频道搜索关键词已自动规整: '{raw_keywords}' -> '{keywords}'")
            
            # 构建搜索参数
            search_params = {
                'part': 'id,snippet',
                'type': 'video',
                'channelId': channel_id,
                'q': keywords or None,
                'publishedAfter': published_after,
                'maxResults': config.get('max_results', 10),
                'order': config.get('order_by', 'relevance')
            }
            # 根据选择增加 eventType（同上：仅「只要直播」时才限制搜索范围）
            selected_types = str(config.get('video_types', 'video,short,live')).split(',')
            selected_types = [t.strip() for t in selected_types if t.strip()]
            only_live = set(selected_types) == {'live'}
            if only_live:
                search_params['eventType'] = 'live'
            
            # 添加结束日期限制
            if published_before:
                search_params['publishedBefore'] = published_before
            
            logger.info(f"在频道 {channel_id} 内搜索关键词: {keywords}")
            
            # 执行搜索（带重试）
            logger.debug(f"准备执行频道搜索请求，YouTube API 对象: {type(self.youtube)}")
            # 清理 None 值，避免 API 报错
            search_params = {k: v for k, v in search_params.items() if v is not None}
            search_request = self.youtube.search().list(**search_params)
            logger.debug(f"频道搜索请求已创建: {type(search_request)}")
            search_response = self._execute_with_retry(search_request, f'search.list (channel {channel_id})')
            
            # 添加调试日志 - 检查频道搜索响应
            logger.debug(f"频道搜索响应类型: {type(search_response)}, 值: {search_response}")
            if search_response is None:
                logger.error(f"频道 {channel_id} 搜索响应为 None")
                return []
            
            if 'items' not in search_response:
                logger.error(f"频道 {channel_id} 搜索响应中缺少 'items' 字段，响应内容: {search_response}")
                return []
            
            video_ids = [item['id']['videoId'] for item in search_response['items']]
            
            if not video_ids:
                logger.info(f"频道 {channel_id} 搜索无结果")
                return []
            
            # 获取视频详细信息（带重试）
            logger.debug(f"准备执行频道视频详情请求，YouTube API 对象: {type(self.youtube)}")
            videos_request = self.youtube.videos().list(
                part='id,snippet,statistics,contentDetails,liveStreamingDetails',
                id=','.join(video_ids)
            )
            logger.debug(f"频道视频详情请求已创建: {type(videos_request)}")
            videos_response = self._execute_with_retry(videos_request, f'videos.list (channel {channel_id})')
            
            # 添加调试日志 - 检查频道视频响应
            logger.debug(f"频道视频响应类型: {type(videos_response)}, 值: {videos_response}")
            if videos_response is None:
                logger.error(f"频道 {channel_id} 视频响应为 None")
                return []
            
            if 'items' not in videos_response:
                logger.error(f"频道 {channel_id} 视频响应中缺少 'items' 字段，响应内容: {videos_response}")
                return []
            
            return videos_response['items']
            
        except Exception as e:
            logger.error(f"频道搜索失败 {channel_id}: {str(e)}")
            raise
    
    def _fetch_channel_playlist_videos(self, channel_id, config, published_after, published_before=None):
        """从频道播放列表获取视频"""
        try:
            # 获取频道的上传播放列表ID
            if self.youtube is None:
                logger.error(f"频道 {channel_id} YouTube API 对象为 None")
                return []
            channel_request = self.youtube.channels().list(
                part='contentDetails',
                id=channel_id
            )
            channel_response = self._execute_with_retry(channel_request, f'channels.list (channel {channel_id})')
            
            if not channel_response['items']:
                logger.warning(f"找不到频道: {channel_id}")
                return []
            
            upload_playlist_id = channel_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
            logger.debug(f"频道 {channel_id} 上传播放列表ID: {upload_playlist_id}")
            
            # 根据频道模式调整获取数量
            channel_mode = config.get('channel_mode', 'latest')
            if channel_mode == 'historical':
                # 历史搬运模式，获取更多视频，支持分页
                max_results_per_page = 50  # YouTube API单次最大50
                max_total_videos = 500  # 最多检查500个视频
            else:
                # 最新跟进模式
                max_results_per_page = config.get('latest_max_results', 20)
                max_total_videos = max_results_per_page
            
            # 分页获取播放列表中的视频
            all_playlist_items = []
            next_page_token = None
            videos_fetched = 0
            
            while videos_fetched < max_total_videos:
                # 计算本次请求的数量
                current_page_size = min(max_results_per_page, max_total_videos - videos_fetched)
                
                playlist_params = {
                    'part': 'snippet',
                    'playlistId': upload_playlist_id,
                    'maxResults': current_page_size
                }
                
                if next_page_token:
                    playlist_params['pageToken'] = next_page_token
                
                if self.youtube is None:
                    logger.error(f"频道 {channel_id} YouTube API 对象为 None")
                    break
                playlist_request = self.youtube.playlistItems().list(**playlist_params)
                playlist_response = self._execute_with_retry(playlist_request, f'playlistItems.list (channel {channel_id})')
                current_items = playlist_response['items']
                all_playlist_items.extend(current_items)
                videos_fetched += len(current_items)
                
                # 检查是否还有更多页面
                next_page_token = playlist_response.get('nextPageToken')
                if not next_page_token or len(current_items) == 0:
                    break
                
                # 在历史搬运模式下，如果我们已经找到足够的时间范围内的视频，可以提前停止
                if channel_mode == 'historical':
                    # 快速检查当前这批视频中最早的时间
                    if current_items:
                        earliest_video_time = min(item['snippet']['publishedAt'] for item in current_items)
                        # 如果最早的视频都比我们的开始时间还早，说明后面的视频都不会在范围内了
                        if earliest_video_time < published_after:
                            logger.info(f"找到了早于开始时间的视频，停止获取更多视频")
                            break
            
            logger.info(f"频道 {channel_id} 总共获取了 {len(all_playlist_items)} 个播放列表项目")
            
            # 筛选时间范围内的视频
            video_ids = []
            for item in all_playlist_items:
                video_published = item['snippet']['publishedAt']
                
                # 检查开始时间
                if video_published < published_after:
                    continue
                
                # 检查结束时间
                if published_before and video_published >= published_before:
                    continue
                
                video_ids.append(item['snippet']['resourceId']['videoId'])
            
            logger.info(f"频道 {channel_id} 在时间范围内找到 {len(video_ids)} 个视频")
            
            if not video_ids:
                return []
            
            # 获取视频详细信息
            if self.youtube is None:
                logger.error(f"频道 {channel_id} YouTube API 对象为 None")
                return []
            videos_request = self.youtube.videos().list(
                part='id,snippet,statistics,contentDetails,liveStreamingDetails',
                id=','.join(video_ids)
            )
            videos_response = self._execute_with_retry(videos_request, f'videos.list (channel {channel_id})')
            
            videos = videos_response['items']
            
            # 历史搬运模式需要按时间正序排列（从最老到最新）
            channel_mode = config.get('channel_mode', 'latest')
            if channel_mode == 'historical':
                # 按发布时间正序排列（最老的在前）
                videos.sort(key=lambda x: x['snippet']['publishedAt'])
                logger.info(f"历史搬运模式：已按时间正序排列视频（从最老到最新）")
            
            return videos
                    
        except Exception as e:
            logger.error(f"频道播放列表获取失败 {channel_id}: {str(e)}")
            # 向上抛出让上层决定是否继续以及是否更新last_run_time
            raise

    def _execute_with_retry(self, request: Any, description: str, max_attempts: int = 3, backoff_seconds: float = 1.0) -> Any:
        """对YouTube API请求执行带重试的调用，用于处理临时性网络/SSL问题"""
        attempt = 0
        last_exception: Optional[Exception] = None
        while attempt < max_attempts:
            try:
                return request.execute()
            except HttpError as e:
                # 对于5xx或已知可重试错误进行重试
                resp = getattr(e, 'resp', None)
                if resp is not None:
                    status = getattr(resp, 'status', None)
                else:
                    status = None
                if status and 500 <= status < 600:
                    last_exception = e
                else:
                    # 429或配额等错误也可适当重试
                    if status in (429,):
                        last_exception = e
                    else:
                        raise
            except ssl.SSLError as e:
                # 记录并重试，同时尝试重建客户端
                last_exception = e
                logger.warning(f"SSL错误，准备重试并重建API客户端: {str(e)}")
                self._init_youtube_api()
            except OSError as e:
                # 网络层错误，重试
                last_exception = e
            attempt += 1
            sleep_time = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(f"调用 {description} 失败（第 {attempt} 次），{type(last_exception).__name__}: {last_exception}，{sleep_time:.1f}s 后重试...")
            time.sleep(sleep_time)
        # 达到最大重试次数仍失败
        if last_exception is not None:
            raise last_exception
        else:
            raise Exception("未知错误：重试次数耗尽但未捕获到具体异常")
    
    def _filter_videos(self, videos, config):
        """根据配置筛选视频"""
        filtered = []
        
        for video in videos:
            # 基本信息
            video_info = {
                'id': video['id'],
                'title': video['snippet']['title'],
                'channel_title': video['snippet']['channelTitle'],
                'channel_id': video['snippet']['channelId'],
                'published_at': video['snippet']['publishedAt'],
                'duration': video['contentDetails']['duration'],
                'view_count': int(video['statistics'].get('viewCount', 0)),
                'like_count': int(video['statistics'].get('likeCount', 0)),
                'comment_count': int(video['statistics'].get('commentCount', 0))
            }
            # 提取缩略图 URL（按 high -> medium -> default 优先级，或保底通过 video_id 构造）
            thumbnails = video.get('snippet', {}).get('thumbnails', {}) or {}
            thumbnail_url = (
                thumbnails.get('high', {}).get('url')
                or thumbnails.get('medium', {}).get('url')
                or thumbnails.get('default', {}).get('url')
                or f"https://i.ytimg.com/vi/{video['id']}/hqdefault.jpg"
            )
            video_info['thumbnail_url'] = thumbnail_url

            # 基于 API 字段的内容类型判定：live 优先，其次 shorts，再否则 video
            try:
                video_info['video_type'] = self._detect_video_type(video)
            except Exception:
                video_info['video_type'] = 'video'
            
            # 应用筛选条件
            if not self._meets_criteria(video_info, config):
                continue
                
            filtered.append(video_info)
            
            # 限制结果数量
            if len(filtered) >= config['max_results']:
                break
        
        return filtered

    def _detect_video_type(self, video: Dict[str, Any]) -> str:
        """根据 API 字段判定视频类型: live / short / video
        - live: 有 liveStreamingDetails 或 snippet.liveBroadcastContent in {live, upcoming}
        - short: 结合 API 信号判定 Shorts（不单纯依赖时长）：
            1) 标题/描述/标签包含 #shorts 或 shorts（大小写不敏感）
            2) 竖屏缩略图比例（通过 snippet.thumbnails 宽高比判断）且时长 <= 61 秒（作为辅证）
        备注：YouTube Data API 无官方 Shorts 标记，只能多信号近似。
        """
        # 直播判定
        live_flag = str(video.get('snippet', {}).get('liveBroadcastContent', '')).lower()
        has_live_details = bool(video.get('liveStreamingDetails'))
        if has_live_details or live_flag in ('live', 'upcoming'):
            return 'live'

        # Shorts 判定
        if self._is_shorts(video):
            return 'short'

        return 'video'

    def _is_shorts(self, video: Dict[str, Any]) -> bool:
        """综合 API 线索判断是否为 Shorts。
        优先依据 #shorts 标签/文本；其次以竖屏+<=61s 作为辅证，避免仅用时长误判。
        """
        snippet = video.get('snippet', {})
        title = str(snippet.get('title', '')).lower()
        description = str(snippet.get('description', '')).lower()
        tags = [str(t).lower() for t in snippet.get('tags', [])] if isinstance(snippet.get('tags'), list) else []

        # 1) 文本或标签中包含 shorts 相关标识
        shorts_keywords = ['#shorts', 'shorts']
        if any(kw in title or kw in description for kw in shorts_keywords):
            return True
        if any('short' == t or 'shorts' == t or '#shorts' == t for t in tags):
            return True

        # 2) 竖屏比例 + 短时长（<= 61s）作为辅证
        duration_seconds = self._parse_duration(video.get('contentDetails', {}).get('duration', '') or 'PT0S')
        # 放宽判定：若时长 <= 61s 则视为 Shorts（兼容缩略图缺少宽高信息的情况）
        if duration_seconds <= 61:
            return True

        return False

    def _is_vertical_from_thumbnails(self, snippet: Dict[str, Any]) -> bool:
        """根据缩略图宽高判断是否竖屏。取可用缩略图中最接近原比例的一个进行判断。"""
        thumbs = snippet.get('thumbnails', {}) or {}
        # 选取一个具有明确宽高的缩略图条目
        order = ['maxres', 'standard', 'high', 'medium', 'default']
        for key in order:
            t = thumbs.get(key)
            if not t:
                continue
            w = t.get('width')
            h = t.get('height')
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                # 竖屏：高度/宽度比 >= 1.2 视为竖屏
                return (h / w) >= 1.2
        return False
    
    def _meets_criteria(self, video_info, config):
        """检查视频是否符合筛选条件"""
        # 检查开始日期
        if config.get('start_date'):
            try:
                start_date = datetime.strptime(config['start_date'], '%Y-%m-%d')
                video_date = datetime.fromisoformat(video_info['published_at'].replace('Z', '+00:00'))
                if video_date < start_date.replace(tzinfo=video_date.tzinfo):
                    return False
            except Exception as e:
                logger.warning(f"日期比较失败: {str(e)}")
        
        # 类型过滤
        allowed_types = str(config.get('video_types', 'video,short,live')).split(',') if config.get('video_types') is not None else ['video','short','live']
        allowed_types = [t.strip() for t in allowed_types if t.strip()]
        # 兼容旧记录：若未检测出类型，按普通视频处理
        vtype = video_info.get('video_type', 'video')
        if allowed_types and vtype not in allowed_types:
            return False

        # 检查观看数
        if video_info['view_count'] < config['min_view_count']:
            return False
        
        # 检查点赞数
        if video_info['like_count'] < config['min_like_count']:
            return False
        
        # 检查评论数
        if video_info['comment_count'] < config['min_comment_count']:
            return False
        
        # 检查排除关键词
        if config.get('exclude_keywords'):
            exclude_words = [word.strip().lower() for word in config['exclude_keywords'].split(',')]
            title_lower = video_info['title'].lower()
            for word in exclude_words:
                if word and word in title_lower:
                    return False
        
        # 检查直拍监控强白名单校验：
        # 若配置关键词或任务名称中显式包含直拍标识（직캠、fancam、直拍、饭拍），
        # 则视频标题必须包含直拍关键词（직캠 或 fancam），防止自制综艺日常口语切片因黑名单不全而混入
        keywords_str = str(config.get('keywords', '')).lower()
        config_name = str(config.get('name', '')).lower()
        is_fancam_monitor = (
            '직캠' in keywords_str or 'fancam' in keywords_str or
            '직캠' in config_name or 'fancam' in config_name or
            '直拍' in config_name or '饭拍' in config_name
        )
        if is_fancam_monitor:
            title_lower = video_info['title'].lower()
            if '직캠' not in title_lower and 'fancam' not in title_lower:
                return False
        
        # 检查频道ID过滤
        if config['exclude_channel_ids']:
            exclude_channels = [ch.strip() for ch in config['exclude_channel_ids'].split(',')]
            if video_info['channel_id'] in exclude_channels:
                return False
        
        # 检查指定频道（如果没有指定频道，则不限制）
        if config['channel_ids'] and config['channel_ids'].strip():
            include_channels = [ch.strip() for ch in config['channel_ids'].split(',') if ch.strip()]
            if include_channels and video_info['channel_id'] not in include_channels:
                return False
        
        # 检查视频时长
        duration_seconds = self._parse_duration(video_info['duration'])
        if config['min_duration'] > 0 and duration_seconds < config['min_duration']:
            return False
        if config['max_duration'] > 0 and duration_seconds > config['max_duration']:
            return False
        
        return True
    
    def _parse_duration(self, duration_str):
        """解析ISO 8601时长格式为秒数"""
        import re
        
        # PT1H30M45S -> 1小时30分45秒
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
        match = re.match(pattern, duration_str)
        
        if not match:
            return 0
        
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = int(match.group(3) or 0)
        
        return hours * 3600 + minutes * 60 + seconds
    
    def _is_video_processed(self, video_id, config_id):
        """检查视频是否已经处理过"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id FROM monitor_history WHERE video_id = ? AND config_id = ?',
                (video_id, config_id)
            )
            return cursor.fetchone() is not None
    
    def _save_video_history(self, video_info, config_id, auto_add_to_tasks=False, run_id=None):
        """保存视频到历史记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO monitor_history (
                    config_id, video_id, video_type, video_title, channel_title,
                    view_count, like_count, comment_count, duration,
                    published_at, added_to_tasks, thumbnail_url, run_time, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?)
            ''', (
                config_id,
                video_info['id'],
                video_info.get('video_type', 'video'),
                video_info['title'],
                video_info['channel_title'],
                video_info['view_count'],
                video_info['like_count'],
                video_info.get('comment_count', 0),
                video_info.get('duration', ''),
                video_info.get('published_at', ''),
                1 if auto_add_to_tasks else 0,
                video_info.get('thumbnail_url') or f"https://i.ytimg.com/vi/{video_info['id']}/hqdefault.jpg",
                run_id
            ))
            
            conn.commit()
            logger.info(f"视频已保存到历史记录: {video_info['title']}")
            
            # 如果启用自动添加到任务队列，直接添加
            if auto_add_to_tasks:
                task_id = self._add_video_to_tasks(video_info, auto_start=True)
                if task_id:
                    # 更新数据库标记为已添加
                    cursor.execute(
                        'UPDATE monitor_history SET added_to_tasks = 1 WHERE video_id = ? AND config_id = ?',
                        (video_info['id'], config_id)
                    )
    
    def _add_video_to_tasks(self, video_info, auto_start=True, auto_pipeline=True):
        """将视频添加到任务队列"""
        try:
            video_url = f"https://www.youtube.com/watch?v={video_info['id']}"
            task_id = add_task(video_url, upload_target='bilibili', auto_pipeline=auto_pipeline)
            
            if task_id:
                logger.info(f"视频已添加到任务队列: {video_info['title']}, 任务ID: {task_id}, 平台: bilibili, 自动化流水线: {auto_pipeline}")
                
                # 移除自动启动逻辑，让全局任务处理器的队列管理机制来处理
                # 这样避免重复调度和冲突
                logger.info(f"任务已添加，将由队列管理器自动处理: {task_id}")
                
                return task_id  # 返回任务ID而不是布尔值
            else:
                logger.error("添加任务失败，未返回任务ID")
                return None
                
        except Exception as e:
            logger.error(f"添加视频到任务队列失败: {str(e)}")
            return None
    
    def add_video_to_tasks_manually(self, video_id, config_id):
        """手动将视频添加到任务队列"""
        logger.info(f"手动添加视频到任务队列: {video_id}, 配置ID: {config_id}")
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT video_id, video_title, channel_title, view_count, like_count, 
                       comment_count, duration, published_at, added_to_tasks
                FROM monitor_history 
                WHERE video_id = ? AND config_id = ?
            ''', (video_id, config_id))
            
            row = cursor.fetchone()
            if not row:
                logger.warning(f"视频不存在: {video_id}, 配置ID: {config_id}")
                return False, "视频不存在"
            
            if row[8]:  # added_to_tasks
                logger.warning(f"视频已经添加到任务队列: {row[1]}")
                return False, "视频已经添加到任务队列"
            
            # 构建视频信息
            video_info = {
                'id': row[0],
                'title': row[1],
                'channel_title': row[2],
                'view_count': row[3],
                'like_count': row[4],
                'comment_count': row[5],
                'duration': row[6],
                'published_at': row[7]
            }
            
            logger.info(f"准备添加视频: {video_info['title']} (频道: {video_info['channel_title']})")
            
            # 添加到任务队列，是否自动启动由系统配置决定
            auto_start = False
            try:
                from flask import current_app
                if hasattr(current_app, 'config') and 'Y2A_SETTINGS' in current_app.config:
                    auto_start = current_app.config['Y2A_SETTINGS'].get('AUTO_MODE_ENABLED', False)
            except (ImportError, RuntimeError):
                try:
                    cfg = load_config() or {}
                    auto_start = cfg.get('AUTO_MODE_ENABLED', False)
                except Exception as e:
                    logger.warning(f"读取配置文件失败: {str(e)}")
            
            logger.info(f"手动添加视频，自动启动处理: {'是' if auto_start else '否'}")
            task_id = self._add_video_to_tasks(video_info, auto_start=auto_start, auto_pipeline=True)
            if task_id:
                self._mark_video_added_to_tasks(video_id, config_id)
                logger.info(f"视频成功添加到任务队列: {video_info['title']}, 任务ID: {task_id}")
                return True, f"视频已成功添加到任务队列，任务ID: {task_id}"
            else:
                logger.error(f"添加视频到任务队列失败: {video_info['title']}")
                return False, "添加到任务队列失败"
    
    def batch_add_to_tasks(self, record_ids):
        """批量将选中的监控历史记录视频添加到任务队列"""
        if not record_ids:
            return False, "未指定要添加的监控记录", []

        clean_ids = []
        for rid in record_ids:
            try:
                clean_ids.append(int(rid))
            except (ValueError, TypeError):
                continue

        if not clean_ids:
            return False, "记录ID格式无效", []

        # 检查自动启动配置
        auto_start = False
        try:
            from flask import current_app
            if hasattr(current_app, 'config') and 'Y2A_SETTINGS' in current_app.config:
                auto_start = current_app.config['Y2A_SETTINGS'].get('AUTO_MODE_ENABLED', False)
        except (ImportError, RuntimeError):
            try:
                cfg = load_config() or {}
                auto_start = cfg.get('AUTO_MODE_ENABLED', False)
            except Exception as e:
                logger.warning(f"读取配置文件失败: {str(e)}")

        added_record_ids = []
        already_count = 0
        failed_count = 0

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in clean_ids)
            cursor.execute(f'''
                SELECT id, video_id, config_id, video_title, channel_title, view_count, like_count, 
                       comment_count, duration, published_at, added_to_tasks
                FROM monitor_history 
                WHERE id IN ({placeholders})
            ''', clean_ids)
            rows = cursor.fetchall()

            if not rows:
                return False, "未找到对应的记录", []

            for row in rows:
                rec_id = row[0]
                video_id = row[1]
                config_id = row[2]
                title = row[3]
                added_to_tasks = row[10]

                if added_to_tasks:
                    already_count += 1
                    continue

                video_info = {
                    'id': video_id,
                    'title': title,
                    'channel_title': row[4],
                    'view_count': row[5],
                    'like_count': row[6],
                    'comment_count': row[7],
                    'duration': row[8],
                    'published_at': row[9]
                }

                try:
                    task_id = self._add_video_to_tasks(video_info, auto_start=auto_start, auto_pipeline=True)
                    if task_id:
                        self._mark_video_added_to_tasks(video_id, config_id)
                        added_record_ids.append(rec_id)
                        logger.info(f"批量添加成功: {title} (ID: {rec_id}, 任务ID: {task_id})")
                    else:
                        failed_count += 1
                except Exception as e:
                    logger.error(f"批量添加任务异常: {title}, 错误: {e}")
                    failed_count += 1

        msg = f"成功添加 {len(added_record_ids)} 个视频到任务队列"
        if already_count > 0:
            msg += f"（{already_count} 个此前已在队列中）"
        if failed_count > 0:
            msg += f"（{failed_count} 个添加失败）"

        return True, msg, added_record_ids

    def _mark_video_added_to_tasks(self, video_id, config_id):
        """标记视频已添加到任务队列"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE monitor_history SET added_to_tasks = 1 WHERE video_id = ? AND config_id = ?',
                (video_id, config_id)
            )
            conn.commit()
    
    def start_all_schedules(self):
        """启动所有自动调度的监控任务及定时入队任务"""
        logger.info("开始启动所有自动调度的监控任务及定时入队任务")
        
        configs = self.get_monitor_configs()
        auto_configs = [config for config in configs if config.get('enabled') and config.get('schedule_type') in ('auto', 'daily_times')]
        
        logger.info(f"找到 {len(auto_configs)} 个启用的自动调度配置")
        
        for config in auto_configs:
            logger.info(f"启动调度: {config['name']} (ID: {config['id']}), 类型: {config.get('schedule_type')}")
            self._schedule_monitor(config['id'])
        
        # 启动定时入队调度任务
        self._schedule_auto_enqueue()
        
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("调度器已启动")
        
        logger.info(f"所有自动调度任务启动完成，共 {len(auto_configs)} 个监控配置，并已载入定时入队调度")
    
    def _get_historical_time_range(self, config):
        """获取历史搬运模式的智能时间范围"""
        config_id = config.get('config_id') or config.get('id')
        
        # 获取当前进度
        current_offset = config.get('historical_offset', 0)
        start_date_str = config.get('start_date', '')
        end_date_str = config.get('end_date', '')
        
        if not start_date_str:
            logger.error("历史搬运模式需要设置开始日期")
            return None, None, 0
        
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            published_after = start_date.isoformat() + 'Z'
            
            # 设置结束日期
            published_before = None
            if end_date_str:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                # 结束日期加一天，确保包含当天的视频
                end_date = end_date + timedelta(days=1)
                published_before = end_date.isoformat() + 'Z'
                logger.info(f"历史搬运范围: {start_date_str} 到 {end_date_str}，当前偏移量: {current_offset}")
            else:
                # 如果没有结束日期，搬运到当前时间
                published_before = datetime.now().isoformat() + 'Z'
                logger.info(f"历史搬运范围: {start_date_str} 到现在，当前偏移量: {current_offset}")
            
            return published_after, published_before, current_offset
            
        except Exception as e:
            logger.error(f"计算历史搬运时间范围失败: {str(e)}")
            return None, None, 0
    
    def _update_historical_progress(self, config_id, all_filtered_videos, added_count):
        """更新历史搬运进度"""
        try:
            # 获取当前配置
            config = self.get_monitor_config(config_id)
            if not config:
                return
            
            current_offset = config.get('historical_offset', 0)
            
            # 更新偏移量
            new_offset = current_offset + added_count
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE monitor_configs SET historical_offset = ? WHERE id = ?',
                    (new_offset, config_id)
                )
                conn.commit()
            
            logger.info(f"历史搬运偏移量更新为: {new_offset} (本次添加 {added_count} 个视频)")
            
            # 检查是否已经处理完所有视频
            if new_offset >= len(all_filtered_videos):
                logger.info(f"历史搬运已完成！总共处理了 {new_offset} 个视频")
                
        except Exception as e:
            logger.error(f"更新历史搬运进度失败: {str(e)}")
    
    def _update_last_run_time(self, config_id):
        """更新最后运行时间"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE monitor_configs SET last_run_time = datetime('now', 'localtime') WHERE id = ?",
                (config_id,)
            )
            conn.commit()
    
    def _remove_schedule(self, config_id):
        """移除指定配置的所有调度任务（包括 interval 任务与 cron 定点任务）"""
        prefix = f"monitor_{config_id}"
        try:
            removed_count = 0
            for job in list(self.scheduler.get_jobs()):
                if job.id == prefix or job.id.startswith(f"{prefix}_"):
                    self.scheduler.remove_job(job.id)
                    removed_count += 1
            if removed_count > 0:
                logger.info(f"已移除配置 (ID: {config_id}) 的 {removed_count} 个调度任务")
        except Exception as e:
            logger.error(f"移除调度任务失败 (ID: {config_id}): {str(e)}")

    def _schedule_monitor(self, config_id, interval_minutes=None):
        """添加监控任务到调度器，自动支持 interval (固定间隔) 和 daily_times (每日定点)"""
        try:
            config = self.get_monitor_config(config_id)
            if not config or not config.get('enabled'):
                return
            
            config_name = config.get('name', f"ID-{config_id}")
            schedule_type = config.get('schedule_type', 'manual')
            
            # 先清理原有任务
            self._remove_schedule(config_id)
            
            if schedule_type == 'auto':
                minutes = interval_minutes if interval_minutes is not None else config.get('schedule_interval', 120)
                job_id = f"monitor_{config_id}"
                self.scheduler.add_job(
                    func=self.run_monitor,
                    trigger='interval',
                    minutes=minutes,
                    id=job_id,
                    args=[config_id],
                    replace_existing=True
                )
                logger.info(f"添加间隔调度任务: {config_name} ({job_id}), 间隔: {minutes}分钟")
                
            elif schedule_type == 'daily_times':
                time_points = parse_daily_time_points(config.get('schedule_time_points', ''))
                if not time_points:
                    logger.warning(f"配置 {config_name} (ID: {config_id}) 类型为每日定点，但未设置有效的时间点")
                    return
                
                for h, m in time_points:
                    job_id = f"monitor_{config_id}_cron_{h:02d}_{m:02d}"
                    self.scheduler.add_job(
                        func=self.run_monitor,
                        trigger=CronTrigger(hour=h, minute=m),
                        id=job_id,
                        args=[config_id],
                        replace_existing=True
                    )
                    logger.info(f"添加每日定点调度任务: {config_name} ({job_id}), 触发时间: {h:02d}:{m:02d}")
            
            if not self.scheduler.running:
                self.scheduler.start()
                logger.info("调度器已启动")
                
        except Exception as e:
            logger.error(f"添加调度任务失败 (ID: {config_id}): {str(e)}")

    def _update_schedule(self, config_id, config_data):
        """更新调度任务"""
        self._remove_schedule(config_id)
        if config_data.get('enabled') and config_data.get('schedule_type') in ('auto', 'daily_times'):
            self._schedule_monitor(config_id, config_data.get('schedule_interval'))

    def get_monitor_history(self, config_id=None, limit=100):
        """获取监控历史记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if config_id:
                cursor.execute('''
                    SELECT h.*, c.name as config_name 
                    FROM monitor_history h
                    JOIN monitor_configs c ON h.config_id = c.id
                    WHERE h.config_id = ?
                    ORDER BY h.run_time DESC
                    LIMIT ?
                ''', (config_id, limit))
            else:
                cursor.execute('''
                    SELECT h.*, c.name as config_name 
                    FROM monitor_history h
                    JOIN monitor_configs c ON h.config_id = c.id
                    ORDER BY h.run_time DESC
                    LIMIT ?
                ''', (limit,))
            
            columns = [description[0] for description in cursor.description]
            history = []
            for row in cursor.fetchall():
                record = dict(zip(columns, row))
                if not record.get('thumbnail_url'):
                    vid = record.get('video_id', '')
                    record['thumbnail_url'] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ''
                history.append(record)
            
            return history

    def get_monitor_kpi_stats(self, config_id=None) -> Dict[str, Any]:
        """获取监控记录中心的全局或分配置 KPI 统计"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                run_where = 'WHERE config_id = ?' if config_id and int(config_id) > 0 else ''
                hist_where = 'WHERE config_id = ?' if config_id and int(config_id) > 0 else ''
                params = (int(config_id),) if config_id and int(config_id) > 0 else ()

                # 1. 运行统计
                cursor.execute(f'''
                    SELECT
                        COUNT(*) as total_runs,
                        SUM(CASE WHEN date(start_time) = date('now', 'localtime') THEN 1 ELSE 0 END) as today_runs,
                        SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_runs,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_runs
                    FROM monitor_runs
                    {run_where}
                ''', params)
                r_row = cursor.fetchone()
                total_runs = r_row[0] or 0
                today_runs = r_row[1] or 0
                success_runs = r_row[2] or 0
                failed_runs = r_row[3] or 0
                success_rate = round(success_runs / total_runs * 100, 1) if total_runs > 0 else 100.0

                # 2. 视频统计
                cursor.execute(f'''
                    SELECT
                        COUNT(*) as total_videos,
                        SUM(CASE WHEN added_to_tasks = 1 THEN 1 ELSE 0 END) as added_videos,
                        AVG(view_count) as avg_views,
                        AVG(like_count) as avg_likes
                    FROM monitor_history
                    {hist_where}
                ''', params)
                h_row = cursor.fetchone()
                total_videos = h_row[0] or 0
                added_videos = h_row[1] or 0
                unadded_videos = total_videos - added_videos
                avg_views = int(h_row[2] or 0)
                avg_likes = int(h_row[3] or 0)

                return {
                    'total_runs': total_runs,
                    'today_runs': today_runs,
                    'success_runs': success_runs,
                    'failed_runs': failed_runs,
                    'success_rate': success_rate,
                    'total_videos': total_videos,
                    'unadded_videos': unadded_videos,
                    'added_videos': added_videos,
                    'avg_views': avg_views,
                    'avg_likes': avg_likes
                }
        except Exception as e:
            logger.error(f"获取监控 KPI 统计失败: {e}")
            return {
                'total_runs': 0,
                'today_runs': 0,
                'success_runs': 0,
                'failed_runs': 0,
                'success_rate': 100.0,
                'total_videos': 0,
                'unadded_videos': 0,
                'added_videos': 0,
                'avg_views': 0,
                'avg_likes': 0
            }

    def get_monitor_runs_paginated(self, config_id=None, status=None, page=1, per_page=20) -> Dict[str, Any]:
        """
        获取分页的监控执行记录 (Run History)
        
        Args:
            config_id: 监控配置ID（None 或 <=0 表示全部）
            status: 状态筛选，'all'(全部), 'success'(成功), 'has_new'(有新视频), 'no_new'(无新视频), 'failed'(失败)
            page: 当前页码
            per_page: 每页条数
        """
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1

        try:
            per_page = max(1, min(100, int(per_page)))
        except (ValueError, TypeError):
            per_page = 20

        status = str(status or 'all').strip().lower()
        if status not in ('all', 'success', 'has_new', 'no_new', 'failed'):
            status = 'all'

        where_clauses = []
        params = []

        if config_id and int(config_id) > 0:
            where_clauses.append('r.config_id = ?')
            params.append(int(config_id))

        if status == 'success':
            where_clauses.append("r.status = 'success'")
        elif status == 'has_new':
            where_clauses.append("r.status = 'success' AND r.new_count > 0")
        elif status == 'no_new':
            where_clauses.append("r.status = 'success' AND r.new_count = 0")
        elif status == 'failed':
            where_clauses.append("r.status = 'failed'")

        where_sql = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(f'SELECT COUNT(*) FROM monitor_runs r {where_sql}', params)
                total = cursor.fetchone()[0] or 0

                total_pages = max(1, (total + per_page - 1) // per_page)
                if page > total_pages:
                    page = total_pages
                offset = (page - 1) * per_page

                query_params = list(params) + [per_page, offset]
                cursor.execute(f'''
                    SELECT r.*, c.name as config_name
                    FROM monitor_runs r
                    LEFT JOIN monitor_configs c ON r.config_id = c.id
                    {where_sql}
                    ORDER BY r.id DESC
                    LIMIT ? OFFSET ?
                ''', query_params)

                columns = [col[0] for col in cursor.description]
                runs = [dict(zip(columns, row)) for row in cursor.fetchall()]

                has_prev = page > 1
                has_next = page < total_pages

                return {
                    'runs': runs,
                    'total': total,
                    'page': page,
                    'per_page': per_page,
                    'total_pages': total_pages,
                    'has_prev': has_prev,
                    'has_next': has_next,
                    'prev_page': page - 1 if has_prev else None,
                    'next_page': page + 1 if has_next else None,
                    'iter_pages': _generate_iter_pages(page, total_pages),
                    'status': status
                }
        except Exception as e:
            logger.error(f"获取分页监控运行记录失败: {e}")
            return {
                'runs': [],
                'total': 0,
                'page': page,
                'per_page': per_page,
                'total_pages': 1,
                'has_prev': False,
                'has_next': False,
                'prev_page': None,
                'next_page': None,
                'iter_pages': [1],
                'status': status
            }

    def get_monitor_run_details(self, run_id: int) -> Optional[Dict[str, Any]]:
        """获取指定监控运行的详情与本次发现的视频"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT r.*, c.name as config_name
                    FROM monitor_runs r
                    LEFT JOIN monitor_configs c ON r.config_id = c.id
                    WHERE r.id = ?
                ''', (int(run_id),))
                row = cursor.fetchone()
                if not row:
                    return None
                columns = [col[0] for col in cursor.description]
                run_data = dict(zip(columns, row))

                cursor.execute('''
                    SELECT * FROM monitor_history
                    WHERE run_id = ?
                    ORDER BY id ASC
                ''', (int(run_id),))
                v_cols = [col[0] for col in cursor.description]
                videos = [dict(zip(v_cols, r)) for r in cursor.fetchall()]
                for v in videos:
                    if not v.get('thumbnail_url'):
                        vid = v.get('video_id', '')
                        v['thumbnail_url'] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ''

                return {
                    'run': run_data,
                    'videos': videos
                }
        except Exception as e:
            logger.error(f"获取监控运行详情(ID: {run_id})失败: {e}")
            return None

    def get_monitor_history_stats(self, config_id=None) -> Dict[str, Any]:
        """获取监控历史记录的全局统计数据（总数、已添加数、未添加数、平均播放量、平均点赞量）"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                where_clause = 'WHERE config_id = ?' if config_id and int(config_id) > 0 else ''
                params = (int(config_id),) if config_id and int(config_id) > 0 else ()
                cursor.execute(f'''
                    SELECT 
                        COUNT(*) as total_records,
                        SUM(CASE WHEN added_to_tasks = 1 THEN 1 ELSE 0 END) as added_to_tasks,
                        AVG(view_count) as avg_views,
                        AVG(like_count) as avg_likes
                    FROM monitor_history
                    {where_clause}
                ''', params)
                row = cursor.fetchone()
                total = row[0] or 0
                added = row[1] or 0
                unadded = total - added
                return {
                    'total_records': total,
                    'added_to_tasks': added,
                    'unadded_records': unadded,
                    'avg_views': int(row[2] or 0),
                    'avg_likes': int(row[3] or 0)
                }
        except Exception as e:
            logger.error(f"获取监控历史统计数据失败: {e}")
            return {
                'total_records': 0,
                'added_to_tasks': 0,
                'unadded_records': 0,
                'avg_views': 0,
                'avg_likes': 0
            }

    def get_monitor_history_paginated(self, config_id=None, status=None, page=1, per_page=20, run_id=None, order_by='run_time_desc') -> Dict[str, Any]:
        """
        获取分页的监控历史记录，支持状态筛选 (全部 / 未添加 / 已添加) 及按运行批次筛选
        
        Args:
            config_id: 监控配置ID（为 None 或 <=0 表示全部）
            status: 状态筛选，'all'(全部), 'unadded'(未添加), 'added'(已添加)
            page: 页码，从 1 开始
            per_page: 每页显示条数，默认 20
            run_id: 指定监控运行批次ID（可选）
            order_by: 排序方式 ('run_time_desc', 'view_count', 'published_at')
            
        Returns:
            dict: 包含 records, total, page, per_page, total_pages, has_prev, has_next, prev_page, next_page, iter_pages, status 等
        """
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1
            
        try:
            per_page = max(1, min(100, int(per_page)))
        except (ValueError, TypeError):
            per_page = 20

        status = str(status or 'all').strip().lower()
        if status not in ('all', 'unadded', 'added'):
            status = 'all'

        where_clauses = []
        params = []

        if config_id and int(config_id) > 0:
            where_clauses.append('h.config_id = ?')
            params.append(int(config_id))

        if run_id and int(run_id) > 0:
            where_clauses.append('h.run_id = ?')
            params.append(int(run_id))

        if status == 'unadded':
            where_clauses.append('h.added_to_tasks = 0')
        elif status == 'added':
            where_clauses.append('h.added_to_tasks = 1')

        where_sql = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        order_sql = 'h.run_time DESC, h.id DESC'
        if order_by == 'view_count':
            order_sql = 'h.view_count DESC, h.id DESC'
        elif order_by == 'published_at':
            order_sql = 'h.published_at DESC, h.id DESC'

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 1. 查询总数
                cursor.execute(f'SELECT COUNT(*) FROM monitor_history h {where_sql}', params)
                total = cursor.fetchone()[0] or 0
                
                total_pages = max(1, (total + per_page - 1) // per_page)
                if page > total_pages:
                    page = total_pages
                offset = (page - 1) * per_page
                
                # 2. 查询当前页记录
                query_params = list(params) + [per_page, offset]
                cursor.execute(f'''
                    SELECT h.*, c.name as config_name 
                    FROM monitor_history h
                    LEFT JOIN monitor_configs c ON h.config_id = c.id
                    {where_sql}
                    ORDER BY {order_sql}
                    LIMIT ? OFFSET ?
                ''', query_params)
                
                columns = [description[0] for description in cursor.description]
                records = []
                for row in cursor.fetchall():
                    record = dict(zip(columns, row))
                    if not record.get('thumbnail_url'):
                        vid = record.get('video_id', '')
                        record['thumbnail_url'] = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ''
                    records.append(record)
                    
                has_prev = page > 1
                has_next = page < total_pages
                
                return {
                    'records': records,
                    'total': total,
                    'page': page,
                    'per_page': per_page,
                    'total_pages': total_pages,
                    'has_prev': has_prev,
                    'has_next': has_next,
                    'prev_page': page - 1 if has_prev else None,
                    'next_page': page + 1 if has_next else None,
                    'iter_pages': _generate_iter_pages(page, total_pages),
                    'status': status,
                    'run_id': run_id,
                    'order_by': order_by
                }
        except Exception as e:
            logger.error(f"获取分页监控历史失败: {e}")
            return {
                'records': [],
                'total': 0,
                'page': page,
                'per_page': per_page,
                'total_pages': 1,
                'has_prev': False,
                'has_next': False,
                'prev_page': None,
                'next_page': None,
                'iter_pages': [1],
                'status': status,
                'run_id': run_id,
                'order_by': order_by
            }
    
    def clear_monitor_history(self, config_id):
        """清除指定配置的监控历史记录"""
        logger.info(f"开始清除监控历史记录，配置ID: {config_id}")
        
        try:
            # 获取配置信息用于日志
            config = self.get_monitor_config(config_id)
            config_name = config['name'] if config else f"ID-{config_id}"
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 获取要删除的记录数
                cursor.execute('SELECT COUNT(*) FROM monitor_history WHERE config_id = ?', (config_id,))
                count = cursor.fetchone()[0]
                
                # 获取关联的 video_id 用于清除封面缓存
                cursor.execute('SELECT video_id FROM monitor_history WHERE config_id = ?', (config_id,))
                video_ids = [row[0] for row in cursor.fetchall() if row[0]]
                
                # 无论是否有历史记录，均重置该配置的历史偏移量与上次运行时间，确保状态彻底重置
                cursor.execute('''
                    UPDATE monitor_configs 
                    SET historical_offset = 0, historical_progress_date = '', last_run_time = NULL 
                    WHERE id = ?
                ''', (config_id,))
                
                if count > 0:
                    # 删除历史记录与运行记录
                    cursor.execute('DELETE FROM monitor_history WHERE config_id = ?', (config_id,))
                    cursor.execute('DELETE FROM monitor_runs WHERE config_id = ?', (config_id,))
                    conn.commit()
                    self._clear_cover_cache(video_ids)
                    logger.info(f"已清除配置 {config_name} (ID: {config_id}) 的 {count} 条监控历史记录并重置历史状态")
                    return True, f"成功清除 {count} 条历史记录并重置状态"
                else:
                    cursor.execute('DELETE FROM monitor_runs WHERE config_id = ?', (config_id,))
                    conn.commit()
                    logger.info(f"配置 {config_name} (ID: {config_id}) 没有历史记录需要清除（已重置运行状态）")
                    return True, "没有历史记录需要清除（已重置运行状态）"
                    
        except Exception as e:
            logger.error(f"清除监控历史记录失败，配置ID: {config_id}, 错误: {str(e)}")
            return False, f"清除历史记录失败: {str(e)}"
    
    def _clear_cover_cache(self, video_ids=None):
        """清空封面本地缓存文件"""
        try:
            from modules.utils import get_app_subdir
            cache_dir = os.path.join(get_app_subdir('cache'), 'monitor_covers')
            if not os.path.exists(cache_dir):
                return
            if video_ids:
                for vid in video_ids:
                    fpath = os.path.join(cache_dir, f'{vid}.jpg')
                    if os.path.exists(fpath):
                        os.remove(fpath)
            else:
                for fname in os.listdir(cache_dir):
                    fpath = os.path.join(cache_dir, fname)
                    if os.path.isfile(fpath):
                        os.remove(fpath)
        except Exception as e:
            logger.warning(f"清除封面本地缓存失败: {e}")

    def clear_all_monitor_history(self):
        """清除所有监控历史记录并重置所有配置的运行状态与封面缓存"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # 获取要删除的记录数
                cursor.execute('SELECT COUNT(*) FROM monitor_history')
                count = cursor.fetchone()[0]
                
                # 重置所有配置的运行状态、偏移量与进度，彻底清除排重历史痕迹
                cursor.execute('''
                    UPDATE monitor_configs 
                    SET historical_offset = 0, historical_progress_date = '', last_run_time = NULL
                ''')
                
                # 删除所有历史记录与运行记录
                cursor.execute('DELETE FROM monitor_history')
                cursor.execute('DELETE FROM monitor_runs')
                conn.commit()
                self._clear_cover_cache()
                logger.info(f"已清除所有监控历史记录与执行记录，并重置所有配置状态与封面缓存")
                return True, "成功清除所有监控历史与执行记录并重置运行状态"
                    
        except Exception as e:
            logger.error(f"清除所有监控历史记录失败: {str(e)}")
            return False, f"清除历史记录失败: {str(e)}"
    
    def delete_monitor_history_records(self, record_ids):
        """删除指定的监控历史记录（支持单条或多选批量删除）"""
        if not record_ids:
            return False, "未指定要删除的记录"

        try:
            # 标准化为整数列表
            if isinstance(record_ids, (int, str)):
                clean_ids = [int(record_ids)] if str(record_ids).isdigit() else []
            else:
                clean_ids = [int(rid) for rid in record_ids if str(rid).isdigit()]

            if not clean_ids:
                return False, "无效的记录ID"

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                placeholders = ','.join('?' * len(clean_ids))

                # 获取关联的 video_id 用于清理封面缓存
                cursor.execute(f'SELECT video_id FROM monitor_history WHERE id IN ({placeholders})', clean_ids)
                video_ids = [row[0] for row in cursor.fetchall() if row[0]]

                # 执行删除
                cursor.execute(f'DELETE FROM monitor_history WHERE id IN ({placeholders})', clean_ids)
                deleted_count = cursor.rowcount
                conn.commit()

                if video_ids:
                    # 检查是否还有其它记录引用这些 video_id
                    placeholders_v = ','.join('?' * len(video_ids))
                    cursor.execute(f'SELECT DISTINCT video_id FROM monitor_history WHERE video_id IN ({placeholders_v})', video_ids)
                    still_used = {row[0] for row in cursor.fetchall()}
                    to_remove_covers = [vid for vid in video_ids if vid not in still_used]
                    if to_remove_covers:
                        self._clear_cover_cache(to_remove_covers)

                logger.info(f"成功删除 {deleted_count} 条监控历史记录: {clean_ids}")
                return True, f"成功删除 {deleted_count} 条记录"
        except Exception as e:
            logger.error(f"删除监控历史记录失败: {str(e)}")
            return False, f"删除失败: {str(e)}"

    def get_auto_enqueue_config(self) -> Dict[str, Any]:
        """获取定时入队配置（单例记录）"""
        default_cfg = dict(AUTO_ENQUEUE_FIELD_DEFAULTS)
        default_cfg['id'] = 1
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM monitor_auto_enqueue_config WHERE id = 1')
                row = cursor.fetchone()
                if row:
                    cfg = dict(row)
                    cfg['enabled'] = bool(cfg.get('enabled'))
                    return cfg
        except Exception as e:
            logger.error(f"获取定时入队配置失败: {e}")
        return default_cfg

    def update_auto_enqueue_config(self, config_data: Dict[str, Any]) -> Tuple[bool, str]:
        """更新定时入队配置并同步备份及重载调度"""
        try:
            enabled = 1 if config_data.get('enabled') in (True, 1, '1', 'true', 'on') else 0
            schedule_type = str(config_data.get('schedule_type', 'daily_times')).strip()
            if schedule_type not in ('daily_times', 'auto'):
                schedule_type = 'daily_times'
            
            schedule_time_points = str(config_data.get('schedule_time_points', '')).strip()
            try:
                schedule_interval = max(5, min(1440, int(config_data.get('schedule_interval', 60))))
            except (ValueError, TypeError):
                schedule_interval = 60
                
            try:
                batch_count = max(1, min(50, int(config_data.get('batch_count', 2))))
            except (ValueError, TypeError):
                batch_count = 2
                
            order_by = str(config_data.get('order_by', 'view_count')).strip()
            if order_by not in ('view_count', 'published_at', 'run_time_desc', 'run_time_asc'):
                order_by = 'view_count'
                
            try:
                filter_config_id = int(config_data.get('filter_config_id', 0))
            except (ValueError, TypeError):
                filter_config_id = 0

            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO monitor_auto_enqueue_config (
                        id, enabled, schedule_type, schedule_time_points, schedule_interval,
                        batch_count, order_by, filter_config_id, updated_time
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                    ON CONFLICT(id) DO UPDATE SET
                        enabled = excluded.enabled,
                        schedule_type = excluded.schedule_type,
                        schedule_time_points = excluded.schedule_time_points,
                        schedule_interval = excluded.schedule_interval,
                        batch_count = excluded.batch_count,
                        order_by = excluded.order_by,
                        filter_config_id = excluded.filter_config_id,
                        updated_time = excluded.updated_time
                ''', (
                    enabled, schedule_type, schedule_time_points, schedule_interval,
                    batch_count, order_by, filter_config_id
                ))
                conn.commit()

            self._backup_auto_enqueue_config()
            self.reload_auto_enqueue_schedule()
            logger.info(f"定时入队配置已更新: enabled={enabled}, type={schedule_type}, points={schedule_time_points}, batch={batch_count}")
            return True, "定时入队配置已更新并生效"
        except Exception as e:
            logger.error(f"更新定时入队配置失败: {e}")
            return False, f"更新配置失败: {str(e)}"

    def _backup_auto_enqueue_config(self):
        """备份定时入队配置到 JSON 文件"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            os.makedirs(config_dir, exist_ok=True)
            cfg_path = os.path.join(config_dir, 'auto_enqueue_config.json')
            cfg = self.get_auto_enqueue_config()
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            logger.info("定时入队配置已备份到 JSON 文件")
        except Exception as e:
            logger.error(f"备份定时入队配置失败: {e}")

    def _restore_auto_enqueue_config_from_file(self):
        """从 JSON 文件恢复定时入队配置到数据库"""
        try:
            config_dir = os.path.join(get_app_subdir('config'), 'youtube_monitor')
            cfg_path = os.path.join(config_dir, 'auto_enqueue_config.json')
            if not os.path.exists(cfg_path):
                return
            with open(cfg_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data and isinstance(data, dict):
                logger.info("从配置文件中恢复定时入队配置")
                self.update_auto_enqueue_config(data)
        except Exception as e:
            logger.error(f"从配置文件恢复定时入队配置失败: {e}")

    def _update_auto_enqueue_run_status(self, status_msg: str):
        """更新定时入队最后运行时间和状态"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE monitor_auto_enqueue_config
                    SET last_run_time = datetime('now', 'localtime'),
                        last_run_status = ?
                    WHERE id = 1
                ''', (status_msg,))
                conn.commit()
        except Exception as e:
            logger.error(f"更新定时入队运行状态失败: {e}")

    def get_unadded_history_count(self, config_id: Optional[int] = None) -> int:
        """获取当前待入队的监控记录数量（素材池）"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if config_id and config_id > 0:
                    cursor.execute('SELECT COUNT(*) FROM monitor_history WHERE added_to_tasks = 0 AND config_id = ?', (config_id,))
                else:
                    cursor.execute('SELECT COUNT(*) FROM monitor_history WHERE added_to_tasks = 0')
                row = cursor.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"获取待入队记录数失败: {e}")
            return 0

    def execute_auto_enqueue(self, trigger_type='scheduled') -> Tuple[bool, str, int]:
        """执行自动入队任务：从监控历史中检索未添加的视频，按设定规则批量推送到任务队列"""
        logger.info(f"开始执行自动入队任务, 触发源: {trigger_type}")
        cfg = self.get_auto_enqueue_config()
        if trigger_type == 'scheduled' and not cfg.get('enabled'):
            logger.info("定时入队任务未启用，跳过调度执行")
            return False, "定时入队任务已禁用", 0

        batch_count = cfg.get('batch_count', 2)
        order_by = cfg.get('order_by', 'view_count')
        filter_config_id = cfg.get('filter_config_id', 0)

        order_clause_map = {
            'view_count': 'h.view_count DESC, h.id DESC',
            'published_at': 'h.published_at DESC, h.id DESC',
            'run_time_desc': 'h.run_time DESC, h.id DESC',
            'run_time_asc': 'h.id ASC'
        }
        order_clause = order_clause_map.get(order_by, 'h.view_count DESC, h.id DESC')

        where_clauses = ['h.added_to_tasks = 0']
        params = []
        if filter_config_id and filter_config_id > 0:
            where_clauses.append('h.config_id = ?')
            params.append(filter_config_id)

        where_sql = ' AND '.join(where_clauses)
        query = f'''
            SELECT h.id, h.video_id, h.video_title, h.channel_title, h.view_count, h.published_at
            FROM monitor_history h
            WHERE {where_sql}
            ORDER BY {order_clause}
            LIMIT ?
        '''
        params.append(batch_count)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

        if not rows:
            status_msg = "待入队池为空（无未添加的视频记录）"
            logger.info(f"自动入队: {status_msg}")
            self._update_auto_enqueue_run_status(status_msg)
            return True, status_msg, 0

        record_ids = [row[0] for row in rows]
        logger.info(f"自动入队选中 {len(record_ids)} 条待添加记录: {record_ids}")

        success, msg, added_ids = self.batch_add_to_tasks(record_ids)
        added_count = len(added_ids)
        run_status = f"成功添加 {added_count} 个视频到队列" if success else f"添加失败: {msg}"
        self._update_auto_enqueue_run_status(run_status)
        logger.info(f"自动入队执行完成: {run_status}")
        return success, run_status, added_count

    def _remove_auto_enqueue_schedule(self):
        """移除现有的自动入队调度任务"""
        try:
            if not self.scheduler:
                return
            for job in list(self.scheduler.get_jobs()):
                if job.id.startswith("auto_enqueue_"):
                    self.scheduler.remove_job(job.id)
                    logger.info(f"已移除自动入队任务: {job.id}")
        except Exception as e:
            logger.error(f"移除自动入队调度任务失败: {e}")

    def _schedule_auto_enqueue(self):
        """添加自动入队调度任务到 APScheduler"""
        try:
            self._remove_auto_enqueue_schedule()
            if not self.scheduler:
                return
            cfg = self.get_auto_enqueue_config()
            if not cfg or not cfg.get('enabled'):
                logger.info("定时入队任务未启用，不注册调度")
                return

            schedule_type = cfg.get('schedule_type', 'daily_times')
            if schedule_type == 'daily_times':
                time_points = parse_daily_time_points(cfg.get('schedule_time_points', ''))
                if not time_points:
                    logger.warning("定时入队启用了每日定点，但未设置任何有效时间点")
                    return
                for h, m in time_points:
                    job_id = f"auto_enqueue_cron_{h:02d}_{m:02d}"
                    self.scheduler.add_job(
                        func=self.execute_auto_enqueue,
                        trigger=CronTrigger(hour=h, minute=m),
                        id=job_id,
                        args=['scheduled'],
                        replace_existing=True
                    )
                    logger.info(f"已注册每日定点入队任务: {job_id}, 触发时间: {h:02d}:{m:02d}")
            elif schedule_type == 'auto':
                interval = cfg.get('schedule_interval', 60)
                job_id = "auto_enqueue_interval"
                self.scheduler.add_job(
                    func=self.execute_auto_enqueue,
                    trigger='interval',
                    minutes=interval,
                    id=job_id,
                    args=['scheduled'],
                    replace_existing=True
                )
                logger.info(f"已注册固定间隔入队任务: {job_id}, 间隔: {interval} 分钟")

            if not self.scheduler.running:
                self.scheduler.start()
        except Exception as e:
            logger.error(f"注册自动入队任务失败: {e}")

    def reload_auto_enqueue_schedule(self):
        """重新加载并生效自动入队任务调度"""
        self._schedule_auto_enqueue()
    
    def stop_all_schedules(self):
        """停止所有调度任务"""
        try:
            jobs = self.scheduler.get_jobs()
            for job in jobs:
                self.scheduler.remove_job(job.id)
            logger.info(f"已停止所有监控调度任务，共移除 {len(jobs)} 个任务")
        except Exception as e:
            logger.error(f"停止所有调度任务失败: {str(e)}")
    
    def restore_configs_from_files_manually(self):
        """手动从配置文件恢复监控配置"""
        logger.info("手动触发配置恢复...")
        
        try:
            # 停止所有现有调度
            self.stop_all_schedules()
            
            # 重新恢复配置
            self._restore_configs_from_files()
            
            # 重新启动调度
            self._restart_restored_schedules()
            
            logger.info("配置恢复完成")
            return True, "配置恢复完成"
            
        except Exception as e:
            logger.error(f"配置恢复失败: {str(e)}")
            return False, f"配置恢复失败: {str(e)}"
    
    def reset_historical_offset(self, config_id):
        """重置历史搬运偏移量"""
        logger.info(f"重置历史搬运偏移量，配置ID: {config_id}")
        
        try:
            # 获取配置信息用于日志
            config = self.get_monitor_config(config_id)
            if not config:
                return False, "配置不存在"
            
            config_name = config['name']
            current_offset = config.get('historical_offset', 0)
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE monitor_configs SET historical_offset = 0 WHERE id = ?',
                    (config_id,)
                )
                conn.commit()
            
            logger.info(f"配置 {config_name} (ID: {config_id}) 的历史搬运偏移量已从 {current_offset} 重置为 0")
            return True, f"偏移量已从 {current_offset} 重置为 0，将重新开始历史搬运"
            
        except Exception as e:
            logger.error(f"重置历史搬运偏移量失败，配置ID: {config_id}, 错误: {str(e)}")
            return False, f"重置偏移量失败: {str(e)}"

# 全局监控实例
youtube_monitor = YouTubeMonitor()
