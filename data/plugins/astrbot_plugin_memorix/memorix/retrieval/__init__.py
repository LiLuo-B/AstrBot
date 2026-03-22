"""Retrieval facade."""

from .dual_path import (
    DualPathRetriever,
    DualPathRetrieverConfig,
    RetrievalResult,
    RetrievalStrategy,
    TemporalQueryOptions,
)
from .search_execution import SearchExecutionRequest, SearchExecutionResult, SearchExecutionService

__all__ = [
    "DualPathRetriever",
    "DualPathRetrieverConfig",
    "RetrievalResult",
    "RetrievalStrategy",
    "TemporalQueryOptions",
    "SearchExecutionRequest",
    "SearchExecutionResult",
    "SearchExecutionService",
]
