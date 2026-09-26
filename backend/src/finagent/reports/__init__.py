"""审计报告组装。只读取已给出的结构化结果，不落盘、不调用模型。"""

from finagent.reports.annual_report import ReportBuildError, build_annual_report, render_markdown

__all__ = ["ReportBuildError", "build_annual_report", "render_markdown"]
