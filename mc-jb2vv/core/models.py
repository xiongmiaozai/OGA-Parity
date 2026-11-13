"""数据模型与状态枚举（PRD 6.7）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Java pack.mcmeta 的 description 常是「文本组件」（dict/list，含 text/color/bold
# 与 § 格式码），而基岩版 manifest.json 的 name/description 只接受纯文本或本地化键。
# 直接 str(dict) 会把 Python 字面量、单引号与 § 方块显示进游戏 UI，即「封面乱码」。
_SECTION_SIGN = re.compile(r"§[0-9A-Fa-fK-Ok-oRr]")
_CTRL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def flatten_text_component(value: Any) -> str:
    """把 Java 文本组件递归摊平成纯文本，丢弃颜色/粗体等样式。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "".join(flatten_text_component(v) for v in value)
    if isinstance(value, dict):
        parts = [value[k] for k in ("text", "translate", "keybind")
                 if isinstance(value.get(k), str)]
        for key in ("with", "extra"):
            sub = value.get(key)
            if sub is not None:
                parts.append(flatten_text_component(sub))
        return "".join(parts)
    return ""


def safe_text(value: Any, limit: int = 120) -> str:
    """manifest 字符串兜底：摊平组件 → 去 § 码 → 去控制字符 → 折叠空白 → 截断。
    保留 CJK 等非 ASCII 文字，仅剔除会破坏 JSON/UI 的字符。"""
    text = flatten_text_component(value)
    text = _CTRL_CHAR.sub("", _SECTION_SIGN.sub("", text))
    return re.sub(r"\s+", " ", text).strip()[:limit]


class TaskState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    TIMEOUT = "TIMEOUT"
    INTERRUPTED = "INTERRUPTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


class Stage(str, Enum):
    S1_ARCHIVE = "S1 读取归档"
    S2_PARSE = "S2 解析元数据"
    S3_GUARD = "S3 光影包拦截"
    S4_IDMAP = "S4 标识符映射"
    S5_CHANNEL = "S5 通道转换"
    S6_ANIM = "S6 动画迁移"
    S7_EMIT = "S7 生成 JSON"
    S8_VALIDATE = "S8 校验与打包"


@dataclass
class MaterialGroup:
    """一张 basecolor 及其 LabPBR 附属贴图构成的材质组。"""

    key: str                      # Java 相对路径（不含扩展名），如 blocks/stone
    basecolor: str | None         # VFS 内路径
    normal: str | None = None     # _n
    specular: str | None = None   # _s
    mcmeta: str | None = None
    category: str = "blocks"      # blocks / items / entity / particle
    target: str = ""              # Bedrock 短名
    profile: str = "plain"        # labpbr13 / oldlabpbr / plain
    confidence: float = 1.0
    mapped: bool = True
    notes: list[str] = field(default_factory=list)


@dataclass
class ParseSummary:
    pack_name: str = "resourcepack"
    pack_format: int | None = None
    description: str = ""
    total_images: int = 0
    material_count: int = 0
    labpbr_groups: int = 0
    animation_count: int = 0
    skipped_images: int = 0
    skipped_dirs: dict[str, int] = field(default_factory=dict)
    categories: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    shaderpack: dict[str, Any] | None = None
    files: list[str] = field(default_factory=list)


@dataclass
class ConvertOptions:
    """转换预设参数（对应 PRD 6.4 / 5.2 右侧参数面板）。"""

    bump: str = "normal"              # normal / heightmap
    packed: str = "mer"               # mer（V1.0 仅 MER）
    ao_bake: float = 0.0              # 0~1，AO 烘焙进 basecolor 的强度
    max_size: int = 2048              # 重采样上限（mip 友好尺寸）
    resample: bool = True
    flip_normal_y: bool = False       # 非 LabPBR 的 OpenGL 法线需开启
    ai_repair: bool = False           # 本地启发式补全（默认关闭）
    ctm_variants: bool = False         # 是否迁移 OptiFine CTM/random 变体（可选；默认关闭 = 仅主目录全量转换）
    omit_near_flat_normals: bool = False  # 省略「源有 _n 但近平面/低细节」的法线层（消 VV 蓝紫伪影、省体积）；默认 False = 保留所有源法线（雪/冰/石英等都有）
    legacy_compat: bool = False           # 兼容模式：产出不含 pbr/texture_set 的传统资源包，min_engine 降至 1.20.0，供不支持 Vibrant Visuals 的设备/版本/网易版加载；代价是失去法线与 PBR 效果
    profile: str = "labpbr13"         # labpbr13 / oldlabpbr
    # 注：roughness 严格按 MERS 规范存「感知粗糙度」= 1 - 感知平滑度，见
    # core/convert.py::decode_specular。禁止在此叠加任何 gamma/cap 之类的观感校准——
    # 那会偏离官方 Lab2MERS 基准并引入二次平方，正是 VV 反射过曝发白的根因。
    parse_timeout_s: float = 30.0
    image_timeout_s: float = 15.0
    anim_timeout_s: float = 20.0
    export_timeout_s: float = 120.0
    task_timeout_s: float = 1800.0
    threads: int = 0                   # S5 并行 worker 数；0 = 按 CPU 核数自动（上限 8）

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ConvertOptions":
        d = d or {}
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class CheckResult:
    id: str
    item: str
    ok: bool
    detail: str = ""


@dataclass
class ConversionReport:
    generated_at: str = ""
    rules_version: str = ""
    engine_version: str = ""
    preset: str = ""
    strategy: dict[str, str] = field(default_factory=dict)
    mapped: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    downgraded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    ai_actions: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    duration_ms: int = 0
    output_size: int = 0

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "rules_version": self.rules_version,
            "engine_version": self.engine_version,
            "preset": self.preset,
            "strategy": self.strategy,
            "mapped": self.mapped,
            "unmapped": self.unmapped,
            "skipped": self.skipped,
            "downgraded": self.downgraded,
            "failed": self.failed,
            "ai_actions": self.ai_actions,
            "checks": [vars(c) for c in self.checks],
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "output_size": self.output_size,
        }


@dataclass
class TaskSnapshot:
    """API 返回的任务快照。"""

    id: str
    state: str
    stage: str
    progress: float
    current_file: str
    elapsed_s: float
    eta_s: float
    started_at: str
    ended_at: str
    logs_tail: list[str]
    summary: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    output_name: str = ""
    error: str = ""
    heartbeat_ok: bool = True
