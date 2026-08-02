# -*- coding: utf-8 -*-
"""
parser.py —— 视频地址解析器

职责：输入一个网页 URL，自动找出页面里所有可能的视频资源地址。

解析策略（按可信度从高到低）：
1. <video src="..."> 标签              —— 最直接
2. <video> 内的 <source src="...">     —— 次直接
3. video 标签懒加载属性 data-src 等
4. 整个页面文本 / <script> 里正则扫描 .m3u8 / .mp4 链接
5. JS 播放器配置字段（videoUrl / playUrl / hlsUrl 等）
6. 递归解析 <iframe> 内嵌页面
7. 可选：Playwright 渲染 JS 动态页面，拦截真实媒体网络请求
"""

import logging
import os
import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from config import (DEFAULT_HEADERS, MAX_IFRAME_DEPTH, PLAYWRIGHT_WAIT,
                    PLAYWRIGHT_WAIT_VIDEO, TIMEOUT)
from utils import (build_session, get_logger, request_with_retry, resolve_url)

logger = get_logger("parser")


class VideoInfo:
    """一个候选视频资源"""

    def __init__(self, url: str, kind: str, source: str, title: str = "",
                 headers: dict = None):
        self.url = url          # 视频绝对地址
        self.kind = kind        # "mp4" 或 "m3u8"
        self.source = source    # 发现途径（便于调试）
        self.title = title      # 页面标题，可用作文件名
        self.headers = headers or {}  # 浏览器实际请求该视频时用到的防盗链头

    def __repr__(self):
        return f"<VideoInfo[{self.kind}] from {self.source}: {self.url}>"


# JS 播放器配置字段扫描正则：匹配形如
#   "videoUrl": "https://..."   /   url: 'https://...'
_JS_KEY_PATTERN = re.compile(
    r"""["']?(?:url|src|videoUrl|playUrl|mediaUrl|mp4Url|hlsUrl|m3u8Url|cdnUrl)["']?"""
    r"""\s*[:=]\s*["'](https?://[^"']+)["']""",
    re.IGNORECASE,
)

# 页面文本 / 脚本中的媒体链接正则：捕获 .m3u8/.mp4 等结尾的完整 URL（含 query）
_MEDIA_URL_PATTERN = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:m3u8|mp4|m4v|webm|ts)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)


