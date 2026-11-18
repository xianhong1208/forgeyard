"""Text classification at /v1/classify (1.4.4, #144): texts to a text classification model (forgeyard/models/textcls.py),
the most likely label and every label's probability back, in vLLM's shape:

    request   {"model": "sentiment", "input": "a text" | ["a text", "another"],
               "truncate_prompt_tokens": 256, "use_activation": true}      (the last two optional)
    answer    {"id", "object": "list", "created", "model",
               "data": [{"index": 0, "label": "positive", "probs": [0.01, 0.02, 0.97], "num_classes": 3}, …],
               "usage": {"prompt_tokens": 9, "total_tokens": 9, …}}

The gateway checks the body and sends the engine only the fields above (`model` becomes the name the engine
knows), at the engine's /classify (vLLM serves it without /v1). Token ids, chat `messages` (vLLM's chat form,
which renders a chat template and could carry media) and every other field are not passed on: unknown fields
are dropped, `messages` and token ids are refused. The answer comes back as the engine made it, counted in
prompt tokens like embeddings. Local deployments only: no provider target serves it, so it never spills to the
cloud.
"""

from __future__ import annotations

from forgeyard.gateway.errors import GatewayError

ENDPOINT = "classify"
UPSTREAM_PATH = "/classify"
MAX_INPUTS = 2048          # texts per request (OpenAI's limit for embeddings)
OPTIONAL = ("truncate_prompt_tokens", "use_activation")


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check(obj: dict) -> dict:
    """The body for the engine: `model`, `input` and the optional fields the request gave, checked. Raises a 400
    GatewayError naming the field."""
    if "messages" in obj:
        raise GatewayError(400, "invalid_request", "Send the text to classify in 'input' (a string or a list of "
                                                   "strings); chat messages are not taken here.", param="messages")
    inp = obj.get("input")
    texts = [inp] if isinstance(inp, str) else inp
    if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
        raise GatewayError(400, "invalid_request", "'input' is the text to classify: a string or a non-empty list "
                                                   "of strings (token ids are not taken).", param="input")
    if len(texts) > MAX_INPUTS:
        raise GatewayError(400, "invalid_request", f"'input' has {len(texts)} texts; at most {MAX_INPUTS} per request.",
                           param="input")
    empty = next((i for i, t in enumerate(texts) if not t.strip()), None)
    if empty is not None:
        where = "" if isinstance(inp, str) else f" (input[{empty}])"
        raise GatewayError(400, "invalid_request", f"'input' has an empty text{where}.", param="input")
    out = {"model": obj.get("model"), "input": inp}
    t = obj.get("truncate_prompt_tokens")
    if t is not None:
        if not _int(t) or t < -1 or t == 0:
            raise GatewayError(400, "invalid_request", "'truncate_prompt_tokens' is a number of tokens (at least 1), "
                                                       "or -1 for the model's maximum.", param="truncate_prompt_tokens")
        out["truncate_prompt_tokens"] = t
    a = obj.get("use_activation")
    if a is not None:
        if not isinstance(a, bool):
            raise GatewayError(400, "invalid_request", "'use_activation' is true or false.", param="use_activation")
        out["use_activation"] = a
    return out
