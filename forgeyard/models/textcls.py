"""Text classification (1.4.4, #144): a small encoder with a fixed set of labels (sentiment, language, toxicity,
topic…) as a model kind of the catalog (task `classify`), served by vLLM's pooling runner through the built-in
`vllm-pooling` recipe and answered at /v1/classify (forgeyard/gateway/classify.py).

What counts as one, read from a transformers repository's config.json with vLLM's own rule
(vllm/entrypoints/pooling/utils.py `enable_scoring_api` at v0.32.0): a sequence-classification model
(an architecture ending in `ForSequenceClassification`) with **two labels or more** is a classifier; with one
output it scores pairs and is a reranker (task `rerank`), never a classifier. The labels are counted as
transformers counts them (forgeyard/models/sources.py `sequence_labels`).

Refused, with the reason, when it is added as a text classification model (no engine runs it):

- **one output** (num_labels 1): a reranker; add it as a rerank model;
- **a regression head** (`problem_type: regression`): it predicts numbers, not labels.

Refused **for vllm-pooling only** (1.5.4, #144: `vllm_refusal`; Forgeyard's transformers server, the built-in
`transformers` recipe, runs them, forgeyard/models/encoders.py):

- **an NLI (zero-shot) model** (labels such as entailment / not_entailment / contradiction): it compares a text with
  a hypothesis, so it needs labels in the request, which vLLM's /classify does not take. The transformers server
  takes them (`labels`, `multi_label`, `hypothesis_template` at /v1/classify); `summary` marks it `zero_shot`;
- **an architecture vLLM 0.32 does not run as a classifier** (VLLM_ARCHITECTURES): DeBERTa, DistilBERT, BART and
  the others.

The pooling runner picks the classify task by itself for these architectures (vllm/config/model.py
`get_pooling_task`: their pooler serves `classify` and `token_classify`, never `embed`), so the recipe needs no
task flag. vLLM answers /classify (without /v1: the agent's key guard covers it), with each input's most likely
label, the probabilities of every label in config.json's order, and the prompt tokens in `usage`. Models whose
config says `problem_type: multi_label_classification` get independent probabilities (a sigmoid per label);
the others a softmax.
"""

from __future__ import annotations

from typing import Optional

from forgeyard.models import sources

TASK = "classify"
API = "classify"                 # the route's protocol: /v1/classify (forgeyard/gateway/classify.py)
LABEL = "Text classification"
PIPELINE_TAGS = ("text-classification",)

# vLLM 0.32.0's text-only sequence classifiers (vllm/model_executor/models/registry.py,
# _SEQUENCE_CLASSIFICATION_MODELS): the architectures it runs natively with a classification head. Decoder
# models it would convert (`--convert classify`) and the multimodal ones are left out. Check the list again
# when the pinned vLLM moves (FY-TCLS-002 names the version).
VLLM_CHECKED_WITH = "0.32.0"
VLLM_ARCHITECTURES = (
    "BertForSequenceClassification",
    "GPT2ForSequenceClassification",
    "GteNewForSequenceClassification",
    "JambaForSequenceClassification",
    "LlamaBidirectionalForSequenceClassification",
    "ModernBertForSequenceClassification",
    "RobertaForSequenceClassification",
    "XLMRobertaForSequenceClassification",
)
MAX_LABEL_NAMES = 256            # label names kept in the catalog entry (the count is always kept)
MAX_LABEL_CHARS = 64
NLI_WORDS = ("entail", "contradict")


def _architecture(config: dict) -> Optional[str]:
    archs = [a for a in config.get("architectures") or [] if isinstance(a, str)]
    return next((a for a in archs if a.endswith("ForSequenceClassification")), None)


def label_names(config: dict) -> list[str]:
    """config.json's `id2label` values in index order ([] when it has none)."""
    labels = config.get("id2label")
    if not isinstance(labels, dict):
        return []

    def index(k) -> int:
        try:
            return int(k)
        except (TypeError, ValueError):
            return 1 << 30

    return [str(v)[:MAX_LABEL_CHARS] for _, v in sorted(labels.items(), key=lambda kv: index(kv[0]))]