class Parser:
    """网页视频地址解析器"""

    def __init__(self, session: requests.Session = None):
        # 页面解析默认直连（避免国内站点对海外代理出口返回 403）
        self.session = session or build_session()
        self._proxy_session = None   # 惰性创建，用于探测海外 CDN 的视频地址

    @property
    def proxy_session(self) -> requests.Session:
        """带代理的 session，用于探测/访问海外视频 CDN"""
        if self._proxy_session is None:
            self._proxy_session = build_session(use_proxy=True)
        return self._proxy_session

    # ------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------
    def parse(self, page_url: str, use_browser: bool = False) -> list[VideoInfo]:
        """解析页面，返回候选视频列表（已去重，m3u8 排前）"""
        logger.info("开始解析页面: %s", page_url)
        results: list[VideoInfo] = []
        visited: set[str] = set()

        # 第 1 步：获取页面 HTML
        html = self._fetch_page(page_url)
        if html is None and not use_browser:
            logger.error("页面获取失败，无法解析。可尝试 --browser 参数渲染 JS 页面。")
            return results

        title = ""
        if html:
            soup = BeautifulSoup(html, "html.parser")
            title = self._get_title(soup, page_url)

            # 第 2 步：video / source 标签（可信度最高）
            self._parse_video_tags(soup, page_url, results)

            # 第 3 步：页面文本 + script 正则扫描
            self._scan_text(html, page_url, results)

            # 第 4 步：递归解析 iframe
            self._parse_iframes(soup, page_url, results, visited, depth=1)

        # 第 5 步：JS 动态渲染（可选，--browser）
        if use_browser:
            results.extend(self._parse_with_browser(page_url))

        # 统一补上页面标题，便于生成有意义的文件名
        for r in results:
            if not r.title:
                r.title = title
        return self._dedupe(results)

    # ------------------------------------------------------------
    # 页面获取
    # ------------------------------------------------------------
    def _fetch_page(self, url: str):
        """请求页面并返回 HTML 文本；失败返回 None

        策略：先直连（国内站常见，避免代理出口被 403），
        失败或非 200 时再尝试经本地代理抓取。
        """
        common = {"Referer": url, "Accept-Language": "zh-CN,zh;q=0.9"}
        # 第一步：直连
        try:
            resp = request_with_retry(self.session, "GET", url, headers=common)
            if resp.status_code == 200:
                # 编码识别：requests 默认可能错判，用 apparent_encoding 兜底
                if resp.encoding in (None, "ISO-8859-1"):
                    resp.encoding = resp.apparent_encoding
                return resp.text
            logger.warning("页面返回状态码 %s（直连）", resp.status_code)
        except Exception as e:
            logger.warning("获取页面失败（直连）%s: %s", url, e)
        # 第二步：直连失败再走代理（此时才惰性创建代理 session）
        try:
            resp = request_with_retry(self.proxy_session, "GET", url, headers=common)
            if resp.status_code == 200:
                if resp.encoding in (None, "ISO-8859-1"):
                    resp.encoding = resp.apparent_encoding
                return resp.text
            logger.warning("页面返回状态码 %s（代理）", resp.status_code)
        except Exception as e:
            logger.warning("获取页面失败（代理）%s: %s", url, e)
        return None

    @staticmethod
    def _get_title(soup: BeautifulSoup, page_url: str) -> str:
        """从 <title> 提取页面标题，缺省用域名兜底"""
        t = soup.title
        if t and t.get_text(strip=True):
            return t.get_text(strip=True)
        return urlparse(page_url).netloc

    # ------------------------------------------------------------
    # 各类解析
    # ------------------------------------------------------------
    @staticmethod
    def _parse_video_tags(soup: BeautifulSoup, page_url: str,
                          results: list[VideoInfo]) -> None:
        """解析 <video> 与 <source> 标签"""
        for video in soup.find_all("video"):
            # 1) video 标签直接带 src
            src = video.get("src")
            if src:
                Parser._add_result(results, resolve_url(page_url, src), "video标签src")

            # 2) video 内部的 <source> 标签
            for source in video.find_all("source"):
                s = source.get("src")
                if s:
                    Parser._add_result(results, resolve_url(page_url, s), "source标签src")

            # 3) 懒加载属性：data-src / data-url / data-source
            for attr in ("data-src", "data-url", "data-source", "data-mp4"):
                v = video.get(attr)
                if v:
                    Parser._add_result(results, resolve_url(page_url, v), f"video标签{attr}")

    @staticmethod
    def _scan_text(text: str, page_url: str, results: list[VideoInfo]) -> None:
        """在页面文本 / <script> 中正则扫描媒体地址"""
        if not text:
            return
        # 直接扫 .m3u8/.mp4 结尾的链接
        for m in _MEDIA_URL_PATTERN.finditer(text):
            Parser._add_result(results, m.group(0), "正则扫描")
        # 扫 JS 播放器配置字段
        for m in _JS_KEY_PATTERN.finditer(text):
            url = m.group(1)
            if url.startswith("http"):
                Parser._add_result(results, url, "JS配置字段")

    def _parse_iframes(self, soup: BeautifulSoup, page_url: str,
                       results: list[VideoInfo], visited: set[str], depth: int) -> None:
        """递归解析 <iframe> 内嵌页面（视频常放在内嵌播放器页里）"""
        if depth > MAX_IFRAME_DEPTH:
            return
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src")
            if not src:
                continue
            abs_url = resolve_url(page_url, src)
            if abs_url in visited:
                continue
            visited.add(abs_url)
            logger.info("递归解析 iframe(深度%d): %s", depth, abs_url)
            html = self._fetch_page(abs_url)
            if not html:
                continue
            sub_soup = BeautifulSoup(html, "html.parser")
            self._parse_video_tags(sub_soup, abs_url, results)
            self._scan_text(html, abs_url, results)
            self._parse_iframes(sub_soup, abs_url, results, visited, depth + 1)

    def _parse_with_browser(self, page_url: str) -> list[VideoInfo]:
        """用 Playwright 渲染 JS 动态页面，找出视频地址

        适用场景：视频地址由 JS 动态拼接 / blob: 播放 / 接口加密返回等情况。
        策略：
        1. 拦截所有 m3u8/mp4 网络响应（含 iframe 内的）
        2. 轮询读取主页面及所有 iframe 里 <video>/<source> 的 src
           （很多播放器把地址写进 video.src，但不实际加载，拦截不到）
        3. 无扩展名的媒体 URL 用 HEAD 请求探测类型
        依赖：pip install playwright && playwright install chromium
        """
        results: list[VideoInfo] = []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("未安装 playwright，跳过浏览器渲染。"
                           "可执行: pip install playwright && playwright install chromium")
            return results

        # 打包成 exe 后，playwright 会把浏览器路径解析到临时解压目录，
        # 导致找不到浏览器。这里手动指回系统 ms-playwright 缓存目录。
        # （用户可用环境变量 PLAYWRIGHT_BROWSERS_PATH 覆盖）
        if os.environ.get("LOCALAPPDATA"):
            os.environ.setdefault(
                "PLAYWRIGHT_BROWSERS_PATH",
                os.path.join(os.environ["LOCALAPPDATA"], "ms-playwright"),
            )

        # 说明：浏览器保持直连加载页面/播放器（避免海外代理出口被国内站 403）。
        # 视频源地址只要被写进 video.src 就能读出来，实际下载由 requests 走代理完成。
        media_urls: set[str] = set()
        media_headers: dict[str, dict] = {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=self.session.headers.get("User-Agent"))

                # 拦截响应：凡是 m3u8/mp4/webm 类型或媒体后缀结尾的，都记下来，
                # 并记录该请求的防盗链头（Referer/Origin/UA 等），
                # 供下载时原样重放，绕过 CDN 的 Referer 校验（403）
                def on_response(resp):
                    ctype = resp.headers.get("content-type", "").lower()
                    url = resp.url
                    if ("mpegurl" in ctype or ctype.startswith("video/")
                            or urlparse(url).path.lower().endswith(
                                (".m3u8", ".mp4", ".webm", ".ts"))):
                        media_urls.add(url)
                        hdrs = {}
                        for k, v in resp.request.headers.items():
                            if k.lower() in ("referer", "origin", "user-agent",
                                             "accept", "accept-language"):
                                hdrs[k.lower()] = v
                        media_headers[url] = hdrs

                page.on("response", on_response)

                try:
                    page.goto(page_url, timeout=PLAYWRIGHT_WAIT + 3000,
                              wait_until="domcontentloaded")
                    # 轮询等待：主页面 + iframe 里的 video 元素出现 src
                    deadline = time.time() + PLAYWRIGHT_WAIT_VIDEO / 1000
                    while time.time() < deadline:
                        for frame in page.frames:
                            try:
                                srcs = frame.eval_on_selector_all(
                                    "video, source",
                                    "els => els.map(e => e.src || e.currentSrc)"
                                    ".filter(Boolean)",
                                )
                                for s in srcs:
                                    if not s.startswith("http"):
                                        continue
                                    media_urls.add(s)
                                    # 从 video.src 读到的地址没有网络请求头，
                                    # 用所在页面 URL 作为 Referer（CDN 防盗链认这个）
                                    if s not in media_headers:
                                        media_headers[s] = {"referer": frame.url}
                            except Exception:
                                continue   # 该 frame 还没加载完，忽略
                        if any(u for u in media_urls
                               if not u.endswith((".js", ".css"))):
                            break
                        page.wait_for_timeout(1000)

                    # 读取可能存放地址的全局配置对象
                    config = page.evaluate(
                        "window.playerConfig || window.videoConfig || "
                        "window.__INITIAL_STATE__ || null"
                    )
                    if isinstance(config, str):
                        self._scan_text(config, page_url, results)
                except Exception as e:
                    logger.warning("浏览器渲染失败: %s", e)
                finally:
                    browser.close()
        except Exception as e:
            logger.warning("Playwright 启动失败: %s", e)

        for u in media_urls:
            kind = self._classify_url(u)
            if kind:
                results.append(VideoInfo(u, kind, "浏览器抓取",
                                         headers=media_headers.get(u)))
        return results

    def _classify_url(self, url: str) -> str | None:
        """判断媒体 URL 类型；无扩展名时用 HEAD 探测 content-type"""
        path = urlparse(url).path.lower()
        if path.endswith(".m3u8"):
            return "m3u8"
        if path.endswith((".mp4", ".m4v", ".webm", ".mkv", ".mov")):
            return "mp4"
        # 无扩展名的地址（如 CDN 签名 URL）——优先代理探测（海外 CDN 居多）
        for session in (self.proxy_session, self.session):
            try:
                resp = session.head(url, timeout=TIMEOUT, allow_redirects=True)
                ctype = resp.headers.get("Content-Type", "").lower()
                if "mpegurl" in ctype:
                    return "m3u8"
                if ctype.startswith("video/"):
                    return "mp4"
            except requests.RequestException:
                continue
        return None

    # ------------------------------------------------------------
    # 结果整理
    # ------------------------------------------------------------
    @staticmethod
    def _add_result(results: list[VideoInfo], url: str, source: str) -> None:
        """校验并追加一个候选视频地址（只保留 mp4/m3u8 等可下载资源）"""
        url = (url or "").strip()
        if not url or url.startswith(("blob:", "data:", "javascript:", "about:")):
            return
        if url.startswith("//"):
            url = "https:" + url
        path = urlparse(url).path.lower()
        if path.endswith(".m3u8"):
            results.append(VideoInfo(url, "m3u8", source))
        elif path.endswith((".mp4", ".m4v", ".webm", ".mkv", ".mov")):
            results.append(VideoInfo(url, "mp4", source))

    @staticmethod
    def _dedupe(results: list[VideoInfo]) -> list[VideoInfo]:
        """按 URL 去重，并把 m3u8 排到前面（m3u8 通常代表最终视频源）"""
        seen: set[str] = set()
        out: list[VideoInfo] = []
        for r in results:
            if r.url in seen:
                continue
            seen.add(r.url)
            out.append(r)
        out.sort(key=lambda r: (0 if r.kind == "m3u8" else 1))
        return out
