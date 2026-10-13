"""评测命令：读取开发者提供的数据集和评分器，输出本次运行结果与回归对比。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from app.cli.app import cli


@cli.command()
def evaluate(
    dataset: Path,
    scorer: str,
    output: Path,
    baseline: Path | None = None,
    threshold: float | None = None,
    concurrency: int = typer.Option(1, help="并发运行数量，默认串行执行"),
    show_progress: bool = typer.Option(True, help="显示进度信息"),
):
    """读取开发者真实数据集和评分器，输出本次运行与回归结果。

    示例：

    \b
    # 基本评估
    agent-template evaluate \\
        --dataset evals/datasets/my_test.jsonl \\
        --scorer evals.scorers.builtin:exact_match \\
        --output results.json

    \b
    # 并发执行并检查回归
    agent-template evaluate \\
        --dataset evals/datasets/my_test.jsonl \\
        --scorer evals.scorers.builtin:combined \\
        --output results.json \\
        --baseline baseline.json \\
        --threshold 0.05 \\
        --concurrency 4
    """
    from app.services.evaluation import evaluate_dataset

    asyncio.run(
        evaluate_dataset(dataset, scorer, output, baseline, threshold, concurrency, show_progress)
    )
