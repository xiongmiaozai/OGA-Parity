"""MC-JB2VV Android 离线版 · Python 侧入口。

通过 Chaquopy 在设备本地运行，复用桌面版完整 core/ 转换内核（一行未改），
不使用任何网络能力。Java 侧通过 Chaquopy 调用本模块暴露的函数：

    init(data_dir, rules_dir)  -> 初始化（必须在任何其它调用之前）
    start(zip_path, options)   -> 创建并启动转换任务，返回 tid
    poll(tid)                  -> 返回 JSON 字符串：state/progress/stage/error/output...
    cancel(tid)                -> 取消任务
    rules_version()            -> 当前规则包版本

设计要点：
  * MCJB_DATA_DIR 由 Java 注入到 app 私有可写目录（core/pipeline.py 已预留）。
  * core.convert.RULES_DIR 指向从 assets 复制出来的规则目录 —— Android 里
    __file__ 指向 Chaquopy asset 虚拟路径，不能用于 read_text()。
  * MANAGER 在 import core.pipeline 时即构造（会 load_rules、连 sqlite），
    因此必须先设好环境变量与 RULES_DIR 再 import，故这里用延迟导入。
"""

import json
import os
from pathlib import Path

_ready = False
_MANAGER = None
_OUTPUT = None
_TERMINAL = {"SUCCESS", "FAILED", "TIMEOUT", "CANCELLED", "BLOCKED", "INTERRUPTED"}


def init(data_dir, rules_dir):
    """初始化离线运行时。由 Application.onCreate 调用一次。"""
    global _ready, _MANAGER, _OUTPUT
    if _ready:
        return "already"

    data_dir = str(data_dir)
    rules_dir = str(rules_dir)
    os.environ["MCJB_DATA_DIR"] = data_dir
    for sub in ("uploads", "output", "logs", "previews"):
        (Path(data_dir) / sub).mkdir(parents=True, exist_ok=True)

    # 规则目录重定向（先于 core.pipeline 导入）
    import core.convert as convert
    convert.RULES_DIR = Path(rules_dir)

    import core.pipeline as pipeline
    # 自检：环境变量必须在 import 时已生效，否则 DATA 会落到不可写的 asset 虚拟路径
    if pipeline.DATA.resolve() != Path(data_dir).resolve():
        raise RuntimeError("MCJB_DATA_DIR 注入失败：DATA=%s 期望=%s"
                           % (pipeline.DATA, data_dir))
    _MANAGER = pipeline.MANAGER
    _OUTPUT = pipeline.OUTPUT
    _ready = True
    return "ok:" + _MANAGER.rules["bundle"]["rules_version"]


def rules_version():
    if not _ready:
        return "uninitialized"
    return _MANAGER.rules["bundle"]["rules_version"]


def presets():
    """返回可用预设（供 UI 展示，离线版默认只用 vv_standard）。"""
    if not _ready:
        return "[]"
    try:
        return json.dumps(_MANAGER.presets.list(), ensure_ascii=False)
    except Exception:
        return "[]"


def start(zip_path, options=None):
    """创建并启动转换任务，返回 tid。options 为 JSON 字符串或 dict。

    options 可含 preset 字段指定预设（默认 vv_standard）；其余字段作为转换参数覆盖。
    """
    if not _ready:
        raise RuntimeError("mcjb_app.init() 尚未调用")
    if isinstance(options, str):
        options = json.loads(options or "{}")
    opts = dict(options or {})
    preset_id = opts.pop("preset", "vv_standard")
    # 离线设备内存有限，默认限制任务超时与最大贴图边长
    opts.setdefault("task_timeout_s", 1800)
    t = _MANAGER.create(str(zip_path), preset_id, opts)
    _MANAGER.start(t.id)
    return t.id


def poll(tid):
    """返回任务快照 JSON 字符串（供 Java 轮询）。"""
    if not _ready:
        return json.dumps({"state": "FAILED", "error": "未初始化"})
    s = _MANAGER.snapshot(str(tid))
    s["terminal"] = s.get("state") in _TERMINAL
    # 只保留 UI 需要的字段，避免把完整 report/大对象跨 JNI 传输
    out = {k: s.get(k) for k in (
        "id", "state", "stage", "progress", "current_file", "elapsed_text",
        "eta_text", "output_name", "error", "error_code", "terminal",
        "summary", "report", "logs_tail")}
    if out.get("state") == "SUCCESS":
        t = _MANAGER.tasks.get(str(tid))
        out["output_path"] = str(t.output_path) if t and t.output_path else ""
    return json.dumps(out, ensure_ascii=False, default=str)


def cancel(tid):
    if not _ready:
        return "uninitialized"
    try:
        _MANAGER.cancel(str(tid))
        return "ok"
    except Exception as e:
        return "err:" + str(e)


def output_dir():
    """产物目录（Java 侧用于复制 .mcpack 到公共下载目录）。"""
    return str(_OUTPUT) if _OUTPUT else ""


def result(tid):
    """返回任务成功后的完整快照（含预览 base64 缩略图与校验报告），供结果页展示。"""
    if not _ready:
        return json.dumps({"state": "FAILED", "error": "未初始化"})
    s = _MANAGER.snapshot(str(tid), include_previews=True)
    out = {k: s.get(k) for k in (
        "id", "state", "stage", "progress", "current_file", "elapsed_text",
        "eta_text", "output_name", "error", "error_code", "terminal",
        "summary", "report", "previews", "preview_count")}
    if out.get("state") == "SUCCESS":
        t = _MANAGER.tasks.get(str(tid))
        out["output_path"] = str(t.output_path) if t and t.output_path else ""
    return json.dumps(out, ensure_ascii=False, default=str)


def history(page=0, limit=50):
    """返回本地转换历史（SQLite），供历史面板展示。"""
    if not _ready:
        return "[]"
    try:
        rows = _MANAGER.history.query(limit=int(limit))
        return json.dumps(rows, ensure_ascii=False, default=str)
    except Exception:
        return "[]"