def summary(config: dict) -> Optional[dict]:
    """What the catalog keeps of a sequence-classification model (None for any other model): its architecture,
    how many labels it has, their names in index order, and its problem type; an NLI (zero-shot) model also
    `zero_shot: true` (1.5.4) and its entailment label (the one the transformers server scores)."""
    if not isinstance(config, dict):
        return None
    n = sources.sequence_labels(config)
    if n is None:
        return None
    problem = config.get("problem_type")
    names = label_names(config)
    out = {"architecture": _architecture(config), "labels": n, "label_names": names[:MAX_LABEL_NAMES],
           "problem": problem if isinstance(problem, str) and problem else None}
    if is_nli(names):
        out["zero_shot"] = True
        out["entailment"] = next((x for x in names if x.lower().startswith("entail")), None)
    return out


def is_nli(names: list[str]) -> bool:
    """Labels of a natural-language-inference head (entailment / neutral / contradiction, entailment /
    not_entailment): a zero-shot classifier, not a fixed-label one."""
    return any(w in n.lower() for n in names for w in NLI_WORDS)


def is_zero_shot(meta: dict) -> bool:
    """A catalog entry's text classifier is a zero-shot (NLI) one, told the labels in each request (1.5.4): marked so
    when it was added, or (an entry added before 1.5.4, when NLI models were refused) known by its label names."""
    s = meta.get("classify") if isinstance(meta.get("classify"), dict) else None
    if s is None:
        return False
    return bool(s.get("zero_shot")) or is_nli([str(x) for x in s.get("label_names") or []])


def refusal(s: Optional[dict]) -> Optional[str]:
    """Why a model (its `summary`) cannot be added as a text classification model at all (no engine runs it), or
    None. What only vLLM cannot run is `vllm_refusal`'s."""
    if s is None:
        return ("This is not a text classifier: Forgeyard serves text classification with sequence-classification "
                "models (an architecture ending in ForSequenceClassification and two labels or more in config.json).")
    if s.get("labels") == 1:
        return ("This model has one output (num_labels 1 in its config.json): it scores pairs, so it is a reranker. "
                "Add it as a rerank model.")
    if s.get("problem") == "regression":
        return "This model predicts numbers (problem_type regression in its config.json), not labels."
    return None


def vllm_refusal(s: Optional[dict]) -> Optional[str]:
    """Why vLLM's pooling runner (vllm-pooling) cannot serve this text classifier (its `summary`), or None: an NLI
    model, or an architecture vLLM does not run as a classifier. Forgeyard's transformers server runs both
    (1.5.4)."""
    if s is None or refusal(s):
        return refusal(s)
    if s.get("zero_shot") or is_nli(list(s.get("label_names") or [])):
        return ("This is a zero-shot (NLI) model: it compares a text with a hypothesis, so it needs the labels in the "
                "request, which vLLM's /classify does not take. Deploy it on Forgeyard's transformers server "
                "(transformers), which takes them.")
    arch = s.get("architecture")
    if arch not in VLLM_ARCHITECTURES:
        return (f"vLLM {VLLM_CHECKED_WITH} cannot run {arch} as a text classifier: it runs BERT, RoBERTa, "
                "XLM-RoBERTa, ModernBERT, GTE, GPT-2 and Jamba classifiers. Deploy it on Forgeyard's transformers "
                "server (transformers), which runs DeBERTa, DistilBERT, BART and the others.")
    return None


def out(meta: dict) -> Optional[dict]:
    """A text classifier's labels for the catalog and the Playground, or None. A zero-shot (NLI) one says so
    (1.5.4): its labels are the request's, not these."""
    s = meta.get("classify") if isinstance(meta.get("classify"), dict) else None
    if s is None:
        return None
    o = {k: s.get(k) for k in ("architecture", "labels", "label_names", "problem") if s.get(k) is not None}
    if is_zero_shot(meta):
        o["zero_shot"] = True
    return o
