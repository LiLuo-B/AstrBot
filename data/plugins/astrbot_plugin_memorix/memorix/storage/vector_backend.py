"""Vector backend abstraction."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .vector_faiss import FaissVectorBackend
from .vector_numpy import NumpyVectorBackend


class VectorBackend(Protocol):
    def add(self, vectors, ids): ...
    def search(self, query, k=10): ...
    def delete(self, ids): ...
    def save(self): ...
    def load(self): ...
    def clear(self): ...


def create_vector_backend(*, dimension: int, data_dir: Path, prefer_faiss: bool = True):
    if prefer_faiss:
        try:
            return FaissVectorBackend(dimension=dimension, data_dir=data_dir)
        except Exception:
            pass
    return NumpyVectorBackend(dimension=dimension, data_dir=data_dir)

