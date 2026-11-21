"""Structured output is never lost silently (1.4.5, #162): a request with a JSON schema, a grammar or
another guided-decoding parameter skips every provider target that would drop or ignore it, and the
parameters actually left out on the way to a provider are named in X-Forgeyard-Dropped-Params
(FY-SO-*). Every provider here is a local stand-in (tests/cloud_helpers.py)."""

import threading

import pytest

from forgeyard.gateway import quirks
from tests.cloud_helpers import FakeCloud, add_provider, cloud_key, model_id, set_tiers
from tests.gateway_helpers import api_client, set_state
from tests.test_routing import background, chat, in_flight_soon, mixed  # noqa: F401  (the fixture)
from tests.waits import wait_until

SCHEMA = {"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}}}
STRICT = {"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}, "strict": True}}
TOOL = {"type": "function", "function": {"name": "f", "parameters": {"type": "object"}, "strict": True}}
CLOUD_KINDS = ("openai", "anthropic", "gemini", "azure", "openrouter")


def test_fy_so_001_what_counts_as_structured_output():
    """FY-SO-001: guided decoding, structured_outputs, grammar, json_schema, a response_format that asks
    for JSON (or an engine's own type) and strict tools are structured output; plain text, sampling
    parameters and tools without strict are not."""
    assert quirks.structured({"messages": [], "temperature": 0.2, "top_k": 3}) == []
    assert quirks.structured({"response_format": {"type": "text"}, "tools": [{"function": {"name": "f"}}]}) == []
    assert quirks.structured({"response_format": {"type": "json_object"}}) == ["response_format"]
    assert quirks.structured({"guided_json": {}, "guided_choice": ["a"], "guided_regex": "x", "guided_grammar": "g",
                              "structured_outputs": {"choice": ["a"]}, "grammar": "root ::= x", "json_schema": {}}) == [
        "structured_outputs", "guided_json", "guided_regex", "guided_choice", "guided_grammar", "grammar", "json_schema"]
    assert quirks.structured({"tools": [TOOL]}) == ["tools.strict"]
    assert quirks.structured({"guided_json": None}) == [] and quirks.structured(None) == []


@pytest.mark.parametrize("kind", CLOUD_KINDS + ("openai_compatible",))
def test_fy_so_002_what_each_kind_would_lose(kind):
    """FY-SO-002: per kind, which structured-output parameters would not take effect: every cloud kind
    drops the engine ones (vLLM's guided_* and structured_outputs, llama.cpp's grammar and json_schema);
    Anthropic also ignores json_object, a schema without strict and strict tools; an engine-only
    response_format type reaches only another OpenAI-compatible server; that kind loses nothing unless
    the model's own drop list removes it."""
    cloud = kind != "openai_compatible"
    engine_only = {"guided_json": {"type": "object"}, "structured_outputs": {"choice": ["a", "b"]},
                   "grammar": "root ::= \"a\"", "json_schema": {"type": "object"}, "guided_choice": ["a"]}
    for name, value in engine_only.items():
        assert quirks.structure_lost(kind, {name: value}, "chat/completions") == ([name] if cloud else [])
    anthropic = kind == "anthropic"
    assert quirks.structure_lost(kind, {"response_format": {"type": "json_object"}}, "chat/completions") == \
        (["response_format"] if anthropic else [])
    assert quirks.structure_lost(kind, {"response_format": SCHEMA}, "chat/completions") == \
        (["response_format"] if anthropic else [])
    assert quirks.structure_lost(kind, {"response_format": STRICT}, "chat/completions") == []
    assert quirks.structure_lost(kind, {"tools": [TOOL]}, "chat/completions") == (["tools.strict"] if anthropic else [])
    assert quirks.structure_lost(kind, {"response_format": {"type": "structural_tag"}}, "chat/completions") == \
        (["response_format"] if cloud else [])
    assert quirks.structure_lost(kind, {"response_format": STRICT}, "chat/completions",
                                 extra_drop=["response_format"]) == ["response_format"]
    assert quirks.structure_lost(kind, {"temperature": 0.1, "top_k": 4}, "chat/completions") == []


def test_fy_so_003_dropped_parameters_are_named():
    """FY-SO-003: outgoing() reports every parameter it leaves out, sorted, including a max_tokens given
    next to max_completion_tokens (the newer name is kept)."""
    out, dropped = quirks.outgoing("openai", {"max_tokens": 5, "max_completion_tokens": 7, "top_k": 2,
                                              "guided_json": {}}, "chat/completions")
    assert out == {"max_completion_tokens": 7} and dropped == ["guided_json", "max_tokens", "top_k"]
    assert quirks.outgoing("openai", {"max_tokens": 5}, "chat/completions")[1] == []


def test_fy_so_004_busy_local_never_spills_structured_output_to_a_provider_that_drops_it(mixed):  # noqa: F811
    """FY-SO-004: local is busy and the cloud tier would drop guided_json: the request is not spilled
    but sent to the local replica after all (where the engine queues it), and the provider hears
    nothing. A response_format the provider keeps spills as before."""
    mixed.engine.gate = threading.Event()
    try:
        t1, _ = background(lambda: chat(mixed.api))
        in_flight_soon()
        t2, second = background(lambda: chat(mixed.api, guided_json={"type": "object"}))
        wait_until(lambda: len(mixed.engine.posts()) == 2, "the structured request reached the local engine")
        assert mixed.cloud.posts() == []
        r = chat(mixed.api, response_format=SCHEMA)       # OpenAI keeps a JSON schema: it may spill
        assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "busy"
        assert "x-forgeyard-dropped-params" not in r.headers
        assert mixed.cloud.posts()[-1]["body"]["response_format"] == SCHEMA
    finally:
        mixed.engine.gate.set()
    t1.join()
    t2.join()
    r = second["r"]
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
    assert "x-forgeyard-spilled" not in r.headers and "x-forgeyard-dropped-params" not in r.headers
    assert mixed.engine.posts()[-1]["body"]["guided_json"] == {"type": "object"}
    assert len(mixed.cloud.posts()) == 1


def test_fy_so_005_unavailable_local_answers_why_it_did_not_spill(mixed):  # noqa: F811
    """FY-SO-005: no ready replica and only a provider that would drop the grammar: 503 no_ready_replica,
    which says the provider was skipped and why; without the grammar the same request spills."""
    set_state("chat", 0, "starting", "loading")
    r = chat(mixed.api, grammar="root ::= \"yes\"")
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_ready_replica"
    msg = r.json()["error"]["message"]
    assert "Not sent to the cloud: cloudy would drop grammar" in msg and "structured output" in msg
    assert ". Not sent to the cloud" in msg and ".. Not" not in msg
    assert mixed.cloud.posts() == []
    r = chat(mixed.api)
    assert r.status_code == 200 and r.headers["x-forgeyard-spilled"] == "unavailable"


def test_fy_so_006_a_cloud_only_route_refuses_what_it_would_lose(mixed):  # noqa: F811
    """FY-SO-006: a route only such providers serve answers 400 structured_output_not_supported naming
    the provider and the parameter; a strict tool reaches OpenAI, and a request without structured
    output goes through."""
    r = mixed.owner.post("/api/routes", json={"served_name": "gpt", "api": "openai-chat",
                                              "targets": [{"kind": "provider", "provider_model_id": mixed.pm}]})
    assert r.status_code == 201, r.text
    on = api_client(mixed.owner, cloud_key(mixed.owner, "on-key", "on"))
    r = chat(on, model="gpt", structured_outputs={"choice": ["a", "b"]})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "structured_output_not_supported" and "cloudy would drop structured_outputs" in err["message"]
    assert mixed.cloud.posts() == []
    r = chat(on, model="gpt", tools=[TOOL])
    assert r.status_code == 200 and mixed.cloud.posts()[-1]["body"]["tools"] == [TOOL]
    assert chat(on, model="gpt").status_code == 200


@pytest.mark.parametrize("kind", CLOUD_KINDS + ("openai_compatible",))
def test_fy_so_007_every_kind_end_to_end(mixed, kind):  # noqa: F811
    """FY-SO-007: per provider kind, with the local tier unavailable: json_object spills except to
    Anthropic (which ignores it), guided_json spills only to another OpenAI-compatible server, and a
    model whose drop list removes response_format is skipped for it."""
    cloud = FakeCloud(kind, key="sk-ant-test-abcdef0123456789" if kind == "anthropic" else "sk-test-cloud-0123456789abcdef",
                      style=kind if kind in ("anthropic", "azure") else "openai")
    try:
        p = add_provider(mixed.owner, cloud, name=f"p-{kind}", kind=kind, models=(("m1", "openai-chat"),))
        set_tiers(mixed.owner, "chat", [[("local", mixed.dep)], [("provider", model_id(p, "m1"))]])
        set_state("chat", 0, "starting", "loading")
        r = chat(mixed.api, response_format={"type": "json_object"})
        assert r.status_code == (503 if kind == "anthropic" else 200), r.text
        r = chat(mixed.api, guided_json={"type": "object"})
        assert r.status_code == (200 if kind == "openai_compatible" else 503), r.text
        if kind == "openai_compatible":
            assert cloud.posts()[-1]["body"]["guided_json"] == {"type": "object"}
            assert "x-forgeyard-dropped-params" not in r.headers
        r = mixed.owner.patch(f"/api/provider-models/{model_id(p, 'm1')}",
                              json={"drop_params": ["response_format"]})
        assert r.status_code == 200, r.text
        sent = len(cloud.posts())
        r = chat(mixed.api, response_format=STRICT)
        assert r.status_code == 503 and f"p-{kind} would drop response_format" in r.json()["error"]["message"]
        assert len(cloud.posts()) == sent
    finally:
        cloud.close()


def test_fy_so_008_the_dropped_params_header(mixed):  # noqa: F811
    """FY-SO-008: parameters left out for a provider are named in X-Forgeyard-Dropped-Params, on an
    answer and on a provider's error; a local answer and a request that lost nothing carry none."""
    r = chat(mixed.api, top_k=20)
    assert r.headers["x-forgeyard-target"] == "local:chat#0" and "x-forgeyard-dropped-params" not in r.headers
    set_state("chat", 0, "starting", "loading")
    r = chat(mixed.api, top_k=20, chat_template_kwargs={"enable_thinking": False}, temperature=0.1)
    assert r.status_code == 200 and r.headers["x-forgeyard-dropped-params"] == "chat_template_kwargs,top_k"
    r = chat(mixed.api, temperature=0.1)
    assert r.status_code == 200 and "x-forgeyard-dropped-params" not in r.headers
    r = chat(mixed.api, stream=True, min_p=0.05)
    assert r.status_code == 200 and r.headers["x-forgeyard-dropped-params"] == "min_p"
    mixed.cloud.status = 400
    r = chat(mixed.api, top_k=3)
    assert r.status_code == 400 and r.headers["x-forgeyard-dropped-params"] == "top_k"


def test_fy_so_009_a_local_answer_after_a_failed_provider_names_nothing_dropped(mixed):  # noqa: F811
    """FY-SO-009: local busy, the provider tried (top_k left out for it) and failing, the request then
    answered locally: that answer kept top_k, so it carries no X-Forgeyard-Dropped-Params."""
    mixed.engine.gate = threading.Event()
    mixed.cloud.status = 500
    try:
        t1, _ = background(lambda: chat(mixed.api))
        in_flight_soon()
        t2, second = background(lambda: chat(mixed.api, top_k=5))
        wait_until(lambda: len(mixed.cloud.posts()) == 1, "the second request tried the cloud tier")
    finally:
        mixed.engine.gate.set()
    t1.join()
    t2.join()
    r = second["r"]
    assert r.status_code == 200 and r.headers["x-forgeyard-target"] == "local:chat#0"
    assert "x-forgeyard-dropped-params" not in r.headers and mixed.engine.posts()[-1]["body"]["top_k"] == 5
