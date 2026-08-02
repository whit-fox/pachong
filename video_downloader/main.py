# -*- coding: utf-8 -*-
"""
main.py —— 程序入口

工作流程：解析页面 -> 挑选视频源 -> 下载（mp4 直链 / m3u8 流）-> 输出

用法示例：
    # 解析网页并自动下载
    python main.py https://example.com/video.html

    # 直接点 PyCharm 运行（不传参数，交互式输入 URL）
    python main.py

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
import datetime
import io
import logging
import os
import sys
import traceback
import urllib3
from urllib.parse import urlparse

# ================= 崩溃诊断（必须在项目模块导入之前） =================
# 双击运行闪退时，把错误写入 exe 旁的"错误日志.txt"，便于排查。
# 注意：exe 打包后 __file__ 指向临时目录，这里统一用可执行文件所在目录。
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ERR_LOG = os.path.join(_APP_DIR, "错误日志.txt")


def _write_log(text: str) -> None:
    """把诊断信息追加写入错误日志（写失败也绝不抛异常）"""
    try:
        with open(_ERR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now()}] {text}\n")
    except Exception:
        pass


_write_log("程序启动")


def _excepthook(exc_type, exc_value, exc_tb) -> None:
    """兜底所有未捕获异常：写入日志 + 保持窗口"""
    _write_log("未捕获异常:\n" + "".join(
        traceback.format_exception(exc_type, exc_value, exc_tb)))
    try:
        print(f"\n程序发生错误，详情见: {_ERR_LOG}")
    except Exception:
        pass
    try:
        input("按回车键退出...")
    except Exception:
        pass


sys.excepthook = _excepthook

# 部分字符（如 ❌）在 GBK 控制台编码会抛 UnicodeEncodeError 导致闪退，
# 这里把编码错误替换为占位符，宁可显示不全也不崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# windowed（无黑窗口）模式下 stdout/stderr 为 None，print 会崩，
# 这里换成空缓冲，保证 print 永远安全。
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

# 打包后 tkinter 找不到 tcl/tk 数据目录，手动指到解压目录（须在 import tkinter 之前）
if getattr(sys, "frozen", False):
    _mei = getattr(sys, "_MEIPASS", "")
    if _mei:
        os.environ.setdefault("TCL_LIBRARY", os.path.join(_mei, "tcl8.6"))
        os.environ.setdefault("TK_LIBRARY", os.path.join(_mei, "tk8.6"))
# ================= 崩溃诊断结束 =================

from config import BASE_DIR, DEFAULT_DOWNLOAD_DIR
from downloader import Downloader
from m3u8_downloader import M3U8Downloader
from parser import Parser, VideoInfo
from utils import get_logger, is_video_url, sanitize_filename, setup_logging

# 忽略证书校验告警（部分站点证书链不完整）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = get_logger("main")

# 当前正在下载的下载器实例，供 GUI 的"暂停/继续"按钮访问
current_downloader = None


def load_saved_urls() -> list[str]:
    """从 urls.txt 文件中读取预存的 URL（一行一个，# 开头为注释）"""
    url_file = str(BASE_DIR / "urls.txt")
    if not os.path.exists(url_file):
        return []
    urls = []
    with open(url_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def save_url(url: str) -> None:
    """把 URL 追加保存到 urls.txt（去重）"""
    url_file = str(BASE_DIR / "urls.txt")
    existing = load_saved_urls()
    if url not in existing:
        with open(url_file, "a", encoding="utf-8") as f:
            f.write(url + "\n")


def _safe_input(prompt: str = "") -> str:
    """安全读取输入：stdin 被关闭/重定向时返回空串，避免 EOFError 闪退"""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def interactive_select_url() -> str | None:
    """交互式选择或输入 URL，返回选中的 URL，用户取消返回 None"""
    saved = load_saved_urls()
    print("\n" + "=" * 64)
    print("  视频下载工具")
    print("=" * 64)

    if saved:
        print("\n已保存的 URL（输入序号选择，或直接粘贴新 URL）：")
        for i, u in enumerate(saved, 1):
            print(f"  [{i}] {u}")
        print(f"  [N] 粘贴新 URL")
        print(f"  [Q] 退出")
        print()
        choice = _safe_input("请选择: ")

        if choice.upper() == "Q":
            return None
        if choice.upper() == "N" or choice == "":
            pass  # 走下面的粘贴逻辑
        elif choice.isdigit() and 1 <= int(choice) <= len(saved):
            return saved[int(choice) - 1]
        else:
            # 用户直接粘贴了 URL
            return choice

    # 没有保存的 URL，直接让用户粘贴
    print("\n请输入视频页面 URL：")
    url = _safe_input("URL: ")
    if not url or url.upper() in ("Q", "QUIT", "EXIT"):
        return None
    return url


def parse_args() -> argparse.Namespace:
    """解析命令行参数。不传 URL 时进入交互模式。"""
    parser = argparse.ArgumentParser(
        description="视频下载工具：解析网页中的 mp4 / m3u8 并下载",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("urls", nargs="*", help="视频网页 URL，支持一次传入多个。不传则交互式输入")
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
    parser.add_argument("--console", action="store_true",
                        help="使用命令行模式（不弹出图形界面）")
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


def process_one(page_url: str, args: argparse.Namespace,
                progress_cb=None) -> bool:
    """处理单个 URL 的完整下载流程。返回 True 表示成功。

    progress_cb(downloaded, total, speed)：下载进度回调，供 GUI 使用。
    """
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

    # ---- 2. 静态解析失败时，自动尝试浏览器模式 ----
    if not results and not args.browser:
        logger.warning("静态解析未找到视频地址，自动启用浏览器模式重试...")
        logger.warning("（以后可直接加 --browser 跳过此步骤）")
        parser2 = Parser()
        results = parser2.parse(page_url, use_browser=True)

    if not results:
        logger.error("=" * 64)
        logger.error("未找到视频地址！")
        logger.error("=" * 64)
        logger.error("可能原因：")
        logger.error("  1) 网站使用 JS 动态加载视频 → 请加 --browser 参数")
        logger.error("  2) 网站需要登录 → 请用 --cookies 传入 Cookie")
        logger.error("  3) 视频地址被加密 → 浏览器模式会自动尝试解密")
        logger.error("  4) 目标页面本身不包含视频（可能是列表页/详情页）")
        logger.error("=" * 64)
        return False

    # 打印所有候选（带来源，便于判断哪个才是真源）
    logger.info("共找到 %d 个候选视频源:", len(results))
    for idx, r in enumerate(results, 1):
        logger.info("  [%d] [%s] %s", idx, r.kind.upper(), r.url)
        logger.info("      来源: %s", r.source)

    # ---- 3. 选择第一个（parser 已按 m3u8 优先排序）----
    chosen = results[0]
    logger.info("选择下载: [%s] %s", chosen.kind.upper(), chosen.url)
    filename = build_filename(chosen, page_url, args.format)
    referer = args.referer or page_url

    # 浏览器抓取到的源带真实防盗链头（Referer/Origin/UA），优先使用
    extra_headers = dict(chosen.headers or {})
    if "referer" in extra_headers:
        referer = extra_headers.pop("referer")

    # ---- 4. 按类型分派下载 ----
    global current_downloader
    try:
        if chosen.kind == "m3u8":
            dl = M3U8Downloader(
                chosen.url, args.output, filename=filename,
                referer=referer, cookies=args.cookies,
                concurrency=args.concurrency, quality=args.quality,
                keep_ts=args.keep_ts, extra_headers=extra_headers,
                progress_callback=progress_cb,
            )
        else:
            dl = Downloader(
                chosen.url, args.output, filename=filename,
                referer=referer, cookies=args.cookies,
                threads=args.threads, extra_headers=extra_headers,
                progress_callback=progress_cb,
            )
        # 暴露给 GUI 的暂停/继续按钮
        current_downloader = dl
        try:
            out = dl.download()
        finally:
            current_downloader = None
        logger.info("★ 下载完成: %s", out)
        return True
    except KeyboardInterrupt:
        logger.warning("用户中断下载（下次运行可断点续传）")
        raise
    except Exception as e:
        logger.error("下载失败: %s", e)
        if args.debug:
            import traceback
            traceback.print_exc()
        return False


def _main() -> None:
    """主流程（被 main() 包裹，用于兜底异常和结束时暂停窗口）"""
    args = parse_args()
    setup_logging(logging.DEBUG if args.debug else logging.INFO)
    os.makedirs(args.output, exist_ok=True)

    # 命令行 --proxy 覆盖 config 里的代理设置
    if args.proxy:
        import config
        config.PROXY = args.proxy

    # 获取要处理的 URL 列表
    urls = list(args.urls)

    # 没有命令行参数 → 交互模式
    if not urls:
        url = interactive_select_url()
        if not url:
            print("已退出。")
            return
        # 自动保存用户输入的 URL
        save_url(url)
        urls = [url]

    success_count = 0
    for page_url in urls:
        if process_one(page_url, args):
            success_count += 1

    # 汇总
    print()
    logger.info("=" * 64)
    logger.info("完成：%d/%d 个链接下载成功", success_count, len(urls))
    if success_count < len(urls):
        logger.error("有 %d 个链接下载失败，请检查上面的错误信息。", len(urls) - success_count)
    logger.info("=" * 64)


def main() -> None:
    """程序入口：默认弹图形界面；--console 或 GUI 不可用时走命令行"""
    args = parse_args()

    # 默认启动图形界面（双击运行不再出现黑窗口）
    if not args.console:
        try:
            from gui import run_gui
            run_gui(args, initial_urls=args.urls)
            return
        except Exception:
            _write_log("GUI 启动失败，回退命令行模式:\n" + traceback.format_exc())

    # 命令行模式：兜底所有异常 + 结束时暂停
    try:
        _main()
    except KeyboardInterrupt:
        print("\n用户中断。")
    except EOFError:
        print("\n输入已结束。")
    except Exception:
        # 任何未预期的错误都要显示出来，而不是闪退
        print("\n" + "!" * 60)
        print("  程序发生错误（请截图此窗口反馈）：")
        traceback.print_exc()
        print("!" * 60)
    finally:
        # 双击运行：窗口保持打开，按回车退出
        print()
        _safe_input("按回车键退出...")


if __name__ == "__main__":
    main()