"""Faiss vector backend implementation."""

from __future__ import annotations

from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np

from ..core.storage.vector_store import VectorStore


class FaissVectorBackend:
    """Thin wrapper around migrated A_memorix VectorStore."""

    def __init__(self, *, dimension: int, data_dir: Path):
        self.store = VectorStore(dimension=dimension, data_dir=data_dir)

    def add(self, vectors: np.ndarray, ids: Sequence[str]) -> int:
        return self.store.add(vectors, list(ids))

    def search(self, query: np.ndarray, k: int = 10) -> Tuple[List[str], List[float]]:
        return self.store.search(query, k=k)

    def delete(self, ids: Sequence[str]) -> int:
        return self.store.delete(list(ids))

    def save(self) -> None:
        self.store.save()

    def load(self) -> None:
        self.store.load()

    def clear(self) -> None:
        self.store.clear()

