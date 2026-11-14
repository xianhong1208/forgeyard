"""Fetch the models of models.json at their pinned revisions into a directory (the image does this at build
time, so a container never needs the network). Anonymous: every model is public and ungated.

    python download.py /models
"""

import json
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

HERE = Path(__file__).resolve().parent


def main(dest: str) -> None:
    for task, m in json.loads((HERE / "models.json").read_text()).items():
        target = Path(dest) / m["dir"]
        for name in m["files"]:
            hf_hub_download(m["repo"], name, revision=m["revision"], local_dir=target)
        print(f"{task}: {m['repo']}@{m['revision'][:12]} -> {target}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/models")
