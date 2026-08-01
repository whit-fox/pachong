# -*- coding: utf-8 -*-
"""
m3u8_downloader.py —— HLS(m3u8) 流媒体下载

功能：
1. 解析 m3u8 文件（自动区分主播放列表 / 媒体播放列表）
2. 主播放列表自动选择清晰度（best / lowest / 指定分辨率）
3. 使用 aiohttp 并发下载所有 .ts 分片（可配置并发数）
4. 支持 AES-128 分片解密（密钥文件或 data: URI 内联密钥）
5. 分片失败自动重试，失败分片不阻塞整个任务
6. 优先用 ffmpeg 合并分片为 MP4；无 ffmpeg 时退化为二进制拼接

依赖说明：
- ffmpeg 合并需要系统安装 ffmpeg 可执行文件（Windows 下可下载安装包或
  使用 winget install ffmpeg），ffmpeg-python 只是它的 Python 封装。
- AES-128 解密需要 pycryptodome（requirements 已包含）。
"""

import asyncio
import os
import re
import shutil

import aiohttp
from tqdm import tqdm

from config import (M3U8_SEGMENT_DIR, MERGE_CLEANUP, SEGMENT_CONCURRENCY,
                    TIMEOUT)
from utils import (build_session, decode_data_uri, ensure_dir, get_logger,
                   request_with_retry, resolve_proxy, resolve_url)

logger = get_logger("m3u8")


