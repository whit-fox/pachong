# -*- coding: utf-8 -*-
"""
main.py —— 程序入口

工作流程：解析页面 -> 挑选视频源 -> 下载（mp4 直链 / m3u8 流）-> 输出

用法示例：
    # 解析网页并自动下载
    python main.py https://example.com/video.html

    # 指定输出目录 + m3u8 清晰度
    python main.py https://example.com/video.html -o D:/videos --quality 1080

    # 直接下载 mp4 / m3u8 直链
    python main.py https://example.com/video.mp4
    python main.py https://example.com/playlist.m3u8

    # 带 Cookie（登录态）与 Referer（防盗链）
    python main.py https://example.com/video.html --cookies cookies.txt --referer https://example.com

    # JS 动态渲染页面（需要 playwright + 浏览器内核）
    python main.py https://example.com/dynamic.html --browser

    # 调试模式，输出详细日志
    python main.py https://example.com/video.html --debug
"""

import argparse
import logging
import os
import sys
import urllib3
from urllib.parse import urlparse

from config import DEFAULT_DOWNLOAD_DIR
from downloader import Downloader
from m3u8_downloader import M3U8Downloader
from parser import Parser, VideoInfo
from utils import get_logger, is_video_url, sanitize_filename, setup_logging

# 忽略证书校验告警（部分站点证书链不完整）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = get_logger("main")


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="视频下载工具：解析网页中的 mp4 / m3u8 并下载",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("urls", nargs="+", help="视频网页 URL，支持一次传入多个")
    parser.add_argument("-o", "--output", default=str(DEFAULT_DOWNLOAD_DIR),
                        help="输出目录")
    parser.add_argument("--quality", default="best",
                        help="m3u8 清晰度：best / lowest / 或具体分辨率如 1080、720")
    parser.add_argument("--threads", type=int, default=8,
                        help="mp4 多线程分片下载线程数")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="m3u8 分片并发下载数")
    parser.add_argument("--cookies", default=None,
                        help="Cookie 文件路径（支持 Netscape / JSON 格式）")
    parser.add_argument("--referer", default=None,
                        help="自定义 Referer，用于防盗链（默认用页面 URL）")
    parser.add_argument("--proxy", default=None,
                        help="代理地址（如 http://127.0.0.1:7897）。视频在海外 CDN 时需走代理")
    parser.add_argument("--browser", action="store_true",
                        help="使用 Playwright 渲染 JS 动态页面（需安装浏览器内核）")
    parser.add_argument("--keep-ts", action="store_true",
                        help="合并后保留 ts 分片临时文件")
    parser.add_argument("--format", choices=["mp4", "ts"], default="mp4",
                        help="m3u8 输出格式：mp4（ffmpeg 合并）或 ts（保留 TS 容器）")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    return parser.parse_args()


def build_filename(chosen: VideoInfo, page_url: str, fmt: str) -> str:
    """根据视频类型生成合理文件名"""
    title = sanitize_filename(chosen.title or "video")
    if chosen.kind == "m3u8":
        # m3u8 合并后按 --format 决定扩展名
        return f"{title}.{'mp4' if fmt == 'mp4' else 'ts'}"
    # mp4 直链优先用 URL 里的文件名（保留清晰度等信息）
    name = os.path.basename(urlparse(chosen.url).path)
    if not name or "." not in name:
        name = f"{title}.mp4"
    return name


def process_one(page_url: str, args: argparse.Namespace) -> None:
    """处理单个 URL 的完整下载流程"""
    logger.info("=" * 64)
    logger.info("处理: %s", page_url)

    # ---- 0. 直链直接下载，跳过页面解析 ----
    if is_video_url(page_url):
        kind = "m3u8" if page_url.lower().endswith(".m3u8") else "mp4"
        results = [VideoInfo(page_url, kind, "直接输入",
                             title=page_url.rsplit("/", 1)[-1].split("?")[0])]
    else:
        # ---- 1. 解析页面，找出候选视频地址 ----
        parser = Parser()
        results = parser.parse(page_url, use_browser=args.browser)

    if not results:
        logger.error("未找到视频地址！")
        logger.error("排查建议：1) 用 --debug 看解析日志; 2) 用 --browser 渲染动态页面;"
                     " 3) 参考 README《调试指南》手工定位真实地址")
        return

    # 打印所有候选（带来源，便于判断哪个才是真源）
    logger.info("共找到 %d 个候选视频源:", len(results))
    for idx, r in enumerate(results, 1):
        logger.info("  [%d] [%s] %s", idx, r.kind.upper(), r.url)
        logger.info("      来源: %s", r.source)

    # ---- 2. 选择第一个（parser 已按 m3u8 优先排序）----
    chosen = results[0]
    logger.info("选择下载: [%s] %s", chosen.kind.upper(), chosen.url)
    filename = build_filename(chosen, page_url, args.format)
    referer = args.referer or page_url

    # ---- 3. 按类型分派下载 ----
    try:
        if chosen.kind == "m3u8":
            dl = M3U8Downloader(
                chosen.url, args.output, filename=filename,
                referer=referer, cookies=args.cookies,
                concurrency=args.concurrency, quality=args.quality,
                keep_ts=args.keep_ts,
            )
            out = dl.download()
        else:
            dl = Downloader(
                chosen.url, args.output, filename=filename,
                referer=referer, cookies=args.cookies,
                threads=args.threads,
            )
            out = dl.download()
        logger.info("★ 下载完成: %s", out)
    except KeyboardInterrupt:
        logger.warning("用户中断下载（下次运行可断点续传）")
        sys.exit(130)
    except Exception as e:
        logger.error("下载失败: %s", e)
        if args.debug:
            import traceback
            traceback.print_exc()


def main() -> None:
    args = parse_args()
    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    os.makedirs(args.output, exist_ok=True)

    # 命令行 --proxy 覆盖 config 里的代理设置
    if args.proxy:
        import config
        config.PROXY = args.proxy

    for page_url in args.urls:
        process_one(page_url, args)


if __name__ == "__main__":
    main()
