"""MC-JB2VV Android 入口（Chaquopy 专用）。

必须在 import 后端之前注入 MCJB_DATA_DIR，把上传/产物/日志/DB 等数据目录
重定向到 Android 可写存储。APK 内的 python 源码位于 assets，运行时只读，
直接往那里写会失败，因此数据必须落在 getFilesDir() 之下。

流程（由 MainActivity 在 Python.start 之后 getModule("mcjb_app") 触发）：
  1. 取 Application context -> 拼出可写 data 目录；
  2. 写入环境变量（core.pipeline 的 DATA 在 import 时读取该变量）；
  3. import server（此时 core.pipeline 已用新 DATA 初始化 MANAGER / DB）；
  4. run() 在后台线程启动 Flask，监听 127.0.0.1:5000。
"""
import os
from pathlib import Path

from com.chaquo.python import Python

ctx = Python.getPlatform().getApplication()
files_dir = ctx.getFilesDir().getAbsolutePath()

# 数据目录（上传包 / 产物 / 日志 / history.db / 预设）—— 必须可写
os.environ["MCJB_DATA_DIR"] = os.path.join(files_dir, "mcjb_data")
Path(os.environ["MCJB_DATA_DIR"]).mkdir(parents=True, exist_ok=True)

# WEB 走 python 目录下的 web/（Chaquopy 提取到内部存储，可写可读），无需重定向。

# env 就绪后再 import 后端，确保 core.pipeline.DATA 已指向可写目录
import server  # noqa: E402

HOST = "127.0.0.1"
PORT = 5000


def run():
    server.app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)


def url():
    return f"http://{HOST}:{PORT}/"
