"""时间工具：获取当前时间、格式化、解析。"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from app.tools.governance import governed_tool


class CurrentTimeParams(BaseModel):
    """获取当前时间参数。"""

    timezone: str = Field(
        default="UTC", description="时区，如 'UTC'、'Asia/Shanghai'、'America/New_York'"
    )


class FormatTimeParams(BaseModel):
    """格式化时间参数。"""

    timestamp: float = Field(description="Unix 时间戳（秒）")
    format: str = Field(
        default="%Y-%m-%d %H:%M:%S", description="格式字符串，如 '%Y-%m-%d %H:%M:%S'"
    )
    timezone: str = Field(default="UTC", description="时区，如 'UTC'、'Asia/Shanghai'")


class ParseTimeParams(BaseModel):
    """解析时间参数。"""

    time_string: str = Field(description="时间字符串，如 '2024-01-01 12:00:00'")
    format: str = Field(
        default="%Y-%m-%d %H:%M:%S", description="格式字符串，如 '%Y-%m-%d %H:%M:%S'"
    )
    timezone: str = Field(default="UTC", description="时区，如 'UTC'、'Asia/Shanghai'")


@governed_tool(scopes=["tools:time"])
async def current_time(params: CurrentTimeParams) -> dict:
    """获取指定时区的当前时间。

    返回 ISO 格式、时间戳、各个字段和星期几。
    """
    try:
        tz = ZoneInfo(params.timezone)
    except ZoneInfoNotFoundError:
        return {
            "error": "invalid_timezone",
            "message": f"时区 '{params.timezone}' 不存在",
            "suggestion": "使用标准 IANA 时区名称，如 'Asia/Shanghai'、'America/New_York'",
        }

    now = datetime.now(tz)

    return {
        "timezone": params.timezone,
        "iso": now.isoformat(),
        "timestamp": now.timestamp(),
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "hour": now.hour,
        "minute": now.minute,
        "second": now.second,
        "weekday": now.strftime("%A"),
        "formatted": now.strftime("%Y-%m-%d %H:%M:%S"),
    }


@governed_tool(scopes=["tools:time"])
async def format_time(params: FormatTimeParams) -> dict:
    """格式化 Unix 时间戳为指定格式和时区的时间字符串。"""
    try:
        tz = ZoneInfo(params.timezone)
    except ZoneInfoNotFoundError:
        return {
            "error": "invalid_timezone",
            "message": f"时区 '{params.timezone}' 不存在",
        }

    try:
        dt = datetime.fromtimestamp(params.timestamp, tz)
        formatted = dt.strftime(params.format)

        return {
            "timestamp": params.timestamp,
            "timezone": params.timezone,
            "format": params.format,
            "formatted": formatted,
            "iso": dt.isoformat(),
        }
    except ValueError as e:
        return {
            "error": "invalid_timestamp",
            "message": f"时间戳无效: {e}",
        }
    except Exception as e:
        return {
            "error": "format_failed",
            "message": f"格式化失败: {e}",
        }


@governed_tool(scopes=["tools:time"])
async def parse_time(params: ParseTimeParams) -> dict:
    """解析时间字符串为 Unix 时间戳。

    根据指定的格式和时区解析时间字符串。
    """
    try:
        tz = ZoneInfo(params.timezone)
    except ZoneInfoNotFoundError:
        return {
            "error": "invalid_timezone",
            "message": f"时区 '{params.timezone}' 不存在",
        }

    try:
        # 解析为 naive datetime
        dt_naive = datetime.strptime(params.time_string, params.format)
        # 添加时区信息
        dt = dt_naive.replace(tzinfo=tz)

        return {
            "time_string": params.time_string,
            "format": params.format,
            "timezone": params.timezone,
            "timestamp": dt.timestamp(),
            "iso": dt.isoformat(),
            "year": dt.year,
            "month": dt.month,
            "day": dt.day,
            "hour": dt.hour,
            "minute": dt.minute,
            "second": dt.second,
        }
    except ValueError as e:
        return {
            "error": "parse_failed",
            "message": f"解析失败: {e}",
            "suggestion": "检查时间字符串和格式是否匹配",
        }
    except Exception as e:
        return {
            "error": "unknown_error",
            "message": f"未知错误: {e}",
        }
