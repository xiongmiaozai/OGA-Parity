"""OptiFine CTM / random 变体解析（Java -> Bedrock terrain_texture variations）。

背景
----
Java 材质包把「同一方块的多种外观」放在 ``assets/<ns>/optifine/ctm/`` 与
``assets/<ns>/optifine/random/`` 下，由 ``*.properties`` 描述：

    matchTiles = obsidian
    tiles = 1 <default> 3-16
    method = repeat
    weights = 1 1 1 ...

其中 ``matchTiles`` 是基岩版短名（如 ``obsidian``）或 ``textures/block/obsidian.png``；
``tiles`` 列出变体编号（``<default>`` 表示回退到主贴图）；同目录下 ``1.png`` /
``1_n.png`` / ``1_s.png`` 分别是变体 color / 法线 / 镜面。

基岩版没有「连接纹理 / 按坐标切换」机制，但有 **terrain_texture.json 的加权随机
变体**（``variations`` 数组）。本模块把每个 CTM 规则解析成一组变体，由 pipeline
落盘为独立 texture_set 并注册成 variations。连接型（``method=ctm/connected``）会因
平台限制降级为加权随机，并在日志中标注。

引擎限制（如实告知）：MCPE-126617 至今未修复——variations 引用的 texture_set
**不加载 normal / MER 层**。因此变体的「颜色随机」能生效，但变体法线在当前引擎
可能仍不显示（主贴图 obsidian 的法线不受影响）。
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class CtmVariant:
    """单个变体贴图（已配对 color + 可选 _n/_s）。"""

    index: int                 # 变体编号（来自 tiles 数字）；<default> 用 -1
    color: str                 # VFS 内 color 路径（optifine 目录内）
    normal: str | None = None  # _n
    specular: str | None = None  # _s
    is_default: bool = False   # <default> 占位：复用主贴图，不生成独立文件


@dataclass
class CtmRule:
    """一条 CTM 规则：一个 matchTiles 短名 + 一组变体。"""

    target_short: str          # 基岩短名，如 obsidian
    category: str              # blocks / items（继承自主贴图）
    method: str                # 原始 method（repeat/ctm/horizontal...），仅用于日志
    weights: list[int] | None  # 可选权重，长度须与变体序列匹配
    variants: list[CtmVariant] = field(default_factory=list)


_TILE_TOKEN = re.compile(r"(\d+)(?:-(\d+))?|<default>")
_PROP_RE = re.compile(r"^([A-Za-z_]+)\s*=\s*(.*)$", re.M)
_CTM_PATH_RE = re.compile(r"/optifine/(?:ctm|random)/", re.I)
_PATH_TILE_RE = re.compile(
    r"(?:textures/)?(?:block|blocks|item|items)/([^/]+?)(?:\.png)?$", re.I
)


def _parse_tiles(tiles: str) -> list[tuple[int | None, bool]]:
    """解析 tiles 表达式，返回按顺序的 [(编号|None, is_default)]。

    支持 ``3``、``3-16`` 范围、``<default>`` 占位符。
    """
    out: list[tuple[int | None, bool]] = []
    for m in _TILE_TOKEN.finditer(tiles):
        tok = m.group(0)
        if tok == "<default>":
            out.append((None, True))
            continue
        a = int(m.group(1))
        b = m.group(2)
        if b:
            for i in range(a, int(b) + 1):
                out.append((i, False))
        else:
            out.append((a, False))
    return out


def _resolve_match_tiles(val: str, short_to_cat: dict[str, str]) -> tuple[str | None, str]:
    """把 matchTiles / matchBlocks 解析成 (短名, 分类)。

    优先精确命中已存在短名；其次尝试从 ``textures/block/xxx.png`` 路径提取。
    """
    cand = val.strip().split()[0] if val.strip() else ""
    if not cand:
        return None, "blocks"
    if cand in short_to_cat:
        return cand, short_to_cat[cand]
    m = _PATH_TILE_RE.search(cand)
    if m:
        name = m.group(1).lower()
        if name in short_to_cat:
            return name, short_to_cat[name]
    return None, "blocks"


def parse_ctm(
    reader: Any,
    short_to_cat: dict[str, str],
    log: Any = None,
) -> dict[str, CtmRule]:
    """扫描 optifine/ctm 与 optifine/random，解析变体，只保留能映射到已有短名的规则。

    Args:
        reader: 具备 ``list_files() -> list[str]`` 与 ``read(path) -> bytes`` 的归档读取器。
        short_to_cat: 主目录已映射的 ``短名 -> 分类(blocks/items)``，用于决定变体落盘目录。
        log: 可选回调 ``log(level, msg)``，用于记录降级/跳过信息。

    Returns:
        ``{短名: CtmRule}``，仅含成功解析出至少 1 个变体的规则。
    """
    files = reader.list_files()
    props = [f for f in files if f.lower().endswith(".properties") and _CTM_PATH_RE.search(f)]
    rules: dict[str, CtmRule] = {}
    skipped_no_match = 0
    skipped_no_tile = 0
    degraded_method = 0

    for pf in props:
        try:
            raw = reader.read(pf)
            txt = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:  # noqa: BLE001 - 单个配置损坏不应中断整包
            continue
        d = dict(_PROP_RE.findall(txt))
        match = d.get("matchTiles") or d.get("matchBlocks") or ""
        short, cat = _resolve_match_tiles(match, short_to_cat)
        if not short:
            skipped_no_match += 1
            continue
        tiles = (d.get("tiles") or "").strip()
        if not tiles:
            skipped_no_tile += 1
            continue

        base_dir = "/".join(pf.split("/")[:-1])
        dir_images = [
            f for f in files
            if f.startswith(base_dir + "/") and f.lower().endswith(".png")
        ]
        # 按「去后缀基名」分组：1.png / 1_n.png / 1_s.png 同属基名 "1"，
        # 否则 _n/_s 会被错误地拆成独立键，变体拿不到法线/镜面（已修复）。
        by_stem: dict[str, list[str]] = defaultdict(list)
        for f in dir_images:
            raw = f.split("/")[-1][:-4]
            base = re.sub(r"_(?:n|s|mer|heightmap)$", "", raw)
            by_stem[base].append(f)

        weights: list[int] | None = None
        if d.get("weights"):
            try:
                weights = [int(x) for x in re.findall(r"-?\d+", d["weights"])]
            except ValueError:
                weights = None

        method = (d.get("method") or "repeat").strip().lower()
        if method not in ("repeat", "random", "fixed"):
            degraded_method += 1
            if log:
                log("WARN", f"CTM {short}：method={method} 基岩无连接纹理，降级为加权随机变体")

        tokens = _parse_tiles(tiles)
        variants: list[CtmVariant] = []
        for num, is_def in tokens:
            if is_def:
                variants.append(CtmVariant(index=-1, color="", is_default=True))
                continue
            stem = str(num)
            imgs = by_stem.get(stem)
            if not imgs:
                continue
            color = next((f for f in imgs if not f.endswith(("_n.png", "_s.png"))), None)
            if not color:
                continue
            n = next((f for f in imgs if f.endswith("_n.png")), None)
            s = next((f for f in imgs if f.endswith("_s.png")), None)
            variants.append(CtmVariant(index=num, color=color, normal=n, specular=s))

        if not variants:
            continue
        rules[short] = CtmRule(short, cat, method, weights, variants)

    if log:
        log("INFO", f"CTM 解析：命中 {len(rules)} 条规则；"
                    f"未匹配短名 {skipped_no_match}；无 tiles {skipped_no_tile}；"
                    f"连接型降级 {degraded_method}")
    return rules


def build_variant_groups(
    rules: dict[str, CtmRule],
) -> tuple[list[Any], dict[str, list[tuple[str, int]]]]:
    """把解析出的规则展开成独立的 MaterialGroup 与图集变体映射。

    Returns:
        groups: 变体 MaterialGroup 列表（每个非 default 变体一个）。
        ctm_variations: ``{短名: [(变体atlas键, 权重), ...]}``，
            每个短名的第一个元素是 ``(短名自身, 默认权重)``，代表主贴图（<default>）。
    """
    from .models import MaterialGroup  # 延迟导入，避免循环引用

    groups: list[Any] = []
    ctm_variations: dict[str, list[tuple[str, int]]] = {}

    for short, rule in rules.items():
        cat = rule.category
        var_keys: list[tuple[str, int]] = []   # 编号变体（不含量 default）
        main_weight = 1                          # 主贴图（<default>）权重

        for i, v in enumerate(rule.variants):
            if v.is_default:
                # 主贴图复用，不生成独立 group；权重取 weights 对应位（若有）
                if rule.weights and i < len(rule.weights):
                    main_weight = rule.weights[i]
                continue
            # 变体 atlas 键：主短名 + _ctm_ + 编号，保证唯一且可读
            vkey = f"{short}_ctm_{v.index}"
            w = 1
            if rule.weights and i < len(rule.weights):
                w = rule.weights[i]
            var_keys.append((vkey, w))
            g = MaterialGroup(
                key=f"ctm/{short}/{v.index}",
                basecolor=v.color,
                normal=v.normal,
                specular=v.specular,
                category=cat,
                target=f"{cat}/{vkey}",
                profile="labpbr13",
                mapped=True,
            )
            groups.append(g)

        if var_keys or main_weight:
            # 主贴图永远作为 variations[0]，确保「基准外观」也是随机选项之一
            ctm_variations[short] = [(short, main_weight)] + var_keys

    return groups, ctm_variations
