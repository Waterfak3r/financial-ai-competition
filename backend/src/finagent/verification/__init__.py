"""原文与数值核验。当前只复核引用区域内的金额和单位换算。"""

from finagent.verification.source_amount import verify_source_amounts

__all__ = ["verify_source_amounts"]
