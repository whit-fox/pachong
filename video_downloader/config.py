# -*- coding: utf-8 -*-
"""
config.py —— 全局配置文件

集中管理所有可调参数，方便使用者按需修改：
- 请求参数（超时、重试、请求头）
- 下载参数（线程数、分片并发、块大小）
- m3u8 参数（分片目录、合并清理）
- 解析参数（iframe 深度、Playwright 等待时间）
"""

import sys
from pathlib import Path

# ============================================================
# 基础路径
# ============================================================
# 项目根目录：源码运行时是 config.py 所在目录；
# 打包成 exe 后，__file__ 指向临时解压目录，改用 exe 所在目录，
# 这样下载文件、urls.txt 都会保存到 exe 旁边。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# 默认下载目录（可通过命令行 -o 覆盖）
DEFAULT_DOWNLOAD_DIR = BASE_DIR / "downloads"

# ============================================================
# 代理配置
# ============================================================
# 手动指定代理（如 "http://127.0.0.1:7897"）。None 时走自动检测。
PROXY = None
# 是否自动检测本机运行中的常见代理工具端口（Clash/V2ray/Shadowsocks 等）
AUTO_DETECT_PROXY = True
# 自动检测时依次尝试的常见代理端口
PROXY_PORTS = (7897, 7890, 10809, 1080, 8888)

# ============================================================
# HTTP 请求配置
# ============================================================
# 默认请求头，模拟真实浏览器，降低被拦截概率
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# 单次请求超时时间（秒）
TIMEOUT = 20
# 单个 HTTP 请求最大重试次数
MAX_RETRIES = 5
# 指数退避的底数：重试等待 = RETRY_BACKOFF ** 尝试次数
RETRY_BACKOFF = 1.5

# ============================================================
# 下载配置
# ============================================================
# mp4 多线程分片下载线程数
THREADS = 8
# m3u8 中 ts 分片的并发下载数（aiohttp 并发请求量）
SEGMENT_CONCURRENCY = 10
# 流式读取的块大小（字节），影响内存占用与磁盘 IO 频率
CHUNK_SIZE = 64 * 1024
# 分片临时文件后缀（下载中断后下次启动可续传）
PART_FILE_SUFFIX = ".part"

# ============================================================
# m3u8 配置
# ============================================================
# ts 分片存放的临时目录名（相对于输出目录）
M3U8_SEGMENT_DIR = "segments"
# 合并完成后是否自动清理临时分片
MERGE_CLEANUP = True

# ============================================================
# 页面解析配置
# ============================================================
# 递归解析 <iframe> 的最大深度（防止无限嵌套）
MAX_IFRAME_DEPTH = 2
# Playwright 等待页面 DOM 加载的毫秒数
PLAYWRIGHT_WAIT = 5000
# Playwright 等待 <video> 元素出现 / 媒体接口请求的毫秒数
PLAYWRIGHT_WAIT_VIDEO = 10000
