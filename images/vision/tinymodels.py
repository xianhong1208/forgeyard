"""Tiny random transformers models for the vision server's tests and smoke test (1.5.4): real classes, real files
(config.json, safetensors, tokenizer, preprocessor), a few hundred kilobytes each, made offline with a fixed seed.
They say nothing about quality; they make the server load and answer each kind of model the transformers recipe
runs, the way it loads real ones.

    python tinymodels.py <directory>          # writes <directory>/<name>/ for each of MODELS

Needs torch, transformers and tokenizers (the image's own environment).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORDS = ["a", "an", "the", "photo", "of", "cat", "cats", "dog", "car", "street", "this", "text", "is", "about",
         "sports", "politics", "cooking", "technology", "travel", "good", "bad", "i", "love", "it", "word", "."]
MODELS = ("clip", "siglip", "dinov2", "bert-fixed", "bert-multi-label", "bert-nli-2", "bert-nli-3")


def tokenizer(max_length: int):
    """A word-level fast tokenizer with BERT's special tokens; unknown words become [UNK]."""
    from tokenizers import Tokenizer, models, pre_tokenizers, processors
    from transformers import PreTrainedTokenizerFast

    vocab = {t: i for i, t in enumerate(["[PAD]", "[UNK]", "[CLS]", "[SEP]"] + WORDS)}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Sequence([pre_tokenizers.Whitespace(), pre_tokenizers.Punctuation()])
    tok.post_processor = processors.TemplateProcessing(single="[CLS] $A [SEP]", pair="[CLS] $A [SEP] $B [SEP]",
                                                       special_tokens=[("[CLS]", 2), ("[SEP]", 3)])
    return PreTrainedTokenizerFast(tokenizer_object=tok, pad_token="[PAD]", unk_token="[UNK]", cls_token="[CLS]",
                                   sep_token="[SEP]", model_max_length=max_length)


def _bert(out: Path, labels: list[str], problem: str | None = None) -> None:
    from transformers import BertConfig, BertForSequenceClassification

    cfg = BertConfig(vocab_size=4 + len(WORDS), hidden_size=16, num_hidden_layers=1, num_attention_heads=2,
                     intermediate_size=32, max_position_embeddings=64, num_labels=len(labels),
                     id2label=dict(enumerate(labels)), label2id={n: i for i, n in enumerate(labels)},
                     problem_type=problem)
    BertForSequenceClassification(cfg).eval().save_pretrained(out)
    tokenizer(64).save_pretrained(out)


def make(name: str, out: Path) -> Path:
    import torch

    torch.manual_seed(0)
    out.mkdir(parents=True, exist_ok=True)
    if name in ("clip", "siglip"):
        if name == "clip":
            from transformers import CLIPConfig as Config, CLIPImageProcessor as ImageProcessor, CLIPModel as Model
            from transformers import CLIPProcessor as Processor
        else:
            from transformers import SiglipConfig as Config, SiglipImageProcessor as ImageProcessor, SiglipModel as Model
            from transformers import SiglipProcessor as Processor
        text = {"vocab_size": 4 + len(WORDS), "hidden_size": 16, "intermediate_size": 32, "num_hidden_layers": 1,
                "num_attention_heads": 2, "max_position_embeddings": 16, "pad_token_id": 0, "bos_token_id": 2,
                "eos_token_id": 3}
        vision = {"hidden_size": 16, "intermediate_size": 32, "num_hidden_layers": 1, "num_attention_heads": 2,
                  "image_size": 32, "patch_size": 8}
        kw = {"projection_dim": 12} if name == "clip" else {}
        Model(Config(text_config=text, vision_config=vision, **kw)).eval().save_pretrained(out)
        size = {"shortest_edge": 32} if name == "clip" else {"height": 32, "width": 32}
        ip = ImageProcessor(size=size, crop_size={"height": 32, "width": 32}) if name == "clip" else ImageProcessor(size=size)
        Processor(image_processor=ip, tokenizer=tokenizer(16)).save_pretrained(out)
    elif name == "dinov2":
        from transformers import BitImageProcessor, Dinov2Config, Dinov2Model

        Dinov2Model(Dinov2Config(hidden_size=16, num_hidden_layers=1, num_attention_heads=2, intermediate_size=32,
                                 image_size=32, patch_size=8)).eval().save_pretrained(out)
        BitImageProcessor(size={"shortest_edge": 32}, crop_size={"height": 32, "width": 32}).save_pretrained(out)
    elif name == "bert-fixed":
        _bert(out, ["negative", "neutral", "positive"])
    elif name == "bert-multi-label":
        _bert(out, ["toxic", "insult", "threat"], "multi_label_classification")
    elif name == "bert-nli-2":
        _bert(out, ["entailment", "not_entailment"])
    elif name == "bert-nli-3":
        _bert(out, ["entailment", "neutral", "contradiction"])
    else:
        raise SystemExit(f"no tiny model {name!r}: one of {', '.join(MODELS)}")
    # A model directory as the model cache holds one: nothing but files (no generation config is needed).
    json.loads((out / "config.json").read_text())
    return out


def main(dest: str) -> None:
    for name in MODELS:
        print(name, make(name, Path(dest) / name), flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tiny-models")
