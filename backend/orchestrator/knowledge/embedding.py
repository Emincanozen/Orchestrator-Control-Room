"""Optional vector retrieval building blocks (OFF by default).

`HashingEmbedder` is a LEXICAL hash embedding (hashing trick over word unigrams and bigrams). It is
NOT semantic: two texts are similar only if they share words. It exists so the optional vector path is
exercisable offline without torch/numpy or any external embedding API. Swap in a real model by
implementing the `Embedder` protocol and passing it to `KnowledgeService(embedder=...)`.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Protocol, Sequence, runtime_checkable

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


@runtime_checkable
class Embedder(Protocol):
    name: str  # stored in knowledge_vectors.model
    dim: int
    semantic: bool  # honest label: True only for real semantic models

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    name = "lexical-hash-v1-256"
    description = "lexical hash embedding - not semantic"
    semantic = False

    def __init__(self, dim: int = 256):
        self.dim = dim
        if dim != 256:
            self.name = f"lexical-hash-v1-{dim}"

    def embed(self, text: str) -> list[float]:
        words = [w.lower() for w in _WORD.findall(text or "")][:4000]
        feats = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
        vec = [0.0] * self.dim
        for f in feats:
            h = hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest()
            n = int.from_bytes(h, "big")
            vec[n % self.dim] += 1.0 if (n >> 63) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm else vec


def pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
