# -*- coding: utf-8 -*-
"""
downloader.py —— 单文件下载核心（适用于 mp4 / webm 等直链）

功能：
1. HTTP / HTTPS 下载（requests + stream 流式写入）
2. 多线程分片下载（Range 请求并发拉取，速度成倍提升）
3. 断点续传：.part 分片文件保留，下次启动自动跳过 / 续传
4. 自定义请求头：User-Agent / Referer / Cookie
5. 网络中断自动重试（指数退避）
6. tqdm 实时显示下载进度与速度

设计说明：
- 服务器支持 Range 时走多线程分片；不支持时自动退化为单线程流式下载
- 分片文件命名：<目标文件名>.part.0000 等，全部完成后合并再删除
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from tqdm import tqdm

from config import CHUNK_SIZE, MAX_RETRIES, PART_FILE_SUFFIX, THREADS, TIMEOUT
from utils import (build_session, ensure_dir, get_logger, human_size,
                   request_with_retry)

logger = get_logger("downloader")


class RangeNotSupported(Exception):
    """服务器不支持 Range 请求时抛出，触发退化到单线程下载"""


class Downloader:
    """单文件下载器"""

    def __init__(self, url: str, output_dir: str = "downloads",
                 filename: str = None, referer: str = None,
                 cookies=None, threads: int = THREADS,
                 extra_headers: dict = None,
                 session: requests.Session = None):
        self.url = url
        self.output_dir = output_dir
        self.filename = filename or self._guess_filename(url)
        self.referer = referer
        self.threads = max(1, threads)
        self.extra_headers = extra_headers or {}
        # 下载文件走代理（视频 CDN 常为海外节点，直连超时）
        self.session = session or build_session(cookies=cookies, use_proxy=True)

        self._progress = None            # tqdm 进度条对象
        self._lock = threading.Lock()    # 多线程更新进度时加锁
        self._downloaded = 0             # 本程序运行期间实际下载字节数

    # ------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------
    def download(self) -> str:
        """执行下载，返回最终文件的完整路径"""
        ensure_dir(self.output_dir)
        out_path = os.path.join(self.output_dir, self.filename)

        # 文件已存在且非空 -> 视为已下载完成，直接跳过
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            logger.info("文件已存在，跳过: %s", out_path)
            return out_path

        # 探测服务端信息：文件大小、是否支持断点续传
        size, accept_ranges = self._probe()
        logger.info("目标: %s (%s, accept-ranges=%s)",
                    self.filename, human_size(size or 0), accept_ranges)

        # 满足条件才多线程分片：知道大小 && 支持 Range && 文件足够大 && 线程数>1
        if (size and accept_ranges and self.threads > 1
                and size > CHUNK_SIZE * 8):
            self._download_multithread(out_path, size)
        else:
            self._download_single(out_path, size)
        return out_path

    # ------------------------------------------------------------
    # 服务端探测
    # ------------------------------------------------------------
    def _probe(self) -> tuple:
        """探测文件大小与是否支持 Range 请求

        优先用 HEAD；HEAD 失败或被拒时退化为 GET + Range(bytes=0-0)，
        依据返回的 Content-Range 头判断。
        """
        headers = self._headers()
        # ---- 方式一：HEAD ----
        try:
            resp = self.session.head(self.url, headers=headers,
                                     allow_redirects=True, timeout=TIMEOUT)
            cl = resp.headers.get("Content-Length")
            if resp.status_code < 400 and cl:
                size = int(cl)
                ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
                logger.debug("HEAD 探测: size=%s ranges=%s", size, ranges)
                return size, ranges
        except requests.RequestException:
            pass

        # ---- 方式二：GET + Range=bytes=0-0 ----
        try:
            resp = self.session.get(
                self.url, headers={**headers, "Range": "bytes=0-0"},
                stream=True, allow_redirects=True, timeout=TIMEOUT)
            try:
                if resp.status_code == 206:   # Partial Content：支持 Range
                    cr = resp.headers.get("Content-Range", "")  # bytes 0-0/12345
                    total = cr.rsplit("/", 1)[-1]
                    size = int(total) if total.isdigit() else None
                    return size, True
                if resp.status_code == 200:   # 忽略 Range，返回全量
                    cl = resp.headers.get("Content-Length")
                    return (int(cl) if cl else None), False
            finally:
                resp.close()
        except requests.RequestException as e:
            logger.warning("探测失败: %s", e)
        return None, False

    def _headers(self) -> dict:
        """组装请求头（User-Agent 已含于 session，这里补 Referer）"""
        headers = dict(self.extra_headers)
        if self.referer:
            headers["Referer"] = self.referer
        return headers

    # ------------------------------------------------------------
    # 多线程分片下载
    # ------------------------------------------------------------
    def _download_multithread(self, out_path: str, size: int) -> None:
        """多线程分片下载：把文件切成 N 段，各线程并发拉取"""
        # 计算每个分片的字节范围
        part_size = (size + self.threads - 1) // self.threads
        ranges = [(i * part_size, min((i + 1) * part_size - 1, size - 1))
                  for i in range(self.threads)]
        part_files = [f"{out_path}{PART_FILE_SUFFIX}.{i:04d}"
                      for i in range(len(ranges))]

        self._init_progress(size, initial=0)
        error = None
        try:
            with ThreadPoolExecutor(max_workers=self.threads) as pool:
                futures = [pool.submit(self._download_part, part_files[i], s, e, i)
                           for i, (s, e) in enumerate(ranges)]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        error = e
                        break
                # 收尾：取出剩余 future 的结果/异常，避免 "exception never retrieved" 告警
                for f in futures:
                    if not f.cancelled():
                        try:
                            f.exception()
                        except Exception:
                            pass
        except Exception as e:
            error = e
        finally:
            self._close_progress()

        if isinstance(error, RangeNotSupported):
            # 服务器不认 Range，退化为单线程整包下载
            logger.warning("服务器不支持 Range，退化为单线程下载")
            self._cleanup_parts(part_files)
            self._download_single(out_path, size)
            return
        if error:
            raise error

        self._merge_parts(out_path, part_files)

    def _download_part(self, path: str, start: int, end: int, part_no: int) -> None:
        """下载单个分片（支持分片内部断点续传）

        - 分片文件已完整（大小 >= 目标）-> 跳过
        - 分片文件部分存在 -> 从已有大小处发 Range 续传
        """
        target_size = end - start + 1
        offset = 0

        # 已有分片文件的续传判断
        if os.path.exists(path):
            existing = os.path.getsize(path)
            if existing >= target_size:
                logger.debug("分片 %d 已存在，跳过", part_no)
                self._add_progress(target_size)
                return
            offset = existing
            if existing > target_size:      # 大小异常（文件损坏），重下
                os.remove(path)
                offset = 0

        headers = self._headers()
        if offset:
            headers["Range"] = f"bytes={start + offset}-{end}"
        elif end > start:
            headers["Range"] = f"bytes={start}-{end}"

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.get(self.url, headers=headers, stream=True,
                                        allow_redirects=True, timeout=TIMEOUT)
                # 请求了 Range 却返回 200 -> 服务器忽略 Range，无法分片
                if "Range" in headers and resp.status_code == 200:
                    resp.close()
                    raise RangeNotSupported("服务器不支持分段下载")
                if resp.status_code not in (206, 200):
                    resp.close()
                    raise requests.HTTPError(f"HTTP {resp.status_code}")

                mode = "r+b" if offset else "wb"
                with open(path, mode) as f:
                    if offset:
                        f.seek(offset)
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            self._add_progress(len(chunk))

                # 校验分片完整性
                if os.path.getsize(path) < target_size:
                    raise requests.ConnectionError("分片不完整")
                return
            except (requests.ConnectionError, requests.Timeout,
                    requests.HTTPError) as e:
                if attempt >= MAX_RETRIES:
                    raise
                logger.warning("分片 %d 下载中断，重试 %d/%d: %s",
                               part_no, attempt, MAX_RETRIES, e)
                # 续传：从当前文件大小接着下（已写的字节此前已计入进度）
                if os.path.exists(path):
                    offset = os.path.getsize(path)
                    if offset >= target_size:
                        return
                    headers["Range"] = f"bytes={start + offset}-{end}"

    # ------------------------------------------------------------
    # 单线程流式下载
    # ------------------------------------------------------------
    def _download_single(self, out_path: str, size: int = None) -> None:
        """单线程流式下载，支持断点续传"""
        offset = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if size and offset >= size:           # 已下载完成
            logger.info("文件已完整，跳过: %s", out_path)
            return
        self._init_progress(size, initial=offset)

        attempt = 0
        while True:
            attempt += 1
            headers = self._headers()
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                resp = self.session.get(self.url, headers=headers, stream=True,
                                        allow_redirects=True, timeout=TIMEOUT)
                if resp.status_code not in (206, 200):
                    resp.close()
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                # 请求了 Range 却返回 200 -> 服务器不支持断点，只能从头下
                if offset and resp.status_code == 200:
                    logger.warning("服务器忽略 Range，从头开始下载")
                    resp.close()
                    self._reset_progress()
                    offset = 0
                    continue

                mode = "wb" if offset == 0 else "r+b"
                with open(out_path, mode) as f:
                    if offset:
                        f.seek(offset)
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            self._add_progress(len(chunk))
                break
            except (requests.ConnectionError, requests.Timeout,
                    requests.HTTPError) as e:
                if attempt >= MAX_RETRIES:
                    self._close_progress()
                    raise
                logger.warning("下载中断，重试 %d/%d: %s", attempt, MAX_RETRIES, e)
                if os.path.exists(out_path):
                    offset = os.path.getsize(out_path)
        self._close_progress()
        logger.info("下载完成: %s", out_path)

    # ------------------------------------------------------------
    # 合并与清理
    # ------------------------------------------------------------
    @staticmethod
    def _merge_parts(out_path: str, part_files: list) -> None:
        """按顺序把分片合并为完整文件，合并后删除分片"""
        logger.info("合并 %d 个分片 -> %s", len(part_files), out_path)
        with open(out_path, "wb") as out:
            for pf in part_files:
                if not os.path.exists(pf):
                    raise FileNotFoundError(f"缺少分片文件: {pf}")
                with open(pf, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                os.remove(pf)
        logger.info("分片已合并并清理: %s", out_path)

    @staticmethod
    def _cleanup_parts(part_files: list) -> None:
        """删除遗留的分片临时文件"""
        for pf in part_files:
            if os.path.exists(pf):
                os.remove(pf)

    # ------------------------------------------------------------
    # 进度显示辅助
    # ------------------------------------------------------------
    def _init_progress(self, total, initial: int) -> None:
        self._total = total
        self._downloaded = 0
        self._progress = tqdm(
            total=total, initial=initial, unit="B", unit_scale=True,
            desc=self.filename, mininterval=0.2, ncols=90,
        )

    def _add_progress(self, n: int) -> None:
        with self._lock:
            self._downloaded += n
            if self._progress:
                self._progress.update(n)

    def _reset_progress(self) -> None:
        """从头下载时重置进度条"""
        with self._lock:
            self._downloaded = 0
            if self._progress:
                self._progress.n = 0
                self._progress.refresh()

    def _close_progress(self) -> None:
        if self._progress:
            self._progress.close()
            self._progress = None

    # ------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------
    @staticmethod
    def _guess_filename(url: str) -> str:
        """从 URL 路径推断文件名，无扩展名时补 .mp4"""
        name = os.path.basename(urlparse(url).path)
        if not name:
            name = "download"
        if "." not in name:
            name += ".mp4"
        return name
