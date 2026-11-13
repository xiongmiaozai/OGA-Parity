"""生成端到端演示用的 Java 资源包与光影包样本。

示例包包含：
  - 8 个方块 + 2 个物品，均带 LabPBR _n / _s（sand 故意缺 _s 用于演示 AI 补全）
  - 2 组 mcmeta 动画（自定义逐帧时长 + 等间隔插值）
光影包样本用于验证 FR-L-01 拦截。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
SIZE = 64

# name -> (RGB 基色, smoothness, F0/金属ID, emission 0..1, 帧数, 法线振幅)
# 法线振幅：多数块用近平面 0.09（演示「近平面法线被省略」）；
# stone 用 0.38（明显立体感，演示「真实有细节的法线被保留」）。
MATERIALS = {
    "stone":            ((128, 128, 128), 0.15, 10, 0.0, 1, 0.20),
    "iron_block":       ((198, 198, 202), 0.78, 232, 0.0, 1, 0.09),
    "glowstone":        ((226, 186, 96), 0.32, 10, 0.92, 1, 0.09),
    "grass_block_top":  ((96, 158, 74), 0.20, 10, 0.0, 1, 0.09),
    "diamond_ore":      ((118, 128, 132), 0.35, 10, 0.10, 1, 0.09),
    "oak_planks":       ((166, 126, 78), 0.25, 10, 0.0, 1, 0.09),
    "lava_flow":        ((226, 92, 32), 0.55, 10, 0.85, 4, 0.09),
    "water_flow":       ((48, 112, 208), 0.82, 10, 0.0, 2, 0.09),
    "sand":             ((222, 210, 166), 0.18, 10, 0.0, 1, 0.09),
}


def _texture(name: str, frames: int = 1) -> dict[str, bytes]:
    color, smoothness, f0, emission, _, normal_amp = MATERIALS[name]
    h, w = SIZE * frames, SIZE
    yy, xx = np.mgrid[0:h, 0:w]
    noise = (np.sin(xx * 0.33) * np.cos(yy * 0.27) + 1.0) * 0.5
    grain = ((xx * 7 + yy * 13) % 17 < 2).astype(float) * 0.12

    # basecolor：基色 + 噪声 + 纹理颗粒
    base = np.zeros((h, w, 4), dtype=np.float32)
    for c in range(3):
        base[:, :, c] = color[c] * (0.82 + 0.3 * noise) + grain * 90
    if name == "diamond_ore":
        gem = ((xx % 16 < 5) & (yy % 16 < 5)).astype(float)
        base[:, :, :3] = base[:, :, :3] * (1 - gem[..., None]) + np.array([80, 226, 214]) * gem[..., None]
    if name == "oak_planks":
        seam = (xx % 32 < 2).astype(float)
        base[:, :, :3] *= (1 - 0.35 * seam[..., None])
    if frames > 1:
        for f in range(frames):
            base[f * SIZE:(f + 1) * SIZE, :, :3] *= (0.75 + 0.25 * f / max(1, frames - 1))
    base[:, :, 3] = 255

    # _n：R/G = DirectX 法线 XY，B = AO，A = 高度
    nx = 0.5 + normal_amp * np.sin(xx * 0.28) * np.cos(yy * 0.11)
    ny = 0.5 + normal_amp * np.cos(yy * 0.31)
    ao = np.clip(0.80 + 0.20 * noise, 0, 1)
    height = np.clip(0.55 + 0.35 * noise, 0, 1)
    normal_map = np.stack([nx, ny, ao, height], axis=-1)

    # _s：R = 感知平滑度，G = F0/金属ID，B = porosity
    #     A = 自发光，LabPBR 1.3 为正向编码：0=0% 发光、254=100% 发光、
    #         255 是「RGB 无 alpha 默认」的保留值（不发光）。 emitting 0..1 -> 0..254
    spec = np.zeros((h, w, 4), dtype=np.float32)
    spec[:, :, 0] = smoothness
    spec[:, :, 1] = f0 / 255.0
    spec[:, :, 2] = 0.0
    spec[:, :, 3] = round(emission * 254) / 255.0

    def enc(arr: np.ndarray, mode: str) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), mode=mode).save(buf, format="PNG")
        return buf.getvalue()

    return {
        "color": enc(base, "RGBA"),
        "normal": enc(normal_map * 255.0, "RGBA"),
        "specular": enc(spec * 255.0, "RGBA"),
    }


def build_java_pack(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps({
            "pack": {
                "pack_format": 15,
                "description": "Demo Java LabPBR 1.3 Resource Pack - 用于转换工具端到端演示",
            }
        }, ensure_ascii=False, indent=2))
        for name, (_, _, _, _, frames, _) in MATERIALS.items():
            tex = _texture(name, frames)
            base = f"assets/minecraft/textures/block/{name}"
            zf.writestr(f"{base}.png", tex["color"])
            zf.writestr(f"{base}_n.png", tex["normal"])
            if name != "sand":  # 故意缺失 _s，演示 AI 补全与报告提示
                zf.writestr(f"{base}_s.png", tex["specular"])
            if name == "lava_flow":
                zf.writestr(f"{base}.png.mcmeta", json.dumps({
                    "animation": {
                        "frametime": 2,
                        "frames": [
                            {"index": 0, "time": 4}, {"index": 1, "time": 2},
                            {"index": 2, "time": 4}, {"index": 3, "time": 2},
                        ],
                    }
                }, indent=2))
            if name == "water_flow":
                zf.writestr(f"{base}.png.mcmeta", json.dumps({
                    "animation": {"frametime": 3, "frames": [0, 1], "interpolate": True}
                }, indent=2))

        for item, rgb in (("apple", (206, 48, 44)), ("iron_ingot", (206, 206, 210))):
            yy, xx = np.mgrid[0:SIZE, 0:SIZE]
            arr = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
            for c in range(3):
                arr[:, :, c] = np.clip(rgb[c] * (0.8 + 0.3 * ((xx + yy) % 9 < 4)), 0, 255)
            arr[:, :, 3] = 255
            b = io.BytesIO()
            Image.fromarray(arr, "RGBA").save(b, format="PNG")
            ibase = f"assets/minecraft/textures/item/{item}"
            zf.writestr(f"{ibase}.png", b.getvalue())
            # 物品也给一套 LabPBR（用于验证 items 强制 normal）
            n = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
            n[:, :, 0], n[:, :, 1], n[:, :, 2], n[:, :, 3] = 128, 128, 235, 210
            s = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
            s[:, :, 0], s[:, :, 1], s[:, :, 2], s[:, :, 3] = 60, 233, 0, 255
            for suffix, a in (("_n", n), ("_s", s)):
                nb = io.BytesIO()
                Image.fromarray(a, "RGBA").save(nb, format="PNG")
                zf.writestr(f"{ibase}{suffix}.png", nb.getvalue())
    return path


def build_shaderpack(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15, "description": "BSL-like shaderpack demo"}}))
        zf.writestr("shaders/gbuffers_terrain.vsh", "#version 330\nvoid main() { gl_Position = vec4(0.0); }\n")
        zf.writestr("shaders/final.fsh", "#version 330\nout vec4 c; void main() { c = vec4(1.0); }\n")
        zf.writestr("shaders/composite.properties", "uniform float viewWidth;\n")
        zf.writestr("shaders/lang/en_US.lang", "screen.VS=VS\n")
    return path


if __name__ == "__main__":
    a = build_java_pack(HERE / "demo_java_pack.zip")
    b = build_shaderpack(HERE / "demo_shaderpack.zip")
    print("示例资源包:", a, a.stat().st_size, "bytes")
    print("光影包样本:", b, b.stat().st_size, "bytes")
