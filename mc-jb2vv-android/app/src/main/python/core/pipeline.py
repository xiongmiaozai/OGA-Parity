"""任务状态机、日志、历史、预设、转换报告（PRD 6.3 / 6.5 / 6.7，FR-T-01~06）。

状态机：PENDING -> RUNNING <-> PAUSED -> SUCCESS | FAILED | TIMEOUT | CANCELLED | INTERRUPTED | BLOCKED
每个阶段边界写 Checkpoint，支持超时 / 暂停 / 后台中断后续跑。
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .archive import ArchiveError, ArchiveReader, MetaParser
from .clock import CLOCK, ProgressTimer, TimeGuard, TimeoutGuard, hms
from .ctm import build_variant_groups, parse_ctm
from .convert import (
    AnimMigrator,
    ChannelConverter,
    IdMapper,
    JsonEmitter,
    PackWriter,
    Validator,
    load_rules,
    resolve_pack_icon,
)
from .models import (
    ConversionReport,
    ConvertOptions,
    MaterialGroup,
    ParseSummary,
    Stage,
    TaskState,
)

ROOT = Path(__file__).resolve().parent.parent
# MCJB_DATA_DIR 由 Android 入口（mcjb_app）注入，指向可写目录；桌面版不设置则沿用默认 data/。
DATA = Path(os.environ.get("MCJB_DATA_DIR", str(ROOT / "data")))
UPLOADS = DATA / "uploads"
OUTPUT = DATA / "output"
LOGS = DATA / "logs"
PREVIEWS = DATA / "previews"
DB_PATH = DATA / "history.db"
PRESET_PATH = DATA / "presets.json"
TIME_STATE = DATA / "time_anchor.json"
for d in (UPLOADS, OUTPUT, LOGS, PREVIEWS):
    d.mkdir(parents=True, exist_ok=True)

DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "vv_standard": {
        "id": "vv_standard", "name": "VV 标准（Normal + MER）", "description": "官方推荐策略，兼容方块与物品，默认预设",
        "builtin": True, "created_at": "2026-11-11 20:46:23",
        "options": {"bump": "normal", "packed": "mer", "ao_bake": 0.0, "max_size": 2048, "ai_repair": False},
    },
    "vv_heightmap": {
        "id": "vv_heightmap", "name": "VV 高度图（Heightmap + MER）", "description": "方块材质使用高度图；物品自动回退 normal",
        "builtin": True, "created_at": "2026-11-11 20:46:23",
        "options": {"bump": "heightmap", "packed": "mer", "ao_bake": 0.0, "max_size": 2048, "ai_repair": False},
    },
    "vv_quality": {
        "id": "vv_quality", "name": "精细（AO 烘焙 + AI 补全）", "description": "烘焙 AO、补全缺失贴图、更高重采样上限",
        "builtin": True, "created_at": "2026-11-11 20:46:23",
        "options": {"bump": "normal", "packed": "mer", "ao_bake": 0.35, "max_size": 4096, "ai_repair": True},
    },
    "vv_fast": {
        "id": "vv_fast", "name": "低内存 / 快速", "description": "小尺寸重采样、关闭 AI，适配移动端与 2 GB 内存设备",
        "builtin": True, "created_at": "2026-11-11 20:46:23",
        "options": {"bump": "normal", "packed": "mer", "ao_bake": 0.0, "max_size": 512, "ai_repair": False},
    },
}

ERROR_TEXT = {
    "E_NOT_FOUND": "找不到导入的文件或文件夹，请重新选择。",
    "E_FORMAT": "不支持的文件格式，请导入 ZIP 或文件夹。",
    "E_ZIP_SLIP": "压缩包包含非法路径（路径穿越），已阻止导入以保护系统安全。",
    "E_ZIP_BOMB": "检测到疑似解压炸弹，已阻止导入。",
    "E_CRC": "压缩包成员损坏或 CRC 校验失败，请重新下载资源包。",
    "E_TOO_LARGE": "压缩包解压后体积超过 4 GB 上限。",
    "E_TIMEOUT": "任务超时，已自动终止并保存进度，可从断点续跑。",
    "E_VALIDATION": "产物未通过官方 Schema 校验，已禁止导出，请查看校验清单。",
    "E_PATH_ESCAPE": "检测到越界文件读取，已阻止。",
    "E_UNKNOWN": "转换过程中出现未预期错误，已保存日志与进度。",
}


# ---------------------------------------------------------------- 持久化
class HistoryRepo:
    """FR-T-06：全量历史记录，SQLite 本地持久化。"""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    input_name TEXT, input_path TEXT,
                    started_at TEXT, ended_at TEXT,
                    duration_ms INTEGER, status TEXT,
                    output_path TEXT, output_size INTEGER,
                    engine_ver TEXT, rules_ver TEXT,
                    preset_id TEXT, error_code TEXT, progress REAL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def start(self, task: "Task", rules_ver: str) -> None:
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.id, Path(task.source).name, str(task.source), task.timer.started_at(), "",
                 0, task.state.value, "", 0, "1.21.120+", rules_ver, task.preset_name, "", 0.0),
            )

    def finish(self, task: "Task") -> None:
        out = task.output_path or ""
        size = Path(out).stat().st_size if out and Path(out).exists() else 0
        with self._lock, self._connect() as c:
            c.execute(
                "UPDATE tasks SET ended_at=?, duration_ms=?, status=?, output_path=?, "
                "output_size=?, error_code=?, progress=? WHERE id=?",
                (CLOCK.fmt(), int(task.timer.elapsed() * 1000), task.state.value, out,
                 size, task.error_code, round(task.progress, 4), task.id),
            )

    def query(self, date_from: str = "", date_to: str = "", status: str = "",
              sort: str = "started_at", desc: bool = True, limit: int = 200) -> list[dict[str, Any]]:
        allowed = {"started_at", "ended_at", "duration_ms", "status", "input_name"}
        sort = sort if sort in allowed else "started_at"
        clauses, args = [], []
        if date_from:
            clauses.append("started_at >= ?")
            args.append(date_from + ("" if len(date_from) > 10 else " 00:00:00"))
        if date_to:
            clauses.append("started_at <= ?")
            args.append(date_to + ("" if len(date_to) > 10 else " 23:59:59"))
        if status:
            clauses.append("status = ?")
            args.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM tasks{where} ORDER BY {sort} {'DESC' if desc else 'ASC'} LIMIT ?"
        args.append(limit)
        with self._connect() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def count(self) -> int:
        with self._connect() as c:
            return int(c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    def clear(self) -> None:
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM tasks")


class PresetRepo:
    def __init__(self, path: Path = PRESET_PATH):
        self.path = path
        if not path.exists():
            self._save(DEFAULT_PRESETS)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return dict(DEFAULT_PRESETS)

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> list[dict[str, Any]]:
        return list(self._load().values())

    def get(self, pid: str) -> dict[str, Any] | None:
        return self._load().get(pid)

    def save(self, preset: dict[str, Any]) -> dict[str, Any]:
        data = self._load()
        pid = preset.get("id") or ("preset_" + uuid.uuid4().hex[:8])
        preset["id"] = pid
        preset.setdefault("created_at", CLOCK.fmt())
        preset.setdefault("builtin", False)
        preset.setdefault("description", "")
        data[pid] = preset
        self._save(data)
        return preset

    def delete(self, pid: str) -> bool:
        data = self._load()
        if data.get(pid, {}).get("builtin"):
            return False
        return bool(data.pop(pid, None)) and (self._save(data) or True)


# ---------------------------------------------------------------- 任务
@dataclass
class Task:
    id: str
    source: str
    preset_name: str
    opts: ConvertOptions
    state: TaskState = TaskState.PENDING
    stage: str = ""
    progress: float = 0.0
    eta_s: float = 0.0
    current_file: str = ""
    timer: ProgressTimer = field(default_factory=ProgressTimer)
    pause_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    logs: list[str] = field(default_factory=list)
    summary: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    output_path: str = ""
    error: str = ""
    error_code: str = ""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    entries: dict[str, bytes] = field(default_factory=dict)
    previews: dict[str, dict[str, str]] = field(default_factory=dict)
    thread: threading.Thread | None = None
    heartbeat: float = 0.0
    rules_ver: str = ""


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.history = HistoryRepo()
        self.presets = PresetRepo()
        self.time_guard = TimeGuard(TIME_STATE)
        self.rules = load_rules()
        self._lock = threading.Lock()

    # ---- 日志 ----
    @staticmethod
    def _log(t: Task, level: str, msg: str) -> None:
        line = f"[{CLOCK.fmt()}] [{level}] {msg}"
        t.logs.append(line)
        if len(t.logs) > 3000:
            del t.logs[:1000]
        with (LOGS / f"{t.id}.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ---- 生命周期 ----
    def create(self, source: str, preset_id: str, overrides: dict[str, Any] | None = None) -> Task:
        preset = self.presets.get(preset_id) or self.presets.get("vv_standard")
        opts = ConvertOptions.from_dict({**(preset or {}).get("options", {}), **(overrides or {})})
        tid = "T" + uuid.uuid4().hex[:10]
        t = Task(id=tid, source=source, preset_name=(preset or {}).get("id", "custom"), opts=opts)
        t.rules_ver = self.rules["bundle"]["rules_version"]
        with self._lock:
            self.tasks[tid] = t
        self._log(t, "INFO", f"任务创建 · 预设 {t.preset_name} · 规则包 {t.rules_ver}")
        return t

    def start(self, tid: str, resume: bool = False) -> Task:
        t = self.tasks[tid]
        if resume:
            t.cancel_event.clear()
            t.pause_event.clear()
            if t.thread and t.thread.is_alive() and t.state == TaskState.PAUSED:
                t.state = TaskState.RUNNING
                self._log(t, "INFO", "任务从暂停恢复")
                return t
            self._log(t, "INFO", f"从 Checkpoint 续跑，已完成 {len(t.checkpoint.get('done', []))} 个材质")
        th = threading.Thread(target=self._run, args=(t, resume), daemon=True, name=f"convert-{tid}")
        t.thread = th
        th.start()
        return t

    def pause(self, tid: str) -> None:
        t = self.tasks[tid]
        if t.state == TaskState.RUNNING:
            t.pause_event.set()
            t.state = TaskState.PAUSED
            self._log(t, "INFO", "任务已暂停，进度已保存")

    def cancel(self, tid: str) -> None:
        t = self.tasks[tid]
        t.cancel_event.set()
        t.pause_event.clear()
        if t.state not in (TaskState.SUCCESS, TaskState.BLOCKED):
            t.state = TaskState.CANCELLED
            self._log(t, "WARN", "用户取消任务，进度已保存")

    # ---- 主流水线 ----
    def _run(self, t: Task, resume: bool) -> None:
        timeouts = TimeoutGuard({
            "parse": t.opts.parse_timeout_s,
            "image": t.opts.image_timeout_s,
            "anim": t.opts.anim_timeout_s,
            "export": t.opts.export_timeout_s,
            "task": t.opts.task_timeout_s,
        })
        try:
            t.state = TaskState.RUNNING
            if not resume:
                t.timer = ProgressTimer()
                t.entries, t.previews, t.checkpoint = {}, {}, {}
            t.heartbeat = CLOCK.mono()
            self._log(t, "INFO", f"开始转换 · 开始时间 {t.timer.started_at()}")
            self.history.start(t, t.rules_ver)

            reader = ArchiveReader(t.source)
            try:
                # S1 / S2
                t.stage = Stage.S1_ARCHIVE.value
                files = reader.list_files()
                self._log(t, "INFO", f"S1 归档读取完成，共 {len(files)} 个文件")
                t.stage = Stage.S2_PARSE.value
                parse_start = CLOCK.mono()
                summary, groups, animations = MetaParser(reader, self.rules).parse()
                if timeouts.expired("parse", parse_start):
                    raise TimeoutError("S2 文件解析超时")
                t.summary = self._summary_dict(summary)
                self._log(t, "INFO", f"S2 解析完成：{summary.material_count} 个材质 / "
                                     f"{summary.labpbr_groups} 组 LabPBR / {summary.animation_count} 个动画")

                # S3 光影包拦截（FR-L-01）
                t.stage = Stage.S3_GUARD.value
                if summary.shaderpack:
                    t.state = TaskState.BLOCKED
                    t.progress = 1.0
                    t.report = {
                        "generated_at": CLOCK.fmt(), "rules_version": t.rules_ver,
                        "passed": False, "shaderpack": summary.shaderpack,
                        "strategy": {}, "checks": [], "mapped": [], "unmapped": [],
                        "skipped": [], "downgraded": [], "failed": [], "ai_actions": [],
                    }
                    self._log(t, "WARN", f"S3 预检拦截（{summary.shaderpack.get('kind')}）："
                                        f"{summary.shaderpack.get('state_label')}，不产出 .mcpack")
                    self.history.finish(t)
                    return

                # S4 标识符映射
                t.stage = Stage.S4_IDMAP.value
                mapper = IdMapper(self.rules)
                targets: dict[str, str] = {}
                report_mapped: list[str] = []
                report_unmapped: list[str] = []
                # 目标键冲突防护：IdMapper 取路径末段作短名，不同目录可能塌缩到同一
                # target（如 environment/snow 与 block/snow）。groups 已按目录优先级
                # 排序，此处只接受第一个，其余显式跳过，绝不静默覆盖已落盘的贴图。
                claimed: dict[str, str] = {}
                collision_skips: list[str] = []
                for g in groups:
                    target, hit = mapper.map(g)
                    if target in claimed:
                        collision_skips.append(
                            f"{g.key}: 目标名 {target} 已被 {claimed[target]} 占用，"
                            "已跳过以避免覆盖方块贴图"
                        )
                        continue
                    claimed[target] = g.key
                    targets[g.key] = target
                    g.target = target
                    (report_mapped if hit else report_unmapped).append(f"{g.key} -> {target}")
                groups = [g for g in groups if g.key in targets]
                if collision_skips:
                    summary.material_count = len(groups)
                    summary.warnings.append(
                        f"S4 检测到 {len(collision_skips)} 个同名目标键冲突，已保留优先级最高者"
                    )
                    t.summary = self._summary_dict(summary)
                self._log(t, "INFO", f"S4 标识符映射：命中 {len(report_mapped)}，回退 {len(report_unmapped)}，"
                                     f"同名冲突跳过 {len(collision_skips)}")

                # S4.5 可选：OptiFine CTM / random 变体迁移
                ctm_variations: dict[str, list[tuple[str, int]]] = {}
                if t.opts.ctm_variants:
                    short_to_cat = {
                        g.target.rsplit("/", 1)[-1]: g.category
                        for g in groups if g.target
                    }
                    ctm_rules = parse_ctm(reader, short_to_cat, log=lambda lv, m: self._log(t, lv, m))
                    if ctm_rules:
                        vgroups, ctm_variations = build_variant_groups(ctm_rules)
                        # 变体组追加到转换队列，各自分配唯一 target（已在 build 内确定）
                        claimed_v: set[str] = set()
                        for vg in vgroups:
                            if vg.key in targets:
                                continue
                            targets[vg.key] = vg.target
                            vg.target = vg.target
                            groups.append(vg)
                            claimed_v.add(vg.key)
                        summary.material_count = len(groups)
                        t.summary = self._summary_dict(summary)
                        self._log(t, "INFO", f"S4.5 CTM 变体：{len(ctm_rules)} 条规则 → "
                                             f"{len(vgroups)} 个变体贴图组，覆盖 {len(ctm_variations)} 个方块/物品短名")
                    else:
                        self._log(t, "INFO", "S4.5 CTM 变体：未解析到可迁移规则（源包无 optifine 变体或均不匹配）")

                # 恢复断点
                entries = t.entries
                previews = t.previews
                channels: dict[str, int] = dict(t.checkpoint.get("channels", {}))
                texture_sets: dict[str, dict[str, Any]] = dict(t.checkpoint.get("texture_sets", {}))
                categories: dict[str, str] = dict(t.checkpoint.get("categories", {}))
                atlas_blocks: dict[str, str] = dict(t.checkpoint.get("atlas_blocks", {}))
                atlas_items: dict[str, str] = dict(t.checkpoint.get("atlas_items", {}))
                done: set[str] = set(t.checkpoint.get("done", []))
                downgraded: list[str] = list(t.checkpoint.get("downgraded", []))
                skipped: list[str] = list(t.checkpoint.get("skipped", [])) + collision_skips
                ai_actions: list[str] = list(t.checkpoint.get("ai_actions", []))
                notes_all: list[str] = []

                emitter = JsonEmitter(self.rules)
                converter = ChannelConverter(reader, t.opts, self.rules)
                total = max(1, len(groups))

                # S5 通道转换
                t.stage = Stage.S5_CHANNEL.value
                for idx, g in enumerate(groups):
                    if t.cancel_event.is_set():
                        self._save_checkpoint(t, done, channels, texture_sets, categories,
                                              atlas_blocks, atlas_items, downgraded, skipped, ai_actions)
                        return
                    while t.pause_event.is_set():
                        if t.cancel_event.is_set():
                            return
                        time.sleep(0.15)
                    if timeouts.expired("task", t.timer.start_mono):
                        raise TimeoutError(f"S5 整任务超时（上限 {t.opts.task_timeout_s}s）")
                    if g.key in done:
                        continue

                    t.current_file = g.basecolor or g.key
                    t.heartbeat = CLOCK.mono()
                    img_start = CLOCK.mono()
                    try:
                        result = converter.convert(g, targets[g.key])
                    except (ArchiveError, OSError, ValueError) as exc:
                        skipped.append(f"{g.key}: 处理失败已跳过 ({exc})")
                        self._log(t, "WARN", f"{g.key} 处理失败，已跳过：{exc}")
                        done.add(g.key)
                        continue
                    if timeouts.expired("image", img_start):
                        skipped.append(f"{g.key}: 单图处理超时，已跳过")
                        self._log(t, "WARN", f"{g.key} 单图超时，已跳过")
                        done.add(g.key)
                        continue

                    stem = targets[g.key].rsplit("/", 1)[-1]
                    folder = f"textures/{targets[g.key].rsplit('/', 1)[0]}"
                    available = set(result["layers"].keys())
                    ts, ts_notes = emitter.texture_set(
                        stem, available, g.category, t.opts, result.get("uniforms", {})
                    )

                    # 只落盘 texture_set 实际引用的层。normal/heightmap 互斥时，
                    # 未被引用的一张若留在包里，会被误认为「法线/高度没生效」。
                    ref_layer = {
                        stem: "color",
                        f"{stem}_normal": "normal",
                        f"{stem}_heightmap": "heightmap",
                        f"{stem}_mer": "mer",
                    }
                    used = {"color"}
                    for ref in ts["minecraft:texture_set"].values():
                        if isinstance(ref, str) and ref in ref_layer:
                            used.add(ref_layer[ref])
                    suffix = {"color": "", "normal": "_normal",
                              "heightmap": "_heightmap", "mer": "_mer"}
                    for layer, (blob, ch) in result["layers"].items():
                        if layer not in used:
                            continue
                        rel = f"{folder}/{stem}{suffix[layer]}.png"
                        entries[rel] = blob
                        channels[rel] = ch

                    # 预览仍展示全部通道（含互斥层），便于人工比对；产物按上面裁剪
                    previews[stem] = {
                        layer: "data:image/png;base64," + base64.b64encode(blob).decode()
                        for layer, (blob, _) in result["layers"].items()
                    }
                    try:
                        previews[stem]["source"] = "data:image/png;base64," + base64.b64encode(
                            reader.read(g.basecolor)
                        ).decode()
                    except ArchiveError:
                        pass
                    previews[stem]["meta"] = {
                        "width": result["width"], "height": result["height"],
                        "category": result["category"], "animated": result["animated"],
                    }

                    ts_path = f"{folder}/{stem}.texture_set.json"
                    entries[ts_path] = _json_bytes(ts)
                    texture_sets[ts_path] = ts
                    categories[ts_path] = g.category
                    # terrain_texture/item_texture 只分别注册 blocks/items。
                    # entity/particle 仍能生成 texture_set，但没有对应的方块图集入口，
                    # 塞进 terrain_texture 会让游戏把它们当方块贴图解析。
                    if g.category == "items":
                        atlas_items[stem] = f"{folder}/{stem}"
                    elif g.category == "blocks":
                        atlas_blocks[stem] = f"{folder}/{stem}"
                    notes_all.extend(ts_notes)
                    ai_actions.extend(result["ai_actions"])
                    downgraded.extend(result["notes"])
                    if result["animated"]:
                        notes_all.append(f"{stem}: 动画条带保持纵横比，未强制 mip 正方形")

                    done.add(g.key)
                    t.timer.record(CLOCK.mono() - img_start)
                    t.progress = len(done) / total
                    t.eta_s = t.timer.eta(len(done), total)
                    if idx % 5 == 0 or len(done) == total:
                        self._log(t, "INFO", f"S5 进度 {len(done)}/{total} · 耗时 {t.timer.elapsed_text()} · "
                                             f"ETA {hms(t.eta_s)} · 当前 {t.current_file}")
                        self._save_checkpoint(t, done, channels, texture_sets, categories,
                                              atlas_blocks, atlas_items, downgraded, skipped, ai_actions)

                # S6 动画迁移
                t.stage = Stage.S6_ANIM.value
                anim_start = CLOCK.mono()
                flipbook, anim_downgrades = AnimMigrator(self.rules).migrate(animations, targets)
                downgraded.extend(anim_downgrades)
                downgraded.extend(notes_all)
                if timeouts.expired("anim", anim_start):
                    raise TimeoutError("S6 动画迁移超时")
                if flipbook:
                    entries["textures/flipbook_textures.json"] = _json_bytes(flipbook)
                    self._log(t, "INFO", f"S6 动画迁移完成，生成 {len(flipbook['flipbook_textures'])} 条 flipbook")
                else:
                    self._log(t, "INFO", "S6 无动画内容，不生成空 flipbook 文件")

                # S7 生成 JSON
                t.stage = Stage.S7_EMIT.value
                pack_name = Path(t.source).stem
                description = (summary.description if isinstance(summary.description, str) else "")[:120]
                manifest = emitter.manifest(pack_name, description)
                entries["manifest.json"] = _json_bytes(manifest)

                # 封面：Java pack.png → 基岩版 pack_icon.png，纯字节重命名（规则包可配）。
                # 不解码 / 不重编码，避免破坏 gAMA、调色板、APNG 等 PNG 附加块；
                # 输入包只读、绝不原地改写（PRD 保留原始文件不覆盖）。
                icon = resolve_pack_icon(
                    reader.list_files(), reader.read,
                    self.rules["bundle"].get("icon_rename"),
                )
                if icon:
                    icon_name, icon_bytes, icon_src = icon
                    entries[icon_name] = icon_bytes
                    self._log(t, "INFO",
                              f"S7 封面重命名 {icon_src} → {icon_name}"
                              f"（{len(icon_bytes)} bytes 原样搬运，未重编码）")
                else:
                    self._log(t, "WARN", "S7 未找到有效 pack.png 封面，基岩版将回退默认图标")

                # Bedrock 原版「路径覆盖」：先按传统短名补齐实体贴图与纹理集，
                # 再做图集键双注册，两条映射通路同时命中。
                terrain_entries, terrain_made = _materialize_bedrock_aliases(
                    entries, atlas_blocks, self.rules, texture_sets, categories, channels)
                item_entries, item_made = _materialize_bedrock_aliases(
                    entries, atlas_items, self.rules, texture_sets, categories, channels)
                terrain_entries, terrain_aliases = _expand_atlas_aliases(terrain_entries, self.rules)
                item_entries, item_aliases = _expand_atlas_aliases(item_entries, self.rules)

                # S7.5 CTM 变体：把主短名的图集条目改写为加权随机 variations 数组。
                # 主贴图（variations[0]）与每个编号变体各自有独立 texture_set，引擎按
                # weight 随机挑选；多余散落的变体 atlas 键在此折叠删除，避免图集出现
                # 无主短名。注意 MCPE-126617：变体引用的 normal/MER 在当前引擎可能不生效。
                if ctm_variations:
                    folded = 0
                    for short, var_list in ctm_variations.items():
                        for entries_map in (terrain_entries, item_entries):
                            if short not in entries_map:
                                continue
                            main_path = entries_map[short]
                            variations = [{"path": main_path, "weight": max(1, int(var_list[0][1]))}]
                            for vkey, w in var_list[1:]:
                                vp = entries_map.pop(vkey, None)
                                if vp:
                                    variations.append({"path": vp, "weight": max(1, int(w))})
                            entries_map[short] = {"variations": variations}
                            folded += 1
                            break
                    if folded:
                        self._log(t, "INFO",
                                  f"S7.5 CTM variations：{folded} 个短名改写为加权随机变体")

                if terrain_entries:
                    entries["textures/terrain_texture.json"] = _json_bytes(
                        emitter.atlas(terrain_entries, "terrain")
                    )
                if item_entries:
                    entries["textures/item_texture.json"] = _json_bytes(
                        emitter.atlas(item_entries, "items")
                    )
                total_alias = terrain_made + item_made + terrain_aliases + item_aliases
                if total_alias:
                    self._log(t, "INFO",
                              f"S7 别名实体化 {terrain_made + item_made} 组 / 图集键 +{terrain_aliases + item_aliases} 条"
                              f"（terrain {terrain_made}/{terrain_aliases} · items {item_made}/{item_aliases}）")
                self._log(t, "INFO", f"S7 生成 manifest / {len(texture_sets)} 个 texture_set / 图集索引")

                # S8 校验 + 打包
                t.stage = Stage.S8_VALIDATE.value
                checks = Validator(self.rules).validate(
                    entries, manifest, texture_sets, categories, channels, flipbook
                )
                report = ConversionReport(
                    generated_at=CLOCK.fmt(), rules_version=t.rules_ver,
                    engine_version=".".join(str(x) for x in self.rules["bundle"]["engine_min_version"]),
                    preset=t.preset_name,
                    strategy={"bump": t.opts.bump, "packed": t.opts.packed,
                              "profile": t.opts.profile, "ao_bake": str(t.opts.ao_bake)},
                    mapped=report_mapped, unmapped=report_unmapped,
                    skipped=skipped, downgraded=downgraded,
                    failed=[c.detail for c in checks if not c.ok],
                    ai_actions=ai_actions, checks=checks,
                )
                if not report.passed:
                    t.error_code = "E_VALIDATION"
                    t.error = ERROR_TEXT["E_VALIDATION"]
                    t.state = TaskState.FAILED
                    t.report = {**report.to_dict(), "checks": [vars(c) for c in checks]}
                    self._log(t, "ERROR", "S8 校验未通过，禁止导出：" +
                              ", ".join(c.item for c in checks if not c.ok))
                    self.history.finish(t)
                    return

                export_start = CLOCK.mono()
                out = PackWriter.write(entries, OUTPUT, pack_name)
                if timeouts.expired("export", export_start):
                    raise TimeoutError("S8 导出打包超时")
                t.output_path = str(out)
                report.duration_ms = int(t.timer.elapsed() * 1000)
                report.output_size = out.stat().st_size
                t.report = {**report.to_dict(), "checks": [vars(c) for c in checks]}
                t.progress, t.eta_s, t.state = 1.0, 0.0, TaskState.SUCCESS
                t.current_file = ""
                self._log(t, "INFO", f"S8 校验 12/12 通过，导出 {out.name} "
                                     f"({report.output_size / 1024:.1f} KB)，总耗时 {t.timer.elapsed_text()}")
                self.history.finish(t)
            finally:
                reader.close()

        except TimeoutError as exc:
            self._fail_timeout(t, str(exc))
        except ArchiveError as exc:
            self._fail(t, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证不静默卡死
            self._log(t, "ERROR", "未预期异常：" + traceback.format_exc(limit=3))
            self._fail(t, "E_UNKNOWN", str(exc))

    # ---- 辅助 ----
    def _save_checkpoint(self, t: Task, done, channels, texture_sets, categories,
                         atlas_blocks, atlas_items, downgraded, skipped, ai_actions) -> None:
        t.checkpoint = {
            "done": sorted(done), "channels": channels, "texture_sets": texture_sets,
            "categories": categories, "atlas_blocks": atlas_blocks, "atlas_items": atlas_items,
            "downgraded": downgraded, "skipped": skipped, "ai_actions": ai_actions,
            "saved_at": CLOCK.fmt(),
        }

    def _fail_timeout(self, t: Task, msg: str) -> None:
        t.state = TaskState.TIMEOUT
        t.error_code = "E_TIMEOUT"
        t.error = ERROR_TEXT["E_TIMEOUT"]
        self._log(t, "ERROR", f"超时自动终止：{msg}；已保存进度 {t.progress:.1%}，可续跑")
        self.history.finish(t)

    def _fail(self, t: Task, code: str, msg: str) -> None:
        t.state = TaskState.FAILED
        t.error_code = code
        t.error = ERROR_TEXT.get(code, ERROR_TEXT["E_UNKNOWN"]) + f"（{msg}）" if msg else ERROR_TEXT.get(code, "")
        self._log(t, "ERROR", f"任务失败 [{code}] {msg}")
        self.history.finish(t)

    @staticmethod
    def _summary_dict(s: ParseSummary) -> dict[str, Any]:
        return {
            "pack_name": s.pack_name, "pack_format": s.pack_format,
            "description": s.description, "total_images": s.total_images,
            "material_count": s.material_count, "labpbr_groups": s.labpbr_groups,
            "animation_count": s.animation_count, "categories": s.categories,
            "skipped_images": s.skipped_images, "skipped_dirs": s.skipped_dirs,
            "warnings": s.warnings, "shaderpack": s.shaderpack,
        }

    def snapshot(self, tid: str, include_previews: bool = False) -> dict[str, Any]:
        t = self.tasks.get(tid)
        if not t:
            return {"error": "任务不存在"}
        snap = {
            "id": t.id, "state": t.state.value, "stage": t.stage,
            "progress": round(t.progress, 4), "current_file": t.current_file,
            "elapsed_s": round(t.timer.elapsed(), 1), "elapsed_text": t.timer.elapsed_text(),
            "eta_s": round(t.eta_s, 1), "eta_text": hms(t.eta_s),
            "started_at": t.timer.started_at(),
            "ended_at": CLOCK.fmt() if t.state in (TaskState.SUCCESS, TaskState.FAILED,
                                                   TaskState.TIMEOUT, TaskState.CANCELLED,
                                                   TaskState.BLOCKED) else "",
            "logs_tail": t.logs[-60:], "log_count": len(t.logs),
            "summary": t.summary, "report": t.report,
            "output_name": Path(t.output_path).name if t.output_path else "",
            "error": t.error, "error_code": t.error_code,
            "heartbeat_ok": (CLOCK.mono() - t.heartbeat) < 30 if t.heartbeat else True,
            "checkpoint_saved_at": t.checkpoint.get("saved_at", ""),
            "preview_count": len(t.previews),
            "previews": t.previews,
        }
        if not include_previews:
            snap.pop("previews", None)
        return snap

    def bootstrap(self) -> dict[str, Any]:
        b = self.rules["bundle"]
        return {
            "app": "MC Java -> Bedrock Vibrant Visuals 材质转换工具",
            "version": "1.0.0",
            "rules_version": b["rules_version"],
            "engine_min_version": b["engine_min_version"],
            "capabilities": b["capability_whitelist"],
            "constraints": b["constraints"],
            "sources": b["sources"],
            "presets": self.presets.list(),
            "history_count": self.history.count(),
            "clock": self.time_guard.check(),
            "timeouts": TimeoutGuard({
                "parse": 30, "image": 15, "anim": 20, "export": 120, "task": 1800
            }).describe(),
            "server_time": CLOCK.fmt(),
            "note": "核心转换链路完全离线运行；AI 补全为本地启发式算法，不联网。",
        }


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def _expand_atlas_aliases(entries: dict[str, str], rules: dict[str, Any]) -> tuple[dict[str, str], int]:
    """S7 图集别名双注册。

    现代 Bedrock 的 blocks.json 多用 Java 风格 ID 查询 atlas 键（如 oak_log、
    acacia_planks），而原版 textures/blocks 又沿用 planks_oak / log_oak 这类
    传统短名；两种命名在不同引擎版本与方块上都可能出现。同一张贴图同时登记
    等价组内的所有名字、指向同一路径，命中率最高，且不会增加图集实际贴图数。

    - 组内恰好命中 1 个真实键才补别名，避免同组两张不同贴图互相顶替。
    - setdefault 语义，绝不覆盖任何真实注册（防冲突）。
    """
    cfg = rules["id_map"].get("atlas_alias_groups", {})
    groups = cfg.get("groups", []) if isinstance(cfg, dict) else []
    out = dict(entries)
    added = 0
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        hits = [k for k in group if k in entries]
        if len(hits) != 1:
            continue
        path = entries[hits[0]]
        for k in group:
            if k in out:
                continue
            out[k] = path
            added += 1
    return out, added


def _materialize_bedrock_aliases(
    entries: dict[str, bytes],
    atlas: dict[str, str],
    rules: dict[str, Any],
    texture_sets: dict[str, dict[str, Any]],
    categories: dict[str, str],
    channels: dict[str, int],
) -> tuple[dict[str, str], int]:
    """把等价组内的别名落实为真实文件（Bedrock 原版「路径覆盖」机制）。

    官方 Overwriting Vanilla Assets 明确覆盖原版贴图有两条独立通路：
      1) Path    —— 把自定义图片放在原版同名路径（textures/blocks/log_oak.png）；
      2) Catalog —— 在 terrain_texture.json 登记短名到图片路径的映射。
    现代引擎不少木方块走通路 1。此前只做了「图集键双注册」，产物里
    planks_oak.png 存在而 log_oak.png 缺失，于是木板生效、原木回退原版，
    表现为「木头转了却不生效」。这里按 Bedrock 传统短名补齐整套实体文件
    （color / normal / mer / heightmap + texture_set.json），并让别名键指向
    自己的路径，使两条通路同时命中。

    - 仅当组内恰好命中 1 个真实注册时才复制，避免两张不同贴图互相顶替。
    - uniform 数组层（纯常量 MER）原样继承，不产出纯色 PNG。
    - 已存在同名实体文件时不覆盖，保护真实注册。
    """
    if not rules["bundle"].get("materialize_bedrock_aliases", True):
        return dict(atlas), 0
    cfg = rules["id_map"].get("atlas_alias_groups", {})
    groups = cfg.get("groups", []) if isinstance(cfg, dict) else []
    out = dict(atlas)
    made = 0
    for group in groups:
        if not isinstance(group, list) or len(group) < 2:
            continue
        hits = [k for k in group if k in atlas]
        if len(hits) != 1:
            continue
        real = hits[0]
        real_path = atlas[real]
        folder = real_path.rsplit("/", 1)[0]
        ts_path = real_path + ".texture_set.json"
        ts = texture_sets.get(ts_path)
        if not ts:
            continue
        layers = ts.get("minecraft:texture_set", {})
        for alias in group:
            if alias == real or alias in atlas:
                continue
            alias_path = f"{folder}/{alias}"
            if f"{alias_path}.png" in entries or f"{alias_path}.texture_set.json" in entries:
                continue
            new_layers: dict[str, Any] = {}
            ok = True
            for layer, ref in layers.items():
                if not isinstance(ref, str):
                    new_layers[layer] = ref  # uniform 数组值原样继承
                    continue
                if ref == real:
                    src, suffix = f"{folder}/{ref}.png", ""
                elif ref.startswith(real + "_"):
                    src, suffix = f"{folder}/{ref}.png", ref[len(real):]
                else:
                    ok = False
                    break
                if src not in entries:
                    ok = False
                    break
                dst = f"{folder}/{alias}{suffix}.png"
                entries[dst] = entries[src]
                channels[dst] = channels.get(src, 4)
                new_layers[layer] = f"{alias}{suffix}"
            if not ok:
                continue
            alias_ts_path = alias_path + ".texture_set.json"
            new_ts = {
                "format_version": ts.get("format_version", "1.21.30"),
                "minecraft:texture_set": new_layers,
            }
            entries[alias_ts_path] = _json_bytes(new_ts)
            texture_sets[alias_ts_path] = new_ts
            categories[alias_ts_path] = categories.get(ts_path, "blocks")
            out[alias] = alias_path
            made += 1
    return out, made


MANAGER = TaskManager()
