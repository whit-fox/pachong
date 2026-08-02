# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把视频下载器打包成单个 exe。

关键点：
1. playwright 在函数里延迟导入，且带 node 驱动，需 collect_all 整体收集。
   注意：chromium 浏览器内核不打包进 exe（约 200MB），
   运行时读取用户目录里已安装的浏览器（ms-playwright 缓存目录）。
2. pycryptodome 的 Crypto.Cipher.AES 也是延迟导入，需 collect_submodules 收集。
3. ffmpeg-python 延迟导入，需 hiddenimports。
4. console=True：程序需要命令行交互（输入 URL / 选择序号）。
"""
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []

# playwright：整体收集其包文件与驱动（不含浏览器内核）
pw_datas, pw_binaries, pw_hidden = collect_all('playwright')
datas += pw_datas
binaries += pw_binaries
hiddenimports += pw_hidden

# OpenSSL DLL：Anaconda 的 _ssl 依赖它们，漏打包会导致启动时
# "ssl.SSLError: [DSO: LOAD_FAILED] could not load the shared library" 闪退
OPENSSL_DIR = 'D:/STUDY/ANACONDA3/Library/bin'
for dll in ('libssl-3-x64.dll', 'libcrypto-3-x64.dll'):
    p = os.path.join(OPENSSL_DIR, dll)
    if os.path.exists(p):
        binaries.append((p, '.'))

# tkinter 的 tcl/tk 运行时：Anaconda 布局特殊（DLL 在 Library/bin，
# 数据在 Library/lib/tcl8.6、tk8.6），PyInstaller 钩子没自动收集，
# 漏了会导致 "_tkinter DLL load failed"（GUI 起不来）。手动补齐。
TCL_BIN = 'D:/STUDY/ANACONDA3/Library/bin'
TCL_LIB = 'D:/STUDY/ANACONDA3/Library/lib'
for dll in ('tcl86t.dll', 'tk86t.dll'):
    p = os.path.join(TCL_BIN, dll)
    if os.path.exists(p):
        binaries.append((p, '.'))
datas.append((os.path.join(TCL_LIB, 'tcl8.6'), 'tcl8.6'))
datas.append((os.path.join(TCL_LIB, 'tk8.6'), 'tk8.6'))

# pycryptodome：AES 解密用
hiddenimports += collect_submodules('Crypto')

# ffmpeg-python（m3u8 合并，可选）
hiddenimports += ['ffmpeg', 'ffmpeg._run', 'ffmpeg._utils']

# 图形界面（main.py 里延迟导入，PyInstaller 检测不到，需手动收集）
hiddenimports += ['gui']

# 排除 Anaconda 自带但本程序用不到的重型库，缩小体积、避免无谓的 DLL 告警。
# 注意：tkinter 是图形界面必需的，不能排除（PyInstaller 会自动打包它）。
excludes = [
    'numpy', 'pandas', 'scipy', 'matplotlib', 'tables', 'numexpr',
    'IPython', 'jupyter', 'jupyterlab', 'notebook', 'notebook_server',
    'PyQt5', 'PySide6', 'PySide2', 'pytest', 'flask', 'django', 'mpl_toolkits',
    # setuptools/pkg_resources：启动钩子会报 platformdirs 缺失，
    # 本程序运行时不依赖 pkg_resources，直接排除最省事
    'setuptools', 'pkg_resources',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='视频下载器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed：不弹黑窗口，只显示图形界面
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
