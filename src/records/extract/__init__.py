"""Extraction: LLM port, shape classifier, provenance extractor.

The Anthropic adapter is NOT imported here — offline tests and CI must
never need the SDK. Production code imports it explicitly:
`from records.extract.anthropic_client import AnthropicClient`.
"""

from records.extract.classifier import classify_shape
from records.extract.doc_classifier import classify_doc_type
from records.extract.extractor import ExtractionError, extract_lines
from records.extract.field_extractor import extract_fields
from records.extract.llm import FakeLLMClient, LLMClient, LLMResponse, strip_fences
from records.extract.telemetry import record_llm_usage

__all__ = [
    "ExtractionError",
    "FakeLLMClient",
    "LLMClient",
    "LLMResponse",
    "classify_doc_type",
    "classify_shape",
    "extract_fields",
    "extract_lines",
    "record_llm_usage",
    "strip_fences",
]
