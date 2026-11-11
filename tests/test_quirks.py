"""The provider quirks table (1.2): how each kind takes its key, what leaves a request, how usage is
read back (FY-QK-*). Pure functions, no network."""

from forgeyard.gateway import quirks
from forgeyard.gateway.proxy import CloudTarget, provider_error, retry_after

import httpx


def test_fy_qk_001_auth_headers_per_kind():
    """FY-QK-001: Bearer for OpenAI, Gemini, OpenRouter and other servers; Azure's api-key; Anthropic
    also x-api-key with a version; nothing without a key."""
    assert quirks.auth_headers("openai", "k1") == {"Authorization": "Bearer k1"}
    assert quirks.auth_headers("gemini", "k1") == {"Authorization": "Bearer k1"}
    assert quirks.auth_headers("openrouter", "k1") == {"Authorization": "Bearer k1"}
    assert quirks.auth_headers("openai_compatible", "k1") == {"Authorization": "Bearer k1"}
    assert quirks.auth_headers("azure", "k1") == {"api-key": "k1"}
    assert quirks.auth_headers("anthropic", "k1") == {"Authorization": "Bearer k1", "x-api-key": "k1",
                                                      "anthropic-version": "2023-06-01"}
    assert quirks.auth_headers("openai", None) == {} and quirks.auth_headers("azure", "") == {}


def test_fy_qk_002_what_leaves_a_request():
    """FY-QK-002: engine extras are dropped for clouds (OpenRouter keeps the sampling ones it passes
    on); OpenAI and Azure get max_completion_tokens; Anthropic loses an explicit text response
    format; another server keeps everything; a model's own drop list applies too."""
    body = {"model": "m", "messages": [], "max_tokens": 9, "top_k": 5, "min_p": 0.1, "chat_template_kwargs": {},
            "repetition_penalty": 1.1, "temperature": 0.3, "seed": 7, "response_format": {"type": "text"}}
    out, dropped = quirks.outgoing("openai", body, "chat/completions")
    assert out == {"model": "m", "messages": [], "max_completion_tokens": 9, "temperature": 0.3, "seed": 7,
                   "response_format": {"type": "text"}}
    assert dropped == ["chat_template_kwargs", "min_p", "repetition_penalty", "top_k"]
    assert "max_tokens" in body                              # the caller's dict is untouched
    out, _ = quirks.outgoing("azure", {**body, "max_completion_tokens": 4}, "chat/completions")
    assert out["max_completion_tokens"] == 4 and "max_tokens" not in out
    out, dropped = quirks.outgoing("anthropic", body, "chat/completions")
    assert out == {"model": "m", "messages": [], "max_tokens": 9, "temperature": 0.3, "seed": 7}
    assert "response_format" in dropped
    out, _ = quirks.outgoing("openrouter", body, "chat/completions")
    assert {"top_k", "min_p", "repetition_penalty"} <= set(out) and "chat_template_kwargs" not in out
    out, _ = quirks.outgoing("openai_compatible", body, "chat/completions")
    assert out == body
    out, _ = quirks.outgoing("openai_compatible", body, "chat/completions", extra_drop=["seed"])
    assert "seed" not in out
    out, _ = quirks.outgoing("openai", {"model": "m", "input": "x", "max_tokens": 3}, "embeddings")
    assert out == {"model": "m", "input": "x", "max_tokens": 3}   # renames only on chat


