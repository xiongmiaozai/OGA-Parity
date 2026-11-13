"""命令行转换入口（FR-C-10 / NFR-15：内核可独立于 UI 做 golden 测试）。

用法：
  python3 cli.py samples/demo_java_pack.zip --preset vv_standard
  python3 cli.py samples/demo_java_pack.zip --preset vv_heightmap --ai
  python3 cli.py samples/demo_shaderpack.zip
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.pipeline import MANAGER  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="MC Java -> Bedrock Vibrant Visuals 转换器 CLI")
    ap.add_argument("source", help="Java 资源包 ZIP 或文件夹")
    ap.add_argument("--preset", default="vv_standard")
    ap.add_argument("--bump", choices=["normal", "heightmap"], default=None)
    ap.add_argument("--ai", action="store_true", help="开启本地 AI 补全（默认关闭）")
    ap.add_argument("--ao", type=float, default=None, help="AO 烘焙强度 0~1")
    ap.add_argument("--max-size", type=int, default=None)
    ap.add_argument("--task-timeout", type=float, default=None)
    ap.add_argument("--json", action="store_true", help="仅输出 JSON 结果")
    args = ap.parse_args()

    overrides = {k: v for k, v in {
        "bump": args.bump, "ai_repair": args.ai, "ao_bake": args.ao,
        "max_size": args.max_size, "task_timeout_s": args.task_timeout,
    }.items() if v is not None}

    task = MANAGER.create(args.source, args.preset, overrides)
    MANAGER.start(task.id)
    last = -1
    while task.thread and task.thread.is_alive():
        snap = MANAGER.snapshot(task.id)
        pct = int(snap["progress"] * 100)
        if pct != last and not args.json:
            print(f"\r  [{pct:3d}%] {snap['stage']:<16} 已耗时 {snap['elapsed_text']} ETA {snap['eta_text']}", end="", flush=True)
            last = pct
        time.sleep(0.15)
    if task.thread:
        task.thread.join()

    snap = MANAGER.snapshot(task.id)
    report = snap.get("report") or {}
    result = {
        "task_id": task.id,
        "state": snap["state"],
        "elapsed": snap["elapsed_text"],
        "output": snap["output_name"],
        "checks_passed": report.get("passed"),
        "checks": [c["item"] for c in report.get("checks", []) if not c["ok"]],
        "mapped": len(report.get("mapped", [])),
        "unmapped": report.get("unmapped", []),
        "downgraded": report.get("downgraded", [])[:5],
        "shaderpack_blocked": bool(report.get("shaderpack")),
        "error": snap["error"],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if snap["state"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
