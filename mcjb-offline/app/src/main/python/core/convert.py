"""S4~S8：标识符映射、LabPBR 通道转换、动画迁移、JSON 生成、Schema 校验、打包（PRD 6.4 / 8.3）。

核心数值规则（均来自官方 / shaderLABS 规范，见 core/rules/bundle.json）：
  - MERS.B = 感知粗糙度 = 1 - 感知平滑度 = 255 - _s.R（对齐官方 Lab2MERS 工具），
    【禁止】用 (1-R)^2：那是 LabPBR 规范给 shader 内部把感知平滑度转成「线性粗糙度」
    （GGX alpha）的公式，不是贴图存储值。VV 用 Cook-Torrance，内部自行对贴图值平方；
    若把线性值再存进贴图，VV 会二次平方成 (1-s)^4，材质普遍过光滑、反射过曝发白。
  - metalness 由 _s.G 的 F0/金属 ID 阈值化（>=230 为金属，<=229 为电介质）
  - emissive  = _s.A / 254（A=0 不发光、A=254 全发光、A=255 保留值须归零），不是反演
  - normal 的 Z 分量由 sqrt(1 - X^2 - Y^2) 重建，平面基准 (128,128,255)
  - LabPBR 与 Bedrock 同为 DirectX Y- 约定，默认不翻转 G
  - normal/heightmap 互斥、MER/MERS 互斥、heightmap 禁用于 items/entity/particle
  - 封面 pack.png → pack_icon.png 默认只做字节重命名（保护 gAMA/调色板/APNG）；
    仅当图标超大 / 非方形时才缩放重编码，避免移动端解码失败（见 normalize_pack_icon）

参考规范：
  - LabPBR 材质标准 https://docs.minegraph.cn/labpbrmaterialstandard.html
  - MERS 材质标准   https://docs.minegraph.cn/mersmaterialstandard.html
  - 官方 Lab2MERS 转换工具 https://github.com/G2F1SH/Lab2MERS
"""
from __future__ import annotations

import io
import json
import math
import re
import uuid
import zipfile
from functools import reduce
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .archive import ArchiveReader
from .clock import CLOCK
from .models import CheckResult, ConvertOptions, MaterialGroup, safe_text

RULES_DIR = Path(__file__).parent / "rules"


def load_rules() -> dict[str, Any]:
    """加载规则包（可热更新，不随主程序发版）。"""
    return {
        "bundle": json.loads((RULES_DIR / "bundle.json").read_text(encoding="utf-8")),
        "id_map": json.loads((RULES_DIR / "id_map.json").read_text(encoding="utf-8")),
        "schemas": json.loads((RULES_DIR / "schemas.json").read_text(encoding="utf-8")),
    }


# ---------------------------------------------------------------- 通道推断
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def resolve_pack_icon(
    files: list[str], read, cfg: dict[str, Any] | None
) -> tuple[str, bytes, str] | None:
    """S7 封面：按规则包做「纯字节重命名」，不做任何解码 / 重编码。

    Java 资源包图标是**根目录** pack.png；基岩版硬性要求**根目录** pack_icon.png
    且格式必须为 PNG。两者只是文件名不同，字节可直接搬运；若经 PIL 解码再编码，
    会丢失 gAMA / 调色板 / APNG 等附加块甚至改变像素，反而是缺陷来源。
    输入包只读、绝不原地改写（PRD 保留原始文件不覆盖）。

    返回 (目标文件名, 原始字节, 源文件名)；无可用封面或校验不通过时返回 None。
    """
    if not cfg or cfg.get("enabled") is False:
        return None
    target: str = cfg.get("target", "pack_icon.png")
    candidates = {c.lower() for c in cfg.get("candidates", ["pack.png"])}
    want_magic: bool = cfg.get("require_png_magic", True)
    prefer_root: bool = cfg.get("prefer_pack_root", True)

    def base(path: str) -> str:
        return path.rsplit("/", 1)[-1].lower()

    def rank(path: str) -> tuple[int, int]:
        depth = path.count("/")
        return (0 if depth == 0 else 1, depth) if prefer_root else (0, depth)

    # 源包已带目标名（混合包 / 半转换包）→ 原样保留，优先级最高，避免重复条目
    existing = sorted((f for f in files if base(f) == target.lower()), key=rank)
    hits = sorted((f for f in files if base(f) in candidates), key=rank)
    # 按优先级逐个尝试：已有目标名损坏时自动回退到 Java 的 pack.png
    for src in existing + hits:
        try:
            raw: bytes = read(src)
        except Exception:
            continue
        if not raw or (want_magic and not raw.startswith(PNG_MAGIC)):
            continue
        return target, raw, src
    return None