class M3U8Downloader:
    """m3u8 流媒体下载器"""

    def __init__(self, url: str, output_dir: str = "downloads",
                 filename: str = None, referer: str = None,
                 cookies=None, concurrency: int = SEGMENT_CONCURRENCY,
                 quality: str = "best", keep_ts: bool = False):
        self.url = url                # m3u8 地址
        self.output_dir = output_dir  # 输出目录
        self.filename = filename or "video.mp4"
        self.referer = referer        # 防盗链 Referer
        self.cookies = cookies
        self.concurrency = concurrency
        self.quality = quality        # best / lowest / "1080" 等
        self.keep_ts = keep_ts        # 合并后是否保留 ts 分片

        self.segments = []            # 分片列表: [{uri, duration, key}]
        self.media_sequence = 0       # 第一个分片的序号（用于默认 IV）
        self.base_url = url           # 当前播放列表的基准地址
        self._key_cache = {}          # AES 密钥缓存 {key_uri: bytes}
        self.proxy = resolve_proxy()  # 自动检测本机代理，海外 CDN 分片可下载

    # ============================================================
    # 对外入口
    # ============================================================
    def download(self) -> str:
        """解析 -> 下载分片 -> 合并，返回输出文件完整路径"""
        self._prepare()                             # 解析 m3u8，得到分片列表
        segment_dir = self._make_segment_dir()
        self._download_segments(segment_dir)        # 并发下载所有分片
        out_path = self._merge(segment_dir)         # 合并为完整文件
        return out_path

    # ============================================================
    # m3u8 解析
    # ============================================================
    def _prepare(self) -> None:
        """获取并解析 m3u8：处理主播放列表 -> 媒体播放列表 -> 分片列表"""
        content = self._fetch_text(self.url)
        if not content:
            raise RuntimeError(f"无法获取 m3u8 文件: {self.url}")

        # 主播放列表特征：#EXT-X-STREAM-INF，需再选一个清晰度子列表
        if "#EXT-X-STREAM-INF" in content:
            logger.info("检测到主播放列表，选择清晰度: %s", self.quality)
            self.url = self._choose_variant(content)
            self.base_url = self.url
            content = self._fetch_text(self.url)
            if not content:
                raise RuntimeError(f"无法获取媒体播放列表: {self.url}")

        self._parse_media_playlist(content)

    def _choose_variant(self, content: str) -> str:
        """从主播放列表中选择一个清晰度子列表地址"""
        variants = []
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                # 解析 BANDWIDTH / RESOLUTION / CODECS 等属性
                attrs = {}
                for key, quoted, unquoted in re.findall(
                        r'([A-Za-z0-9_-]+)=(?:"([^"]*)"|([^,]*))', line):
                    attrs[key] = quoted if quoted != "" else unquoted
                # 下一行（可能跳过空行）是子播放列表地址
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i < len(lines) and not lines[i].strip().startswith("#"):
                    uri = resolve_url(self.base_url, lines[i].strip())
                    bw = int(attrs.get("BANDWIDTH") or 0)
                    height = 0
                    if "x" in (attrs.get("RESOLUTION") or ""):
                        height = int(attrs["RESOLUTION"].split("x")[-1])
                    variants.append({"uri": uri, "bandwidth": bw, "height": height})
            i += 1

        if not variants:
            raise RuntimeError("主播放列表解析失败，未找到清晰度子列表")

        # 清晰度选择策略
        key = lambda v: v["bandwidth"] or v["height"]   # noqa: E731
        if self.quality == "lowest":
            chosen = min(variants, key=key)
        elif self.quality == "best":
            chosen = max(variants, key=key)
        else:
            # 指定分辨率，如 "1080"、"720"；找不到则取最高
            matched = [v for v in variants
                       if str(self.quality) in str(v["height"])]
            chosen = matched[0] if matched else max(variants, key=key)

        logger.info("已选择: %s (%.1f Mbps, %s)",
                    chosen["height"] or "未知分辨率",
                    (chosen["bandwidth"] or 0) / 1_000_000,
                    chosen["uri"])
        return chosen["uri"]

    def _parse_media_playlist(self, content: str) -> None:
        """解析媒体播放列表，提取分片 URI / 时长 / 密钥信息"""
        m_seq = re.search(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", content)
        self.media_sequence = int(m_seq.group(1)) if m_seq else 0

        segments = []
        key_info = {}          # 当前生效的加密信息
        lines = content.splitlines()

        for i, line in enumerate(lines):
            line = line.strip()
            if line.startswith("#EXT-X-KEY"):
                # 例: #EXT-X-KEY:METHOD=AES-128,URI="key.key",IV=0x...
                method_m = re.search(r"METHOD=([A-Za-z0-9-]+)", line)
                method = method_m.group(1) if method_m else "NONE"
                if method == "NONE":
                    key_info = {}       # 后续分片不再加密
                    continue
                uri_m = re.search(r'URI="([^"]+)"', line)
                iv_m = re.search(r"IV=(0x[0-9a-fA-F]+)", line)
                key_info = {
                    "method": method,
                    "uri": resolve_url(self.base_url, uri_m.group(1)) if uri_m else None,
                    "iv": iv_m.group(1) if iv_m else None,
                }
            elif line.startswith("#EXTINF"):
                # 下一行为分片地址
                dur_m = re.search(r"#EXTINF:\s*([0-9.]+)", line)
                duration = float(dur_m.group(1)) if dur_m else 0.0
                if i + 1 < len(lines):
                    uri = lines[i + 1].strip()
                    if uri and not uri.startswith("#"):
                        segments.append({
                            "uri": resolve_url(self.base_url, uri),
                            "duration": duration,
                            "key": dict(key_info) if key_info else None,
                        })

        if not segments:
            raise RuntimeError("媒体播放列表解析失败，未找到任何分片")
        self.segments = segments
        logger.info("共 %d 个分片，总时长约 %.1f 秒",
                    len(segments), sum(s["duration"] for s in segments))

    # ============================================================
    # 分片下载（aiohttp 异步并发）
    # ============================================================
    def _download_segments(self, segment_dir: str) -> None:
        """同步封装：运行异步分片下载任务"""
        asyncio.run(self._download_segments_async(segment_dir))

    async def _download_segments_async(self, segment_dir: str) -> None:
        """异步并发下载所有分片，支持重试与 AES 解密"""
        sem = asyncio.Semaphore(self.concurrency)
        headers = {"Referer": self.referer} if self.referer else {}
        timeout = aiohttp.ClientTimeout(total=TIMEOUT * 3)

        async with aiohttp.ClientSession(timeout=timeout, cookies=self.cookies or {},
                                         trust_env=True, proxy=self.proxy) as session:
            session.headers.update(headers)
            prog = tqdm(total=len(self.segments), unit="ts",
                        desc="下载分片", mininterval=0.2, ncols=80)

            async def download_one(idx: int, seg: dict):
                async with sem:
                    path = os.path.join(segment_dir, f"{idx:05d}.ts")
                    # 已存在的分片直接跳过（断点续传）
                    if os.path.exists(path) and os.path.getsize(path) > 0:
                        prog.update(1)
                        return
                    data = await self._fetch_bytes_retry(seg["uri"], session)
                    # AES-128 解密
                    if seg["key"] and seg["key"].get("method") != "NONE":
                        data = await self._decrypt_segment(data, seg["key"], idx, session)
                    with open(path, "wb") as f:
                        f.write(data)
                    prog.update(1)

            tasks = [asyncio.create_task(download_one(i, seg))
                     for i, seg in enumerate(self.segments)]
            # return_exceptions：单个分片失败不阻塞其它分片，全部跑完再统一报错
            results = await asyncio.gather(*tasks, return_exceptions=True)
            prog.close()
            failed = [r for r in results if isinstance(r, Exception)]
            if failed:
                raise RuntimeError(f"有 {len(failed)} 个分片下载失败，可重试续传")

    async def _fetch_bytes_retry(self, url: str, session: aiohttp.ClientSession,
                                 retries: int = 5) -> bytes:
        """异步获取字节数据，失败自动重试（指数退避）"""
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                logger.warning("分片获取失败 %s (重试 %d/%d): %s",
                               url, attempt, retries, e)
                await asyncio.sleep(1.0 * attempt)
        raise last_exc or RuntimeError(f"分片下载失败: {url}")

    async def _decrypt_segment(self, data: bytes, key_info: dict, idx: int,
                               session: aiohttp.ClientSession) -> bytes:
        """对单个分片做 AES-128-CBC 解密（带 PKCS7 去填充）"""
        key = await self._get_key(key_info, session)
        if not key:
            return data
        # IV：播放列表给了就用；没给则用「分片序号」作为默认 IV
        iv_hex = key_info.get("iv")
        if iv_hex:
            iv = bytes.fromhex(iv_hex[2:] if iv_hex.startswith("0x") else iv_hex)
        else:
            iv = (self.media_sequence + idx).to_bytes(16, "big")

        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
        plain = cipher.decrypt(data)
        # 去除 PKCS7 填充
        if plain:
            pad = plain[-1]
            if 1 <= pad <= 16:
                plain = plain[:-pad]
        return plain

    async def _get_key(self, key_info: dict,
                       session: aiohttp.ClientSession) -> bytes:
        """获取并缓存 AES-128 密钥（支持文件地址与 data: URI）"""
        uri = key_info.get("uri")
        if not uri:
            return b""
        if uri in self._key_cache:
            return self._key_cache[uri]
        if uri.startswith("data:"):
            key = decode_data_uri(uri)
        else:
            key = await self._fetch_bytes_retry(uri, session)
        if len(key) != 16:
            logger.warning("AES 密钥长度异常: %d 字节", len(key))
        self._key_cache[uri] = key
        return key

    # ============================================================
    # 合并
    # ============================================================
    def _merge(self, segment_dir: str) -> str:
        """把分片合并为完整视频文件，返回输出路径"""
        ts_names = sorted(n for n in os.listdir(segment_dir) if n.endswith(".ts"))
        if not ts_names:
            raise RuntimeError("没有可合并的分片文件")
        ts_paths = [os.path.join(segment_dir, n) for n in ts_names]

        out_path = os.path.join(self.output_dir, self.filename)
        ensure_dir(self.output_dir)
        logger.info("合并 %d 个分片 -> %s", len(ts_paths), out_path)

        ok = self._merge_with_ffmpeg(ts_paths, out_path)
        if not ok:
            # ffmpeg 失败（未安装或参数不兼容）-> 二进制直接拼接
            logger.warning("ffmpeg 合并失败，退化为二进制拼接（输出为 TS 容器）")
            self._merge_binary(ts_paths, out_path)
            if self.filename.lower().endswith(".mp4"):
                # 二进制拼接的结果其实是 TS 容器，改扩展名便于播放器识别
                renamed = out_path[:-4] + ".ts"
                os.replace(out_path, renamed)
                out_path = renamed

        if MERGE_CLEANUP and not self.keep_ts:
            shutil.rmtree(segment_dir, ignore_errors=True)
            logger.info("已清理临时分片目录: %s", segment_dir)
        return out_path

    def _merge_with_ffmpeg(self, ts_paths: list, out_path: str) -> bool:
        """用 ffmpeg concat demuxer 无损合并（-c copy 不重新编码），成功返回 True"""
        list_file = os.path.join(os.path.dirname(out_path), "_concat_list.txt")
        try:
            import ffmpeg
            # Windows 路径里反斜杠可能被 ffmpeg 误解析，统一转成正斜杠
            with open(list_file, "w", encoding="utf-8") as f:
                for p in ts_paths:
                    f.write(f"file '{p.replace(os.sep, '/')}'\n")
            ffmpeg.input(list_file, f="concat", safe=0).output(
                out_path, c="copy").run(overwrite_output=True, quiet=True)
            logger.info("ffmpeg 合并完成")
            return True
        except Exception as e:
            logger.warning("ffmpeg 调用失败: %s", e)
            return False
        finally:
            if os.path.exists(list_file):
                os.remove(list_file)

    @staticmethod
    def _merge_binary(ts_paths: list, out_path: str) -> None:
        """直接按字节拼接 TS 分片（大多数同编码分片可正常播放）"""
        with open(out_path, "wb") as out:
            for p in ts_paths:
                with open(p, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)

    # ============================================================
    # 工具
    # ============================================================
    def _fetch_text(self, url: str):
        """同步获取文本内容（m3u8 / 播放列表），m3u8 文件也在海外 CDN 时走代理"""
        session = build_session(cookies=self.cookies, use_proxy=True)
        headers = {"Referer": self.referer} if self.referer else {}
        try:
            resp = request_with_retry(session, "GET", url, headers=headers)
            if resp.status_code != 200:
                logger.warning("m3u8 返回状态码 %s", resp.status_code)
                return None
            if resp.encoding in (None, "ISO-8859-1"):
                resp.encoding = resp.apparent_encoding
            return resp.text
        except Exception as e:
            logger.warning("获取 m3u8 失败 %s: %s", url, e)
            return None

    def _make_segment_dir(self) -> str:
        """创建分片临时目录"""
        ensure_dir(self.output_dir)
        seg_dir = os.path.join(self.output_dir, M3U8_SEGMENT_DIR)
        ensure_dir(seg_dir)
        return seg_dir
