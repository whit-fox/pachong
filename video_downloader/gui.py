# -*- coding: utf-8 -*-
"""
gui.py —— 图形界面（tkinter）

双击 exe 后不再出现黑窗口，而是弹出这个界面：
粘贴视频 URL -> 点"开始下载" -> 看进度条、网速和日志。

说明：
- 下载在后台线程跑，通过队列把日志/进度发回界面刷新，不卡界面。
- 浏览器模式默认勾选（处理 JS 加密/动态页面，如 agedm）。
- 下载完成的 URL 自动保存到 urls.txt，下次可从下拉框选择。
"""

import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from config import BASE_DIR, DEFAULT_DOWNLOAD_DIR
from utils import human_size, setup_logging

URLS_FILE = BASE_DIR / "urls.txt"


class QueueLogHandler(logging.Handler):
    """把日志格式化后放进队列，GUI 轮询后显示到日志区"""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


class VideoDownloaderApp(tk.Tk):
    """视频下载器图形界面"""

    def __init__(self, args=None, initial_urls=()):
        super().__init__()
        self.args = args
        self.q: queue.Queue = queue.Queue()
        self.worker = None          # 后台下载线程
        self._saved = []            # 已保存的 URL 列表
        self._progress_max = None   # 进度条 maximum（避免每次重复配置）

        self.title("视频下载器")
        self.geometry("780x620")
        self.minsize(640, 480)

        self._build_ui()
        self._setup_logging()

        # 从命令行传入 URL 时自动填充并开始
        if initial_urls:
            self.url_var.set(initial_urls[0])
            self.after(300, self.start_download)

        self.after(100, self._poll_queue)

    # ------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------
    def _build_ui(self):
        # 顶部：URL 输入 + 下载按钮
        top = ttk.Frame(self, padding=(10, 10, 10, 5))
        top.pack(fill="x")
        ttk.Label(top, text="视频 URL:").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.url_entry.bind("<Return>", lambda e: self.start_download())
        self.dl_btn = ttk.Button(top, text="开始下载", command=self.start_download)
        self.dl_btn.pack(side="left")
        self.pause_btn = ttk.Button(top, text="暂停", command=self.toggle_pause,
                                    state="disabled")
        self.pause_btn.pack(side="left", padx=(4, 0))

        # 已保存 URL
        saved_row = ttk.Frame(self, padding=(10, 0, 10, 5))
        saved_row.pack(fill="x")
        ttk.Label(saved_row, text="已保存:").pack(side="left")
        self.saved_combo = ttk.Combobox(saved_row, state="readonly")
        self.saved_combo.pack(side="left", fill="x", expand=True, padx=6)
        self.saved_combo.bind("<<ComboboxSelected>>", self._pick_saved)
        ttk.Button(saved_row, text="刷新", command=self._load_saved).pack(side="left")

        # 选项
        opts = ttk.Frame(self, padding=(10, 0, 10, 5))
        opts.pack(fill="x")
        self.browser_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="浏览器模式（处理 JS 加密 / 动态页面）",
                        variable=self.browser_var).pack(side="left")

        # 进度区
        prog_frame = ttk.Frame(self, padding=(10, 5, 10, 5))
        prog_frame.pack(fill="x")
        self.progress = ttk.Progressbar(prog_frame, maximum=100)
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(prog_frame, textvariable=self.status_var).pack(anchor="w", pady=(4, 0))

        # 日志区
        log_frame = ttk.Frame(self, padding=(10, 5, 10, 10))
        log_frame.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, state="disabled", height=12, font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

        self._load_saved()

    def _setup_logging(self):
        """把日志也输出到界面（同时保留控制台）"""
        setup_logging(logging.INFO)
        handler = QueueLogHandler(self.q)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s | %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(handler)

    # ------------------------------------------------------------
    # 已保存 URL
    # ------------------------------------------------------------
    def _load_saved(self):
        urls = []
        try:
            if URLS_FILE.exists():
                for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
        except Exception:
            pass
        self._saved = urls
        self.saved_combo["values"] = urls if urls else ["（无已保存 URL）"]

    def _pick_saved(self, _event=None):
        sel = self.saved_combo.get()
        if sel in self._saved:
            self.url_var.set(sel)

    def _save_url(self, url):
        try:
            existing = set()
            if URLS_FILE.exists():
                existing = set(URLS_FILE.read_text(encoding="utf-8").splitlines())
            if url not in existing:
                with open(URLS_FILE, "a", encoding="utf-8") as f:
                    f.write(url + "\n")
            self._load_saved()
        except Exception:
            pass

    # ------------------------------------------------------------
    # 日志 / 进度显示
    # ------------------------------------------------------------
    def _append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self):
        """主线程轮询队列，刷新日志与进度"""
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "progress":
                    _, downloaded, total, speed = item
                    self._update_progress(downloaded, total, speed)
                elif item[0] == "done":
                    self._on_done(item[1])
                else:
                    self._append_log(item[1])
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _update_progress(self, downloaded, total, speed):
        if total:
            # 只在 total 变化时配置一次 maximum，避免每次更新都重绘进度条
            if total != self._progress_max:
                self.progress.configure(mode="determinate", maximum=total)
                self._progress_max = total
            self.progress["value"] = downloaded
            pct = downloaded / total * 100
            self.status_var.set(
                f"{human_size(downloaded)} / {human_size(total)}"
                f"  ({pct:.1f}%)   速度: {human_size(speed)}/s")
        else:
            # m3u8 未知总大小，只显示已下载量与速度
            self.progress.configure(mode="determinate", maximum=100)
            self.progress["value"] = 0
            self.status_var.set(f"已下载 {human_size(downloaded)}   速度: {human_size(speed)}/s")

    def _on_done(self, ok):
        self.dl_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="暂停")
        self.status_var.set("完成" if ok else "失败，详见日志")

    def toggle_pause(self):
        """暂停/继续当前下载"""
        try:
            import main
        except Exception:
            return
        dl = main.current_downloader
        if dl is None:
            return
        if dl.is_paused():
            dl.resume()
            self.pause_btn.configure(text="暂停")
            self.status_var.set("已继续")
        else:
            dl.pause()
            self.pause_btn.configure(text="继续")
            self.status_var.set("已暂停")

    # ------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------
    def start_download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("提示", "请输入视频 URL")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "正在下载中，请稍候")
            return

        self._append_log("=" * 60)
        self._append_log(f"开始: {url}")
        self.dl_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="暂停")
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self._progress_max = None
        self.status_var.set("解析中...")

        self.worker = threading.Thread(
            target=self._do_download, args=(url,), daemon=True)
        self.worker.start()

    def _do_download(self, url: str):
        """后台线程：解析并下载，结果通过队列发回界面"""
        ok = False
        try:
            import main
            args = self.args or main.parse_args([])
            args.output = str(DEFAULT_DOWNLOAD_DIR)
            args.browser = self.browser_var.get()
            ok = main.process_one(url, args, progress_cb=self._on_progress)
            self._save_url(url)
        except Exception:
            import traceback
            self._append_log_from_thread("下载线程异常:\n" + traceback.format_exc())
        finally:
            self.q.put(("done", ok))

    def _append_log_from_thread(self, text):
        self.q.put(("log", text))

    def _on_progress(self, downloaded, total, speed):
        self.q.put(("progress", downloaded, total, speed))


def run_gui(args=None, initial_urls=()) -> None:
    """启动图形界面（阻塞直到窗口关闭）"""
    app = VideoDownloaderApp(args, initial_urls)
    app.mainloop()
