"""What differs between the OpenAI-compatible endpoints of the providers Forgeyard talks to (1.2).

Forgeyard speaks only the OpenAI shape to providers (no native translators, no LiteLLM): their own
OpenAI-compatible endpoints, plus this small table of differences per kind of provider:

- how the key is sent (Bearer, Azure's `api-key`, Anthropic's `x-api-key` as well);
- the base URL (a preset; Azure and other servers take your own);
- request parameters to drop (engine extras that vLLM and llama.cpp accept and clouds refuse) or to
  rename (OpenAI's newer models refuse `max_tokens`);
- whether embeddings and `stream_options.include_usage` exist;
- which structured-output parameters (a JSON schema, a grammar) would not take effect there, so a
  request that carries one is not sent to such a provider (1.4.5, `structure_lost`);
- how `usage` is read back (cached and reasoning tokens; Anthropic always leaves those empty), and
  which OpenRouter ids a kind's models have, to suggest price matches (forgeyard/gateway/pricing.py).

What each provider's compatibility endpoint cannot do is documented in the guide
(docs/guide/cloud-providers.md); it is not papered over here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Parameters local engines take that are not part of OpenAI's API. Sent to a cloud, they are refused
# (OpenAI: 400 "Unrecognized request argument") or silently ignored, so they are removed when a
# request leaves for a provider (most often when a local route spills).
ENGINE_EXTRAS = frozenset({
    # vLLM sampling and structured output extras
    "top_k", "min_p", "repetition_penalty", "length_penalty", "use_beam_search", "min_tokens", "ignore_eos",
    "skip_special_tokens", "spaces_between_special_tokens", "include_stop_str_in_output", "truncate_prompt_tokens",
    "prompt_logprobs", "allowed_token_ids", "bad_words", "stop_token_ids", "guided_json", "guided_regex",
    "guided_choice", "guided_grammar", "guided_decoding_backend", "guided_whitespace_pattern", "structured_outputs",
    "chat_template", "chat_template_kwargs", "add_generation_prompt", "continue_final_message", "add_special_tokens",
    "documents", "mm_processor_kwargs", "priority", "request_id", "return_tokens_as_token_ids", "cache_salt",
    "kv_transfer_params", "vllm_xargs",
    # llama.cpp server extras
    "n_predict", "n_keep", "n_discard", "n_probs", "cache_prompt", "id_slot", "slot_id", "samplers", "typical_p",
    "tfs_z", "mirostat", "mirostat_tau", "mirostat_eta", "dynatemp_range", "dynatemp_exponent", "penalty_last_n",
    "repeat_penalty", "repeat_last_n", "presence_penalty_last_n", "dry_multiplier", "dry_base",
    "dry_allowed_length", "dry_penalty_last_n", "dry_sequence_breakers", "xtc_probability", "xtc_threshold",
    "top_n_sigma", "grammar", "json_schema", "t_max_predict_ms", "t_max_prompt_ms", "return_tokens",
    "timings_per_token", "post_sampling_probs", "lora", "reasoning_format", "thinking_forced_open",
})

# OpenRouter passes these sampling parameters on to the providers that take them.
OPENROUTER_KEEPS = frozenset({"top_k", "min_p", "repetition_penalty", "top_a"})

# Request parameters that constrain the shape of the answer (structured output, 1.4.5): vLLM's guided
# decoding and structured_outputs, llama.cpp's grammar and json_schema, and OpenAI's response_format
# when it asks for more than plain text. A request that carries one is never sent to a provider that
# would drop or ignore it (forgeyard/gateway/proxy.py): the answer would come back unconstrained.
STRUCTURED = ("response_format", "structured_outputs", "guided_json", "guided_regex", "guided_choice",
              "guided_grammar", "grammar", "json_schema")
# response_format types OpenAI's API defines; another type (vLLM's structural_tag…) only an engine takes.
OPENAI_RESPONSE_FORMATS = ("text", "json_object", "json_schema")


@dataclass(frozen=True)
class Kind:
    name: str
    label: str
    base_url: str                         # the preset ("" when you must give your own)
    base_url_hint: str
    auth: str                             # bearer | api-key | anthropic
    drop: frozenset = frozenset()
    rename: dict = field(default_factory=dict)   # on /chat/completions only
    embeddings: bool = True
    stream_usage: bool = True             # honours stream_options.include_usage
    models_endpoint: bool = True          # GET {base}/models lists what can be imported
    strip_model_prefix: str = ""          # Gemini lists "models/gemini-…"; requests take "gemini-…"
    default_external: bool = True
    key_required: bool = True
    # usage.prompt_tokens_details.cached_tokens is reported. Anthropic's endpoint leaves the details
    # empty, so a request there is priced as if nothing was cached and counted as an estimate.
    reports_cached: bool = True
    # The OpenRouter model ids of this kind start with this ("openai/gpt-4o-mini"); used to suggest
    # a price match, which an admin confirms.
    catalog_vendor: str = ""
    # Structured output the endpoint accepts but ignores (1.4.5): Anthropic's compatibility endpoint
    # honours response_format only as a json_schema with strict: true, and ignores strict on tools.
    schema_needs_strict: bool = False
    tool_strict: bool = True
    # Takes every response_format type (another server: a vLLM or llama.cpp of your own).
    any_response_format: bool = False
    note: str = ""


KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind("openai", "OpenAI", "https://api.openai.com/v1", "", "bearer", drop=ENGINE_EXTRAS,
         rename={"max_tokens": "max_completion_tokens"}, catalog_vendor="openai",
         note="Complete on Chat Completions and embeddings. Newer models refuse max_tokens, so it is sent as "
              "max_completion_tokens."),
    Kind("anthropic", "Anthropic", "https://api.anthropic.com/v1", "", "anthropic", drop=ENGINE_EXTRAS,
         embeddings=False, reports_cached=False, catalog_vendor="anthropic", schema_needs_strict=True, tool_strict=False,
         note="Anthropic's OpenAI compatibility endpoint: no prompt caching, no thinking returned, no PDF or "
              "file input, n must be 1, usage has no cached or reasoning breakdown, and there are no embeddings."),
    Kind("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "", "bearer",
         drop=ENGINE_EXTRAS, strip_model_prefix="models/", catalog_vendor="google",
         note="Gemini's OpenAI compatibility endpoint is in beta: no file upload; Gemini 3 thought signatures pass "
              "through untouched, but a conversation that mixes local and Gemini answers can lose them."),
    Kind("azure", "Azure OpenAI", "", "https://<resource>.openai.azure.com/openai/v1", "api-key",
         drop=ENGINE_EXTRAS, rename={"max_tokens": "max_completion_tokens"}, catalog_vendor="openai",
         note="The v1 API (no api-version). The model is your Azure deployment's name; /models lists base models, "
              "not deployments, so add deployments by name."),
    Kind("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "", "bearer",
         drop=ENGINE_EXTRAS - OPENROUTER_KEEPS,
         note="Reports the cost of each request; routes between its own providers."),
    Kind("openai_compatible", "OpenAI-compatible server", "", "https://llm.example.org/v1", "bearer",
         default_external=False, key_required=False, any_response_format=True,
         note="Any server with the OpenAI API: a vLLM or llama.cpp of your own elsewhere, Together, Groq… Nothing "
              "is dropped. Outside your network is suggested from its address: check it for a third party."),
)}


def get(kind: str) -> Kind:
    return KINDS.get(kind) or KINDS["openai_compatible"]


def auth_headers(kind: str, api_key: Optional[str]) -> dict[str, str]:
    """The headers that carry the provider key (none without one)."""
    if not api_key:
        return {}
    k = get(kind)
    if k.auth == "api-key":
        return {"api-key": api_key}
    if k.auth == "anthropic":
        # The compatibility endpoint takes the Bearer token like the OpenAI SDK sends it; the native
        # routes (GET /v1/models for the connection test) want x-api-key and a version.
        return {"Authorization": f"Bearer {api_key}", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {api_key}"}


def outgoing(kind: str, obj: dict, endpoint: str, extra_drop: Optional[list] = None) -> tuple[dict, list[str]]:
    """The request body for a provider (a copy) and the parameters that were removed."""
    k = get(kind)
    out = dict(obj)
    drop = set(k.drop) | set(extra_drop or [])
    dropped = sorted(name for name in out if name in drop)
    for name in dropped:
        out.pop(name, None)
    if endpoint == "chat/completions":
        for old, new in k.rename.items():
            if old in out and new not in out:
                out[new] = out.pop(old)
            elif old in out:
                out.pop(old)                      # both given: the newer name wins
                dropped.append(old)
        if k.name == "anthropic":
            rf = out.get("response_format")
            if isinstance(rf, dict) and rf.get("type") == "text":
                out.pop("response_format")  # plain text is the default; Anthropic refuses it spelled out
                dropped.append("response_format")
    if not k.stream_usage:
        out.pop("stream_options", None)
    return out, sorted(dropped)


def _strict_tools(obj: dict) -> bool:
    tools = obj.get("tools")
    return isinstance(tools, list) and any(
        isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("strict") is True
        for t in tools)


def structured(obj: Optional[dict]) -> list[str]:
    """The structured-output parameters a request carries (`tools.strict`: a tool with strict: true)."""
    if not isinstance(obj, dict):
        return []
    out = []
    for name in STRUCTURED:
        if name not in obj or obj[name] is None:
            continue
        if name == "response_format":
            rf = obj[name]
            if isinstance(rf, dict) and rf.get("type") in (None, "text"):
                continue                          # plain text: nothing to lose
        out.append(name)
    if _strict_tools(obj):
        out.append("tools.strict")
    return out


def structure_lost(kind: str, obj: Optional[dict], endpoint: str, extra_drop: Optional[list] = None) -> list[str]:
    """The structured-output parameters of a request that would not take effect at a provider of this
    kind: dropped on the way (the kind's or the model's drop list) or ignored by its endpoint."""
    asked = structured(obj)
    if not asked:
        return []
    k = get(kind)
    _, dropped = outgoing(kind, obj, endpoint, extra_drop)
    lost = [name for name in asked if name in dropped]
    rf = obj.get("response_format")
    if "response_format" in asked and "response_format" not in lost:
        kind_of = rf.get("type") if isinstance(rf, dict) else None
        if not k.any_response_format and kind_of not in OPENAI_RESPONSE_FORMATS:
            lost.append("response_format")        # a type only engines know
        elif k.schema_needs_strict and not (
                kind_of == "json_schema" and isinstance(rf.get("json_schema"), dict)
                and rf["json_schema"].get("strict") is True):
            lost.append("response_format")        # json_object, or a schema without strict: ignored
    if "tools.strict" in asked and (not k.tool_strict or "tools" in dropped):
        lost.append("tools.strict")
    return lost


@dataclass
class Usage:
    prompt: int = 0
    completion: int = 0
    cached: int = 0                       # of prompt: read from the provider's prompt cache
    reasoning: int = 0                    # of completion: reasoning (already counted in completion)
    cost: Optional[float] = None          # USD, when the provider reports it (OpenRouter)
    cache_write: int = 0                  # of prompt: written to the provider's prompt cache
    reported: bool = False                # the answer said anything about tokens or cost


def _num(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def read_usage(usage) -> Usage:
    """`usage` in any of the shapes seen: OpenAI's (with or without details; Anthropic's details
    are always empty `{}` and Gemini's may be missing), embeddings' and rerank's (prompt or total
    only), OpenRouter's (cost, cache writes) and Anthropic's native input/output names.

    Cached and cache-write tokens are always a part of `prompt`, reasoning tokens a part of
    `completion`, as in OpenAI's shape; tokens that only `total_tokens` counts (Gemini's thinking)
    are added to `completion` as reasoning. Anthropic's native `input_tokens` leaves the cache out, so
    there the prompt is input + cache reads + cache writes."""
    if not isinstance(usage, dict):
        return Usage()
    prompt, completion = _num(usage.get("prompt_tokens")), _num(usage.get("completion_tokens"))
    pd = usage.get("prompt_tokens_details")
    cd = usage.get("completion_tokens_details")
    cached = _num(pd.get("cached_tokens")) if isinstance(pd, dict) else 0
    written = _num(pd.get("cache_write_tokens")) if isinstance(pd, dict) else 0
    if not prompt and not completion:
        prompt = _num(usage.get("input_tokens"))
        completion = _num(usage.get("output_tokens"))
        if prompt or completion:
            cached = cached or _num(usage.get("cache_read_input_tokens"))
            written = written or _num(usage.get("cache_creation_input_tokens"))
            prompt += cached + written
        else:
            prompt = _num(usage.get("total_tokens"))
    cached = cached or _num(usage.get("cache_read_input_tokens"))
    reasoning = _num(cd.get("reasoning_tokens")) if isinstance(cd, dict) else 0
    total = _num(usage.get("total_tokens"))
    if "completion_tokens" in usage and total > prompt + completion:
        # Gemini's OpenAI-compatible endpoint leaves its thinking tokens out of completion_tokens and
        # reports no reasoning_tokens; they show only in total_tokens, and Google bills them as output.
        hidden = total - prompt - completion
        completion += hidden
        reasoning += hidden
    cost = usage.get("cost")
    cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0 else None
    if prompt:
        cached = min(cached, prompt)
        written = min(written, prompt - cached)
    return Usage(prompt=prompt, completion=completion, cached=cached,
                 reasoning=min(reasoning, completion) if completion else reasoning, cost=cost, cache_write=written,
                 reported=bool(prompt or completion or cost is not None))


def model_id(kind: str, listed: str) -> str:
    """The name to send in requests for a model as /models lists it."""
    prefix = get(kind).strip_model_prefix
    return listed[len(prefix):] if prefix and listed.startswith(prefix) else listed
