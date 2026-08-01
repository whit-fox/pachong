# -*- coding: utf-8 -*-
"""
utils.py —— 通用工具函数

功能清单：
1. 日志初始化（setup_logging / get_logger）
2. 文件名清洗（去除 Windows / Linux 非法字符、限制长度）
3. 相对地址 / 协议相对地址 -> 绝对地址（resolve_url）
4. 视频链接识别（is_video_url）
5. 带指数退避重试的 HTTP 请求封装（request_with_retry）
6. Cookie 文件解析（支持 Netscape 与 JSON 两种格式）
7. 字节数格式化为可读大小（human_size）
8. data: URI 解码（decode_data_uri，用于 m3u8 内联密钥）
"""

import json
import logging
import os
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests

from config import (AUTO_DETECT_PROXY, DEFAULT_HEADERS, MAX_RETRIES, PROXY,
                    PROXY_PORTS, RETRY_BACKOFF, TIMEOUT)

# 常见视频 / 流媒体文件扩展名
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv", ".avi", ".m3u8")


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """初始化全局日志配置（重复调用不会重复添加 handler）"""
    logger = logging.getLogger()
    if logger.handlers:            # 已配置过则直接返回，避免重复输出
        logger.setLevel(level)
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 logger（用于各模块分类输出）"""
    return logging.getLogger(name)


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清洗文件名，去掉系统非法字符并限制长度

    Windows 非法字符: \\ / : * ? " < > |，这些会替换为下划线。
    另外去掉控制字符、首尾空格和点，避免路径安全问题。
    """
    name = str(name).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)   # 非法字符替换
    name = "".join(ch for ch in name if ord(ch) >= 32)  # 去掉控制字符
    name = name.strip().strip(".")                      # 去首尾空白和点
    if len(name) > max_len:
        name = name[:max_len]
    return name or "download"                           # 空文件名兜底


def resolve_url(base_url: str, url: str) -> str:
    """把相对地址解析为完整绝对地址

    例：base = "https://a.com/video/index.html", url = "../media/1.mp4"
        -> "https://a.com/media/1.mp4"
    另外处理协议相对地址（//cdn.com/a.mp4 -> https://cdn.com/a.mp4）。
    """
    if not url:
        return ""
    if url.startswith("//"):
        scheme = urlparse(base_url).scheme or "https"
        return f"{scheme}:{url}"
    return urljoin(base_url, url)


def is_video_url(url: str) -> bool:
    """判断 URL 是否直接指向视频 / 流媒体文件"""
    if not url:
        return False
    return urlparse(url).path.lower().endswith(VIDEO_EXTENSIONS)


def detect_local_proxy(ports: tuple = PROXY_PORTS):
    """自动检测本机运行中的常见代理端口

    依次探测 127.0.0.1 上是否有端口在监听，返回形如
    "http://127.0.0.1:7897" 的代理地址；找不到返回 None。
    适用于 Clash(7890) / Clash Verge(7897) / V2rayN(10809) / Shadowsocks(1080) 等。
    """
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return None


def resolve_proxy() -> str | None:
    """确定要使用的代理地址，优先级：手动配置 > 环境变量 > 自动检测"""
    # 1. 配置文件里手动指定
    if PROXY:
        return PROXY
    # 2. 环境变量（requests 也认 HTTPS_PROXY）
    for env in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if os.environ.get(env):
            return os.environ[env]
    # 3. 自动检测本地代理工具
    if AUTO_DETECT_PROXY:
        return detect_local_proxy()
    return None


def build_session(cookies=None, headers: dict = None,
                  use_proxy: bool = False) -> requests.Session:
    """构造一个带默认请求头与 Cookie 的 requests.Session

    cookies 支持三种形态：
    - dict：{name: value}
    - 文件路径字符串：自动调用 parse_cookie_file 解析
    - 列表：[{"name":..., "value":...}, ...]

    代理：use_proxy=True 时自动应用 resolve_proxy() 的结果。
    典型用法：页面解析用直连（use_proxy=False，避免国内站被海外出口 403），
    视频文件下载走代理（use_proxy=True，海外 CDN 才连得上）。
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    if headers:
        session.headers.update(headers)
    if cookies:
        if isinstance(cookies, str):
            cookies = parse_cookie_file(cookies)
        elif isinstance(cookies, list):
            # 列表格式统一转成 dict
            cookies = {c["name"]: c.get("value", "") for c in cookies}
        session.cookies.update(cookies)
    if use_proxy:
        proxy = resolve_proxy()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
            logging.getLogger("http").info("使用代理: %s", proxy)
    return session


def request_with_retry(session: requests.Session, method: str, url: str,
                       retries: int = MAX_RETRIES, **kwargs) -> requests.Response:
    """带重试的 HTTP 请求封装

    - 网络中断（ConnectionError / Timeout）自动重试，指数退避
    - 服务端 5xx 错误也会重试
    - 4xx 客户端错误直接返回（重试无意义）
    用法与 requests.request 一致，额外支持 retries 关键字参数。
    """
    kwargs.setdefault("timeout", TIMEOUT)
    logger = get_logger("http")
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            if resp.status_code < 500:            # 2xx / 3xx / 4xx 直接返回
                return resp
            logger.warning("HTTP %s %s (attempt %d/%d)",
                           resp.status_code, url, attempt, retries)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            logger.warning("请求失败 %s (attempt %d/%d): %s",
                           url, attempt, retries, e)
        if attempt < retries:
            time.sleep(RETRY_BACKOFF ** attempt)  # 指数退避
    if last_exc:
        raise last_exc
    raise requests.HTTPError(f"请求失败（服务端持续返回错误），URL: {url}")


def parse_cookie_file(path: str) -> dict:
    """解析 Cookie 文件为 dict

    支持两种格式：
    1. Netscape 格式（curl -c cookies.txt 导出的传统格式）
       # HTTP Cookie File
       .example.com  TRUE  /  FALSE  1789456000  sid  abc123
    2. JSON 格式
       [{"name": "sid", "value": "abc", ...}]  或  {"sid": "abc"}
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Cookie 文件不存在: {path}")

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read().strip()

    # ---- JSON 格式 ----
    if content.startswith("["):
        data = json.loads(content)
        return {item["name"]: item.get("value", "") for item in data}
    if content.startswith("{"):
        return json.loads(content)

    # ---- Netscape 格式 ----
    # 每列以 TAB 分隔: domain  flag  path  secure  expiry  name  value
    cookies = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


def human_size(num_bytes: float) -> str:
    """把字节数格式化为可读的 B/KB/MB/GB 字符串"""
    num_bytes = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


def decode_data_uri(data_uri: str) -> bytes:
    """解析 data: URI，返回原始字节

    支持形如：
    - data:text/plain;base64,xxxxx
    - data:application/octet-stream;base64,xxxxx
    - data:application/octet-stream;charset=utf-8,xxxxx
    用于 m3u8 中内联的 AES-128 密钥。
    """
    header, sep, body = data_uri.partition(",")
    if not sep:
        return b""
    if ";base64" in header.lower():
        import base64
        return base64.b64decode(body)
    # 未 base64 编码则按 URL 编码文本处理
    from urllib.parse import unquote
    return unquote(body).encode("utf-8")


def ensure_dir(path: str) -> None:
    """确保目录存在（不存在则创建）"""
    os.makedirs(path, exist_ok=True)
