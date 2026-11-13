"""S1 ArchiveReader / S2 MetaParser / S3 ShaderGuard（PRD 6.3）。

安全：Zip Slip 防护、解压膨胀比与总大小上限、CRC 校验（PRD NFR-13）。
"""
from __future__ import annotations

import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from .models import MaterialGroup, ParseSummary, safe_text

IMAGE_EXT = {".png", ".tga", ".jpg", ".jpeg"}
# 注意：.properties / .glsl / .fsh 等在 Java 材质包（OptiFine CTM）与基岩版材质包里都会出现，
# 仅凭扩展名或 /shaders/ 路径判定"Java 光影包"会误伤。此集合只用于统计着色器相关文件。
SHADER_EXT = {".fsh", ".vsh", ".glsl", ".gsh", ".csh", ".properties",
              ".material", ".vertex", ".fragment", ".fio"}
MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024 * 1024  # 4 GB
MAX_COMPRESSION_RATIO = 250

# Java 光影包（OptiFine / Iris）专属强特征文件名；基岩版 RenderDragon 的
# shaders/glsl/ALPHA.fsh 之类不在此列，不能据此判为光影包。
_SHADPACK_STEM = re.compile(
    r"^(gbuffers[_\d]|composite\d*|final|deferred\d*|prepare\d*|shadow(comp)?\d*|"
    r"world\d*|dh_|bloom|ssao|clouds|skybasic|skytextured|terrain_|entities_|"
    r"hand_|weather|particle|spider|entity\.|block\.|shaders\.properties)",
    re.I,
)
_SHADPACK_GLSL_EXT = {".fsh", ".vsh", ".gsh", ".csh", ".glsl"}
_JAVA_ASSET_RE = re.compile(r"(?:^|/)assets/[^/]+/textures/", re.I)
_BEDROCK_DIR_RE = re.compile(r"(?:^|/)textures/(?:blocks|items)/", re.I)
# 只有这些 Java textures 子目录里的贴图能对应 Bedrock terrain_texture/item_texture。
# environment(天气雨雪/日月)、painting(画)、colormap(群系色表)、mcpatcher/sky(天空盒)、
# gui、font、misc 等不是方块/物品贴图：把它们塞进 blocks 会污染图集，甚至因同名
# 覆盖掉真正的方块贴图（典型事故：environment/snow.png 盖掉 block/snow.png）。
_JAVA_TEX_DIR_RE = re.compile(r"(?:^|/)assets/[^/]+/textures/([^/]+)/", re.I)

