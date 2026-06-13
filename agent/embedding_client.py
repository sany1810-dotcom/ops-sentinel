"""
Embedding client — Qwen text-embedding-v3 via DashScope OpenAI-compatible API.

Model choice: text-embedding-v3 (Qwen Cloud) over sentence-transformers because:
  - Same API key already configured: no extra infra, no model download
  - Counts as "advanced Qwen API usage" for the hackathon
  - 1024-dim, multilingual — handles Russian diagnosis text from SQLite
  - Falls back to text-overlap search (Week 1) if API is unavailable (§5)
"""
import logging
from typing import Optional

import numpy as np
from openai import OpenAI

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "text-embedding-v3"
_MAX_INPUT_LEN = 8000   # API safety guard (chars)


def build_embed_text(
    symptoms: list[str],
    metrics: dict | None = None,
    diagnosis: str = "",
) -> str:
    """
    Canonical text representation of an incident used for both storage and query.
    Keeping this function stable ensures stored and query embeddings are comparable.
    """
    parts = ["symptoms: " + ", ".join(sorted(symptoms))]
    if metrics:
        # Numeric/fault context only — booleans like 'reachable' add noise
        useful = {k: v for k, v in metrics.items()
                  if k not in ("reachable",) and v is not None}
        if useful:
            parts.append("metrics: " + " ".join(f"{k}={v}" for k, v in useful.items()))
    if diagnosis:
        parts.append("diagnosis: " + diagnosis.strip()[:500])
    return " | ".join(parts)


class EmbeddingClient:
    """
    Thin, thread-safe wrapper around the Qwen text-embedding API.
    Returns unit-normalised float32 vectors so cosine similarity = dot product.
    """

    def __init__(self, api_key: str, base_url: str, model: str = _DEFAULT_MODEL):
        self._client  = OpenAI(api_key=api_key.strip(), base_url=base_url.strip(), timeout=15.0)
        self._model   = model.strip()
        self.available = True

    def embed(self, text: str) -> Optional[np.ndarray]:
        """
        Embed text and return a unit-normalised float32 vector, or None on failure.
        On failure sets self.available = False so callers know to use text fallback.
        """
        try:
            resp = self._client.embeddings.create(
                model=self._model,
                input=text[:_MAX_INPUT_LEN],
                encoding_format="float",
            )
            vec = np.array(resp.data[0].embedding, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            self.available = True
            return vec
        except Exception as exc:
            logger.warning("Embedding API unavailable (%s: %s) — text-overlap fallback active",
                           type(exc).__name__, exc)
            self.available = False
            return None
