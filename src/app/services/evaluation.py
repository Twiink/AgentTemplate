"""真实评估运行；数据集、注册实现和评分器由项目提供，报告保留失败与未知用量。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import math
import os
import time
from pathlib import Path
from statistics import mean
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.config import load_settings
from app.core.container import Container
from app.core.errors import AppError, Code
from app.core.security import Authenticator
from app.db.models import Run


class EvaluationCase(BaseModel):
    """每行 JSONL 的评估契约，expected 由评分器解释，不预设业务评价指标。"""

    id: str
    agent_id: str
    input: dict[str, Any]
    expected: Any = None
    options: dict[str, Any] = Field(default_factory=dict)


async def publish_scores(resources, run, scores):
    """关联已保存的真实 trace ID；配置反馈后端时失败准确写入报告。"""
    config = resources.settings.tracing
    trace_id = run.trace_context.get("run_trace_id")
    if not trace_id:
        return {"status": "unconfigured", "message": "运行没有开启 Trace"}
    report: dict[str, Any] = {}
    if config.langfuse_url:
        if not config.langfuse_public_key or not config.langfuse_secret_key:
            raise AppError(Code.FEEDBACK_CONFIGURATION, "Langfuse 反馈认证未配置")
        for name, value in scores.items():
            response = await resources.http[""].post(
                config.langfuse_url.rstrip("/") + "/api/public/scores",
                auth=(
                    config.langfuse_public_key.get_secret_value(),
                    config.langfuse_secret_key.get_secret_value(),
                ),
                json={"name": name, "value": value, "traceId": trace_id, "dataType": "NUMERIC"},
            )
            if response.status_code >= 400:
                raise AppError(Code.FEEDBACK_FAILED, "Langfuse 评分关联失败", 502)
        report["langfuse"] = "submitted"
    if config.langsmith_url:
        if not config.langsmith_api_key:
            raise AppError(Code.FEEDBACK_CONFIGURATION, "LangSmith 反馈认证未配置")
        from langsmith import Client

        client = Client(
            api_url=config.langsmith_url, api_key=config.langsmith_api_key.get_secret_value()
        )
        try:
            for name, value in scores.items():
                await asyncio.to_thread(
                    client.create_feedback,
                    trace_id=UUID(hex=trace_id),
                    key=name,
                    score=value,
                    source_info={"run_id": run.id},
                )
        finally:
            client.close()
        report["langsmith"] = "submitted"
    return report or {"status": "unconfigured"}


async def evaluate_dataset(
    dataset: Path,
    scorer: str,
    output: Path,
    baseline: Path | None,
    threshold: float | None,
    concurrency: int = 1,
    show_progress: bool = True,
):
    """验证身份后执行真实注册项，调用开发者评分器，保存结果、延迟、用量及回归差异。

    支持并发执行和进度显示。
    """
    if not dataset.is_file():
        raise AppError(Code.DATASET_NOT_FOUND, "评估数据集不存在")
    source = dataset.read_bytes()
    cases = [
        EvaluationCase.model_validate_json(line)
        for line in source.decode().splitlines()
        if line.strip()
    ]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise AppError(Code.EVALUATION_CASES, "评估数据集不能为空且 Case ID 必须唯一")
    module, symbol = scorer.split(":", 1)
    fn = getattr(importlib.import_module(module), symbol)
    settings = load_settings()
    credential = os.getenv("AGENT_EVALUATION_TOKEN")
    if not credential and Path(".data/developer.key").is_file():
        credential = Path(".data/developer.key").read_text().strip()
    if not credential:
        raise AppError(Code.EVALUATION_IDENTITY, "评估需要 AGENT_EVALUATION_TOKEN 或私有开发凭据")
    principal = await Authenticator(settings.auth).verify(credential)
    principal.require("resources:write")
    principal.require("resources:read")
    results: list[dict[str, Any]] = []

    async with Container(settings) as resources:
        if resources.background is None:
            raise AppError(Code.EVALUATION_REQUIRES_BACKGROUND, "评测需要启用 background 模块")
        background = resources.background

        # 创建信号量控制并发
        semaphore = asyncio.Semaphore(concurrency)
        completed = 0
        total = len(cases)

        async def run_case(case: EvaluationCase) -> dict[str, Any]:
            """执行单个评估用例。"""
            nonlocal completed
            async with semaphore:
                started = time.monotonic()
                thread = await resources.threads.create(principal)
                submitted = await background.submit(
                    principal, case.agent_id, thread["id"], case.input, case.options
                )
                await resources.runs.execute(submitted["id"])
                # 如果后台 Worker 已先领取，等待同一持久运行，而不是启动第二份评估。
                while True:
                    run = await resources.repository.get(Run, submitted["id"], principal)
                    if run["status"] in {"succeeded", "failed", "cancelled", "waiting_input"}:
                        break
                    if (
                        time.monotonic() - started
                        > settings.run_timeout_seconds + settings.lease_seconds
                    ):
                        raise AppError(Code.EVALUATION_TIMEOUT, "评估运行未在预算内完成")
                    await asyncio.sleep(0.25)
                record: dict[str, Any] = {
                    "id": case.id,
                    "agent_id": case.agent_id,
                    "run_id": run["id"],
                    "status": run["status"],
                    "input": case.input,
                    "expected": case.expected,
                    "output": run["output"],
                    "usage": run["usage"],
                    "latency_seconds": time.monotonic() - started,
                    "error": run["error"],
                    "prompt_versions": run["prompt_versions"],
                    "scores": {},
                    "feedback": {},
                }
                if run["status"] == "succeeded":
                    try:
                        scores = fn(record)
                        if inspect.isawaitable(scores):
                            scores = await scores
                        if not isinstance(scores, dict) or any(
                            not isinstance(v, (float, int)) or not math.isfinite(v)
                            for v in scores.values()
                        ):
                            raise AppError(Code.SCORER_RESULT, "评分器必须返回有限数值的指标字典")
                        record["scores"] = scores
                        async with resources.db.transaction(principal) as session:
                            stored = await session.get(Run, run["id"])
                        record["feedback"] = await publish_scores(resources, stored, scores)
                    except Exception as error:
                        record["evaluation_error"] = {
                            "code": error.code if isinstance(error, AppError) else "scorer_failed",
                            "message": error.message
                            if isinstance(error, AppError)
                            else "评分器执行失败",
                        }

                completed += 1
                if show_progress:
                    print(f"[{completed}/{total}] {case.id}: {record['status']}")

                return record

        # 并发执行所有用例
        if show_progress:
            print(f"开始评估 {total} 个用例（并发度：{concurrency}）...")

        results = await asyncio.gather(*[run_case(case) for case in cases])

    metric_names = {name for row in results for name in row["scores"]}
    metrics = {
        name: mean(row["scores"][name] for row in results if name in row["scores"])
        for name in metric_names
    }
    report: dict[str, Any] = {
        "dataset_sha256": hashlib.sha256(source).hexdigest(),
        "count": len(results),
        "results": results,
        "metrics": metrics,
        "mean_latency_seconds": mean(r["latency_seconds"] for r in results),
        "failed_runs": sum(r["status"] != "succeeded" for r in results),
    }
    if baseline:
        previous = json.loads(baseline.read_text())
        if previous.get("dataset_sha256") != report["dataset_sha256"]:
            raise AppError(Code.EVALUATION_BASELINE, "比较版本必须使用同一数据集")
        differences = {
            k: v - previous["metrics"][k]
            for k, v in metrics.items()
            if k in previous.get("metrics", {})
        }
        report["differences"] = differences
        if threshold is not None:
            report["regression_passed"] = bool(differences) and all(
                v >= -threshold for v in differences.values()
            )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    if show_progress:
        print(f"\n评估完成：{len(results)} 个用例")
        print(f"成功：{sum(r['status'] == 'succeeded' for r in results)}")
        print(f"失败：{report['failed_runs']}")
        if metrics:
            print("\n指标：")
            for k, v in metrics.items():
                print(f"  {k}: {v:.4f}")
        if report.get("differences"):
            print("\n与基线差异：")
            for k, v in report["differences"].items():
                sign = "+" if v > 0 else ""
                print(f"  {k}: {sign}{v:.4f}")

    if report.get("regression_passed") is False:
        raise AppError(Code.EVALUATION_REGRESSION, "评估指标超出回归阈值")
    return report
