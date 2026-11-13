"""离线 Web 工作台服务（PRD L2/L3 适配层的参考实现）。

所有转换在本地完成，无外部网络依赖。启动：python3 server.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.archive import ArchiveError, ArchiveReader, MetaParser  # noqa: E402
from core.clock import CLOCK  # noqa: E402
from core.convert import load_rules  # noqa: E402
from core.pipeline import LOGS, MANAGER, OUTPUT, UPLOADS  # noqa: E402
from samples.make_samples import build_java_pack, build_shaderpack  # noqa: E402

WEB = Path(os.environ.get("MCJB_WEB_DIR", str(ROOT / "web")))
SAMPLES = ROOT / "samples"
SOURCES: dict[str, str] = {}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB（直连/CLI 上限，公网网关另有限制）

# 公网网关（CloudStudio Gateway / stgw）对单个请求体实测约 50~64 MB 即返回 413，
# 整包 POST 会被掐断。分片上传把文件切成 4 MB 逐片发送，绕过网关体积与超时限制。
CHUNKS = UPLOADS / ".chunks"
CHUNK_LIMIT = 8 * 1024 * 1024       # 单片硬上限
MAX_CHUNKS = 4096                   # 最大分片数
MAX_TOTAL_UPLOAD = 4 * 1024 ** 3    # 与内核解压上限一致
CHUNK_TTL = 2 * 3600                # 孤儿分片保留 2 小时


def ok(data: dict, **extra) -> Response:
    return jsonify({"ok": True, **data, **extra})


def fail(msg: str, code: str = "E_BAD_REQUEST", status: int = 400) -> Response:
    return jsonify({"ok": False, "error": msg, "code": code}), status


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", name or "pack.zip")
    return cleaned.strip("._") or "pack.zip"


def summarize(path: str | Path) -> dict:
    reader = ArchiveReader(path)
    try:
        summary, _groups, _anims = MetaParser(reader, load_rules()).parse()
        return MANAGER._summary_dict(summary)
    finally:
        reader.close()


def resolve(sid: str) -> str | None:
    path = SOURCES.get(sid)
    if path and Path(path).exists():
        return path
    return None


# ---------------------------------------------------------------- 页面
@app.get("/")
def index():
    # 前端改动每次读盘即时生效；禁用缓存，避免浏览器/反代返回 304 让用户停留在旧版 JS。
    resp = send_from_directory(WEB, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------------------------------------------------------------- 引导信息
@app.get("/api/bootstrap")
def bootstrap():
    return ok({"bootstrap": MANAGER.bootstrap()})


@app.get("/api/clock")
def clock():
    return ok({"clock": MANAGER.time_guard.check()})


@app.get("/api/rules")
def rules():
    r = load_rules()
    return ok({
        "bundle": r["bundle"],
        "schemas": r["schemas"],
        "id_map": {"version": r["id_map"]["version"], "note": r["id_map"]["note"],
                   "block_aliases": r["id_map"]["block_aliases"],
                   "item_aliases": r["id_map"]["item_aliases"]},
    })


# ---------------------------------------------------------------- 导入
@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return fail("未收到文件", "E_NO_FILE")
    name = safe_name(f.filename)
    ext = Path(name).suffix.lstrip(".") or "zip"
    dest = CLOCK.unique_path(UPLOADS, Path(name).stem, ext)
    f.save(dest)
    sid = "up_" + uuid.uuid4().hex[:10]
    SOURCES[sid] = str(dest)
    try:
        summary = summarize(dest)
    except ArchiveError as exc:
        dest.unlink(missing_ok=True)
        SOURCES.pop(sid, None)
        return fail(str(exc), exc.code)
    return ok({"source_id": sid, "filename": name, "size": dest.stat().st_size,
               "summary": summary, "uploaded_at": CLOCK.fmt()})


# ---------------------------------------------------------------- 分片导入
def _chunk_dir(upload_id: str) -> Path:
    """upload_id 只接受 32 位十六进制，杜绝路径穿越。"""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise ValueError("非法的上传会话 ID")
    return CHUNKS / upload_id


def _read_meta(cdir: Path) -> dict:
    p = cdir / "meta.json"
    if not p.exists():
        raise ValueError("上传会话已失效，请重新选择文件")
    return json.loads(p.read_text(encoding="utf-8"))


@app.post("/api/upload/init")
def upload_init():
    d = request.get_json(silent=True) or {}
    name = safe_name(d.get("filename", ""))
    try:
        size = int(d.get("size", 0))
        total = int(d.get("total_chunks", 0))
    except (TypeError, ValueError):
        return fail("文件大小或分片数不合法", "E_OPTION")
    if not (0 < size <= MAX_TOTAL_UPLOAD):
        return fail("文件大小超出允许范围（上限 4 GB）", "E_TOO_LARGE", 413)
    if not (1 <= total <= MAX_CHUNKS):
        return fail("分片数量不合法", "E_OPTION")
    CHUNKS.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in CHUNKS.iterdir():
        if old.is_dir() and now - old.stat().st_mtime > CHUNK_TTL:
            shutil.rmtree(old, ignore_errors=True)
    uid = uuid.uuid4().hex
    cdir = _chunk_dir(uid)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "meta.json").write_text(
        json.dumps({"filename": name, "size": size, "total": total, "created": now}),
        encoding="utf-8")
    return ok({"upload_id": uid, "total_chunks": total})


@app.post("/api/upload/chunk")
def upload_chunk():
    try:
        cdir = _chunk_dir(request.form.get("upload_id", ""))
        meta = _read_meta(cdir)
        i = int(request.form.get("index", ""))
    except (ValueError, TypeError):
        return fail("上传会话已失效或分片序号不合法，请重新选择文件", "E_SOURCE_EXPIRED")
    if not (0 <= i < meta["total"]):
        return fail("分片序号越界", "E_OPTION")
    f = request.files.get("chunk")
    if not f:
        return fail("缺少分片数据", "E_NO_FILE")
    data = f.read()
    if len(data) > CHUNK_LIMIT:
        return fail("单个分片超过 8 MB 上限", "E_TOO_LARGE", 413)
    (cdir / f"{i:06d}.part").write_bytes(data)
    return ok({"index": i, "received": len(data)})


@app.post("/api/upload/complete")
def upload_complete():
    d = request.get_json(silent=True) or {}
    try:
        cdir = _chunk_dir(d.get("upload_id", ""))
        meta = _read_meta(cdir)
    except ValueError as exc:
        return fail(str(exc), "E_SOURCE_EXPIRED")
    parts = [cdir / f"{i:06d}.part" for i in range(meta["total"])]
    missing = [i for i, p in enumerate(parts) if not p.exists()]
    if missing:
        return fail(f"分片不完整（缺 {len(missing)} 片），请重新上传", "E_INCOMPLETE")
    actual = sum(p.stat().st_size for p in parts)
    if actual != meta["size"]:
        return fail(f"分片总大小不匹配（{actual} / {meta['size']} 字节）", "E_SIZE_MISMATCH")
    name = meta["filename"]
    ext = Path(name).suffix.lstrip(".") or "zip"
    dest = CLOCK.unique_path(UPLOADS, Path(name).stem, ext)
    with dest.open("wb") as out:
        for p in parts:
            with p.open("rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
    shutil.rmtree(cdir, ignore_errors=True)
    sid = "up_" + uuid.uuid4().hex[:10]
    SOURCES[sid] = str(dest)
    try:
        summary = summarize(dest)
    except ArchiveError as exc:
        dest.unlink(missing_ok=True)
        SOURCES.pop(sid, None)
        return fail(str(exc), exc.code)
    return ok({"source_id": sid, "filename": name, "size": dest.stat().st_size,
               "summary": summary, "uploaded_at": CLOCK.fmt(), "chunked": True})


@app.post("/api/sample")
def sample():
    kind = (request.get_json(silent=True) or {}).get("kind", "java")
    try:
        path = build_java_pack(SAMPLES / "demo_java_pack.zip") if kind == "java" \
            else build_shaderpack(SAMPLES / "demo_shaderpack.zip")
    except Exception as exc:  # noqa: BLE001
        return fail(f"示例包生成失败：{exc}", "E_SAMPLE", 500)
    sid = "sample_" + kind + "_" + uuid.uuid4().hex[:6]
    SOURCES[sid] = str(path)
    try:
        summary = summarize(path)
    except ArchiveError as exc:
        return fail(str(exc), exc.code)
    return ok({"source_id": sid, "filename": path.name, "size": path.stat().st_size,
               "summary": summary, "uploaded_at": CLOCK.fmt()})


# ---------------------------------------------------------------- 任务
@app.post("/api/convert")
def convert():
    data = request.get_json(silent=True) or {}
    sid = data.get("source_id", "")
    path = resolve(sid)
    if not path:
        return fail("资源包已失效，请重新导入", "E_SOURCE_EXPIRED")
    allowed = {"bump", "packed", "ao_bake", "max_size", "resample", "flip_normal_y",
               "ai_repair", "ctm_variants", "omit_near_flat_normals", "legacy_compat",
               "profile", "parse_timeout_s", "image_timeout_s",
               "anim_timeout_s", "export_timeout_s", "task_timeout_s"}
    options = {k: v for k, v in (data.get("options") or {}).items() if k in allowed}
    if options.get("bump") not in (None, "normal", "heightmap"):
        return fail("凹凸策略只能是 normal 或 heightmap", "E_OPTION")
    t = MANAGER.create(path, data.get("preset", "vv_standard"), options)
    MANAGER.start(t.id)
    return ok({"task_id": t.id, "snapshot": MANAGER.snapshot(t.id)})


@app.get("/api/task/<tid>")
def task(tid: str):
    if tid not in MANAGER.tasks:
        return fail("任务不存在", "E_NOT_FOUND", 404)
    return ok({"snapshot": MANAGER.snapshot(tid)})


@app.get("/api/previews/<tid>")
def previews(tid: str):
    if tid not in MANAGER.tasks:
        return fail("任务不存在", "E_NOT_FOUND", 404)
    return ok({"previews": MANAGER.snapshot(tid, include_previews=True).get("previews", {})})


@app.post("/api/task/<tid>/<action>")
def task_action(tid: str, action: str):
    if tid not in MANAGER.tasks:
        return fail("任务不存在", "E_NOT_FOUND", 404)
    if action == "pause":
        MANAGER.pause(tid)
    elif action == "cancel":
        MANAGER.cancel(tid)
    elif action == "resume":
        t = MANAGER.tasks[tid]
        overrides = request.get_json(silent=True) or {}
        if "task_timeout_s" in overrides:
            t.opts.task_timeout_s = float(overrides["task_timeout_s"])
        MANAGER.start(tid, resume=True)
    else:
        return fail("未知操作", "E_ACTION")
    return ok({"snapshot": MANAGER.snapshot(tid)})


@app.get("/api/download/<tid>")
def download(tid: str):
    t = MANAGER.tasks.get(tid)
    if not t or not t.output_path or not Path(t.output_path).exists():
        abort(404)
    return send_file(t.output_path, as_attachment=True,
                     download_name=Path(t.output_path).name,
                     mimetype="application/octet-stream")


@app.get("/api/logs/<tid>")
def logs(tid: str):
    t = MANAGER.tasks.get(tid)
    if not t:
        abort(404)
    text = "\n".join(t.logs) + "\n"
    return Response(text, mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{tid}.log"'})
    # 注：日志同时落盘到 data/logs/<tid>.log，可通过历史记录追溯


# ---------------------------------------------------------------- 历史 / 预设
@app.get("/api/history")
def history():
    rows = MANAGER.history.query(
        date_from=request.args.get("from", ""), date_to=request.args.get("to", ""),
        status=request.args.get("status", ""), sort=request.args.get("sort", "started_at"),
        desc=request.args.get("desc", "1") != "0", limit=int(request.args.get("limit", 200)),
    )
    return ok({"rows": rows, "total": MANAGER.history.count()})


@app.post("/api/history/clear")
def history_clear():
    MANAGER.history.clear()
    return ok({"cleared": True})


@app.get("/api/presets")
def presets():
    return ok({"presets": MANAGER.presets.list()})


@app.post("/api/presets")
def preset_save():
    data = request.get_json(silent=True) or {}
    if not data.get("name"):
        return fail("预设名称不能为空", "E_PRESET")
    return ok({"preset": MANAGER.presets.save(data)})


@app.delete("/api/presets/<pid>")
def preset_delete(pid: str):
    if not MANAGER.presets.delete(pid):
        return fail("内置预设不可删除，或预设不存在", "E_PRESET")
    return ok({"deleted": pid})


@app.errorhandler(RequestEntityTooLarge)
def too_large(_e):
    return fail("文件超过 2 GB 上限", "E_TOO_LARGE", 413)


@app.errorhandler(404)
def not_found(_e):
    return fail("资源不存在", "E_NOT_FOUND", 404)


if __name__ == "__main__":
    import os

    # 本地运行默认 127.0.0.1:8000；发布到线上时由平台注入 PORT，需绑定 0.0.0.0
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    print(f"[{CLOCK.fmt()}] 离线转换工作台启动 · http://{host}:{port}")
    print(f"[{CLOCK.fmt()}] 规则包 {MANAGER.rules['bundle']['rules_version']} · "
          f"引擎 >= {'.'.join(map(str, MANAGER.rules['bundle']['engine_min_version']))}")
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
