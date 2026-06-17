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
    # 三个高度相似的版本（共享大量正文，仅个别数字/措辞差异）——演示版本归组
    _p1 = "Q3 算力方案 封面 项目代号 启明 汇报人 张工 第三季度 算力 基础设施 扩容 专项 立项 评审 材料"
    _p2 = ("背景 当前 昇腾 910B 集群 部署 需求 持续 增长 业务侧 大模型 训练 推理 算力 缺口 明显 "
           "现有 集群 利用率 长期 高于 百分之八十五 亟需 扩容 以 支撑 下一阶段 研发 与 生产 上线")

    def _p3(scale: str, n: str, tail: str) -> str:
        return (f"方案 建议 扩容 算力 集群 至 {scale} 规模 采购 昇腾 服务器 {n} 台 预算 评估 "
                f"实施 周期 约 三个月 人力 投入 五人 风险 整体 可控 分阶段 交付 {tail}")

    fx.make_pptx(OUT / "Q3算力方案_v1.pptx", [
        {"body": _p1}, {"body": _p2},
        {"body": _p3("5000P", "两百", "初稿"), "notes": "备注 预算 待确认 方案 待 评审"},
    ])
    fx.make_pptx(OUT / "Q3算力方案_v2.pptx", [
        {"body": _p1}, {"body": _p2},
        {"body": _p3("6000P", "两百四", "修订"), "notes": "备注 预算 已批 规模 上调"},
    ])
    fx.make_pptx(OUT / "Q3算力方案_终稿.pptx", [
        {"body": _p1}, {"body": _p2},
        {"body": _p3("6000P", "两百四", "终稿"), "notes": "备注 已 定稿 准备 汇报"},
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
