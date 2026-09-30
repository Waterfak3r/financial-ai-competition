"""本地确定性财务计算。当前提供年度同比和候选异常筛查。"""

from finagent.finance.annual_change import calculate_annual_changes
from finagent.finance.annual_signals import screen_annual_signals
from finagent.finance.v2_calculation import calculate_v2_annual_changes
from finagent.finance.m3_screening import screen_m3_annual_rules
from finagent.finance.v2_screening import screen_v2_annual_candidates

__all__ = [
    "calculate_annual_changes",
    "calculate_v2_annual_changes",
    "screen_m3_annual_rules",
    "screen_annual_signals",
    "screen_v2_annual_candidates",
]
