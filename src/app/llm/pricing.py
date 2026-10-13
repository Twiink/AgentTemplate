"""模型价格表：按服务商与模型名读取单价，用于费用预算与实时计费。

价格不写在模型别名配置里，避免同一模型在多个别名下填写出不一致的单价。
价格表版本用于标记计价基准，配置的 price_version 与读取到的版本不一致时明确报错。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.errors import AppError, Code

# 价格表文件默认位置；相对运行目录，与 config/ 下其他配置一致。
DEFAULT_PRICING_FILE = Path("config/pricing.yaml")


@dataclass(frozen=True)
class Price:
    """一个模型的输入输出单价，单位为美元每百万 token。"""

    input_per_million: float
    output_per_million: float


class PricingTable:
    """已加载的价格表；查询缺失时返回 None，由调用方决定是拒绝还是降级。"""

    def __init__(self, version: str | None, prices: dict[tuple[str, str], Price]):
        self.version = version
        self.prices = prices

    def get(self, provider: str, model: str) -> Price | None:
        """按服务商与模型名查询价格；未收录时返回 None，不猜测单价。"""
        return self.prices.get((provider, model))


def load_pricing(path: Path | None = None) -> PricingTable:
    """读取价格表；文件缺失时返回空表，使未配置价格的模型仍可使用 token 预算。"""
    path = path or DEFAULT_PRICING_FILE
    if not path.exists():
        return PricingTable(None, {})
    content = yaml.safe_load(path.read_text()) or {}
    version = content.get("version")
    prices: dict[tuple[str, str], Price] = {}
    for provider, models in (content.get("pricing") or {}).items():
        for model, entry in (models or {}).items():
            prices[(provider, model)] = Price(float(entry["input"]), float(entry["output"]))
    return PricingTable(str(version) if version is not None else None, prices)


def require_price(table: PricingTable, provider: str, model: str, version: str | None) -> Price:
    """取回价格并校验版本；缺失或版本不符时抛出明确错误，不按 0 计费。"""
    if version is not None and version != table.version:
        raise AppError(
            "pricing_version_mismatch", "价格表版本与模型声明的 price_version 不一致", 503
        )
    price = table.get(provider, model)
    if price is None:
        raise AppError(Code.PRICING_REQUIRED, "价格表未收录该模型，无法计算费用", 503)
    return price