SHADER_GUIDES = [
    {"title": "Light Sources（光源）", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals/lightingcustomization"},
    {"title": "Atmospheric Effects（大气）", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals/atmosphericscustomization"},
    {"title": "Shadows（阴影）", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals/shadowscustomization"},
    {"title": "Color Grading and Tone Mapping（色调映射）", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals/colorgradingtonemappingcustomization"},
    {"title": "Key Frame JSON Syntax（关键帧语法）", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals/keyframejsonsyntax"},
]

BEDROCK_GUIDES = [
    {"title": "Vibrant Visuals 总览", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals"},
    {"title": "Vibrant Visuals 资源包规范", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals/vvresourcepacks"},
    {"title": "纹理集 texture_set.json", "url": "https://learn.microsoft.com/zh-cn/minecraft/creator/documents/vibrantvisuals/texturesets"},
]


class ArchiveError(Exception):
    """归档类错误，携带用户可读的中文原因与错误码。"""

    def __init__(self, message: str, code: str = "E_ARCHIVE"):
        super().__init__(message)
        self.code = code


class ArchiveReader:
    """统一虚拟文件树（VFS）：ZIP 与文件夹两种来源，按需读取，不整体载入内存。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise ArchiveError("找不到导入的文件或文件夹", "E_NOT_FOUND")
        self.is_zip = zipfile.is_zipfile(self.path)
        self.zf: zipfile.ZipFile | None = None
        self.root = self.path
        self._names: list[str] = []
        if self.is_zip:
            self.zf = zipfile.ZipFile(self.path, "r")
            self._names = self._validate_zip()
        else:
            if not self.path.is_dir():
                raise ArchiveError("不支持的文件格式，请导入 ZIP 或文件夹", "E_FORMAT")
            self._names = self._scan_dir()

    # ---- S1 安全校验 ----
    def _validate_zip(self) -> list[str]:
        assert self.zf is not None
        raw: list[str] = []
        total = 0
        for info in self.zf.infolist():
            name = info.filename.replace("\\", "/")
            parts = name.split("/")
            if name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
                raise ArchiveError(f"ZIP 含非法路径（路径穿越风险）：{name[:80]}", "E_ZIP_SLIP")
            if info.compress_size > 0 and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise ArchiveError(f"检测到疑似解压炸弹：{name[:80]}", "E_ZIP_BOMB")
            total += info.file_size
            if info.is_dir() or not name or name.endswith("/"):
                continue
            if name.startswith("__MACOSX/") or "/." in name or name.startswith("."):
                continue
            raw.append(name)
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise ArchiveError("归档解压后体积超过 4 GB 上限", "E_TOO_LARGE")
        return list(dict.fromkeys(_normalize_vfs(n) for n in _strip_common_root(raw)))

    def _scan_dir(self) -> list[str]:
        names = []
        for p in self.path.rglob("*"):
            if p.is_file():
                rel = p.relative_to(self.path).as_posix()
                if rel.startswith("__MACOSX/") or "/." in rel or rel.startswith("."):
                    continue
                names.append(_normalize_vfs(rel))
        return list(dict.fromkeys(names))

    def list_files(self) -> list[str]:
        return list(self._names)

    def read(self, rel: str) -> bytes:
        """按 VFS 名读取；ZIP 模式由 ZipFile 自动校验 CRC。"""
        # VFS 名已规范化（剔除空白），需映射回磁盘/ZIP 内的真实名（可能含空格）。
        real = self._lookup.get(rel, rel)
        if not self.is_zip:
            target = (self.root / real).resolve()
            if not str(target).startswith(str(self.root.resolve())):
                raise ArchiveError("越界读取被阻止", "E_PATH_ESCAPE")
            return target.read_bytes()
        assert self.zf is not None
        try:
            return self.zf.read(real)
        except (KeyError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ArchiveError(f"读取成员失败或 CRC 校验不通过：{rel}", "E_CRC") from exc

    def close(self) -> None:
        if self.zf:
            self.zf.close()

    # 建立「规范化 VFS 名 -> 磁盘/ZIP 内真实名」的映射
    @property
    def _lookup(self) -> dict[str, str]:
        cached = getattr(self, "_lookup_cache", None)
        if cached is not None:
            return cached
        mapping: dict[str, str] = {}
        if not self.is_zip:
            for p in self.path.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(self.path).as_posix()
                if rel.startswith("__MACOSX/") or "/." in rel or rel.startswith("."):
                    continue
                key = _normalize_vfs(rel)
                if key not in mapping or not any(c.isspace() for c in rel):
                    mapping[key] = rel
        else:
            assert self.zf is not None
            # 过滤条件必须与 _validate_zip 完全一致，保证 stripped 与 self._names 同序等长。
            # 旧实现用 endswith("/"+r) 模糊匹配，会把根文件的 VFS 名错映到嵌套同名文件
            # （如 pack.png -> assets/x/pack.png），导致 read() 读出的字节根本不是目标文件。
            # 改为「剥离后 VFS 名」与「ZIP 内真实名」按位置一一对应，同名不同目录不再串味。
            raw: list[str] = []
            for info in self.zf.infolist():
                name = info.filename.replace("\\", "/")
                if info.is_dir() or not name or name.endswith("/"):
                    continue
                if name.startswith("__MACOSX/") or "/." in name or name.startswith("."):
                    continue
                raw.append(name)
            for vfs, real in zip(_strip_common_root(raw), raw):
                key = _normalize_vfs(vfs)
                # 规范化后可能撞名（源包同时含 "x_n .png" 与 "x_n.png"）。
                # 干净名（原名本身无空白）优先，保证读回的是正常文件的字节。
                if key not in mapping or not any(c.isspace() for c in real):
                    mapping[key] = real
        object.__setattr__(self, "_lookup_cache", mapping)
        return mapping


def _strip_common_root(names: list[str]) -> list[str]:
    """剥离 ZIP 常见的单一外层目录，如 MyPack/pack.mcmeta -> pack.mcmeta。"""
    if not names:
        return names
    first_parts = [n.split("/")[0] for n in names if "/" in n]
    if len(first_parts) == len(names) and len(set(first_parts)) == 1:
        prefix = first_parts[0] + "/"
        return [n[len(prefix):] for n in names]
    return names


def _normalize_vfs(name: str) -> str:
    """规范化 VFS 路径：剔除全部空白字符。

    Java 材质包偶见「扩展名前多一个空格」的脏名，如
    ``assets/.../grass_block_side_overlay_n .png``。它有两重危害：
      1) stem 变成 ``..._n `` 而非 ``..._n``，法线/Specular 配对失效，这张图
         反而被当成独立 basecolor 材质；
      2) 产出的 ``terrain_texture.json`` 键与 PNG 路径都带空格，而基岩版资源
         路径对空格极敏感（移动端尤其），常导致整包「加载失败」。
    Minecraft 资源路径不含合法空白，直接剔除空白是安全且治本的做法。
    仅改 VFS 逻辑名；ZIP 内真实名由 ``_lookup`` 保留，不影响读取字节。
    """
    return _VFS_WS_RE.sub("", name)


_VFS_WS_RE = re.compile(r"\s+")


class ShaderGuard:
    """FR-L-01：输入包类型预检 + Java 光影包（OptiFine / Iris GLSL）识别与拦截。

    判据分三级，避免"见到 /shaders/ 或 .fsh 就判光影包"的误伤：
      1. 基岩版资源包（manifest.json / textures/blocks / .material）→ 提示无需转换；
         其 shaders/ 是 RenderDragon 材质着色器，不是 Java 光影包。
      2. Java 光影包强特征（gbuffers_* / composite* / final.fsh 等，>=2 个）→ 拦截。
      3. Java 材质包附带零星着色器 → 只警告、跳过、继续转换。
      4. 既非 Java 资源包也非基岩包 → 明确报"输入类型不符"，不产出空包。
    """

    @staticmethod
    def detect(files: list[str]) -> dict[str, Any] | None:
        pairs = [(f, f.lower().replace("\\", "/")) for f in files]
        stems = {l.rsplit("/", 1)[-1] for _, l in pairs}

        java_meta = "pack.mcmeta" in stems
        java_assets = any(_JAVA_ASSET_RE.search(l) for _, l in pairs)
        bedrock_score = (
            (2 if "manifest.json" in stems else 0)
            + (2 if any(_BEDROCK_DIR_RE.search(l) for _, l in pairs) else 0)
            + (1 if any(
                l.endswith((".material", ".vertex", ".fragment", ".fio"))
                or "/materials/" in l or "/fogs/" in l or "/biomes/" in l
                or l.rsplit("/", 1)[-1] == "blocks.json"
                for _, l in pairs) else 0)
        )
        shader_files = [f for f, l in pairs if Path(l).suffix in SHADER_EXT]
        strong = [
            f for f, l in pairs
            if (Path(l).suffix in _SHADPACK_GLSL_EXT
                and _SHADPACK_STEM.match(Path(l).stem)
                and (l.startswith("shaders/") or "/shaders/" in l))
            or l.endswith(("shaders.properties", "/shaders/lang/en_us.lang"))
        ]

        # 1) 基岩版资源包：不是 Java 输入，更不是 Java 光影包
        if bedrock_score >= 3 and not java_meta and not java_assets:
            return {
                "kind": "bedrock_pack", "block": True,
                "title": "⛔ 这是基岩版资源包，不是 Java 资源包",
                "reason": "包内检测到 manifest.json、textures/blocks|items 目录等基岩版资源包结构，"
                          "缺少 Java 资源包必需的 pack.mcmeta 与 assets/<命名空间>/textures 目录。"
                          "本工具是「Java → 基岩」单向转换器，它不需要也不应该被转换。",
                "note": "包里的 shaders/ 与 .material/.vertex/.fragment 是基岩版 RenderDragon 自定义材质着色器，"
                        "并非 Java 版 OptiFine / Iris 光影包。此前版本把它们误判为光影包，现已修正。",
                "evidence": [f for f, l in pairs
                             if l.rsplit("/", 1)[-1] in ("manifest.json", "blocks.json", "pack_icon.png")
                             or _BEDROCK_DIR_RE.search(l)][:12],
                "evidence_count": bedrock_score,
                "guides": BEDROCK_GUIDES,
                "state_label": "输入为基岩版资源包，无需转换",
                "toast": "识别为基岩版资源包（非 Java 光影包），无需转换",
            }

        # 2) Java 光影包强特征
        if len(strong) >= 2 and not java_assets:
            return {
                "kind": "java_shaderpack", "block": True,
                "title": "⛔ 已识别为 Java 光影包 · 转换被拦截",
                "reason": "检测到 OptiFine / Iris 光影包的专属着色器文件（gbuffers_/composite/final 等）。"
                          "GLSL 光影与基岩版 Vibrant Visuals 延迟渲染管线不兼容，无法自动转换。",
                "note": "这不是工具缺陷，而是两个渲染体系的技术边界。本工具不会产出半成品资源包。",
                "evidence": strong[:12],
                "evidence_count": len(strong),
                "guides": SHADER_GUIDES,
                "state_label": "光影包已拦截，可运行转换查看完整报告",
                "toast": "已识别为 Java 光影包，转换将被拦截",
            }

        # 3) 正常 Java 材质包：附带着色器只警告，继续转换
        if java_meta or java_assets:
            if shader_files:
                return {
                    "block": False,
                    "warnings": [f"检测到 {len(shader_files)} 个着色器相关文件，"
                                 f"但不含 OptiFine / Iris 光影包特征，已忽略并继续转换材质贴图"],
                }
            return None

        # 4) 结构不认识：明确报输入类型不符，避免误报光影包或产出空包
        return {
            "kind": "not_java_pack", "block": True,
            "title": "⛔ 未能识别为 Java 资源包",
            "reason": "包内既没有 Java 资源包的 pack.mcmeta / assets/<命名空间>/textures 结构，"
                      "也没有基岩版资源包的 manifest.json，无法提取任何可转换的材质贴图。",
            "note": "请导入 Java 版资源包（文件夹或 ZIP）。若压缩包带有外层目录，工具会自动剥离；"
                    "若这是模组 jar 或光影包，请改用对应的源文件。",
            "evidence": [f for _, f in pairs][:12],
            "evidence_count": len(files),
            "guides": BEDROCK_GUIDES,
            "state_label": "输入结构不符合 Java 资源包规范",
            "toast": "未识别到 Java 资源包结构，请检查导入文件",
        }


class MetaParser:
    """S2：解析 pack.mcmeta、assets 目录、LabPBR 贴图组与 mcmeta 动画。"""

    _ASSET_RE = re.compile(r"(?:^|/)assets/([^/]+)/textures/(.+)$")

    def __init__(self, reader: ArchiveReader, rules: dict[str, Any] | None = None):
        self.reader = reader
        bundle = (rules or {}).get("bundle", {})
        self.allowed_dirs: dict[str, str] = bundle.get(
            "convertible_texture_dirs",
            {"block": "blocks", "blocks": "blocks", "item": "items", "items": "items",
             "entity": "entity", "particle": "particle"},
        )
        self.dir_priority: dict[str, int] = bundle.get(
            "texture_dir_priority",
            {"block": 0, "blocks": 1, "item": 0, "items": 1, "entity": 2, "particle": 3},
        )

    def texture_dir(self, path: str) -> str | None:
        """返回 Java 贴图的 textures 一级子目录（小写），非 assets/*/textures/ 路径返回 None。"""
        m = _JAVA_TEX_DIR_RE.search(path.replace("\\", "/"))
        return m.group(1).lower() if m else None

    def parse(self) -> tuple[ParseSummary, list[MaterialGroup], list[dict[str, Any]]]:
        files = self.reader.list_files()
        summary = ParseSummary(files=files)

        guard = ShaderGuard.detect(files)
        if guard and guard.get("block"):
            summary.shaderpack = guard
            summary.warnings.append(f"S3 预检拦截（{guard['kind']}）：{guard['state_label']}")
            return summary, [], []
        if guard:  # 仅警告：Java 材质包附带的非光影着色器，跳过即可
            summary.warnings.extend(guard.get("warnings", []))

        # pack.mcmeta
        meta_name = next((f for f in files if Path(f).name == "pack.mcmeta"), None)
        if meta_name:
            try:
                obj = json.loads(self.reader.read(meta_name).decode("utf-8", "replace"))
                pack = obj.get("pack", {})
                summary.pack_format = pack.get("pack_format")
                # description 可能是文本组件 dict/list（带 § 格式码），必须摊平为纯文本，
                # 否则 str(dict) 会把 Python 字面量写进 manifest，游戏里显示为乱码。
                summary.description = safe_text(pack.get("description", ""), 200)
                summary.pack_name = Path(self.reader.path).stem
            except (ValueError, ArchiveError, UnicodeDecodeError) as exc:
                summary.warnings.append(f"pack.mcmeta 解析失败，已降级推断：{exc}")
        else:
            summary.warnings.append("未找到 pack.mcmeta，已按默认 Java 资源包结构推断")

        all_images = [f for f in files if Path(f.lower()).suffix in IMAGE_EXT]
        summary.total_images = len(all_images)

        # 只转换方块/物品/实体/粒子贴图目录。其余贴图（天气、画、色表、天空盒、
        # GUI 等）在 Bedrock 里没有对应的 Texture Set 注册位置，必须跳过，否则会
        # 因 IdMapper 取末段文件名而与真正的方块贴图撞名并覆盖它。
        images: list[str] = []
        skipped_by_dir: Counter[str] = Counter()
        for f in all_images:
            sub = self.texture_dir(f)
            if sub in self.allowed_dirs:
                images.append(f)
            else:
                skipped_by_dir[sub or "(非 assets/*/textures 路径)"] += 1
        summary.skipped_images = len(all_images) - len(images)
        summary.skipped_dirs = dict(skipped_by_dir.most_common())
        if summary.skipped_images:
            detail = "、".join(f"{k} {v} 张" for k, v in list(skipped_by_dir.items())[:5])
            summary.warnings.append(
                f"已跳过 {summary.skipped_images} 张非方块/物品贴图（{detail}）："
                "天气、画、群系色表、天空盒等不属于 Bedrock Texture Set 转换范围"
            )
        mcmeta_files = {f for f in files if f.lower().endswith(".png.mcmeta")}

        # 收集材质组：以 basecolor 为主体，探测 _n / _s / .mcmeta
        groups: dict[str, MaterialGroup] = {}
        for img in images:
            stem = img[: -len(Path(img).suffix)]
            if stem.endswith("_n") or stem.endswith("_s"):
                continue
            rel = self._rel_name(img)
            key = rel[: -len(Path(rel).suffix)]
            sub = self.texture_dir(img)
            category = self.allowed_dirs.get(sub, self._category(rel))
            ext = Path(img).suffix
            n_path = f"{stem}_n{ext}"
            s_path = f"{stem}_s{ext}"
            has_n = n_path in images
            has_s = s_path in images
            g = MaterialGroup(
                key=key,
                basecolor=img,
                normal=n_path if has_n else None,
                specular=s_path if has_s else None,
                mcmeta=(img + ".mcmeta") if (img + ".mcmeta") in mcmeta_files else None,
                category=category,
                profile="labpbr13" if (has_n or has_s) else "plain",
            )
            if has_n and has_s:
                summary.labpbr_groups += 1
            elif has_n != has_s:
                g.notes.append("缺少 " + ("_s 高光贴图" if has_n else "_n 法线贴图"))
                g.confidence = 0.7
                summary.warnings.append(f"{key}：LabPBR 附属贴图不完整，可开启 AI 补全（FR-B-09）")
            groups[key] = g

        # 目录优先级决定同名目标键的归属：block/ 优先于 blocks/，item/ 优先于 items/。
        # 顺序稳定可复现，配合 S4 的目标键冲突检测，杜绝后处理覆盖先处理。
        real_groups = sorted(
            groups.values(),
            key=lambda g: (
                self.dir_priority.get((self.texture_dir(g.basecolor or "") or "").lower(), 99),
                g.key,
            ),
        )
        summary.material_count = len(real_groups)
        summary.categories = dict(Counter(g.category for g in real_groups))
        summary.animation_count = len(mcmeta_files)

        # 动画配置
        animations: list[dict[str, Any]] = []
        for mf in sorted(mcmeta_files):
            image_path = mf[: -len(".mcmeta")]
            try:
                cfg = json.loads(self.reader.read(mf).decode("utf-8", "replace"))
            except (ValueError, ArchiveError, UnicodeDecodeError) as exc:
                summary.warnings.append(f"{mf} 动画配置解析失败，已跳过：{exc}")
                continue
            hit = next((g for g in real_groups if g.basecolor == image_path), None)
            animations.append({
                "mcmeta": mf,
                "image": image_path,
                "group": hit.key if hit else self._rel_name(image_path),
                "config": cfg.get("animation", cfg),
                "exists": image_path in images,
            })
            if not hit:
                summary.warnings.append(f"{mf} 找不到对应的贴图，动画配置将被跳过")
        if not animations:
            summary.warnings.append("未发现 mcmeta 动画配置，将不生成 flipbook_textures.json")
        return summary, real_groups, animations

    def _rel_name(self, path: str) -> str:
        m = self._ASSET_RE.search(path)
        if m:
            ns, rest = m.group(1), m.group(2)
            rest = rest.lower()
            if ns != "minecraft":
                first, _, tail = rest.partition("/")
                rest = f"{first}/{ns}_{tail}" if tail else f"{ns}_{rest}"
            return rest
        return path.lower()

    @staticmethod
    def _category(rel: str) -> str:
        parts = rel.split("/")
        for p in parts[:-1]:
            if p in ("block", "blocks"):
                return "blocks"
            if p in ("item", "items"):
                return "items"
            if p == "entity":
                return "entity"
            if p == "particle":
                return "particle"
        return "blocks"
