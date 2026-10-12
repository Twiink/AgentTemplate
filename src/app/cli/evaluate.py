"""评测命令：读取开发者提供的数据集和评分器，输出本次运行结果与回归对比。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.cli.app import cli


@cli.command()
def evaluate(
    dataset: Path,
    scorer: str,
    output: Path,
    baseline: Path | None = None,
    threshold: float | None = None,
):
    """读取开发者真实数据集和评分器，输出本次运行与回归结果。"""
    from app.services.evaluation import evaluate_dataset

    asyncio.run(evaluate_dataset(dataset, scorer, output, baseline, threshold))
