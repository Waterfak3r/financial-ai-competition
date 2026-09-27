"""Bounded local retrieval of annual-report narrative context."""

from finagent.retrieval.annual_context import (
    AnnualContextResult,
    NarrativeSnippet,
    SignalContextResult,
    retrieve_annual_context,
)

__all__ = [
    "AnnualContextResult",
    "NarrativeSnippet",
    "SignalContextResult",
    "retrieve_annual_context",
]