def test_fy_qk_003_usage_in_every_shape():
    """FY-QK-003: OpenAI's usage with details; Anthropic's always-empty details; Gemini without them;
    embeddings (prompt or total only); Anthropic's native names; OpenRouter's cost; nonsense."""
    u = quirks.read_usage({"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 60},
                           "completion_tokens_details": {"reasoning_tokens": 20}})
    assert (u.prompt, u.completion, u.cached, u.reasoning, u.cost) == (100, 50, 60, 20, None)
    u = quirks.read_usage({"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18,
                           "prompt_tokens_details": {}, "completion_tokens_details": {}})
    assert (u.prompt, u.completion, u.cached, u.reasoning) == (13, 5, 0, 0)
    u = quirks.read_usage({"prompt_tokens": 13, "completion_tokens": 5, "prompt_tokens_details": None})
    assert (u.prompt, u.cached) == (13, 0)
    assert quirks.read_usage({"prompt_tokens": 4, "total_tokens": 4}).prompt == 4
    assert quirks.read_usage({"total_tokens": 12}).prompt == 12
    # Anthropic's native input_tokens leave the cache out: the prompt is input + reads + writes (1.2 cost).
    u = quirks.read_usage({"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 2,
                           "cache_creation_input_tokens": 4})
    assert (u.prompt, u.completion, u.cached, u.cache_write) == (13, 3, 2, 4)
    u = quirks.read_usage({"prompt_tokens": 50, "completion_tokens": 9,
                           "prompt_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 40}})
    assert (u.prompt, u.cached, u.cache_write) == (50, 30, 20)    # never more than the prompt in all
    assert quirks.read_usage({"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.00042}).cost == 0.00042
    u = quirks.read_usage({"prompt_tokens": 3, "prompt_tokens_details": {"cached_tokens": 9}})
    assert u.cached == 3                                     # never more than the prompt
    for junk in (None, "x", [], {"prompt_tokens": -1, "completion_tokens": True}):
        u = quirks.read_usage(junk)
        assert (u.prompt, u.completion, u.cached, u.reasoning) == (0, 0, 0, 0)


def test_fy_qk_004_model_names_and_kinds():
    """FY-QK-004: Gemini lists "models/…"; requests take the bare name. Anthropic has no embeddings;
    Azure has no preset base URL; an unknown kind behaves like a plain OpenAI-compatible server."""
    assert quirks.model_id("gemini", "models/gemini-2.5-flash") == "gemini-2.5-flash"
    assert quirks.model_id("openai", "models/x") == "models/x"
    assert quirks.KINDS["anthropic"].embeddings is False and quirks.KINDS["azure"].base_url == ""
    assert quirks.get("nope").name == "openai_compatible"


def _target(key="sk-the-real-key-123456"):
    return CloudTarget(target_id=1, provider_model_id=1, provider_id=1, provider="acme", kind="openai",
                       base_url="https://x", upstream_model="m", external=True, allow_http=False, enabled=True,
                       api_key=key)


def test_fy_qk_005_provider_errors_and_retry_after():
    """FY-QK-005: error answers keep message, code and param without key-like strings; 429 and 5xx
    map to 429 / 502; Retry-After in seconds, milliseconds or as a date."""
    e = provider_error(_target(), 400, b'{"error": {"message": "bad sk-the-real-key-123456 and sk-proj-abcdefgh",'
                                       b' "code": "invalid_value", "param": "temperature"}}')
    assert (e.status, e.code, e.param) == (400, "invalid_value", "temperature")
    assert e.message == "acme: bad [redacted] and [redacted]"
    e = provider_error(_target(), 404, b'[{"error": {"message": "models/x is not found", "status": "NOT_FOUND"}}]')
    assert e.status == 404 and e.message == "acme: models/x is not found"
    e = provider_error(_target(), 429, b"{}", 2.5)
    assert (e.status, e.code, e.retry_after) == (429, "provider_rate_limited", 3)
    assert provider_error(_target(), 503, b"oops").status == 502
    assert provider_error(_target(), 400, b"AIzaSyA-0123456789abcdefghij is not valid").message == \
        "acme: [redacted] is not valid"
    assert retry_after(httpx.Headers({"retry-after": "7"})) == 7.0
    assert retry_after(httpx.Headers({"retry-after-ms": "1500", "retry-after": "9"})) == 1.5
    assert 0 <= retry_after(httpx.Headers({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})) <= 0.0
    assert retry_after(httpx.Headers({})) is None and retry_after(httpx.Headers({"retry-after": "soon"})) is None
