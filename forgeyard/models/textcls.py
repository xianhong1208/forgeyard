"""Text classification (1.4.4, #144): a small encoder with a fixed set of labels (sentiment, language, toxicity,
topic…) as a model kind of the catalog (task `classify`), served by vLLM's pooling runner through the built-in
`vllm-pooling` recipe and answered at /v1/classify (forgeyard/gateway/classify.py).

What counts as one, read from a transformers repository's config.json with vLLM's own rule
(vllm/entrypoints/pooling/utils.py `enable_scoring_api` at v0.32.0): a sequence-classification model
(an architecture ending in `ForSequenceClassification`) with **two labels or more** is a classifier; with one
output it scores pairs and is a reranker (task `rerank`), never a classifier. The labels are counted as
transformers counts them (forgeyard/models/sources.py `sequence_labels`).

Refused, with the reason, when it is added as a text classification model:

- **one output** (num_labels 1): a reranker; add it as a rerank model;
- **a regression head** (`problem_type: regression`): it predicts numbers, not labels;
- **an NLI (zero-shot) model** (labels such as entailment / not_entailment / contradiction): it compares a
  text with a hypothesis, so it needs labels in the request, which /classify does not take. Zero-shot
  classification comes with the transformers engine in 1.5 (#144); a chat model already classifies into
  any labels with structured outputs (docs/guide/how-to.md);
- **an architecture vLLM 0.32 does not run as a classifier** (VLLM_ARCHITECTURES): DeBERTa, DistilBERT, BART
  and the others come with the transformers engine in 1.5 (#144).

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
    how many labels it has, their names in index order, and its problem type."""
    if not isinstance(config, dict):
        return None
    n = sources.sequence_labels(config)
    if n is None:
        return None
    problem = config.get("problem_type")
    return {"architecture": _architecture(config), "labels": n, "label_names": label_names(config)[:MAX_LABEL_NAMES],
            "problem": problem if isinstance(problem, str) and problem else None}


def is_nli(names: list[str]) -> bool:
    """Labels of a natural-language-inference head (entailment / neutral / contradiction, entailment /
    not_entailment): a zero-shot classifier, not a fixed-label one."""
    return any(w in n.lower() for n in names for w in NLI_WORDS)


def refusal(s: Optional[dict]) -> Optional[str]:
    """Why a model (its `summary`) cannot be added as a text classification model, or None."""
    if s is None:
        return ("This is not a text classifier: Forgeyard serves text classification with sequence-classification "
                "models (an architecture ending in ForSequenceClassification and two labels or more in config.json).")
    if s.get("labels") == 1:
        return ("This model has one output (num_labels 1 in its config.json): it scores pairs, so it is a reranker. "
                "Add it as a rerank model.")
    if s.get("problem") == "regression":
        return "This model predicts numbers (problem_type regression in its config.json), not labels."
    if is_nli(list(s.get("label_names") or [])):
        return ("This is a zero-shot (NLI) model: it compares a text with a hypothesis, so it needs the labels in "
                "the request. Zero-shot classification comes with the transformers engine in 1.5 (#144); a chat "
                "model can classify into any labels today with structured outputs (see the guide).")
    arch = s.get("architecture")
    if arch not in VLLM_ARCHITECTURES:
        return (f"vLLM {VLLM_CHECKED_WITH} cannot run {arch} as a text classifier: it runs BERT, RoBERTa, "
                "XLM-RoBERTa, ModernBERT, GTE, GPT-2 and Jamba classifiers. DeBERTa, DistilBERT and BART classifiers "
                "come with the transformers engine in 1.5 (#144).")
    return None


def out(meta: dict) -> Optional[dict]:
    """A text classifier's labels for the catalog and the Playground, or None."""
    s = meta.get("classify") if isinstance(meta.get("classify"), dict) else None
    if s is None:
        return None
    return {k: s.get(k) for k in ("architecture", "labels", "label_names", "problem") if s.get(k) is not None}
