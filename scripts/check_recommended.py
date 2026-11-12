"""Check forgeyard/models/recommended.yaml against the real Hugging Face.

For every entry: the repository exists, Forgeyard's inspector can read it at its revision (config,
safetensors headers, GGUF metadata), it holds the format the file says, and the estimate with the
suggested recipe's defaults (printed next to the file's download_gib and memory_gib, so they can be
updated), the licence and what it takes besides text (inputs) against what the inspector reads, and
for GGUF vision models the projector file. Gated entries need a token whose account accepted the licence: without HF_TOKEN they are
only checked to exist and to be gated.

    HF_TOKEN=hf_… python scripts/check_recommended.py [name …]

Exit status 1 if any entry fails.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forgeyard.models import fetch, inspect as inspector, recipes  # noqa: E402
from forgeyard.models.estimate import estimate  # noqa: E402
from forgeyard.models.recommended import recommended  # noqa: E402

GIB = 1024**3
ENDPOINT = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")


def main(names: list[str]) -> int:
    token = os.environ.get("HF_TOKEN") or None
    builtin = {n: r for n, (r, _) in recipes.builtin_recipes().items()}
    policy = fetch.Policy()
    failed = 0
    for e in recommended().models:
        if names and e.name not in names:
            continue
        repo = e.source.removeprefix("hf://")
        try:
            meta = inspector.inspect_hf(repo, e.revision, e.file, endpoint=ENDPOINT, policy=policy, token=token,
                                        mmproj=e.mmproj if e.format == "gguf" and e.mmproj else
                                        ("none" if e.format == "gguf" else None))
        except inspector.InspectError as err:
            if e.gated and not token and "gated" in str(err):
                info = json.loads(fetch.get_bytes(f"{ENDPOINT}/api/models/{repo}", policy))
                ok = bool(info.get("gated"))
                print(f"{'ok  ' if ok else 'FAIL'} {e.name:30} gated ({info.get('gated')}): needs a token to inspect")
                failed += not ok
            else:
                print(f"FAIL {e.name:30} {err}")
                failed += 1
            continue
        problems = []
        if meta["format"] != e.format:
            problems.append(f"format is {meta['format']}, the file says {e.format}")
        if bool(meta.get("gated")) != e.gated:
            problems.append(f"gated is {meta.get('gated')}, the file says {e.gated}")
        lic = meta.get("license")
        if lic is None and e.license_from:
            # A conversion without a licence of its own: the original model's.
            origin = json.loads(fetch.get_bytes(f"{ENDPOINT}/api/models/{e.license_from.removeprefix('hf://')}", policy))
            lic = inspector.license_of(origin)
        if lic != e.license:
            problems.append(f"the licence is {lic}, the file says {e.license}")
        # A speech model's projector is its decoder (and voice encoder), not media it takes in chat.
        if e.task != "tts" and sorted(meta.get("modalities") or []) != sorted(e.inputs):
            problems.append(f"it takes {meta.get('modalities')}, the file says {e.inputs}")
        recipe = builtin[e.recipe]
        params = {k: p.default for k, p in recipe.params.items()} | {"gpus": 0 if e.device == "cpu" else 1,
                                                                       "share": recipe.share}
        est = estimate(meta, recipe, params, device=e.device, gpu_context=400 * 1024**2, task=e.task)
        size, need = meta["size_bytes"] / GIB, est.amount / GIB
        line = (f"{e.name:30} {meta['revision'][:12]}  download {size:6.2f} GiB (file: {e.download_gib:6.2f})  "
                f"{e.device} {need:6.2f} GiB (file: {e.memory_gib:6.2f})")
        print(("FAIL " if problems else "ok   ") + line + ("  " + "; ".join(problems) if problems else ""))
        failed += bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
