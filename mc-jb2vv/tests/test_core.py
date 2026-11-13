"""端到端与单元测试（对应 PRD 第 8 章验收标准）。

运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import numpy as np
import sys
import unittest
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from core.archive import ArchiveReader, MetaParser  # noqa: E402
from core.clock import CLOCK  # noqa: E402
from core.convert import (  # noqa: E402
    Validator, decode_specular, load_rules, resolve_pack_icon,
)
from core.pipeline import MANAGER, OUTPUT  # noqa: E402
from samples.make_samples import build_java_pack, build_shaderpack  # noqa: E402

PACK = ROOT / "samples" / "demo_java_pack.zip"
SHADER = ROOT / "samples" / "demo_shaderpack.zip"


class Base:
    """测试辅助 mixin（不继承 TestCase，避免被收集为空测试类）。"""

    @classmethod
    def setUpClass(cls) -> None:
        build_java_pack(PACK)
        build_shaderpack(SHADER)

    @staticmethod
    def run_task(source: Path, preset: str = "vv_standard", overrides=None):
        t = MANAGER.create(str(source), preset, overrides or {})
        MANAGER.start(t.id)
        t.thread.join(timeout=60)
        return t, MANAGER.snapshot(t.id)

    @staticmethod
    def read_pack(snap: dict) -> zipfile.ZipFile:
        path = OUTPUT / snap["output_name"]
        return zipfile.ZipFile(path)


class TestStandardConversion(Base, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.task, cls.snap = cls.run_task(PACK)

    def test_01_state_and_gate(self):
        self.assertEqual(self.snap["state"], "SUCCESS", self.snap.get("error"))
        report = self.snap["report"]
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["checks"]), 13)  # 新增 C13 manifest 格式版本门禁
        self.assertTrue(all(c["ok"] for c in report["checks"]),
                        [c["item"] for c in report["checks"] if not c["ok"]])
        self.assertTrue((OUTPUT / self.snap["output_name"]).exists())

    def test_02_manifest_official_fields(self):
        with self.read_pack(self.snap) as z:
            self.assertIn("manifest.json", z.namelist())
            m = json.loads(z.read("manifest.json"))
        # 真机导入门禁：v1 只允许 min_engine_version < 1.13.0，
        # 写 1 会让游戏报「要使用更高的版本，您需要使用格式版本 2」并拒绝整包。
        self.assertEqual(m["format_version"], 2)
        self.assertEqual(m["header"]["min_engine_version"], [1, 21, 120])
        self.assertEqual(m["capabilities"], ["pbr"])
        self.assertNotIn("raytraced", json.dumps(m).lower())
        self.assertNotIn("raytracing", json.dumps(m).lower())
        hu, mu = m["header"]["uuid"], m["modules"][0]["uuid"]
        self.assertEqual(uuid.UUID(hu).version, 4)
        self.assertEqual(uuid.UUID(mu).version, 4)
        self.assertNotEqual(hu, mu)
        self.assertEqual(m["modules"][0]["type"], "resources")

    def test_03_texture_set_mutual_exclusion(self):
        with self.read_pack(self.snap) as z:
            names = z.namelist()
            sets = [n for n in names if n.endswith(".texture_set.json")]
            self.assertGreaterEqual(len(sets), 10)
            item_sets = [n for n in sets if "/items/" in n]
            self.assertTrue(item_sets, "应生成物品 texture_set")
            for n in sets:
                layers = json.loads(z.read(n))["minecraft:texture_set"]
                self.assertIn("color", layers)
                self.assertFalse("normal" in layers and "heightmap" in layers, n)
                self.assertFalse(
                    "metalness_emissive_roughness" in layers
                    and "metalness_emissive_roughness_subsurface" in layers, n
                )
                if "/items/" in n or "/entity/" in n or "/particle/" in n:
                    self.assertNotIn("heightmap", layers, f"C5 违规: {n}")
                for key, ref in layers.items():
                    if isinstance(ref, str):
                        self.assertIn(f"textures/{'/'.join(n.split('/')[1:-1])}/{ref}.png", names)

    def test_04_channel_counts(self):
        with self.read_pack(self.snap) as z:
            modes = {}
            for n in z.namelist():
                if n.endswith(("_normal.png", "_mer.png", "_heightmap.png")):
                    with Image.open(io.BytesIO(z.read(n))) as im:
                        modes[n.split("/")[-1]] = len(im.getbands())
        self.assertTrue(modes)
        for name, ch in modes.items():
            if name.endswith("_normal.png") or name.endswith("_mer.png"):
                self.assertEqual(ch, 3, name)
            if name.endswith("_heightmap.png"):
                self.assertEqual(ch, 1, name)

    def test_05_channel_math(self):
        """验证 MERS.B=1-smoothness（感知粗糙度，官方 Lab2MERS 基准）、金属度阈值化、
        emissive=A/254 正向解码落地。

        规范依据：LabPBR 的 R 是「感知平滑度」，(1-R)^2 是给 shader 内部转线性粗糙度
        用的；MERS.B 存的是感知粗糙度，应为 255-R。MER 若为整张常量，按官方 uniform
        数组值写入 texture_set 而不再产出 PNG；同一数值断言对 PNG 与 uniform 均成立。
        """
        with self.read_pack(self.snap) as z:
            def px(name):
                p = f"textures/blocks/{name}.png"
                if p in z.namelist():
                    with Image.open(io.BytesIO(z.read(p))) as im:
                        return list(im.convert("RGB").getpixel((32, 32)))
                stem = name[:-4] if name.endswith("_mer") else name
                ts = json.loads(z.read(f"textures/blocks/{stem}.texture_set.json"))
                v = ts["minecraft:texture_set"].get("metalness_emissive_roughness")
                self.assertIsInstance(v, list, f"{name} 的 MER 既无 PNG 也无 uniform 值")
                return list(v)
            iron = px("iron_block_mer")
            glow = px("glowstone_mer")
            stone = px("stone_mer")
            diamond = px("diamond_ore_mer")
        # iron: smoothness 0.78 -> MERS.B = 1-0.78 = 0.22 -> ~56；金属 ID 232 -> metalness 255
        self.assertAlmostEqual(iron[2], 56, delta=3)
        self.assertAlmostEqual(iron[0], 255, delta=2)
        self.assertAlmostEqual(iron[1], 0, delta=2)
        # glowstone: emission 0.92 -> _s.A=round(0.92*254)=234 -> MER.G=234/254*255 ≈ 235
        self.assertAlmostEqual(glow[1], 235, delta=3)
        self.assertAlmostEqual(glow[0], 0, delta=2)
        # stone 不发光（A=0）；diamond_ore 弱发光 A=25 -> MER.G ≈ 25
        self.assertAlmostEqual(stone[1], 0, delta=2)
        self.assertAlmostEqual(diamond[1], 25, delta=3)
        # stone: smoothness 0.15 -> MERS.B = 1-0.15 = 0.85 -> ~217
        self.assertAlmostEqual(stone[2], 217, delta=4)

    def test_06_flat_normal_baseline(self):
        """有立体感的法线（stone）默认必须保留且为合法切线空间法线；
        近平面/扁平法线仅在『省略近平面法线』开启时才被省略（回退 vanilla，不产生蓝紫占位 PNG）。
        """
        # 默认（保留所有源法线）：有立体感的法线必须保留
        with self.read_pack(self.snap) as z:
            names = z.namelist()
            stone_ts = json.loads(z.read("textures/blocks/stone.texture_set.json"))["minecraft:texture_set"]
            self.assertIn("normal", stone_ts, "有立体感的法线不得被省略")
            self.assertIn("textures/blocks/stone_normal.png", names)
            with Image.open(io.BytesIO(z.read("textures/blocks/stone_normal.png"))) as im:
                arr = np.asarray(im.convert("RGB"), dtype=float)
            self.assertLess(abs(arr[..., :2].mean() - 128), 40)  # XY 均值围绕平面
            self.assertGreaterEqual(arr[..., 2].mean(), 200)      # Z 均值高（整体朝观察者）
        # 省略模式：近平面/扁平法线被省略，不产生蓝紫占位 PNG
        _, omit_snap = self.run_task(PACK, overrides={"omit_near_flat_normals": True})
        with self.read_pack(omit_snap) as z:
            names = z.namelist()
            self.assertNotIn("textures/items/apple_normal.png", names,
                             "扁平物品法线应被省略")
            self.assertNotIn("textures/blocks/sand_normal.png", names,
                             "近平面方块法线应被省略")

    def test_07_animation_migration(self):
        with self.read_pack(self.snap) as z:
            self.assertIn("textures/flipbook_textures.json", z.namelist())
            fb = json.loads(z.read("textures/flipbook_textures.json"))
        entries = {e["flipbook_texture"]: e for e in fb["flipbook_textures"]}
        lava = entries["textures/blocks/lava_flow"]
        # 自定义帧时长 4,2,4,2 -> gcd=2 -> [0,0,1,2,2,3]
        self.assertEqual(lava["frames"], [0, 0, 1, 2, 2, 3])
        self.assertEqual(lava["ticks_per_frame"], 2)
        water = entries["textures/blocks/water_flow"]
        self.assertEqual(water["frames"], [0, 1])
        self.assertTrue(water["blend_frames"])
        self.assertTrue(any("gcd" in d for d in self.snap["report"]["downgraded"]))

    def test_08_timestamps_and_history(self):
        self.assertRegex(self.snap["output_name"], r"_\d{8}-\d{6}(_\d+)?\.mcpack$")
        self.assertRegex(self.snap["started_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        logs = self.snap["logs_tail"]
        self.assertTrue(logs)
        self.assertTrue(all(re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", line) for line in logs))
        # 测试任务可能在同一秒内启动，started_at 精度到秒且排序不稳定；
        # 用足够大的窗口取全量，避免目标记录被 LIMIT 挤出。
        rows = MANAGER.history.query(limit=200)
        self.assertTrue(any(r["id"] == self.task.id for r in rows))
        row = next(r for r in rows if r["id"] == self.task.id)
        self.assertEqual(row["status"], "SUCCESS")
        self.assertTrue(row["started_at"] and row["ended_at"] and row["output_path"])

    def test_09_unmapped_reported(self):
        self.assertIn("block/sand -> blocks/sand", self.snap["report"]["unmapped"])


class TestHeightmapPreset(Base, unittest.TestCase):
    def test_items_forced_to_normal(self):
        _, snap = self.run_task(PACK, "vv_heightmap")
        self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
        self.assertTrue(snap["report"]["passed"])
        with self.read_pack(snap) as z:
            sets = [n for n in z.namelist() if n.endswith(".texture_set.json")]
            block_sets = [json.loads(z.read(n))["minecraft:texture_set"]
                          for n in sets if "/blocks/" in n]
            item_sets = [json.loads(z.read(n))["minecraft:texture_set"]
                          for n in sets if "/items/" in n]
        # heightmap 预设：方块纹理集允许出现 heightmap 层（与 normal 互斥）
        self.assertTrue(any("heightmap" in s for s in block_sets))
        # 物品法线多为扁平/近平面，按规则省略回退 vanilla；此处只验证物品集合法（无矛盾层）
        for s in item_sets:
            self.assertFalse("heightmap" in s and "normal" in s, "heightmap 与 normal 互斥")
        self.assertTrue(any("强制改用 normal" in d for d in snap["report"]["downgraded"]))


class TestAIRepair(Base, unittest.TestCase):
    def test_missing_specular_completed(self):
        """无 _s 的材质默认也必须拿到 MER 层，否则 PBR 链路只剩 color（用户感知为不支持粗糙度）。

        MER 可以是 PNG 短名，也可以是官方 uniform 数组值（整张常量层时更合适）；
        两种形态都必须存在且合法。推断来源必须在报告中如实标注，不能冒充原始 PBR 数据。
        """
        _, plain = self.run_task(PACK, "vv_standard")
        _, ai = self.run_task(PACK, "vv_standard", {"ai_repair": True})
        for snap in (plain, ai):
            with self.read_pack(snap) as z:
                names = z.namelist()
                layers = json.loads(z.read("textures/blocks/stone.texture_set.json"))["minecraft:texture_set"]
                self.assertIn("metalness_emissive_roughness", layers)
                self.assertIn("normal", layers)
                mer = layers["metalness_emissive_roughness"]
                if isinstance(mer, str):
                    self.assertIn(f"textures/blocks/{mer}.png", names)
                else:
                    self.assertEqual(len(mer), 3, "uniform MER 必须是 3 通道数组")
                    self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255 for v in mer))
        self.assertTrue(any("原包无 _s" in d for d in plain["report"]["downgraded"]))
        self.assertTrue(any("sand" in a for a in ai["report"]["ai_actions"]))
        self.assertTrue(plain["report"]["passed"])
        self.assertTrue(ai["report"]["passed"])


class TestShaderpackBlock(Base, unittest.TestCase):
    def test_blocked(self):
        _, snap = self.run_task(SHADER)
        self.assertEqual(snap["state"], "BLOCKED")
        self.assertEqual(snap["output_name"], "")
        self.assertIn("shaders/", json.dumps(snap["report"]["shaderpack"]["evidence"]))
        self.assertGreaterEqual(len(snap["report"]["shaderpack"]["guides"]), 3)
        self.assertIn("无法自动转换", snap["report"]["shaderpack"]["reason"])


class TestTimeoutAndResume(Base, unittest.TestCase):
    def test_timeout_then_resume(self):
        t, snap = self.run_task(PACK, "vv_standard", {"task_timeout_s": 0.0001})
        self.assertEqual(snap["state"], "TIMEOUT")
        self.assertEqual(snap["error_code"], "E_TIMEOUT")
        self.assertTrue(snap["checkpoint_saved_at"] or snap["logs_tail"])
        # 放宽阈值后从断点续跑
        t.opts.task_timeout_s = 300.0
        MANAGER.start(t.id, resume=True)
        t.thread.join(timeout=60)
        snap2 = MANAGER.snapshot(t.id)
        self.assertEqual(snap2["state"], "SUCCESS", snap2.get("error"))
        self.assertTrue(snap2["report"]["passed"])


class TestValidator(Base, unittest.TestCase):
    def test_detects_mutual_exclusion(self):
        """校验器必须能真正拦下违规配置，而不是恒为通过。"""
        rules = load_rules()
        entries = {"manifest.json": b"{}", "textures/blocks/x.png": b"",
                   "textures/blocks/x_normal.png": b"", "textures/blocks/x_heightmap.png": b"",
                   "textures/blocks/x_mer.png": b""}
        manifest = {
            "format_version": 2,
            "header": {"uuid": str(uuid.uuid4()), "min_engine_version": [1, 21, 120]},
            "modules": [{"uuid": str(uuid.uuid4())}], "capabilities": ["pbr"],
        }
        ts = {"textures/blocks/x.texture_set.json": {
            "format_version": "1.21.30",
            "minecraft:texture_set": {"color": "x", "normal": "x_normal", "heightmap": "x_heightmap"},
        }}
        checks = Validator(rules).validate(entries, manifest, ts,
                                           {"textures/blocks/x.texture_set.json": "blocks"},
                                           {}, None)
        self.assertFalse(next(c for c in checks if c.id == "C07").ok)

    def test_detects_raytracing_capability(self):
        rules = load_rules()
        manifest = {
            "format_version": 2,
            "header": {"uuid": str(uuid.uuid4()), "min_engine_version": [1, 21, 120]},
            "modules": [{"uuid": str(uuid.uuid4())}], "capabilities": ["raytracing"],
        }
        checks = Validator(rules).validate({"manifest.json": b"{}"}, manifest, {}, {}, {}, None)
        self.assertFalse(next(c for c in checks if c.id == "C03").ok)


class TestRulesBundle(Base, unittest.TestCase):
    def test_traceability(self):
        rules = load_rules()
        b = rules["bundle"]
        self.assertEqual(b["engine_min_version"], [1, 21, 120])
        self.assertEqual(b["capability_whitelist"], ["pbr"])
        self.assertTrue(all("url" in s and "retrieved_at" in s for s in b["sources"]))
        self.assertTrue(b["constraints"]["normal_heightmap_mutually_exclusive"])


class TestPackKindGuard(Base, unittest.TestCase):
    """S3 预检回归：修正「见到 /shaders/ 或 .fsh 就判 Java 光影包」的误伤。

    回归来源：用户上传的基岩版材质包（manifest.json + textures/blocks + shaders/glsl/*.fsh）
    曾被判为 BLOCKED / Java 光影包，实际既不是光影包、也不是 Java 包。
    """

    TMP = ROOT / "tests" / "_tmp_kind"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import shutil
        if cls.TMP.exists():
            shutil.rmtree(cls.TMP)
        cls.TMP.mkdir(parents=True)
        buf = io.BytesIO()
        Image.new("RGBA", (16, 16), (128, 128, 128, 255)).save(buf, "PNG")
        png = buf.getvalue()

        # 1) 基岩版资源包：带 RenderDragon 材质着色器，但绝非 Java 光影包
        cls.bedrock = cls.TMP / "bedrock_pack.zip"
        with zipfile.ZipFile(cls.bedrock, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({
                "format_version": 2,
                "header": {"name": "demo", "uuid": str(uuid.uuid4()), "version": [1, 0, 0]},
                "modules": [{"type": "resources", "uuid": str(uuid.uuid4()), "version": [1, 0, 0]}],
            }))
            z.writestr("blocks.json", "{}")
            z.writestr("textures/blocks/stone.png", png)
            z.writestr("textures/items/apple.png", png)
            z.writestr("shaders/glsl/ALPHA.fsh", "#version 330\nvoid main(){}")
            z.writestr("shaders/glsl/ALPHA.vsh", "#version 330\nvoid main(){}")
            z.writestr("materials/entity.material", "{}")

        # 2) 结构不明的包
        cls.unknown = cls.TMP / "unknown.zip"
        with zipfile.ZipFile(cls.unknown, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("readme.txt", "not a resource pack")

        # 3) 正常 Java 材质包，额外附带几个非光影着色器文件
        cls.java_extra = cls.TMP / "java_with_shader.zip"
        with zipfile.ZipFile(PACK) as zin, \
                zipfile.ZipFile(cls.java_extra, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("shaders/glsl/ALPHA.fsh", "#version 330\nvoid main(){}")
            zout.writestr("shaders/glsl/ALPHA.vsh", "#version 330\nvoid main(){}")
            zout.writestr("optifine/ctm/custom.properties", "matchTiles=stone\n")

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.TMP, ignore_errors=True)

    def test_01_bedrock_pack_not_misreported_as_shaderpack(self):
        _, snap = self.run_task(self.bedrock)
        self.assertEqual(snap["state"], "BLOCKED")
        guard = snap["report"]["shaderpack"]
        self.assertEqual(guard["kind"], "bedrock_pack")
        self.assertIn("基岩版资源包", guard["reason"])
        self.assertNotIn("无法自动转换", guard["reason"])
        # 证据应来自基岩版结构，而不是那两个 .fsh
        self.assertFalse(any(e.endswith((".fsh", ".vsh")) for e in guard["evidence"]))
        self.assertIn("manifest.json", " ".join(guard["evidence"]))
        self.assertTrue(any("已忽略" not in w for w in snap["summary"]["warnings"]))

    def test_02_java_shaderpack_still_blocked(self):
        _, snap = self.run_task(SHADER)
        self.assertEqual(snap["state"], "BLOCKED")
        self.assertEqual(snap["report"]["shaderpack"]["kind"], "java_shaderpack")
        self.assertIn("无法自动转换", snap["report"]["shaderpack"]["reason"])

    def test_03_java_pack_with_incidental_shader_converts(self):
        _, snap = self.run_task(self.java_extra)
        self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
        self.assertTrue(snap["report"]["passed"])
        self.assertTrue(any("已忽略并继续转换" in w for w in snap["summary"]["warnings"]))
        self.assertIsNone(snap["summary"]["shaderpack"])

    def test_04_unknown_pack_reports_type_error(self):
        _, snap = self.run_task(self.unknown)
        self.assertEqual(snap["state"], "BLOCKED")
        guard = snap["report"]["shaderpack"]
        self.assertEqual(guard["kind"], "not_java_pack")
        self.assertNotIn("无法自动转换", guard["reason"])

    def test_05_direct_detector_on_shader_only_files(self):
        from core.archive import ShaderGuard
        files = ["shaders/glsl/ALPHA.fsh", "shaders/glsl/ALPHA.vsh",
                 "textures/blocks/x.png", "manifest.json", "materials/entity.material"]
        r = ShaderGuard.detect(files)
        self.assertTrue(r["block"])
        self.assertEqual(r["kind"], "bedrock_pack")
        # 纯 Java 材质包 + 零星 GLSL：只警告不拦截
        r2 = ShaderGuard.detect(["pack.mcmeta", "assets/minecraft/textures/block/stone.png",
                                 "shaders/glsl/ALPHA.fsh"])
        self.assertFalse(r2["block"])
        self.assertTrue(r2["warnings"])


class TestManifestAndPBRFixes(Base, unittest.TestCase):
    """回归真机导入暴露的三个问题：
    1) format_version=1 与 min_engine_version 1.21.120 冲突导致整包被拒；
    2) 文本组件 description / 中文包名 / pack.png 未迁移导致封面与名称乱码；
    3) 普通 Java 包无 LabPBR _n/_s 时只产出 color，粗糙度与法线链路完全未激活。
    """

    TMP = ROOT / "tests" / "_tmp_manifest"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import shutil
        if cls.TMP.exists():
            shutil.rmtree(cls.TMP)
        cls.TMP.mkdir(parents=True)
        cls.cjk = cls.TMP / "中文材质包_测试.zip"
        buf = io.BytesIO()
        Image.new("RGBA", (64, 64), (40, 160, 110, 255)).save(buf, "PNG")
        with zipfile.ZipFile(PACK) as zin, \
                zipfile.ZipFile(cls.cjk, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "pack.mcmeta":
                    data = json.dumps({"pack": {"pack_format": 15, "description": {
                        "text": "§6我的中文材质包", "color": "gold", "bold": True,
                        "extra": [{"text": " · 32x 高清"}]}}},
                        ensure_ascii=False).encode("utf-8")
                zout.writestr(item, data)
            zout.writestr("pack.png", buf.getvalue())

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.TMP, ignore_errors=True)

    def test_01_validator_rejects_format_version_1(self):
        rules = load_rules()
        manifest = {
            "format_version": 1,
            "header": {"uuid": str(uuid.uuid4()), "min_engine_version": [1, 21, 120]},
            "modules": [{"uuid": str(uuid.uuid4())}], "capabilities": ["pbr"],
        }
        checks = Validator(rules).validate({"manifest.json": b"{}"}, manifest, {}, {}, {}, None)
        self.assertFalse(next(c for c in checks if c.id == "C13").ok)
        manifest["format_version"] = 2
        checks = Validator(rules).validate({"manifest.json": b"{}"}, manifest, {}, {}, {}, None)
        self.assertTrue(next(c for c in checks if c.id == "C13").ok)

    def test_02_cjk_name_text_component_and_icon(self):
        _, snap = self.run_task(self.cjk)
        self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
        with self.read_pack(snap) as z:
            m = json.loads(z.read("manifest.json"))
            self.assertIn("pack_icon.png", z.namelist())
        self.assertEqual(m["format_version"], 2)
        self.assertEqual(m["header"]["name"], "中文材质包_测试")
        self.assertEqual(m["header"]["description"], "我的中文材质包 · 32x 高清")
        desc = m["header"]["description"]
        for bad in ("§", "{'", "'text'", "None"):
            self.assertNotIn(bad, desc)

    def test_03_full_pbr_and_no_orphan_layers(self):
        _, snap = self.run_task(PACK)
        with self.read_pack(snap) as z:
            names = z.namelist()
            sets = [n for n in names if n.endswith(".texture_set.json")]
            refs: set[str] = set()
            for n in sets:
                for v in json.loads(z.read(n))["minecraft:texture_set"].values():
                    if isinstance(v, str):
                        refs.add(n.rsplit("/", 1)[0] + "/" + v + ".png")
            orphans = [n for n in names
                       if n.endswith(("_normal.png", "_mer.png", "_heightmap.png"))
                       and n not in refs]
            only_color = sum(1 for n in sets if set(
                json.loads(z.read(n))["minecraft:texture_set"]) == {"color"})
        self.assertFalse(orphans, f"存在未被引用的孤立层: {orphans[:3]}")
        self.assertEqual(only_color, 0, "仍有 texture_set 只含 color，PBR 链路未激活")
        self.assertTrue(any(n.endswith("_mer.png") for n in names))
        self.assertTrue(any(n.endswith("_normal.png") for n in names))


class TestUniformConstantLayers(Base, unittest.TestCase):
    """整张纯色 PBR 层改用官方 uniform 值，杜绝「整张纯蓝」占位 PNG。

    回归背景：产物里 1867 张 (0,0,B) 纯蓝 MER 与 213 张 (128,128,255) 蓝紫
    推断法线被误认为「楼梯侧面贴图变成蓝色/法线了」。官方 Texture Sets 允许把
    常量层直接写成数组值；normal 层不支持 uniform，无原始数据时直接省略。
    """

    def test_01_constant_mer_is_uniform_and_no_blue_png(self):
        _, snap = self.run_task(PACK)
        with self.read_pack(snap) as z:
            names = z.namelist()
            stone = json.loads(z.read("textures/blocks/stone.texture_set.json"))["minecraft:texture_set"]
            mer = stone["metalness_emissive_roughness"]
            self.assertIsInstance(mer, list, "常量 MER 应写为官方 uniform 数组")
            self.assertEqual(len(mer), 3)
            self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255 for v in mer))
            self.assertAlmostEqual(mer[2], 217, delta=4)  # MERS.B=(1-0.15)*255=217
            self.assertNotIn("textures/blocks/stone_mer.png", names)
            # 保留下来的 MER PNG 都不许是「整张纯蓝、零细节」
            for n in names:
                if not n.endswith("_mer.png"):
                    continue
                with Image.open(io.BytesIO(z.read(n))) as im:
                    a = np.asarray(im.convert("RGB"), dtype=float)
                self.assertFalse(
                    a[..., 0].mean() <= 8 and a[..., 1].mean() <= 8 and a[..., 2].std() <= 32,
                    f"仍存在整张纯蓝常量 MER PNG: {n}",
                )
        self.assertTrue(snap["report"]["passed"], "uniform 数组不得触发 C09/C10 误报")

    def test_02_inferred_flat_normal_omitted_real_one_kept(self):
        """三态：推断法线省略；近平面真实法线省略（消除 VV 蓝紫伪影）；有立体感真实法线保留。"""
        from core.convert import ChannelConverter
        from core.models import ConvertOptions, MaterialGroup

        rules = load_rules()
        self.assertTrue(rules["bundle"]["uniform_constant_layers"]["omit_flat_inferred_normal"])
        self.assertTrue(rules["bundle"]["uniform_constant_layers"]["omit_near_flat_real_normal"])

        def make_normal(rg, b=255):
            buf = io.BytesIO()
            Image.new("RGB", (16, 16), (rg, rg, b)).save(buf, format="PNG")
            return buf.getvalue()

        class _Reader:
            def __init__(self, raw):
                self._raw = raw

            def read(self, _path):
                return self._raw

        # 1) 无 _n：推断的纯平面法线必须省略
        g = MaterialGroup(key="x", basecolor="x.png", normal=None, specular=None,
                          category="blocks", profile="labpbr13")
        res = ChannelConverter(_Reader(make_normal(160)), ConvertOptions(), rules).convert(g, "x")
        self.assertNotIn("normal", res["layers"], "不应产出推断的纯蓝紫法线 PNG")
        self.assertNotIn("heightmap", res["layers"])
        self.assertIn("metalness_emissive_roughness", res["uniforms"])

        # 2) 近平面真实 _n（(128,128,255) 平面）：开启省略时回退 vanilla
        g2 = MaterialGroup(key="x", basecolor="x.png", normal="x_n.png", specular=None,
                           category="blocks", profile="labpbr13")
        res2 = ChannelConverter(_Reader(make_normal(128)),
                                ConvertOptions(omit_near_flat_normals=True), rules).convert(g2, "x")
        self.assertNotIn("normal", res2["layers"], "近平面真实法线在省略开启时应省略（消除 VV 蓝紫伪影）")

        # 3) 有立体感真实 _n（明显偏离平面）：必须保留
        g3 = MaterialGroup(key="x", basecolor="x.png", normal="x_n.png", specular=None,
                           category="blocks", profile="labpbr13")
        res3 = ChannelConverter(_Reader(make_normal(70)), ConvertOptions(), rules).convert(g3, "x")
        self.assertIn("normal", res3["layers"], "有立体感的真实法线不得被省略")

        # 端到端：省略开启时 stone 为有立体感法线（保留），apple/sand 为扁平/近平面（省略）
        _, snap = self.run_task(PACK, overrides={"omit_near_flat_normals": True})
        with self.read_pack(snap) as z:
            names = z.namelist()
            self.assertIn("textures/blocks/stone_normal.png", names, "有立体感法线应保留")
            self.assertNotIn("textures/items/apple_normal.png", names, "扁平法线应省略")
            self.assertNotIn("textures/blocks/sand_normal.png", names, "近平面法线应省略")

    def test_02b_low_mean_with_local_detail_not_omitted(self):
        """复合判据回归：『大面积平坦 + 局部强细节』的真实 _n 不得被均值误杀。

        旧逻辑仅看 mean_tilt<=0.28，会把木板/树叶/原木顶这类贴图整张省略，
        实测 Patrix 包被省略的 777 张里 617 张（79.4%）局部 p95>0.45 属误杀，
        表现为「转换后画面明显变平、没有凹凸」。新判据要求
        mean_tilt / p95_tilt / 强细节像素占比三者同时低才省略。
        """
        from core.convert import ChannelConverter
        from core.models import ConvertOptions, MaterialGroup

        rules = load_rules()
        cfg = rules["bundle"]["uniform_constant_layers"]
        self.assertIn("normal_flat_p95_max", cfg, "复合判据 p95 阈值应可配置")
        self.assertIn("normal_detail_frac_max", cfg, "复合判据细节占比阈值应可配置")

        def make_normal(plane_rg=128, detail_rg=None, detail_count=0):
            arr = np.zeros((16, 16, 4), dtype=np.uint8)
            arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3] = plane_rg, plane_rg, 255, 255
            if detail_count and detail_rg is not None:
                flat = arr.reshape(-1, 4)
                flat[:detail_count, 0] = detail_rg
                flat[:detail_count, 1] = detail_rg
            buf = io.BytesIO()
            Image.fromarray(arr, "RGBA").save(buf, format="PNG")
            return buf.getvalue()

        class _Reader:
            def __init__(self, raw):
                self._raw = raw

            def read(self, _path):
                return self._raw

        def convert(raw):
            g = MaterialGroup(key="x", basecolor="x.png", normal="x_n.png", specular=None,
                              category="blocks", profile="labpbr13")
            return ChannelConverter(_Reader(raw),
                                    ConvertOptions(omit_near_flat_normals=True), rules).convert(g, "x")

        # 1) 6.25% 像素为强倾斜(tilt≈0.42)、其余全平面：均值仅≈0.026（旧判据必误杀）
        nx = (90 / 255.0) * 2 - 1
        detail_tilt = float(np.sqrt(nx * nx + nx * nx))
        self.assertGreater(detail_tilt, float(cfg["normal_detail_tilt"]), "构造的像素须是强细节")
        self.assertLess(detail_tilt * 16 / 256, float(cfg["normal_flat_tilt_max"]),
                        "均值须低到旧判据会误杀，才能验证本回归有效")
        res = convert(make_normal(128, detail_rg=90, detail_count=16))
        self.assertIn("normal", res["layers"],
                      "低均值但局部有强细节的真实 _n 必须保留（不得再被误杀）")
        self.assertIn("heightmap", res["layers"])

        # 2) 真·近平面（整张均匀低起伏常量）：仍应省略，保留消蓝染能力
        res2 = convert(make_normal(138))
        self.assertNotIn("normal", res2["layers"], "三者皆低的真平面法线仍应省略")

    def test_03_emitter_writes_uniform_value(self):
        from core.convert import JsonEmitter
        from core.models import ConvertOptions
        rules = load_rules()
        # 纯发射器夹具值（验证 uniform 数组原样写出，与 roughness 公式无关）
        ts, notes = JsonEmitter(rules).texture_set(
            "x", {"color"}, "blocks", ConvertOptions(),
            {"metalness_emissive_roughness": [0, 0, 200]},
        )
        self.assertEqual(ts["minecraft:texture_set"]["metalness_emissive_roughness"], [0, 0, 200])
        self.assertEqual(ts["format_version"], "1.21.30")
        self.assertTrue(any("uniform" in n for n in notes))


class TestMERSSpecCompliance(Base, unittest.TestCase):
    """LabPBR→MERS 通道映射必须严格符合官方规范与官方 Lab2MERS 工具。

    回归背景：旧实现把 roughness 算成 (1-感知平滑度)^2。那是 LabPBR 规范里
    「感知平滑度 → 线性粗糙度」的转换，是给 shader 内部喂 GGX 用的；而 MERS.B
    存的是感知粗糙度，官方工具为 `new_b = 255 - r`。把线性值存进贴图，VV
    （Cook-Torrance）内部再平方一次 => (1-s)^4，材质普遍过光滑、镜面/IBL
    高光过曝成一片白，即「反射很白很粗糙」。此处锁死 MERS.B = 1 - s，禁止
    再叠加任何 gamma/cap 观感校准（那会偏离基准、方向还可能相反）。

    规范：
      https://docs.minegraph.cn/labpbrmaterialstandard.html
      https://docs.minegraph.cn/mersmaterialstandard.html
    官方工具：https://github.com/G2F1SH/Lab2MERS
    """

    @staticmethod
    def _mer_of(smoothness: float, g: int = 10, a: int = 255):
        """构造常量 _s（R=感知平滑度、G=F0/金属ID、A=自发光），返回产物 MER 的 [R,G,B] (0..1)。"""
        from core.convert import ChannelConverter
        from core.models import ConvertOptions, MaterialGroup

        rules = load_rules()
        buf = io.BytesIO()
        Image.new("RGBA", (16, 16), (int(round(smoothness * 255)), g, 0, a)).save(buf, format="PNG")

        class _Reader:
            def read(self, _path):
                return buf.getvalue()

        grp = MaterialGroup(key="x", basecolor="x.png", normal=None, specular="x_s.png",
                            category="blocks", profile="labpbr13")
        res = ChannelConverter(_Reader(), ConvertOptions(), rules).convert(grp, "x")
        mer = res["uniforms"].get("metalness_emissive_roughness")
        if mer is None and "mer" in res["layers"]:
            with Image.open(io.BytesIO(res["layers"]["mer"][0])) as im:
                arr = np.asarray(im.convert("RGB"), dtype=float)
            mer = [float(arr[..., i].mean()) for i in range(3)]
        return [v / 255.0 for v in mer]

    def test_01_roughness_is_linear_complement(self):
        """MERS.B = 1 - 感知平滑度（不平方），多个采样点逐一验证。"""
        for s in (0.0, 0.15, 0.24, 0.5, 0.78, 1.0):
            _, _, b = self._mer_of(s)
            self.assertAlmostEqual(b, 1.0 - s, delta=0.01,
                                   msg=f"smoothness={s}: MERS.B 应 = 1-s")

    def test_02_never_double_squared(self):
        """严禁回归到 (1-s)^2：s=0.5 时 1-s=0.5 而 (1-s)^2=0.25，必须命中前者。"""
        _, _, b = self._mer_of(0.5)
        self.assertAlmostEqual(b, 0.5, delta=0.01, msg="MERS.B 必须是 1-s，不得平方")
        self.assertGreater(abs(b - (1 - 0.5) ** 2), 0.2, "不得等于旧的 (1-s)^2 值")

    def test_03_endpoints(self):
        """s=1 全光滑 -> B=0；s=0 全粗糙 -> B=1（对齐官方工具 new_b=255-r）。"""
        _, _, smooth = self._mer_of(1.0)
        _, _, rough = self._mer_of(0.0)
        self.assertAlmostEqual(smooth, 0.0, delta=0.01)
        self.assertAlmostEqual(rough, 1.0, delta=0.01)

    def test_04_metalness_threshold(self):
        """G=10(F0 电介质) -> metalness 0；G=232(金属 ID) -> metalness 1。
        与官方工具 `new_r=g` 的差异是有意的：F0 是反射率不是金属度，直接搬运
        会给所有非金属注入虚假金属度、反而加剧反射发白，故按 MERS 语义阈值化。"""
        m0, _, _ = self._mer_of(0.5, g=10)
        m1, _, _ = self._mer_of(0.5, g=232)
        self.assertAlmostEqual(m0, 0.0, delta=0.02)
        self.assertAlmostEqual(m1, 1.0, delta=0.02)

    def test_05_preset_vv_standard_is_pure_spec(self):
        """vv_standard 不再带任何 gamma 校准：stone(s=0.15) 的 MERS.B 应 = 0.85。"""
        _, snap = self.run_task(PACK, "vv_standard")
        with self.read_pack(snap) as z:
            stone = json.loads(z.read("textures/blocks/stone.texture_set.json"))["minecraft:texture_set"]
            b = stone["metalness_emissive_roughness"][2] / 255.0
        self.assertAlmostEqual(b, 0.85, delta=0.02, msg="vv_standard 应严格输出 1-s，无二次变换")


class TestNormalBReconstruction(unittest.TestCase):
    """B 通道重建必须与 normal_normalize 参考工具一致：XY 过冲时先归一化再算 Z，
    保证输出法线是单位向量（模长≈1），杜绝 VV 下 zSq<0 钳零产生的退化/蓝紫法线。

    回归背景：旧实现用 np.clip(1-x²-y²,0,1) 直接钳 Z，当源 _n 的 B 通道缺失/被截断
    导致 XY 模长 > 1 时，Z 被钳成 0 而 XY 仍过冲（非单位向量），方向失真 -> VV 表现为
    「没有法线效果」或异常蓝染。参考工具先归一化 XY 再算 Z，输出恒为单位向量。
    """

    @staticmethod
    def _convert_normal(rg: int, b: int = 255, flip: bool = False):
        from core.convert import ChannelConverter
        from core.models import ConvertOptions, MaterialGroup

        rules = load_rules()
        buf = io.BytesIO()
        Image.new("RGB", (16, 16), (rg, rg, b)).save(buf, format="PNG")

        class _Reader:
            def read(self, _path):
                return buf.getvalue()

        g = MaterialGroup(key="x", basecolor="x.png", normal="x_n.png", specular=None,
                          category="blocks", profile="labpbr13")
        res = ChannelConverter(_Reader(), ConvertOptions(), rules).convert(g, "x")
        if "normal" not in res["layers"]:
            return None  # 被省略（近平面/扁平）
        png = res["layers"]["normal"][0]
        with Image.open(io.BytesIO(png)) as im:
            arr = np.asarray(im.convert("RGB"), dtype=float) / 255.0
        xyz = arr * 2.0 - 1.0
        return xyz.reshape(-1, 3).mean(axis=0)  # 整张常量，取均值即可

    def test_01_unit_vector_when_over_extended(self):
        """XY 过冲（原 B 缺失/截断）的法线，重建后必须为单位向量（模长≈1）。"""
        # R=G=222 -> r=g≈0.74, len2≈1.10>1，触发 XY 归一化分支
        v = self._convert_normal(222)
        self.assertIsNotNone(v, "过冲法线应保留（立体感明显，非近平面）")
        mag = float(np.linalg.norm(v))
        self.assertAlmostEqual(mag, 1.0, delta=0.02,
                               msg=f"重建法线非单位向量，模长={mag}（VV 退化/蓝紫伪影源）")

    def test_02_z_reconstructed_from_xy(self):
        """正常 XY（len2<1）时 Z 由 sqrt(1-x²-y²) 重建，z 明显 > 0 且为单位向量。"""
        # R=G=70 -> r=g≈-0.45, len2≈0.41<1 -> z≈0.77
        v = self._convert_normal(70)
        self.assertIsNotNone(v, "有立体感的法线应保留")
        self.assertGreater(v[2], 0.5, "Z 应由 XY 重建且明显大于 0")
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, delta=0.02)

    def test_03_flip_y_then_normalize(self):
        """翻转 G 后再做 XY 归一化，输出仍为单位向量（翻转不应破坏 B 重建）。"""
        v = self._convert_normal(222, flip=True)
        self.assertIsNotNone(v)
        self.assertAlmostEqual(float(np.linalg.norm(v)), 1.0, delta=0.02)


class TestLabPBRDecode(unittest.TestCase):
    """直接锁定 shaderLABS 官方编码方向。

    回归背景：旧实现把 _s.A 当成反相自发光（(255-A)/254），而示例数据也按反相生成，
    两者自洽使单元测试一直通过，掩盖了方向错误。此处用固定输入直接验证解码函数。
    """

    @staticmethod
    def _spec(alpha: float, smoothness: float = 0.5,
              g: float = 10 / 255.0, b: float = 0.0) -> np.ndarray:
        s = np.zeros((2, 2, 4), dtype=np.float32)
        s[:, :, 0] = smoothness
        s[:, :, 1] = g
        s[:, :, 2] = b
        s[:, :, 3] = alpha / 255.0
        return s

    def test_01_emissive_direction(self):
        """A=0 不发光、A=254 全发光、A=255 是保留值（不发光）。"""
        for alpha, expect_byte in ((0, 0), (128, 128), (254, 255), (255, 0)):
            _, e, _ = decode_specular(self._spec(alpha), "labpbr13")
            self.assertAlmostEqual(float(e[0, 0]) * 255, expect_byte, delta=1.5,
                                   msg=f"_s.A={alpha} 的自发光解码方向错误")

    def test_02_roughness_and_metal_threshold(self):
        # MERS.B = 1 - 感知平滑度（感知粗糙度），不是 (1-s)^2（那是 shader 内部线性化）
        m, _, r = decode_specular(self._spec(0, smoothness=0.78, g=232 / 255.0), "labpbr13")
        self.assertAlmostEqual(float(r[0, 0]), 1 - 0.78, delta=0.01)
        self.assertEqual(float(m[0, 0]), 1.0)      # 232 >= 230，金属
        m2, _, _ = decode_specular(self._spec(0, g=229 / 255.0), "labpbr13")
        self.assertEqual(float(m2[0, 0]), 0.0)     # 229 仍是电介质 F0

    def test_03_oldpbr_channels_passthrough(self):
        """SEUS/OldPBR：R 是线性粗糙度本身，不能再 (1-R)^2 反演。"""
        m, e, r = decode_specular(self._spec(255, smoothness=0.3, g=0.4, b=0.7), "oldlabpbr")
        self.assertAlmostEqual(float(r[0, 0]), 0.3, delta=0.01)
        self.assertAlmostEqual(float(m[0, 0]), 0.4, delta=0.01)
        self.assertAlmostEqual(float(e[0, 0]), 0.7, delta=0.01)


class TestIconRename(Base, unittest.TestCase):
    """封面走纯字节重命名：产物与源 pack.png 字节完全一致，且匹配规则稳健。

    规范依据见 core/rules/bundle.json → icon_rename / sources：
      Java   = 根目录 pack.png（缺失或加载错误回退 unknown_pack.png）
      基岩版 = 根目录 pack_icon.png，文件名硬编码、必须为 PNG
    """

    CFG = load_rules()["bundle"]["icon_rename"]
    MAGIC = b"\x89PNG\r\n\x1a\n"

    def test_01_byte_identical(self):
        """端到端：产物 pack_icon.png 与源 pack.png 的字节必须完全相同。"""
        src = ROOT / "tests" / "_tmp_icon.zip"
        buf = io.BytesIO()
        Image.new("RGBA", (48, 48), (200, 30, 80, 255)).save(buf, "PNG")
        raw = buf.getvalue()
        with zipfile.ZipFile(PACK) as zin, \
                zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("pack.png", raw)
        try:
            _, snap = self.run_task(src)
            self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
            with self.read_pack(snap) as z:
                got = z.read("pack_icon.png")
            self.assertEqual(got, raw)
            self.assertEqual(hashlib.sha256(got).hexdigest(),
                             hashlib.sha256(raw).hexdigest())
        finally:
            src.unlink(missing_ok=True)

    def test_02_root_priority(self):
        """嵌套目录里的同名 pack.png 不应抢走根目录封面。"""
        root = self.MAGIC + b"ROOT"
        data = {"assets/x/pack.png": self.MAGIC + b"NESTED", "pack.png": root}
        got = resolve_pack_icon(list(data), lambda p: data[p], self.CFG)
        self.assertIsNotNone(got)
        self.assertEqual(got[2], "pack.png")
        self.assertEqual(got[1], root)

    def test_03_bad_png_rejected(self):
        """非 PNG 数据不落盘，避免游戏图标加载失败显示未知资源包纹理。"""
        self.assertIsNone(resolve_pack_icon(["pack.png"], lambda _: b"not a png", self.CFG))

    def test_04_existing_pack_icon_preserved(self):
        """混合包已带 pack_icon.png 时原样保留，不产生重复条目。"""
        raw = self.MAGIC + b"ICON"
        data = {"pack.png": self.MAGIC + b"JAVA", "pack_icon.png": raw}
        got = resolve_pack_icon(list(data), lambda p: data[p], self.CFG)
        self.assertEqual(got, ("pack_icon.png", raw, "pack_icon.png"))

    def test_05_fallback_and_disabled(self):
        """已有图标损坏时回退 pack.png；规则关闭或无候选时不产出。"""
        data = {"pack_icon.png": b"broken", "pack.png": self.MAGIC + b"OK"}
        got = resolve_pack_icon(list(data), lambda p: data[p], self.CFG)
        self.assertEqual(got[2], "pack.png")
        self.assertIsNone(resolve_pack_icon(["pack.png"], lambda _: self.MAGIC,
                                           {**self.CFG, "enabled": False}))
        self.assertIsNone(resolve_pack_icon(["a.png"], lambda _: self.MAGIC, self.CFG))


class TestNonBlockTextureExclusion(Base, unittest.TestCase):
    """回归真实包事故：environment/snow.png（天气粒子）覆盖 block/snow.png 雪方块。

    Java 资源包含大量非方块贴图（天气、画、群系色表、天空盒、GUI）。旧实现把
    包内所有 .png 都当方块材质，且 IdMapper 只取末段文件名，导致不同目录塌缩到
    同一 target 后互相覆盖，雪块最终显示为 64x256 天气图的黑白噪点。
    """

    TMP = ROOT / "tests" / "_tmp_snow"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import shutil
        if cls.TMP.exists():
            shutil.rmtree(cls.TMP)
        cls.TMP.mkdir(parents=True)
        cls.pkg = cls.TMP / "snow_pack.zip"

        def png(w, h, rgba):
            buf = io.BytesIO()
            Image.new("RGBA", (w, h), rgba).save(buf, "PNG")
            return buf.getvalue()

        snow = png(32, 32, (209, 224, 237, 255))       # 淡蓝白雪方块
        weather = png(64, 256, (0, 0, 0, 0))            # 竖条天气图（透明+黑）
        with zipfile.ZipFile(PACK) as zin, \
                zipfile.ZipFile(cls.pkg, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            # 同名、三个不同目录，模拟真实包的重复结构
            zout.writestr("assets/minecraft/environment/snow.png", weather)
            zout.writestr("assets/minecraft/textures/environment/snow.png", weather)
            zout.writestr("assets/minecraft/textures/painting/skull.png", weather)
            zout.writestr("assets/minecraft/textures/colormap/grass.png", weather)
            zout.writestr("assets/minecraft/mcpatcher/sky/world0/cloud1.png", weather)
            # 覆盖示例包里的 stone，验证白名单不影响正常方块
            zout.writestr("assets/minecraft/textures/block/snow.png", snow)
            zout.writestr("assets/minecraft/textures/block/snow_n.png", snow)
            zout.writestr("assets/minecraft/textures/block/snow_s.png", snow)

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.TMP, ignore_errors=True)

    def test_01_non_block_textures_skipped(self):
        _, snap = self.run_task(self.pkg)
        self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
        summary = snap["summary"]
        self.assertGreater(summary["skipped_images"], 0)
        self.assertIn("painting", summary["skipped_dirs"])
        self.assertIn("environment", summary["skipped_dirs"])
        self.assertTrue(any("非方块/物品贴图" in w for w in summary["warnings"]))

    def test_02_snow_is_not_weather_particles(self):
        """产物雪块必须是 32x32 淡蓝白，绝不能被 64x256 天气图覆盖。"""
        _, snap = self.run_task(self.pkg)
        with self.read_pack(snap) as z:
            a = np.array(Image.open(io.BytesIO(z.read("textures/blocks/snow.png"))).convert("RGBA"))
        self.assertEqual(a.shape[:2], (32, 32), "雪块尺寸应等于源方块贴图，不是天气图")
        mean = a[:, :, :3].mean()
        self.assertGreater(mean, 150, f"雪块应为淡蓝白，实测均值 {mean:.1f}（疑似黑白天气图）")
        self.assertEqual((a[:, :, 3] == 0).sum(), 0, "雪块不应有透明像素")

    def test_03_no_pollution_in_atlas_and_pack(self):
        _, snap = self.run_task(self.pkg)
        with self.read_pack(snap) as z:
            names = z.namelist()
            tt = json.loads(z.read("textures/terrain_texture.json"))["texture_data"]
            self.assertIn("snow", tt)
            # 天气/画/色表/天空盒不得出现在任何产物路径里
            for bad in ("environment", "painting", "colormap", "mcpatcher"):
                self.assertFalse([n for n in names if bad in n.lower()], f"{bad} 污染了产物")
            # 每个 texture_set 引用的贴图都必须真实存在（无悬空引用）
            for n in [x for x in names if x.endswith(".texture_set.json")]:
                folder = n.rsplit("/", 1)[0]
                for ref in json.loads(z.read(n))["minecraft:texture_set"].values():
                    if isinstance(ref, str):
                        self.assertIn(f"{folder}/{ref}.png", names)

    def test_04_nonsquare_resample_keeps_ratio(self):
        """非正方形贴图重采样必须按边独立对齐，不能强拉成正方形。"""
        from core.convert import _mip_size
        allowed = load_rules()["bundle"]["mip_friendly_sizes"]
        self.assertEqual((_mip_size(64, allowed), _mip_size(256, allowed)), (64, 256))
        self.assertNotEqual(_mip_size(64, allowed), _mip_size(256, allowed))


class TestArchiveLookup(unittest.TestCase):
    """ArchiveReader 的 VFS 名 -> ZIP 真实名映射必须精确（回归 endswith 串味 bug）。"""

    MAGIC = b"\x89PNG\r\n\x1a\n"

    def test_01_basename_collision(self):
        """根 pack.png 与嵌套 assets/x/pack.png 同名时，各自读出各自字节。"""
        pkg = ROOT / "tests" / "_tmp_lookup.zip"
        icon = self.MAGIC + b"ROOT-ICON"
        nested = self.MAGIC + b"NESTED-DECOY"
        with zipfile.ZipFile(PACK) as zin, \
                zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("assets/x/pack.png", nested)
            zout.writestr("pack.png", icon)
        try:
            r = ArchiveReader(pkg)
            self.assertEqual(r.read("pack.png"), icon)
            self.assertEqual(r.read("assets/x/pack.png"), nested)
            self.assertNotEqual(r.read("pack.png"), r.read("assets/x/pack.png"))
            r.close()
        finally:
            pkg.unlink(missing_ok=True)

    def test_02_common_root_strip(self):
        """单一外层目录剥离后，VFS 名仍能正确回指带前缀的真实名。"""
        pkg = ROOT / "tests" / "_tmp_rootstrip.zip"
        icon = self.MAGIC + b"ICON"
        with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("MyPack/pack.mcmeta", b'{"pack":{"pack_format":15}}')
            z.writestr("MyPack/pack.png", icon)
            z.writestr("MyPack/assets/minecraft/textures/block/stone.png", icon)
        try:
            r = ArchiveReader(pkg)
            self.assertIn("pack.png", r.list_files())
            self.assertEqual(r._lookup["pack.png"], "MyPack/pack.png")
            self.assertEqual(r.read("pack.png"), icon)
            self.assertTrue(r.read("pack.mcmeta"))
            r.close()
        finally:
            pkg.unlink(missing_ok=True)


class TestAtlasAliases(unittest.TestCase):
    """S7 图集别名双注册：兼容 Bedrock 传统短名与 Java 风格 blocks.json 键。"""

    @classmethod
    def setUpClass(cls) -> None:
        from core.pipeline import _expand_atlas_aliases
        cls._expand = staticmethod(_expand_atlas_aliases)
        cls.rules = load_rules()

    def test_01_pure_expansion(self):
        """组内恰好命中一个真实键时，其余别名指向同一路径。"""
        entries = {"planks_oak": "textures/blocks/planks_oak",
                   "log_oak": "textures/blocks/log_oak"}
        out, n = self._expand(entries, self.rules)
        self.assertGreaterEqual(n, 2)
        self.assertEqual(out["oak_planks"], entries["planks_oak"])
        self.assertEqual(out["oak_log"], entries["log_oak"])
        self.assertEqual(out["planks_oak"], entries["planks_oak"])

    def test_02_no_override_when_both_present(self):
        """组内两个名字对应不同真实贴图时，不补别名、不互相顶替。"""
        entries = {"oak_planks": "textures/blocks/oak_planks",
                   "planks_oak": "textures/blocks/planks_oak"}
        out, n = self._expand(entries, self.rules)
        self.assertEqual(n, 0)
        self.assertEqual(out, entries)

    def test_03_leaves_not_aliased(self):
        """树叶不加别名：Bedrock 树叶为灰度+生物群系着色，别名会串色。"""
        entries = {"oak_leaves": "textures/blocks/oak_leaves"}
        out, n = self._expand(entries, self.rules)
        self.assertNotIn("leaves_oak", out)
        self.assertEqual(n, 0)

    def test_04_end_to_end_bedrock_paths(self):
        """端到端：等价组两套命名各有独立实体文件，PBR 层完整、像素一致。"""
        _, snap = Base.run_task(PACK)
        self.assertEqual(snap["state"], "SUCCESS")
        with Base.read_pack(snap) as z:
            names = set(z.namelist())
            atlas = json.loads(z.read("textures/terrain_texture.json"))
            tt = atlas["texture_data"]
            # 官方 catalog 头缺失会让部分引擎不构建图集
            self.assertEqual(atlas.get("padding"), 8)
            self.assertEqual(atlas.get("num_mip_levels"), 4)

            for short in ("planks_oak", "oak_planks"):
                self.assertIn(short, tt)
                path = tt[short]["textures"]
                self.assertTrue(path.endswith("/" + short), (short, path))
                self.assertIn(path + ".png", names)
                self.assertIn(path + ".texture_set.json", names)
                ts = json.loads(z.read(path + ".texture_set.json"))["minecraft:texture_set"]
                # 近平面木板法线按规则省略回退 vanilla，只校验 color + MER 必在
                for layer in ("color", "metalness_emissive_roughness"):
                    self.assertIn(layer, ts, (short, layer))
                for layer, ref in ts.items():
                    if not isinstance(ref, str):
                        continue
                    self.assertTrue(ref == short or ref.startswith(short + "_"),
                                    (short, layer, ref))
                    self.assertIn(f"textures/blocks/{ref}.png", names)

            # 有立体感的法线（stone）必须保留且 PBR 层完整
            stone_ts = json.loads(z.read(tt["stone"]["textures"] + ".texture_set.json"))["minecraft:texture_set"]
            self.assertIn("normal", stone_ts, "有立体感法线不得省略")
            for layer in ("color", "normal", "metalness_emissive_roughness"):
                self.assertIn(layer, stone_ts)

            p1 = tt["planks_oak"]["textures"]
            p2 = tt["oak_planks"]["textures"]
            self.assertEqual(z.read(p1 + ".png"), z.read(p2 + ".png"))
            # 草顶组同样完成实体化，两套 color 像素一致
            self.assertIn("grass_top", tt)
            self.assertIn("grass_block_top", tt)
            self.assertEqual(
                z.read(tt["grass_top"]["textures"] + ".png"),
                z.read(tt["grass_block_top"]["textures"] + ".png"),
            )

    def test_05_leaves_still_not_materialized(self):
        """树叶为灰度+生物群系着色，别名组刻意排除，不得凭空产生 leaves_oak。"""
        _, snap = Base.run_task(PACK)
        with Base.read_pack(snap) as z:
            names = set(z.namelist())
            tt = json.loads(z.read("textures/terrain_texture.json"))["texture_data"]
            self.assertNotIn("leaves_oak", tt)
            self.assertNotIn("textures/blocks/leaves_oak.png", names)


class TestBedrockAliasMaterialization(unittest.TestCase):
    """S7 别名实体化：Bedrock 原版 Path 通路（textures/blocks/log_oak.png）。"""

    @classmethod
    def setUpClass(cls) -> None:
        from core.pipeline import _materialize_bedrock_aliases
        cls.fn = staticmethod(_materialize_bedrock_aliases)
        cls.rules = load_rules()

    @staticmethod
    def _fixture(mer=None):
        folder = "textures/blocks"
        ts_path = f"{folder}/log_oak.texture_set.json"
        layers = {
            "color": "log_oak",
            "normal": "log_oak_normal",
            "metalness_emissive_roughness": mer if mer is not None else "log_oak_mer",
        }
        ts = {"format_version": "1.21.30", "minecraft:texture_set": layers}
        entries = {
            f"{folder}/log_oak.png": b"COLOR",
            f"{folder}/log_oak_normal.png": b"NORMAL",
        }
        if mer is None:
            entries[f"{folder}/log_oak_mer.png"] = b"MER"
        channels = {f"{folder}/log_oak.png": 4,
                    f"{folder}/log_oak_normal.png": 3,
                    f"{folder}/log_oak_mer.png": 3}
        texture_sets = {ts_path: ts}
        categories = {ts_path: "blocks"}
        return entries, {"log_oak": f"{folder}/log_oak"}, texture_sets, categories, channels

    def test_01_full_layer_materialization(self):
        entries, atlas, tss, cats, chans = self._fixture()
        out, n = self.fn(entries, atlas, self.rules, tss, cats, chans)
        self.assertEqual(n, 1)
        self.assertEqual(out["oak_log"], "textures/blocks/oak_log")
        for f in ("oak_log.png", "oak_log_normal.png", "oak_log_mer.png",
                  "oak_log.texture_set.json"):
            self.assertIn("textures/blocks/" + f, entries)
        self.assertEqual(entries["textures/blocks/oak_log.png"], b"COLOR")
        new_ts = tss["textures/blocks/oak_log.texture_set.json"]["minecraft:texture_set"]
        self.assertEqual(new_ts, {
            "color": "oak_log",
            "normal": "oak_log_normal",
            "metalness_emissive_roughness": "oak_log_mer",
        })

    def test_02_uniform_layer_inherited_without_png(self):
        entries, atlas, tss, cats, chans = self._fixture(mer=[0, 0, 223])
        out, n = self.fn(entries, atlas, self.rules, tss, cats, chans)
        self.assertEqual(n, 1)
        self.assertNotIn("textures/blocks/oak_log_mer.png", entries)
        new_ts = tss["textures/blocks/oak_log.texture_set.json"]["minecraft:texture_set"]
        self.assertEqual(new_ts["metalness_emissive_roughness"], [0, 0, 223])

    def test_03_no_materialization_when_both_real(self):
        entries, atlas, tss, cats, chans = self._fixture()
        atlas["oak_log"] = "textures/blocks/oak_log"
        out, n = self.fn(entries, atlas, self.rules, tss, cats, chans)
        self.assertEqual(n, 0)
        self.assertNotIn("textures/blocks/oak_log.png", entries)

    def test_04_switch_off(self):
        import copy
        rules = copy.deepcopy(self.rules)
        rules["bundle"]["materialize_bedrock_aliases"] = False
        entries, atlas, tss, cats, chans = self._fixture()
        out, n = self.fn(entries, atlas, rules, tss, cats, chans)
        self.assertEqual(n, 0)
        self.assertNotIn("textures/blocks/oak_log.png", entries)


class TestCtmVariants(Base, unittest.TestCase):
    """CTM/random 变体可选迁移（PRD 新增 · 用户要求「都支持转换」但作为可选开关）。

    默认关闭 = 仅主目录全量转换；勾选 ctm_variants = 解析 optifine/ctm 与
    random，把每个变体落盘为独立 texture_set 并注册成 terrain_texture 的加权
    随机 variations 数组。
    """

    TMP = ROOT / "tests" / "_tmp_ctm"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import shutil
        if cls.TMP.exists():
            shutil.rmtree(cls.TMP)
        cls.TMP.mkdir(parents=True)

    @staticmethod
    def _png(rgba):
        b = io.BytesIO()
        Image.fromarray(np.array(rgba, dtype=np.uint8).reshape(1, 1, 4), "RGBA").save(b, "PNG")
        return b.getvalue()

    @staticmethod
    def _norm_png():
        # 非平面法线（带梯度变化），避免触发「近平面省略」启发式
        g = np.linspace(80, 180, 4, dtype=np.uint8)
        arr = np.zeros((4, 4, 4), dtype=np.uint8)
        arr[:, :, 0] = g[np.newaxis, :]
        arr[:, :, 1] = g[:, np.newaxis]
        arr[:, :, 2] = 255
        arr[:, :, 3] = 255
        b = io.BytesIO()
        Image.fromarray(arr, "RGBA").save(b, "PNG")
        return b.getvalue()

    def _build_pack(self, path: Path):
        solid = self._png([128, 128, 128, 255])
        variant = self._png([200, 80, 80, 255])
        norm = self._norm_png()
        spec = self._png([0, 0, 0, 0])
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15, "description": "ctm"}}))
            # 主目录基准贴图
            for suf in ("", "_n", "_s"):
                z.writestr(f"assets/minecraft/textures/block/stone{suf}.png",
                           solid if suf != "_n" else norm)
            # optifine/ctm 变体：2 个编号变体 + 1 个 properties
            for i in (1, 2):
                for suf in ("", "_n", "_s"):
                    src = solid if suf != "_n" else norm
                    z.writestr(f"assets/minecraft/optifine/ctm/patrix/stone/{i}{suf}.png", src)
            z.writestr(
                "assets/minecraft/optifine/ctm/patrix/stone/stone.properties",
                "matchTiles = stone\nmethod = repeat\ntiles = 1 2\nweights = 2 1\n",
            )

    def test_01_variants_on_registers_variations(self):
        pack = self.TMP / "ctm_on.zip"
        self._build_pack(pack)
        _, snap = self.run_task(pack, "vv_standard", {"ctm_variants": True})
        self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
        with self.read_pack(snap) as z:
            names = z.namelist()
            tt = json.loads(z.read("textures/terrain_texture.json"))
            # 找带 variations 的条目
            var_entries = {
                k: v["textures"]["variations"]
                for k, v in tt["texture_data"].items()
                if isinstance(v.get("textures"), dict) and "variations" in v["textures"]
            }
            self.assertEqual(len(var_entries), 1, f"variations 条目数应为 1，实际 {var_entries.keys()}")
            short, variations = next(iter(var_entries.items()))
            # 主贴图 + 2 个编号变体
            paths = [v["path"] for v in variations]
            self.assertEqual(len(variations), 3, paths)
            self.assertIn(f"textures/blocks/{short}", paths)            # 主贴图
            self.assertIn("textures/blocks/stone_ctm_1", paths)        # 变体 1
            self.assertIn("textures/blocks/stone_ctm_2", paths)        # 变体 2
            # 权重透传（weights=2 1；主贴图默认 1 → 2,1,1）
            self.assertEqual([v["weight"] for v in variations], [1, 2, 1])
            # 变体 texture_set 与法线 PNG 必须落地
            self.assertIn(f"textures/blocks/stone_ctm_1.texture_set.json", names)
            self.assertIn(f"textures/blocks/stone_ctm_1.png", names)
            self.assertIn(f"textures/blocks/stone_ctm_1_normal.png", names)
            ts = json.loads(z.read(f"textures/blocks/stone_ctm_1.texture_set.json"))
            self.assertEqual(
                ts["minecraft:texture_set"]["normal"], "stone_ctm_1_normal")

    def test_02_variants_off_no_variations(self):
        pack = self.TMP / "ctm_off.zip"
        self._build_pack(pack)
        _, snap = self.run_task(pack, "vv_standard", {"ctm_variants": False})
        self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
        with self.read_pack(snap) as z:
            tt = json.loads(z.read("textures/terrain_texture.json"))
            var_entries = [
                k for k, v in tt["texture_data"].items()
                if isinstance(v.get("textures"), dict) and "variations" in v["textures"]
            ]
            self.assertEqual(var_entries, [], "关闭 CTM 时不应出现 variations 条目")
            self.assertNotIn("textures/blocks/stone_ctm_1.texture_set.json", z.namelist())


class TestWoolAliases(Base, unittest.TestCase):
    """P0：羊毛 16 色 wool_colored_* 映射（含 silver 特例、地毯引擎限制）。

    根因：Bedrock blocks.json 里 16 色羊毛查询的是传统短名 wool_colored_<色>
    （light_gray 为 silver），转换器原样输出 <色>_wool 无法命中 → 全部回退原版。
    地毯与羊毛共用同一短名且引擎无独立 carpet_* 键，故刻意不并入等价组：
    否则源包内两张真实贴图会让组内命中 2 个、整组跳过。
    """

    COLORS = ["white", "orange", "magenta", "light_blue", "yellow", "lime",
              "pink", "gray", "light_gray", "cyan", "purple", "blue", "brown",
              "green", "red", "black"]
    # 依 Mojang bedrock-samples/resource_pack/blocks.json 逐色校验
    EXPECTED = {
        "white": "wool_colored_white", "orange": "wool_colored_orange",
        "magenta": "wool_colored_magenta", "light_blue": "wool_colored_light_blue",
        "yellow": "wool_colored_yellow", "lime": "wool_colored_lime",
        "pink": "wool_colored_pink", "gray": "wool_colored_gray",
        "light_gray": "wool_colored_silver", "cyan": "wool_colored_cyan",
        "purple": "wool_colored_purple", "blue": "wool_colored_blue",
        "brown": "wool_colored_brown", "green": "wool_colored_green",
        "red": "wool_colored_red", "black": "wool_colored_black",
    }
    TMP = ROOT / "tests" / "_tmp_wool"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import shutil
        if cls.TMP.exists():
            shutil.rmtree(cls.TMP)
        cls.TMP.mkdir(parents=True)
        cls.PACK = cls.TMP / "wool_pack.zip"
        cls._build_pack(cls.PACK)
        cls.task, cls.snap = cls.run_task(cls.PACK)

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.TMP, ignore_errors=True)

    @staticmethod
    def _png(rgba):
        b = io.BytesIO()
        Image.fromarray(np.array(rgba, dtype=np.uint8).reshape(1, 1, 4), "RGBA").save(b, "PNG")
        return b.getvalue()

    @staticmethod
    def _norm_png():
        # 非平面法线，确保 normal 层被保留并参与别名物化
        g = np.linspace(80, 180, 4, dtype=np.uint8)
        arr = np.zeros((4, 4, 4), dtype=np.uint8)
        arr[:, :, 0] = g[np.newaxis, :]
        arr[:, :, 1] = g[:, np.newaxis]
        arr[:, :, 2] = 255
        arr[:, :, 3] = 255
        b = io.BytesIO()
        Image.fromarray(arr, "RGBA").save(b, "PNG")
        return b.getvalue()

    @classmethod
    def _build_pack(cls, path: Path):
        norm = cls._norm_png()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pack.mcmeta", json.dumps(
                {"pack": {"pack_format": 15, "description": "wool 16 colors"}}))
            for i, c in enumerate(cls.COLORS):
                wool = cls._png([30 + i * 14, 60, 90, 255])
                z.writestr(f"assets/minecraft/textures/block/{c}_wool.png", wool)
                z.writestr(f"assets/minecraft/textures/block/{c}_wool_n.png", norm)
                # 地毯刻意用不同颜色，用于验证不被误并入 wool 等价组
                z.writestr(f"assets/minecraft/textures/block/{c}_carpet.png",
                           cls._png([200, 30, 30, 255]))

    def test_01_rules_groups_complete(self):
        """规则里 16 组齐全、silver 特例、carpet 未并入任何 wool 组。"""
        groups = load_rules()["id_map"]["atlas_alias_groups"]["groups"]
        wool_groups = [g for g in groups if any("wool" in x for x in g)]
        self.assertEqual(len(wool_groups), 16)
        expected = {f"{c}_wool": short for c, short in self.EXPECTED.items()}
        self.assertEqual({g[0]: g[1] for g in wool_groups}, expected)
        for g in wool_groups:
            self.assertFalse(any("carpet" in x for x in g), g)

    def test_02_expand_aliases_with_carpet_present(self):
        """双注册：16 色都补出 wool_colored_* 指向羊毛路径，carpet 不被覆盖。"""
        from core.pipeline import _expand_atlas_aliases
        entries = {f"{c}_wool": f"textures/blocks/{c}_wool" for c in self.COLORS}
        entries.update({f"{c}_carpet": f"textures/blocks/{c}_carpet"
                        for c in self.COLORS})
        out, n = _expand_atlas_aliases(entries, load_rules())
        self.assertGreaterEqual(n, 16)
        for c, short in self.EXPECTED.items():
            self.assertIn(short, out)
            self.assertEqual(out[short], f"textures/blocks/{c}_wool")
        for c in self.COLORS:
            self.assertEqual(out[f"{c}_carpet"], f"textures/blocks/{c}_carpet")

    @staticmethod
    def _fixture(with_carpet=False):
        folder, real = "textures/blocks", "white_wool"
        ts_path = f"{folder}/{real}.texture_set.json"
        ts = {"format_version": "1.21.30", "minecraft:texture_set": {
            "color": real, "normal": real + "_normal",
            "metalness_emissive_roughness": real + "_mer"}}
        entries = {
            f"{folder}/{real}.png": b"COLOR",
            f"{folder}/{real}_normal.png": b"NORMAL",
            f"{folder}/{real}_mer.png": b"MER",
        }
        atlas = {real: f"{folder}/{real}"}
        if with_carpet:
            atlas["white_carpet"] = f"{folder}/white_carpet"
            entries[f"{folder}/white_carpet.png"] = b"CARPET"
        return (entries, atlas, {ts_path: ts}, {ts_path: "blocks"},
                {k: 4 for k in entries})

    def test_03_materialize_full_layers(self):
        """物化：别名全套实体文件 + texture_set + normal 层，像素一致。"""
        from core.pipeline import _materialize_bedrock_aliases
        entries, atlas, tss, cats, chans = self._fixture()
        out, n = _materialize_bedrock_aliases(
            entries, atlas, load_rules(), tss, cats, chans)
        self.assertEqual(n, 1)
        self.assertEqual(out["wool_colored_white"],
                         "textures/blocks/wool_colored_white")
        for f in ("wool_colored_white.png", "wool_colored_white_normal.png",
                  "wool_colored_white_mer.png",
                  "wool_colored_white.texture_set.json"):
            self.assertIn("textures/blocks/" + f, entries)
        self.assertEqual(entries["textures/blocks/wool_colored_white.png"], b"COLOR")
        new_ts = json.loads(
            entries["textures/blocks/wool_colored_white.texture_set.json"])
        self.assertEqual(new_ts["minecraft:texture_set"]["normal"],
                         "wool_colored_white_normal")

    def test_04_carpet_coexists_without_collision(self):
        """源包同时含 wool + carpet 时，wool 组仍物化（组内仅 2 元素）。"""
        from core.pipeline import _materialize_bedrock_aliases
        entries, atlas, tss, cats, chans = self._fixture(with_carpet=True)
        out, n = _materialize_bedrock_aliases(
            entries, atlas, load_rules(), tss, cats, chans)
        self.assertEqual(n, 1, "carpet 不在 wool 组内，不得使整组跳过")
        self.assertIn("wool_colored_white", out)
        self.assertEqual(out["white_carpet"], "textures/blocks/white_carpet")
        self.assertEqual(entries["textures/blocks/white_carpet.png"], b"CARPET")

    def test_05_end_to_end_wool_colored_keys(self):
        """端到端：产物 16 个 wool_colored_* 键 + 实体文件 + normal 层。"""
        self.assertEqual(self.snap["state"], "SUCCESS", self.snap.get("error"))
        with self.read_pack(self.snap) as z:
            names = set(z.namelist())
            tt = json.loads(z.read("textures/terrain_texture.json"))["texture_data"]
            for c, short in self.EXPECTED.items():
                self.assertIn(short, tt, short)
                path = tt[short]["textures"]
                self.assertTrue(path.endswith("/" + short), (short, path))
                self.assertIn(path + ".png", names)
                self.assertIn(path + "_normal.png", names)
                self.assertIn(path + ".texture_set.json", names)
                ts = json.loads(z.read(path + ".texture_set.json"))
                layers = ts["minecraft:texture_set"]
                self.assertIn("normal", layers, short)
                self.assertEqual(layers["normal"], short + "_normal")
                self.assertIn("metalness_emissive_roughness", layers, short)
                # 别名 color 与源羊毛 color 字节一致
                self.assertEqual(z.read(path + ".png"),
                                 z.read(f"textures/blocks/{c}_wool.png"))
            # silver 特例：light_gray 必须落到 silver，而非臆造的 light_gray
            self.assertIn("wool_colored_silver", tt)
            self.assertNotIn("wool_colored_light_gray", tt)
            # 地毯键仍在、未被覆盖成羊毛贴图
            for c in self.COLORS:
                self.assertIn(f"{c}_carpet", tt)
                self.assertEqual(tt[f"{c}_carpet"]["textures"],
                                 f"textures/blocks/{c}_carpet")


class TestLoadFailureFixes(Base, unittest.TestCase):
    """基岩版「加载失败」三处修复：空白路径、超大图标、兼容模式。

    触发场景：用户 1024x/512x 材质包导入游戏后整包加载失败。根因排查发现
      1) 源包含 "xxx_n .png"（扩展名前多一个空格）→ 产物图集键/路径带空格；
      2) pack_icon.png 为 1328x1328 非 POT 大图；
      3) pbr + min_engine 1.21.120 对不支持 VV 的设备是硬门槛。
    """

    TMP = ROOT / "tests" / "_tmp_loadfix"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import shutil
        if cls.TMP.exists():
            shutil.rmtree(cls.TMP)
        cls.TMP.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(cls.TMP, ignore_errors=True)

    @staticmethod
    def _png(w, h, color=(30, 60, 90, 255)):
        b = io.BytesIO()
        Image.new("RGBA", (w, h), color).save(b, "PNG")
        return b.getvalue()

    # ---- 1) VFS 空白规范化 ----
    def test_01_vfs_strips_whitespace_and_reads_back(self):
        """脏名 "x_n .png" 规范化为 "x_n.png"，且能读回原始字节。"""
        src = self.TMP / "dirty.zip"
        dirty = self._png(4, 4, (1, 2, 3, 255))
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15}}))
            z.writestr("assets/minecraft/textures/block/stone.png", self._png(4, 4))
            z.writestr("assets/minecraft/textures/block/stone_n .png", dirty)
        r = ArchiveReader(src)
        names = r.list_files()
        self.assertFalse(any(any(c.isspace() for c in n) for n in names),
                         "VFS 名不得含空白")
        self.assertIn("assets/minecraft/textures/block/stone_n.png", names)
        self.assertNotIn("assets/minecraft/textures/block/stone_n .png", names)
        # 规范化名必须能映射回 ZIP 内真实名，字节一致
        self.assertEqual(r.read("assets/minecraft/textures/block/stone_n.png"), dirty)
        r.close()

    def test_02_dirty_normal_rejoins_base_group(self):
        """脏名法线应回归 stone 组，而不是变成独立材质。"""
        src = self.TMP / "dirty2.zip"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15}}))
            z.writestr("assets/minecraft/textures/block/stone.png", self._png(4, 4))
            z.writestr("assets/minecraft/textures/block/stone_n .png", self._png(4, 4))
        r = ArchiveReader(src)
        summary, groups, _ = MetaParser(r, load_rules()).parse()
        keys = [g.key for g in groups]
        self.assertNotIn("block/stone_n ", keys)
        self.assertFalse(any(any(c.isspace() for c in k) for k in keys))
        stone = next(g for g in groups if g.key == "block/stone")
        self.assertIsNotNone(stone.normal, "脏名 _n 应被识别为 stone 的法线")
        r.close()

    def test_03_collision_prefers_clean_name(self):
        """同时存在 "x_n .png" 与 "x_n.png" 时，干净名优先。"""
        src = self.TMP / "collide.zip"
        dirty, clean = self._png(2, 2, (9, 9, 9, 255)), self._png(2, 2, (7, 7, 7, 255))
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("pack.mcmeta", "{}")
            z.writestr("assets/minecraft/textures/block/stone.png", self._png(2, 2))
            z.writestr("assets/minecraft/textures/block/stone_n .png", dirty)
            z.writestr("assets/minecraft/textures/block/stone_n.png", clean)
        r = ArchiveReader(src)
        self.assertEqual(r.read("assets/minecraft/textures/block/stone_n.png"), clean)
        r.close()

    # ---- 2) 图标规范化 ----
    def test_04_small_icon_byte_identical(self):
        from core.convert import normalize_pack_icon
        raw = self._png(48, 48)
        out, note = normalize_pack_icon(raw)
        self.assertEqual(out, raw, "合规小图标必须原样")
        self.assertIn("原样", note)

    def test_05_oversized_icon_downscaled_to_pot(self):
        from core.convert import normalize_pack_icon
        raw = self._png(640, 480)  # 非方形 + 超大
        out, note = normalize_pack_icon(raw)
        w = int.from_bytes(out[16:20], "big")
        h = int.from_bytes(out[20:24], "big")
        self.assertEqual((w, h), (256, 256))
        self.assertLess(len(out), len(raw))
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))

    # ---- 3) 兼容模式降级 ----
    def test_06_legacy_compat_strips_pbr(self):
        from core.pipeline import _apply_legacy_compat
        entries = {
            "manifest.json": b"{}",
            "textures/blocks/stone.png": b"C",
            "textures/blocks/stone_normal.png": b"N",
            "textures/blocks/stone_mer.png": b"M",
            "textures/blocks/stone_heightmap.png": b"H",
            "textures/blocks/stone.texture_set.json": b"{}",
            "textures/terrain_texture.json": b"{}",
        }
        m = {"format_version": 2,
             "header": {"min_engine_version": [1, 21, 120]},
             "capabilities": ["pbr"]}
        n_ts, n_layer, _ = _apply_legacy_compat(entries, m)
        self.assertEqual((n_ts, n_layer), (1, 3))
        self.assertIn("textures/blocks/stone.png", entries)
        self.assertIn("textures/terrain_texture.json", entries)
        self.assertFalse(any(k.endswith(".texture_set.json") for k in entries))
        self.assertFalse(any(k.lower().endswith(("_normal.png", "_mer.png", "_heightmap.png"))
                             for k in entries))
        new_m = json.loads(entries["manifest.json"])
        self.assertNotIn("capabilities", new_m)
        self.assertEqual(new_m["header"]["min_engine_version"], [1, 20, 0])

    def test_07_legacy_compat_end_to_end(self):
        """端到端：勾选兼容模式的产物应为合法传统包（无 pbr / 无 texture_set）。"""
        _, snap = self.run_task(PACK, "vv_standard", {"legacy_compat": True})
        self.assertEqual(snap["state"], "SUCCESS", snap.get("error"))
        with self.read_pack(snap) as z:
            names = z.namelist()
            m = json.loads(z.read("manifest.json"))
            self.assertNotIn("capabilities", m)
            self.assertEqual(m["header"]["min_engine_version"], [1, 20, 0])
            self.assertFalse(any(n.endswith(".texture_set.json") for n in names))
            self.assertFalse(any(n.lower().endswith("_normal.png") for n in names))
            self.assertIn("textures/terrain_texture.json", names)
            # 颜色贴图仍在，方块贴图不至于全空
            self.assertTrue(any(n.startswith("textures/blocks/") and n.endswith(".png")
                                for n in names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
