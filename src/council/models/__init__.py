"""CLI model adapters."""

from council.models.base import InvokeRequest, ModelResult
from council.models.registry import get_adapter, invoke_model

__all__ = ["InvokeRequest", "ModelResult", "get_adapter", "invoke_model"]
