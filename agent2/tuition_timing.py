"""Count back from a school payment deadline to concrete FX-purchase and
wire-remittance dates.

The countdown uses conservative working-day buffers (holidays are NOT
skipped — a holiday only pushes the real deadline earlier, so ignoring
them is the safe direction):

- ``FX_PROCESSING_DAYS``: bank-side purchase/processing time before the
  wire can be sent.
- ``WIRE_TRANSIT_DAYS``: cross-border wire in-transit time before the
  school receives the funds.

Both feed two customer surfaces:

- ``timing_step`` — the deterministic FX answer line ("最晚 X 前完成购汇…").
- ``timing_todos`` — idempotent plan rows keyed by deterministic item ids,
  so a tuition turn refreshes wording after a deadline change instead of
  duplicating rows, and a cleared deadline removes the derived todos.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

FX_PROCESSING_DAYS = 2  # 购汇办理/审核缓冲（工作日）
WIRE_TRANSIT_DAYS = 5   # 跨境电汇在途缓冲（工作日）


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _minus_business_days(start: date, days: int) -> date:
    current = start
    remaining = days
    while remaining > 0:
        current -= timedelta(days=1)
        if current.weekday() < 5:  # Mon-Fri
            remaining -= 1
    return current


def tuition_timing(deadline_value, *, today: date | None = None) -> dict | None:
    deadline = _parse_date(deadline_value)
    if deadline is None:
        return None
    wire_by = _minus_business_days(deadline, WIRE_TRANSIT_DAYS)
    fx_by = _minus_business_days(wire_by, FX_PROCESSING_DAYS)
    return {
        "deadline": deadline,
        "wire_by": wire_by,
        "fx_by": fx_by,
        "urgent": wire_by <= (today or date.today()),
        "fx_processing_days": FX_PROCESSING_DAYS,
        "wire_transit_days": WIRE_TRANSIT_DAYS,
    }


def _fmt(day: date) -> str:
    return f"{day.year}年{day.month}月{day.day}日"


def timing_step(deadline_value, *, today: date | None = None) -> str | None:
    """Chat-bubble line: concrete countdown when a deadline is known."""
    info = tuition_timing(deadline_value, today=today)
    if info is None:
        return None
    deadline, wire_by, fx_by = info["deadline"], info["wire_by"], info["fx_by"]
    if info["urgent"]:
        return (f"学校缴费截止日为 {_fmt(deadline)}，已临近：请尽快完成购汇并当日发起电汇，"
                "并向银行确认能否加急。")
    return (
        f"学校缴费截止日按您提供的信息为 {_fmt(deadline)}；倒排时间：最晚 {_fmt(fx_by)} 前完成购汇，"
        f"最晚 {_fmt(wire_by)} 前发起电汇汇出（预留约 {info['fx_processing_days']} 个工作日购汇办理与 "
        f"{info['wire_transit_days']} 个工作日跨境在途，不含法定节假日，以银行受理为准）。"
    )


def timing_todos(deadline_value, profile_id: str, *, today: date | None = None) -> list[tuple[str, str]]:
    """Idempotent plan rows ``[(item_id, text), ...]`` derived from the deadline."""
    info = tuition_timing(deadline_value, today=today)
    if info is None:
        return []
    deadline, wire_by, fx_by = info["deadline"], info["wire_by"], info["fx_by"]
    if info["urgent"]:
        return [(
            f"{profile_id}:timing:wire",
            f"尽快完成购汇并发起电汇（学校缴费截止 {_fmt(deadline)}，时间已紧，请与银行确认加急通道）",
        )]
    return [
        (f"{profile_id}:timing:fx",
         f"最晚 {_fmt(fx_by)} 前完成购汇（按缴费截止 {_fmt(deadline)} 倒推：预留约 "
         f"{info['fx_processing_days']} 个工作日购汇办理 + {info['wire_transit_days']} 个工作日电汇在途，不含法定节假日）"),
        (f"{profile_id}:timing:wire",
         f"最晚 {_fmt(wire_by)} 前向学校发起电汇（按缴费截止 {_fmt(deadline)} 倒推：预留约 "
         f"{info['wire_transit_days']} 个工作日跨境在途，不含法定节假日）"),
    ]
