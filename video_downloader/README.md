# 视频下载工具 video_downloader

解析网页中的公开视频资源并下载，支持 **MP4 直链** 与 **m3u8(HLS) 流**，
自动识别 `<video>` 标签、JS 播放器配置、`<iframe>` 内嵌页面中的视频地址，
支持断点续传、多线程加速、Cookie 登录态、Referer 防盗链。

## 1. 环境要求

- Python 3.11+
- ffmpeg（m3u8 合并为 MP4 时使用，**非 pip 包，需单独安装**）

  ```bash
  # Windows
  winget install ffmpeg
  # 或从 https://www.gyan.dev/ffmpeg/builds/ 下载解压后加入 PATH
  ```

## 2. 安装依赖

```bash
cd video_downloader
pip install -r requirements.txt
```

可选：若需解析 JS 动态页面（`--browser`），还需安装浏览器内核：

```bash
playwright install chromium
```

## 3. 运行示例

```bash
# 解析网页并自动下载（m3u8 自动选最高清晰度）
python main.py https://example.com/video.html

# 直接下载 mp4 / m3u8 直链
python main.py https://example.com/video.mp4
python main.py https://example.com/playlist.m3u8

# 指定输出目录 + 指定清晰度（720p）
python main.py https://example.com/video.html -o D:/videos --quality 720

# 带 Cookie 登录态 + 自定义 Referer（防盗链）
python main.py https://example.com/video.html --cookies cookies.txt --referer https://example.com

# JS 动态渲染页面（视频地址由脚本动态生成时）
python main.py https://example.com/dynamic.html --browser

# 一次下载多个
python main.py https://a.com/v1 https://b.com/v2 -o downloads

# 调试模式（打印详细解析过程）
python main.py https://example.com/video.html --debug
```

### 常用参数速查

| 参数 | 作用 |
|------|------|
| `-o, --output` | 输出目录（默认 `downloads/`） |
| `--quality` | m3u8 清晰度：`best` / `lowest` / `1080`、`720` 等 |
| `--threads N` | mp4 多线程分片数（默认 8） |
| `--concurrency N` | m3u8 分片并发数（默认 10） |
| `--cookies FILE` | Cookie 文件（Netscape / JSON 格式） |
| `--referer URL` | 防盗链 Referer（默认取页面 URL） |
| `--browser` | 用 Playwright 渲染 JS 动态页面 |
| `--keep-ts` | 合并后保留 ts 分片 |
| `--format mp4/ts` | m3u8 输出格式（默认 mp4） |
| `--debug` | 输出调试日志 |

## 4. 项目结构

```
video_downloader/
├── main.py              # 程序入口：参数解析、流程编排
├── downloader.py        # 下载核心：多线程分片 / 断点续传 / 进度显示
├── parser.py            # 视频地址解析：HTML / JS / iframe / 浏览器拦截
├── m3u8_downloader.py   # m3u8 处理：解析 / 并发分片 / AES解密 / ffmpeg合并
├── utils.py             # 工具类：日志 / 重试 / Cookie / 文件名
├── config.py            # 全局配置
└── requirements.txt     # 依赖清单
```

## 5. 调试指南：找不到视频地址怎么办

程序在页面中找不到视频源时，会提示 `未找到视频地址`。按以下步骤排查：

### 步骤 1：先确认「视频地址是否藏在动态 JS 里」

大多数"找不到"的根源是：视频地址由 JavaScript 在运行时才生成，静态 HTML 里根本没有。
优先尝试：

```bash
python main.py URL --browser
```

`--browser` 会用 Playwright 打开真实浏览器渲染页面，并拦截所有 `m3u8/mp4` 网络请求。
这是最可靠的兜底方案，能覆盖 blob: 播放、接口加密等绝大多数情况。

### 步骤 2：看解析日志，确认程序"看到"了什么

```bash
python main.py URL --debug
```

程序会打印每个候选视频源的 URL 和**来源**（`video标签src` / `source标签src` /
`正则扫描` / `JS配置字段` / `浏览器网络拦截`）。
如果没有候选，说明页面静态内容里确实不含媒体链接。

### 步骤 3：用浏览器开发者工具手工定位真实地址

这是最通用的方法，**对任何网站都适用**：

1. 打开视频页，按 `F12` 打开开发者工具
2. 切到 **Network（网络）** 面板，勾选筛选 `Media`（或输入关键字 `m3u8` / `mp4`）
3. 刷新页面 / 点播放，观察列表里出现的媒体请求
4. 右键复制该请求的 URL —— 这就是真实视频地址，可直接运行：

   ```bash
   python main.py "https://真实视频地址.m3u8"
   ```

> 提示：如果 Network 里没有 Media 请求，说明播放器可能走了
> `blob:` 或 MSE 分片，此时切换到 `Fetch/XHR` 标签，找返回 JSON 里含
> `url` / `videoUrl` / `play_url` 等字段的接口，把接口返回的地址拿去下载。

### 步骤 4：常见隐藏位置速查

| 特征 | 地址可能藏在 |
|------|-------------|
| 播放器是 `<video>` 标签 | `src` / `data-src` / `<source src>` |
| 页面有 `<iframe>` | iframe 内嵌的播放器页（程序会自动递归解析） |
| `<script>` 里有 `playerConfig` / `videoUrl` / `hlsUrl` 等 | 脚本字符串里 |
| 页面用 Vue/React 动态渲染 | 需 `--browser` 拦截网络请求 |
| 视频接口返回 JSON | 接口响应里的 `url` / `play_url` 字段 |
| 视频加密（AES-128） | m3u8 里的 `#EXT-X-KEY`（程序已支持解密） |

### 步骤 5：确认下载环境

- 报 `403` / 拿不到内容：很可能防盗链，用 `--referer <页面地址>` 或带 Cookie
  ```bash
  python main.py URL --referer https://页面地址 --cookies cookies.txt
  ```
- **视频地址找到了但下载超时/连不上**：多半是视频在**海外 CDN**（如 Akamai/字节跳动
  节点），当前网络直连不通。程序会自动检测本机运行的 Clash/V2ray 代理并走代理下载；
  也可手动指定 `--proxy http://127.0.0.1:7897`（节点连不通时换 `--proxy` 或关掉）
- **视频在 iframe 里找不到**：播放器常把地址写进 iframe 内 `<video>` 的 src（不带
  `.mp4`/`.m3u8` 后缀），静态扫描发现不了。用 `--browser` 会自动读取所有 iframe 内
  的 video src 并通过 HEAD 探测识别类型
- m3u8 合并失败：确认系统已安装 ffmpeg（`ffmpeg -version`），
  未安装时程序会退化为 ts 直接拼接，输出扩展名为 `.ts`，多数播放器可正常播放
- 下载中断：重跑同一命令即可断点续传，已下载的分片 / 部分文件会跳过