def normalize_pack_icon(raw: bytes, max_side: int = 256,
                        max_bytes: int = 1024 * 1024) -> tuple[bytes, str]:
    """把「超大 / 非方形」的封面图标缩放为 2 的幂方形 RGBA PNG。

    Bedrock 在导入/加载资源包时会先解码 pack_icon.png。部分 Java 材质包把
    宣传大图（如 1328×1328、1.7MB）当作 pack.png，移动端解码常直接导致整包
    「加载失败」。贴图必须保持字节原样（保护 PBR 通道），但图标与 PBR 无关，
    可在必要时重编码。

    判据刻意保守：**只有超过 max_side 或超过 max_bytes 或非正方形才重编码**。
    2 的幂对图标并非硬性要求（官方小图标也常见 48×48 等），且重编码会破坏
    gAMA / 调色板 / APNG 等附加块——因此合规小图标一律原样返回，不改变任何字节。
    """
    if not raw.startswith(PNG_MAGIC) or len(raw) < 24:
        return raw, "非 PNG 或过短，保持原样"
    w = int.from_bytes(raw[16:20], "big")
    h = int.from_bytes(raw[20:24], "big")

    if w == h and w <= max_side and len(raw) <= max_bytes:
        return raw, f"{w}x{h} / {len(raw)//1024}KB 已合规，原样搬运"
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        side = max_side
        im = im.convert("RGBA").resize((side, side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out = buf.getvalue()
        return out, f"{w}x{h} → {side}x{side}（{len(raw)//1024}KB → {len(out)//1024}KB）"
    except Exception:
        return raw, "图标重编码失败，保持原样"


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[:, :, 0] * 0.2126 + rgb[:, :, 1] * 0.7152 + rgb[:, :, 2] * 0.0722


def _edge(gray: np.ndarray) -> np.ndarray:
    """邻域梯度近似局部细节，用于让推断出的粗糙度具有像素级变化。"""
    gx = np.abs(np.roll(gray, 1, axis=1) - np.roll(gray, -1, axis=1))
    gy = np.abs(np.roll(gray, 1, axis=0) - np.roll(gray, -1, axis=0))
    return np.clip((gx + gy) * 0.5, 0.0, 1.0)


def _estimate_normal(color: np.ndarray) -> np.ndarray:
    """无 _n 时推断 LabPBR 占位：RG=平面法线、B=AO 无遮蔽、A=由亮度推断的高度。"""
    gray = _luma(color[:, :, :3])
    n = np.zeros((*gray.shape, 4), dtype=np.float32)
    n[:, :, 0] = 0.5
    n[:, :, 1] = 0.5
    n[:, :, 2] = 1.0
    n[:, :, 3] = np.clip(0.35 + 0.60 * gray, 0.0, 1.0)
    return n


def _estimate_spec(color: np.ndarray) -> np.ndarray:
    """无 _s 时推断 LabPBR 占位：由亮度与局部细节估算 smoothness，
    G 置电介质 F0、A=255 表示无自发光。金属度与发光不臆造，恒为 0。"""
    gray = _luma(color[:, :, :3])
    detail = _edge(gray)
    smoothness = np.clip(0.25 + 0.45 * gray - 0.25 * detail, 0.05, 0.92)
    s = np.zeros((*gray.shape, 4), dtype=np.float32)
    s[:, :, 0] = smoothness
    s[:, :, 1] = 10 / 255.0
    s[:, :, 2] = 0.0
    s[:, :, 3] = 1.0
    return s


# ---------------------------------------------------------------- S4 标识符映射
class IdMapper:
    def __init__(self, rules: dict[str, Any]):
        self.dir_map: dict[str, str] = rules["id_map"]["directory_map"]
        self.blocks: dict[str, str] = rules["id_map"]["block_aliases"]
        self.items: dict[str, str] = rules["id_map"]["item_aliases"]

    def map(self, group: MaterialGroup) -> tuple[str, bool]:
        parts = group.key.split("/")
        src_dir = parts[0] if len(parts) > 1 else ""
        name = parts[-1].lower()
        target_dir = self.dir_map.get(src_dir, group.category)
        aliases = self.blocks if group.category == "blocks" else self.items
        if name in aliases:
            return f"{target_dir}/{aliases[name]}", True
        # 未命中：回退原名并计入 unmapped 报告，绝不静默丢弃（FR-B-03 / R-04）
        return f"{target_dir}/{name}", False


# ---------------------------------------------------------------- S5 通道转换
def _decode(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as im:
        return np.array(im.convert("RGBA"), dtype=np.float32) / 255.0


def _png(arr: np.ndarray, mode: str, optimize: bool = False,
         compresslevel: int = 6) -> bytes:
    """PNG 编码。

    默认 optimize=False：纹理数据本身已高熵，optimize=True 的第二遍最优霍夫曼
    搜索在 Android ARM 上极慢（单组编码占整组耗时的大头）却只换来个位数体积收益；
    关闭后编码快 30~50%，产物体积略增，对 .mcpack 大小影响很小。fast_encode 档
    再把 compresslevel 降到 4，进一步压短 GIL 下的编码时间，让多线程更吃得满。
    """
    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode=mode)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=optimize, compresslevel=compresslevel)
    return buf.getvalue()


def _mip_size(n: int, allowed: list[int]) -> int:
    for size in sorted(allowed):
        if n <= size:
            return size
    return allowed[-1]


def _resize(arr: np.ndarray, size: tuple[int, int], fast: bool = False) -> np.ndarray:
    """RGB 用 Lanczos（或极速档 BILINEAR）；alpha 承载 AO/高度/自发光等数据通道，
    改用最近邻。

    若对 alpha 做 Lanczos，LabPBR 的 A=255（无自发光保留值）在边界会被插值成
    254.x，被解码误判为「接近全发光」；同时 color 的透明边缘也不会出现半透明晕边。
    fast=True（性能/极速档）时 RGB 改用 BILINEAR：缩放大图时几乎无肉眼差异，
    但滤镜开销约为 Lanczos 的 1/3，配合编码去 optimize 能明显压短单组 GIL 时间。
    """
    img = Image.fromarray(np.clip(arr * 255, 0, 255).astype(np.uint8), mode="RGBA")
    rgb_filter = Image.Resampling.BILINEAR if fast else Image.Resampling.LANCZOS
    rgb = img.convert("RGB").resize(size, rgb_filter).convert("RGBA")
    rgb.putalpha(img.getchannel("A").resize(size, Image.Resampling.NEAREST))
    return np.array(rgb, dtype=np.float32) / 255.0


def _default_spec(shape: tuple[int, int]) -> np.ndarray:
    """AI 补全（本地启发式）：无 _s 时给出非金属、无自发光、中等粗糙度常量图。"""
    s = np.zeros((shape[0], shape[1], 4), dtype=np.float32)
    s[:, :, 0] = 0.20                    # 感知平滑度 -> MERS.B = 1 - 0.20 = 0.80
    s[:, :, 1] = 10 / 255.0             # F0 电介质
    s[:, :, 2] = 0.0                    # porosity
    s[:, :, 3] = 1.0                    # LabPBR: A=255 表示无发光
    return s


def _default_normal(shape: tuple[int, int]) -> np.ndarray:
    """AI 补全：平面法线 (128,128,255) + 高度 255。"""
    n = np.zeros((shape[0], shape[1], 4), dtype=np.float32)
    n[:, :, 0] = 0.5
    n[:, :, 1] = 0.5
    n[:, :, 2] = 1.0                    # AO = 无遮蔽
    n[:, :, 3] = 1.0                    # height = 表面
    return n


def decode_specular(spec: np.ndarray, profile: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """解码高光贴图，返回 (metalness, emissive, roughness)，各通道为 0..1。

    LabPBR 1.3（https://docs.minegraph.cn/labpbrmaterialstandard.html）：
      R = 「感知」平滑度 (perceptual smoothness)，255=完全光滑、0=完全粗糙。
          规范给出的 `粗糙度 = (1 - 感知平滑度)^2` 是把感知量转成 **线性粗糙度**
          （GGX 的 alpha）供 shader 内部计算用，不是贴图里存的值。
      G = F0(0..229，线性) 或预定义金属 ID(230..255) -> 二值金属度
      A = 自发光，0=0% 发光、254=100% 发光、255 是「RGB 无 alpha 默认值」的保留值，
          必须归零；发光颜色取自 basecolor。
      B = 孔隙率(0..64)/次表面散射(65..255)，V1.0 仅输出 MER 三通道，暂未迁移到 MERS.A。

    MERS（https://docs.minegraph.cn/mersmaterialstandard.html）：
      B = 粗糙度，0=完全光滑、255=完全粗糙，存的是 **感知粗糙度**。因此
          MERS.B = 1 - 感知平滑度 = 255 - _s.R，与官方转换工具 Lab2MERS 的
          `new_b = 255 - r` 完全一致。VV 渲染时内部自行对贴图值做 perceptual->linear
          平方；若这里提前平方成 (1-s)^2，会被二次平方为 (1-s)^4，导致材质过光滑、
          镜面/IBL 高光过曝成一片白——这正是「反射很白很粗糙」的根因。

    OldPBR / SEUS：R = 线性粗糙度（直通，不能再平方反演）、G = 连续金属度、B = 自发光。
    """
    if profile == "oldlabpbr":
        return spec[:, :, 1], spec[:, :, 2], spec[:, :, 0]
    # 感知粗糙度：直接线性取反，绝不做 (1-s)^2（那是 shader 内部的转换）。
    roughness = np.clip(1.0 - spec[:, :, 0], 0.0, 1.0)
    metalness = np.where(spec[:, :, 1] * 255.0 >= 230.0, 1.0, 0.0)
    a_raw = spec[:, :, 3] * 255.0
    emissive = np.where(a_raw >= 254.5, 0.0, np.clip(a_raw / 254.0, 0.0, 1.0))
    return metalness, emissive, roughness


class ChannelConverter:
    def __init__(self, reader: ArchiveReader, opts: ConvertOptions, rules: dict[str, Any]):
        self.reader = reader
        self.opts = opts
        self.rules = rules

    def convert(self, g: MaterialGroup, target: str) -> dict[str, Any]:
        """返回该材质生成的全部 PNG 层、通道数、预览与备注。"""
        notes: list[str] = []
        ai: list[str] = []
        opts = self.opts
        fast = bool(opts.fast_encode)
        enc_level = 4 if fast else 6
        color = _decode(self.reader.read(g.basecolor))
        h, w = color.shape[:2]
        is_animated = g.mcmeta is not None

        # 缺图处理：绝大多数普通 Java 材质包并不含 LabPBR _n/_s。
        # 旧逻辑在 ai_repair=False 时直接跳过 normal/MER，导致 texture_set 只剩 color，
        # Vibrant Visuals 完全拿不到粗糙度与法线数据（用户感知为「工具不支持 PBR」）。
        # 现在始终生成这两层：有原始数据就用原始数据，没有则由 basecolor 推断，
        # 并在报告里明确标注「非原始数据」，绝不冒充原包 PBR。
        if g.normal:
            normal_src = _decode(self.reader.read(g.normal))
        else:
            normal_src = _estimate_normal(color)
            notes.append(f"{target}: 原包无 _n，法线/高度由 basecolor 推断（非原始 PBR 数据）")
            if opts.ai_repair:
                ai.append(f"{target}: 缺失 _n，已补全平面法线与推断高度")

        if g.specular:
            spec_src = _decode(self.reader.read(g.specular))
        else:
            spec_src = _estimate_spec(color)
            notes.append(f"{target}: 原包无 _s，MER 粗糙度由亮度推断、金属度/发光置 0（非原始 PBR 数据）")
            if opts.ai_repair:
                ai.append(f"{target}: 缺失 _s，已补全推断式 MER")

        # 重采样：动画条带保持纵横比，普通贴图对齐 mip 友好尺寸
        if opts.resample:
            allowed = self.rules["bundle"]["mip_friendly_sizes"]
            if is_animated:
                scale = min(1.0, opts.max_size / max(h, w))
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            else:
                # 按「每条边」独立对齐到 mip 友好尺寸，保持原始纵横比。
                # 旧实现 nw = nh = _mip_size(max(h,w)) 会把 64x256 的竖条
                # 硬拉成 256x256 正方形，纹理被压扁/错位，是贴图错乱的放大器。
                nw = _mip_size(min(w, opts.max_size), allowed)
                nh = _mip_size(min(h, opts.max_size), allowed)
            if (nw, nh) != (w, h):
                color = _resize(color, (nw, nh), fast=fast)
                if normal_src is not None:
                    normal_src = _resize(normal_src, (nw, nh), fast=fast)
                if spec_src is not None:
                    spec_src = _resize(spec_src, (nw, nh), fast=fast)
                w, h = nw, nh
                notes.append(f"重采样至 {w}x{h}（mip 友好尺寸）")

        layers: dict[str, tuple[bytes, int]] = {}
        uniforms: dict[str, list[int]] = {}
        cfg = self.rules["bundle"].get("uniform_constant_layers", {})

        # color（唯一必选层，PRD C4）
        if opts.ao_bake > 0 and normal_src is not None:
            ao = normal_src[:, :, 2:3]
            color[:, :, :3] *= (1.0 - opts.ao_bake * (1.0 - ao))
            notes.append(f"AO 烘焙进 basecolor，强度 {opts.ao_bake:.2f}")
        layers["color"] = (_png(color * 255.0, "RGBA", compresslevel=enc_level), 4)

        # normal（重建 Z 通道）
        if normal_src is not None:
            nx = normal_src[:, :, 0] * 2.0 - 1.0
            ny = normal_src[:, :, 1] * 2.0 - 1.0
            if opts.flip_normal_y:
                ny = -ny
                notes.append("已翻转法线 Y（OpenGL -> DirectX）")
            # B 通道重建（参考 normal_normalize 工具）：法线是单位向量，Z 由 X/Y 推导。
            # 若源贴图 XY 模长 > 1（原 _n 的 B 通道缺失/被截断的常见情形），先归一化
            # XY 再算 Z，避免 zSq<0 被钳成 0 而产出「变平/蓝紫」的退化法线（VV 伪影源）。
            len2 = nx * nx + ny * ny
            over = len2 > 1.0
            if np.any(over):
                inv = np.where(over, 1.0 / np.sqrt(len2), 1.0)
                nx = nx * inv
                ny = ny * inv
            nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
            # B 通道按切线空间法线标准编码为 (Z+1)/2*255（平面法线 nz=1 -> B=255）。
            # 注意：绝不能直接存 nz*255，否则倾斜法线的 B 被压到 128 附近，VV 解码回的
            # Z 严重偏小、法线方向整体失真（表现为「没有法线效果」/ 异常蓝染）。
            n_rgb = np.stack([(nx + 1.0) * 0.5, (ny + 1.0) * 0.5, (nz + 1.0) * 0.5], axis=-1) * 255.0
            # 倾斜角（偏离平面法线的幅度）：用于判定法线是否近平面/低细节。
            # 取归一化后的实际输出法线，保证与产物一致。
            tilt = np.sqrt(nx * nx + ny * ny)
            mean_tilt = float(tilt.mean())
            # 复合判据（v2026.11.13-1）：旧逻辑只看「均值」倾斜角，会被
            # 「大面积平坦 + 局部有细节」的贴图稀释——木板、树叶、原木顶等
            # 均值很低（<=0.28）但纹理边缘/缝隙处法线很陡，整张被误判省略，
            # 表现为「转换后画面很平、没有凹凸」。实测 Patrix 包被省略的 777 张
            # 里 79.4% 局部 p95 倾斜 >0.45，属于误杀。
            # 现在要求三者同时成立才认定是会产生 VV 蓝紫伪影的真·平面法线：
            #   ① 整体平坦 mean_tilt <= normal_flat_tilt_max
            #   ② 高分位也低 p95_tilt <= normal_flat_p95_max（局部无强细节）
            #   ③ 强细节像素占比 detail_frac <= normal_detail_frac_max
            p95_tilt = float(np.percentile(tilt, 95))
            detail_thr = float(cfg.get("normal_detail_tilt", 0.30))
            detail_frac = float((tilt > detail_thr).mean())
            flat_mean_max = float(cfg.get("normal_flat_tilt_max", 0.28))
            flat_p95_max = float(cfg.get("normal_flat_p95_max", 0.35))
            flat_detail_frac_max = float(cfg.get("normal_detail_frac_max", 0.02))
            near_flat_real = (
                g.normal is not None
                and cfg.get("enabled")
                and cfg.get("omit_near_flat_real_normal")
                and opts.omit_near_flat_normals
                and mean_tilt <= flat_mean_max
                and p95_tilt <= flat_p95_max
                and detail_frac <= flat_detail_frac_max
            )
            if near_flat_real:
                # 原包确实给了 _n，但整张都是低倾斜的近平面/低细节法线：在 Vibrant
                # Visuals 下这类法线会产生蓝紫伪影（与纯平面推断法线同类问题），
                # 提供的立体感可忽略，省略后回退 vanilla 平面法线，消除蓝染。
                notes.append(
                    f"{target}: 原包含 _n 但为近平面/低细节法线"
                    f"（均值倾斜角 {mean_tilt:.2f}、p95 {p95_tilt:.2f}、"
                    f"强细节像素占比 {detail_frac:.1%}），"
                    "省略法线层（回退 vanilla，消除 VV 蓝紫伪影）"
                )
            elif g.normal is None and cfg.get("enabled") and cfg.get("omit_flat_inferred_normal"):
                # 原包无 _n 时推断出的是 (128,128,255) 纯平面常量层。官方 normal 层
                # 只接受纹理短名、不支持 uniform 数组，且缺失层会回退 vanilla 法线；
                # 与其产出一张整张蓝紫、零细节的 PNG（最容易被误认为贴图损坏），
                # 不如直接省略该层。原包真的给了 _n 的，即使是平面也原样保留。
                notes.append(f"{target}: 原包无 _n，省略推断的纯平面法线层（回退 vanilla，不产出蓝紫占位 PNG）")
            else:
                layers["normal"] = (_png(n_rgb, "RGB", compresslevel=enc_level), 3)
                layers["heightmap"] = (_png(normal_src[:, :, 3] * 255.0, "L",
                                            compresslevel=enc_level), 1)

        # MER 打包：R=金属度 / G=自发光 / B=粗糙度（MERS 官方定义）
        if spec_src is not None:
            metalness, emissive, roughness = decode_specular(spec_src, opts.profile)
            # roughness 已是「感知粗糙度」(1 - 感知平滑度)，与官方 Lab2MERS 的
            # `new_b = 255 - r` 一致，直接写入 B 通道，不再做任何二次变换。
            mer = np.stack([metalness, emissive, roughness], axis=-1) * 255.0
            flat = mer.reshape(-1, 3)
            mean_rgb = flat.mean(axis=0)
            # 只处理「无金属、无发光」的纯蓝常量层（R/G≈0，B=粗糙度），
            # 这正是整张纯蓝、零细节、被误认为贴图损坏的那一类。
            # 含金属(R)或发光(G)的常量层仍是有效 PBR 数据，保留 PNG 以便逐像素校验。
            const_layer = (cfg.get("enabled")
                           and float(flat.std(axis=0).max()) <= float(cfg.get("mer_std_threshold", 32))
                           and float(mean_rgb[0]) <= float(cfg.get("mer_neutral_max", 8))
                           and float(mean_rgb[1]) <= float(cfg.get("mer_neutral_max", 8)))
            if const_layer:
                # 整张层近似恒定。官方 "Specifying uniform values" 允许把层直接写成
                # [R,G,B] 数组，等价于引用一张纯色图，却不再产出 (0,0,222) 这类
                # 整张纯蓝、零纹理细节的 PNG（用户最容易误认为「贴图变蓝了」）。
                uniforms["metalness_emissive_roughness"] = [
                    int(round(min(255.0, max(0.0, v)))) for v in mean_rgb
                ]
                notes.append(
                    f"MER 为常量层 {uniforms['metalness_emissive_roughness']}，"
                    "按官方 uniform 值写入 texture_set（不产出纯色 PNG）"
                )
            else:
                layers["mer"] = (_png(mer, "RGB", compresslevel=enc_level), 3)
                notes.append(
                    f"MER 打包：金属像素 {int((metalness > 0).sum())}，"
                    f"粗糙度均值 {roughness.mean():.2f}，自发光均值 {emissive.mean():.2f}"
                )

        return {
            "target": target,
            "category": g.category,
            "width": w,
            "height": h,
            "layers": layers,
            "uniforms": uniforms,
            "notes": notes,
            "ai_actions": ai,
            "animated": is_animated,
        }


# ---------------------------------------------------------------- S6 动画迁移
class AnimMigrator:
    """Java *.png.mcmeta -> Bedrock flipbook_textures.json（PRD 6.4.7）。"""

    def __init__(self, rules: dict[str, Any]):
        self.schema = rules["schemas"]["flipbook_textures"]
        self.max_frames = int(self.schema["max_frames"])

    def migrate(self, animations: list[dict[str, Any]], targets: dict[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
        entries: list[dict[str, Any]] = []
        downgraded: list[str] = []
        for a in animations:
            if not a.get("exists"):
                downgraded.append(f"{a['mcmeta']}：找不到对应贴图，已跳过")
                continue
            cfg = a["config"] or {}
            raw_frames = cfg.get("frames")
            frametime = int(cfg.get("frametime", 1) or 1)
            interpolate = bool(cfg.get("interpolate", False))
            target = targets.get(a["group"]) or a["group"]
            ticks = frametime

            if raw_frames is None:
                width, height = int(cfg.get("width", 16) or 16), int(cfg.get("height", 16) or 16)
                if width and height and height > width:
                    count = max(1, height // width)
                    indices = list(range(count))
                    downgraded.append(f"{a['mcmeta']}：未声明 frames，按条带比例推断 {count} 帧")
                else:
                    indices = [0]
                    downgraded.append(f"{a['mcmeta']}：未声明 frames，按单帧处理")
            elif all(isinstance(f, int) for f in raw_frames):
                indices = list(raw_frames)
            else:
                # 自定义逐帧时长：按 gcd 展开为等间隔帧索引序列
                times: list[int] = []
                for f in raw_frames:
                    times.append(int(f.get("time", frametime)) if isinstance(f, dict) else frametime)
                base = reduce(math.gcd, [t for t in times if t > 0] or [1])
                ticks = base
                expanded: list[int] = []
                for i, f in enumerate(raw_frames):
                    idx = int(f.get("index", i)) if isinstance(f, dict) else int(f)
                    repeat = max(1, times[i] // base)
                    expanded.extend([idx] * repeat)
                indices = expanded
                downgraded.append(
                    f"{a['mcmeta']}：自定义帧时长已按 gcd={base} 展开为 {len(expanded)} 帧等间隔序列，"
                    "需在预览中逐帧核验（Java 与 Bedrock 动画语义不等价）"
                )

            if len(indices) > self.max_frames:
                downgraded.append(f"{a['mcmeta']}：帧数 {len(indices)} 超过上限 {self.max_frames}，已截断")
                indices = indices[: self.max_frames]

            entry = {
                "flipbook_texture": f"textures/{target}",
                "frames": indices,
                "ticks_per_frame": max(1, ticks),
                "blend_frames": interpolate,
            }
            allowed = set(self.schema["entry_keys_allowed"])
            entry = {k: v for k, v in entry.items() if k in allowed}
            entries.append(entry)
            if interpolate:
                downgraded.append(f"{a['mcmeta']}：interpolate 已映射为 blend_frames，VV 下插值表现需目视校验")

        if not entries:
            return None, downgraded
        return {"num_mip_levels": 0, "padding": 0, "flipbook_textures": entries}, downgraded


# ---------------------------------------------------------------- S7 JSON 生成
class JsonEmitter:
    def __init__(self, rules: dict[str, Any]):
        self.bundle = rules["bundle"]
        self.schemas = rules["schemas"]

    def manifest(self, pack_name: str, description: str) -> dict[str, Any]:
        # format_version 必须为规则包中的 2：v1 只允许 min_engine_version <= 1.12.0，
        # 而 Vibrant Visuals 要求 1.21.120，写成 1 会让游戏在导入阶段直接拒绝整个包。
        clean = safe_text(pack_name, 64) or "Converted Pack"
        desc = safe_text(description, 120) or (
            f"由 MC Java→Bedrock VV 转换工具生成 · {CLOCK.fmt()}"
        )
        return {
            "format_version": int(self.bundle["manifest_format_version"]),
            "header": {
                "name": clean,
                "description": desc,
                "uuid": str(uuid.uuid4()),
                "version": [1, 0, 0],
                "min_engine_version": list(self.bundle["engine_min_version"]),
            },
            "modules": [
                {"type": "resources", "uuid": str(uuid.uuid4()), "version": [1, 0, 0]}
            ],
            "capabilities": list(self.bundle["capability_whitelist"]),  # 仅 ["pbr"]
        }

    def texture_set(self, stem: str, available: set[str], category: str,
                    opts: ConvertOptions, uniforms: dict[str, list[int]] | None = None
                    ) -> tuple[dict[str, Any], list[str]]:
        """按官方互斥约束生成 Texture Set（PRD 6.4.2）。

        uniforms 中的层改写为官方 uniform 数组值，不引用 PNG。
        """
        notes: list[str] = []
        forbidden = set(self.bundle["constraints"]["heightmap_forbidden_categories"])
        bump = opts.bump
        if bump == "heightmap" and category in forbidden:
            bump = "normal"
            notes.append(f"{stem}：heightmap 不可用于 {category}，已强制改用 normal（官方约束 C5）")
        if bump not in ("normal", "heightmap"):
            bump = "normal"

        obj: dict[str, Any] = {"color": stem}
        if bump == "heightmap" and "heightmap" in available:
            obj["heightmap"] = f"{stem}_heightmap"
        elif "normal" in available:
            obj["normal"] = f"{stem}_normal"
        else:
            notes.append(f"{stem}：无法线/高度数据，texture_set 仅含 color 层")

        if opts.packed == "mer":
            if uniforms and "metalness_emissive_roughness" in uniforms:
                obj["metalness_emissive_roughness"] = uniforms["metalness_emissive_roughness"]
                notes.append(f"{stem}：MER 为常量层，改用官方 uniform 数组值（不产出纯色 PNG）")
            elif "mer" in available:
                obj["metalness_emissive_roughness"] = f"{stem}_mer"
        return {
            "format_version": self.bundle["texture_set_format_version"],
            "minecraft:texture_set": obj,
        }, notes

    def atlas(self, entries: dict[str, str], kind: str) -> dict[str, Any]:
        meta = self.schemas["atlases"][kind]
        obj: dict[str, Any] = {
            "resource_pack_name": "vanilla",
            "texture_name": meta["texture_name"],
        }
        # terrain_texture.json 属官方 catalog 文件，vanilla 与教程示例均带
        # padding / num_mip_levels。缺失时部分引擎版本不构建图集，贴图整体
        # 回退原版（表现为「转了却不生效」）。item_texture 不需要这两个字段。
        if kind == "terrain":
            obj["padding"] = int(self.bundle.get("atlas_padding", 8))
            obj["num_mip_levels"] = int(self.bundle.get("atlas_num_mip_levels", 4))
        td: dict[str, Any] = {}
        for short, path in entries.items():
            if isinstance(path, dict) and "variations" in path:
                # CTM 加权随机变体：path 为 {"variations": [{"path":..,"weight":..}]}
                td[short] = {
                    "textures": {
                        "variations": [
                            {"path": v["path"], "weight": int(v.get("weight", 1))}
                            for v in path["variations"]
                        ]
                    }
                }
            else:
                td[short] = {"textures": path}
        obj["texture_data"] = td
        return obj


# ---------------------------------------------------------------- S8 校验
class Validator:
    """导出前 8.3 合规清单，P0 门禁。"""

    def __init__(self, rules: dict[str, Any]):
        self.bundle = rules["bundle"]
        self.schema = rules["schemas"]

    def validate(
        self,
        entries: dict[str, bytes],
        manifest: dict[str, Any],
        texture_sets: dict[str, dict[str, Any]],
        categories: dict[str, str],
        channels: dict[str, int],
        flipbook: dict[str, Any] | None,
    ) -> list[CheckResult]:
        checks: list[CheckResult] = []

        def add(cid: str, item: str, ok: bool, detail: str = "") -> None:
            checks.append(CheckResult(cid, item, bool(ok), detail))

        add("C01", "manifest.json 位于归档根", "manifest.json" in entries)
        add("C02", "min_engine_version >= [1,21,120]",
            manifest["header"]["min_engine_version"] >= self.bundle["engine_min_version"],
            str(manifest["header"]["min_engine_version"]))
        caps = manifest.get("capabilities", [])
        add("C03", 'capabilities 恰为 ["pbr"]，无 raytraced/raytracing',
            caps == ["pbr"] and not (set(caps) & set(self.bundle["capability_blacklist"])), str(caps))
        hu, mu = manifest["header"]["uuid"], manifest["modules"][0]["uuid"]
        try:
            v4 = uuid.UUID(hu).version == 4 and uuid.UUID(mu).version == 4 and hu != mu
        except ValueError:
            v4 = False
        add("C04", "header/module UUID 为合法 v4 且互不相同", v4, f"{hu[:8]}… / {mu[:8]}…")

        ts_schema = self.schema["texture_set"]
        exclusive = ts_schema["mutually_exclusive_groups"]
        forbidden = set(self.bundle["constraints"]["heightmap_forbidden_categories"])
        ref_ok = True
        excl_ok = True
        cat_ok = True
        chan_ok = True
        path_ok = True
        color_ok = True
        for path, ts in texture_sets.items():
            layers = ts.get("minecraft:texture_set", {})
            if "color" not in layers:
                color_ok = False
            for group in exclusive:
                if all(k in layers for k in group):
                    excl_ok = False
            cat = categories.get(path, "blocks")
            if "heightmap" in layers and cat in forbidden:
                cat_ok = False
            folder = path.rsplit("/", 1)[0] if "/" in path else ""
            for key, ref in layers.items():
                if isinstance(ref, (list, tuple)):
                    # 官方 uniform 数组值：长度须匹配层通道数，且为 0..255 整数
                    expected = ts_schema["channel_count"].get(key)
                    if expected and len(ref) not in expected:
                        chan_ok = False
                    if any(not isinstance(v, int) or v < 0 or v > 255 for v in ref):
                        chan_ok = False
                    continue
                if not isinstance(ref, str):
                    continue
                if ref.lower() != ref or "/" in ref or "\\" in ref or "." in ref:
                    path_ok = False
                if f"{folder}/{ref}.png" not in entries:
                    ref_ok = False
                expected = ts_schema["channel_count"].get(key)
                actual = channels.get(f"{folder}/{ref}.png")
                if expected and actual is not None and actual not in expected:
                    chan_ok = False

        add("C05", "每个 texture_set 含必选 color 层", color_ok)
        add("C06", "层引用图片存在且位于同一资源包", ref_ok, f"共 {len(texture_sets)} 个 texture_set")
        add("C07", "normal/heightmap 与 MER/MERS 互斥约束", excl_ok)
        add("C08", "items/entity/particle 未使用 heightmap", cat_ok)
        add("C09", "各层 PNG 通道数符合官方 Schema", chan_ok)
        add("C10", "引用为无扩展名、小写、/ 分隔短名", path_ok)

        if flipbook:
            schema = self.schema["flipbook_textures"]
            allowed, required = set(schema["entry_keys_allowed"]), set(schema["entry_keys_required"])
            fb_ok = all(
                required.issubset(e.keys()) and set(e.keys()).issubset(allowed)
                and len(e.get("frames", [])) <= self.max_frames(schema)
                for e in flipbook.get("flipbook_textures", [])
            )
            add("C11", "flipbook_textures.json 符合字段白名单与帧数上限", fb_ok,
                f"{len(flipbook.get('flipbook_textures', []))} 条动画")
        else:
            add("C11", "无动画内容，未生成空 flipbook 文件", True, "按 PRD FR-B-07 处理")

        add("C12", "归档根包含 manifest.json 且为 ZIP 兼容结构", "manifest.json" in entries)

        # 真机导入门禁：format_version=1 时游戏强制 min_engine_version < 1.13.0，
        # 而 Vibrant Visuals 需要 1.21.120，两者冲突会让整个包在导入阶段被拒绝。
        fmt = int(manifest.get("format_version", 1))
        eng = list(manifest["header"]["min_engine_version"])
        mf_schema = self.schema["manifest"]
        need_v2 = list(mf_schema["v2_required_when_engine_at_least"])
        fmt_ok = (fmt >= int(mf_schema["format_version_min"])
                  and (eng < need_v2 or fmt >= 2))
        add("C13", "format_version 与 min_engine_version 兼容（≥1.13.0 必须为 v2）",
            fmt_ok, f"format_version={fmt}, min_engine_version={eng}")
        return checks

    @staticmethod
    def max_frames(schema: dict[str, Any]) -> int:
        return int(schema["max_frames"])


# ---------------------------------------------------------------- S8 打包
class PackWriter:
    @staticmethod
    def write(entries: dict[str, bytes], output_dir: Path, base_name: str) -> Path:
        """导出 .mcpack（ZIP 兼容，manifest.json 位于根，文件名带时间戳防覆盖）。"""
        path = CLOCK.unique_path(Path(output_dir), base_name, "mcpack")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for name in sorted(entries):
                zf.writestr(name, entries[name])
        return path
