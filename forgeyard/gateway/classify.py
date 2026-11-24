"""Text classification at /v1/classify (1.4.4, #144): texts to a text classification model (forgeyard/models/textcls.py),
the most likely label and every label's probability back, in vLLM's shape:

    request   {"model": "sentiment", "input": "a text" | ["a text", "another"],
               "truncate_prompt_tokens": 256, "use_activation": true}      (the last two optional)
    answer    {"id", "object": "list", "created", "model",
               "data": [{"index": 0, "label": "positive", "probs": [0.01, 0.02, 0.97], "num_classes": 3}, …],
               "usage": {"prompt_tokens": 9, "total_tokens": 9, …}}

**Zero-shot (1.5.4, #144)**: an NLI model on Forgeyard's transformers server (bge-m3-zeroshot, roberta-zeroshot…) is
told the labels to choose from in each request:

    request   {"model": "topics", "input": …, "labels": ["billing", "technical problem", "praise"],
               "multi_label": false, "hypothesis_template": "This text is about {}."}   (the last two optional)
    answer    the same shape, `probs` in the order of `labels`, and "labels" and "multi_label" echoed

`labels` is 1 to MAX_LABELS names of at most MAX_LABEL_CHARS characters, each once (the probabilities follow their
order); `hypothesis_template` one line with one `{}`. Sent to a model with fixed labels they answer 400
`labels_not_supported`; a zero-shot model without labels 400 `labels_required`; texts x labels above the deployment's
`max_pairs` 400 `too_many_pairs` (`fit`, from each deployment's catalog entry: forgeyard/models/textcls.py).

The gateway checks the body and sends the engine only the fields above (`model` becomes the name the engine
knows), at the engine's /classify (vLLM serves it without /v1). Token ids, chat `messages` (vLLM's chat form,
which renders a chat template and could carry media) and every other field are not passed on: unknown fields
are dropped, `messages` and token ids are refused. The answer comes back as the engine made it, counted in
prompt tokens like embeddings. Local deployments only: no provider target serves it, so it never spills to the
cloud.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from forgeyard.gateway.errors import GatewayError

ENDPOINT = "classify"
UPSTREAM_PATH = "/classify"
MAX_INPUTS = 2048          # texts per request (OpenAI's limit for embeddings)
OPTIONAL = ("truncate_prompt_tokens", "use_activation")
ZERO_SHOT_FIELDS = ("labels", "multi_label", "hypothesis_template")   # 1.5.4
MAX_LABELS = 32            # the transformers server's (images/vision/app.py)
MAX_LABEL_CHARS = 64
MAX_TEMPLATE_CHARS = 200
DEFAULT_MAX_PAIRS = 256    # the transformers recipe's default (max_pairs)


@dataclass(frozen=True)
class Classifies:
    """What a deployment's text classifier is: zero-shot (told the labels) or with fixed labels, and (the
    transformers server) how many text x label pairs one request may run."""

    zero_shot: bool = False
    max_pairs: Optional[int] = None


def classifies(recipe, params: dict, meta: Optional[dict]) -> Classifies:
    from forgeyard.models import encoders, textcls

    zero = textcls.is_zero_shot(meta or {})
    if not encoders.is_server(recipe, "classify"):
        return Classifies(zero_shot=zero)
    raw = (params or {}).get("max_pairs")
    spec = recipe.params.get("max_pairs") if getattr(recipe, "params", None) else None
    default = spec.default if spec is not None and isinstance(spec.default, int) else DEFAULT_MAX_PAIRS
    most = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else default
    return Classifies(zero_shot=zero, max_pairs=most if zero else None)


def _plain(text: str) -> bool:
    return not any(ord(c) < 32 or ord(c) == 127 for c in text)


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
    labels = obj.get("labels")
    if labels is not None:
        if isinstance(labels, str) or not isinstance(labels, list) or not labels or \
                not all(isinstance(x, str) for x in labels):
            raise GatewayError(400, "invalid_request", "'labels' is a non-empty list of label names (strings): the "
                                                       "labels a zero-shot model chooses from.", param="labels")
        if len(labels) > MAX_LABELS:
            raise GatewayError(400, "invalid_request", f"'labels' has {len(labels)} names; at most {MAX_LABELS}.",
                               param="labels")
        seen: set[str] = set()
        for x in labels:
            name = " ".join(x.split())
            if not name or len(name) > MAX_LABEL_CHARS or not _plain(name):
                raise GatewayError(400, "invalid_request", f"Each label is 1 to {MAX_LABEL_CHARS} characters of "
                                                           "plain text.", param="labels")
            if name.lower() in seen:
                raise GatewayError(400, "invalid_request", f"'labels' has {name!r} twice: the probabilities follow "
                                                           "the labels' order, so each comes once.", param="labels")
            seen.add(name.lower())
        out["labels"] = labels
    for k in ("multi_label", "hypothesis_template"):
        if k in obj and obj[k] is not None and labels is None:
            raise GatewayError(400, "invalid_request", f"'{k}' goes with 'labels', for a zero-shot model.", param=k)
    m = obj.get("multi_label")
    if m is not None:
        if not isinstance(m, bool):
            raise GatewayError(400, "invalid_request", "'multi_label' is true or false.", param="multi_label")
        out["multi_label"] = m
    h = obj.get("hypothesis_template")
    if h is not None:
        if not isinstance(h, str) or h.count("{}") != 1 or len(h) > MAX_TEMPLATE_CHARS or not _plain(h):
            raise GatewayError(400, "invalid_request", f"'hypothesis_template' is one line of at most "
                                                       f"{MAX_TEMPLATE_CHARS} characters with one {{}} where the label "
                                                       "goes, e.g. \"This text is about {}.\"",
                               param="hypothesis_template")
        out["hypothesis_template"] = h
    return out


def fit(route, body: dict):
    """The route with only the deployments that can take this body: zero-shot ones when it sends labels (and within
    their max_pairs), the others when it does not. Raises the most telling 400 when none can."""
    from forgeyard.gateway.route import Tier

    labels = body.get("labels")
    texts = 1 if isinstance(body.get("input"), str) else len(body.get("input") or [])
    pairs = texts * len(labels) if labels else 0

    def takes(g) -> bool:
        c = g.classifies
        if c is None:          # its recipe is gone: the engine answers
            return True
        if labels is None:
            return not c.zero_shot
        return c.zero_shot and (c.max_pairs is None or pairs <= c.max_pairs)

    tiers = []
    for tier in route.tiers:
        locals_ = [g for g in tier.locals if takes(g)]
        if locals_:
            tiers.append(Tier(tier.index, locals_, []))
    groups = [g for tier in route.tiers for g in tier.locals]
    if tiers or not groups:
        return replace(route, tiers=tiers) if groups else route
    name = route.served_name
    known = [g.classifies for g in groups if g.classifies is not None]
    if labels is None:
        raise GatewayError(400, "labels_required", f"{name!r} is a zero-shot model: send the labels to choose from, "
                                                   "e.g. \"labels\": [\"billing\", \"technical problem\", "
                                                   "\"praise\"].", param="labels")
    zero = [c for c in known if c.zero_shot]
    if not zero:
        raise GatewayError(400, "labels_not_supported", f"{name!r} has fixed labels: leave 'labels' (and "
                                                        "'multi_label', 'hypothesis_template') out; a zero-shot model "
                                                        "takes them.", param="labels")
    most = max(c.max_pairs or 0 for c in zero)
    raise GatewayError(400, "too_many_pairs", f"{texts} texts x {len(labels)} labels = {pairs} pairs, each one pass of "
                                              f"the model; {name!r} takes at most {most} per request: send fewer texts "
                                              "or labels.", param="labels")
