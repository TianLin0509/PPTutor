"""生成演示用 pptx（多版本 / 备注 / SmartArt / 旧 .ppt）到 demo_decks/。

供真实 E2E 与截图使用，也可让用户先拿这批样本试用。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import fixtures_gen as fx  # noqa: E402

OUT = ROOT / "demo_decks"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    fx.make_pptx(OUT / "Q3算力方案_v1.pptx", [
        {"body": "Q3 算力方案 封面"},
        {"body": "背景：昇腾 910B 集群部署需求"},
        {"body": "方案：扩容 算力 集群 至 5000P 规模", "notes": "备注：预算待确认，方案待评审"},
    ])
    fx.make_pptx(OUT / "Q3算力方案_v2.pptx", [
        {"body": "Q3 算力方案 封面"},
        {"body": "背景：昇腾 910B 集群部署需求（更新版）"},
        {"body": "方案：扩容 算力 集群 至 6000P 规模", "notes": "备注：预算已批"},
    ])
    fx.make_pptx(OUT / "Q3算力方案_终稿.pptx", [
        {"body": "Q3 算力方案 封面"},
        {"body": "背景：昇腾 910B 集群部署需求（终稿）"},
        {"body": "方案：扩容 算力 集群 至 6000P 规模 终稿", "notes": "备注：已定稿，准备汇报"},
    ])
    fx.make_pptx(OUT / "周报模板.pptx", [
        {"body": "本周进展"},
        {"body": "本周 算力 集群 利用率 78%", "notes": "下周计划：优化调度策略"},
    ])
    p = fx.make_pptx(OUT / "客户汇报材料.pptx", [
        {"body": "客户汇报 封面"},
        {"body": "系统架构概览"},
        {"body": "性能指标与算力规划"},
    ])
    fx.inject_smartart(p, 2, "SmartArt 架构图：算力 调度 引擎 关键模块")
    # 旧格式 .ppt：仅文件名可搜
    (OUT / "历史_算力规划2024.ppt").write_bytes(b"\xd0\xcf\x11\xe0 legacy ppt binary stub")

    print("demo decks ->", OUT)
    for f in sorted(OUT.glob("*")):
        print("  ", f.name)


if __name__ == "__main__":
    main()
