"""scripts/accept_transformers.py's own parts (1.5.4, #141 and #144), without a server: the pictures it draws, the
arithmetic it judges with, the plan, and the SKIP while the transformers recipe's image is older than 1.5.4 (FY-ATF-*)."""

import argparse
import importlib.util
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "accept_transformers.py"


@pytest.fixture(scope="module")
def at():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("accept_transformers", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(SCRIPT.parent))


def pixels(png: bytes) -> tuple[int, int, bytes]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", png[16:24])
    at, idat = 8, b""
    while at < len(png):
        n = struct.unpack(">I", png[at:at + 4])[0]
        kind = png[at + 4:at + 8]
        if kind == b"IDAT":
            idat += png[at + 8:at + 8 + n]
        at += 12 + n
    return w, h, zlib.decompress(idat)


def test_fy_atf_001_the_pictures_it_draws(at):
    """FY-ATF-001: a red square and a blue circle on white, valid PNGs of the size asked, the same drawing at any size
    (the centre is the shape's colour, a corner white)."""
    for shape, colour in (("square", b"\xd8\x1e\x1e"), ("circle", b"\x1e\x3c\xd8")):
        for side in (224, 160):
            w, h, raw = pixels(at.shape_png(shape, side))
            assert (w, h) == (side, side) and len(raw) == side * (1 + 3 * side)
            row = raw[(side // 2) * (1 + 3 * side) + 1:][:3 * side]
            assert row[3 * (side // 2):3 * (side // 2) + 3] == colour
            assert raw[1:4] == b"\xff\xff\xff"
    assert at.shape_png("square") != at.shape_png("circle")
    assert at.data_uri(b"\x89PNG").startswith("data:image/png;base64,iVBO")


def test_fy_atf_002_the_arithmetic_it_judges_with(at):
    """FY-ATF-002: cosine, every vector of length 1 (within 0.001), the nearest candidate; the topics a zero-shot answer
    got right (by its label, else its highest probability); the error code of either answer shape."""
    assert at.cosine([1, 0], [1, 0]) == pytest.approx(1) and at.cosine([1, 0], [0, 1]) == 0 and at.cosine([0, 0], [1, 0]) == 0
    assert at.unit_lengths([[0.6, 0.8], [1.0, 0.0]]) and not at.unit_lengths([[0.6, 0.81]]) and not at.unit_lengths([])
    best, sims = at.nearest([1, 0], [[0, 1], [0.9, 0.1], [0.5, 0.5]])
    assert best == 1 and len(sims) == 3
    gold = ["財經", "運動", "政治"]
    answer = {"data": [{"index": 0, "label": "財經", "probs": [0.9, 0.05, 0.05]}, {"index": 1, "label": None, "probs": [0.1, 0.8, 0.1]},
                       {"index": 2, "label": "財經", "probs": [0.5, 0.1, 0.4]}, {"index": 7, "label": "x", "probs": [1]}]}
    assert at.topics_right(answer, gold, gold) == 2
    assert at.topics_right({}, gold, gold) == 0
    assert at.code({"error": {"code": "too_many_pairs"}}) == "too_many_pairs"
    assert at.code({"detail": {"code": "incompatible"}}) == "incompatible"
    assert at.code({"error": "x"}) is None and at.code(None) is None


def test_fy_atf_003_the_plan_and_the_names(at):
    """FY-ATF-003: the CPU models when there is a CPU machine, then the GPU ones when there is a GPU machine; the
    defaults are known models (CLIP, DINOv2, DistilBERT, bge-m3 zero-shot; CLIP and bge-m3 on the GPU); a deployment's
    name is the run's prefix, where it runs and the model, without dots, at most 48 characters."""
    cpu, gpu = {"name": "cpu"}, {"name": "gpu"}
    assert at.plan(["a", "b"], ["a"], cpu, gpu) == [("cpu", "a"), ("cpu", "b"), ("gpu", "a")]
    assert at.plan(["a"], ["a"], None, gpu) == [("gpu", "a")]
    assert at.plan(["a"], ["a"], cpu, None) == [("cpu", "a")]
    assert at.plan(["a"], [], None, None) == []
    assert all(n in at.MODELS for n in at.DEFAULT_ONLY.split(",") + at.DEFAULT_GPU.split(","))
    kinds = {at.MODELS[n]["kind"] for n in at.DEFAULT_ONLY.split(",")}
    assert kinds == {"clip", "image-only", "fixed", "zero-shot"}
    assert at.deployment_name("accept-tf-", "cpu", "bge-m3-zeroshot-v2.0-c") == "accept-tf-cpu-bge-m3-zeroshot-v2-0-c"
    assert len(at.deployment_name("accept-ab12-tf-", "gpu", "roberta-base-zeroshot-v2.0-c")) <= 48
    # Every model is pinned to a commit, the zh-TW set has 12 lines over the 10 topics, the shapes and the limits fit.
    assert all(len(m["revision"]) == 40 for m in at.MODELS.values())
    assert len(at.ZH) == 12 and {g for _, g in at.ZH} <= set(at.ZH_LABELS) and len(at.ZH_LABELS) == 10
    assert len(at.ZH) * len(at.ZH_LABELS) <= at.MAX_PAIRS < 13 * len(at.ZH_LABELS)
    assert at.ZH_TEMPLATE.count("{}") == 1


class FakeAdmin:
    """The server as the script sees it: the catalog, the recipes, keys, and an estimate that answers `estimate`."""

    def __init__(self, estimate: tuple[int, dict]):
        self.estimate = estimate
        self.calls: list[tuple[str, str, object]] = []
        self.models = [{"name": n, "task": m["task"]} for n, m in __import__("accept_transformers").MODELS.items()]

    def call(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if path == "/api/deployments/estimate":
            return self.estimate
        if path == "/api/deployments":
            return 201, {"deployment": {"id": 1}}
        return 200, {}

    def ok(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        if path == "/api/models":
            return {"models": self.models, "recipes": [{"name": "transformers"}, {"name": "tf-copy"}]}
        if path == "/api/keys" and method == "POST":
            return {"key": "fyk_x", "api_key": {"id": 9}}
        if path == "/api/keys":
            return {"keys": [], "models": []}
        return {}


def opts(**over):
    base = dict(only="clip-vit-b-32-laion-embed,dinov2-small-embed", gpu_only="", transformers_recipe="transformers", photo=None,
                image_url="http://x/y.jpg", cloud_provider=None, zh_pass=11, threads=4, timeout=10, place_timeout=5, keep=False)
    return argparse.Namespace(**{**base, **over})


def test_fy_atf_004_a_recipe_image_older_than_1_5_4_is_a_skip_with_its_reason(at, monkeypatch):
    """FY-ATF-004: until the transformers recipe's pin moves, its estimate answers 422 vision_image_too_old: the first
    deployment is a SKIP naming that reason, every later one too, nothing is deployed and nothing fails; another error
    stays a failure. --transformers-recipe names the recipe every request uses."""
    monkeypatch.setattr(at, "photo", lambda path: (None, "offline"))
    old = (422, {"detail": {"code": "vision_image_too_old", "message": "Image embeddings … need Forgeyard's vision image 1.5.4 or "
                                                                      "newer; the transformers recipe runs 1.4.4."}})
    seen = []
    admin = FakeAdmin(old)
    x = at.Ctx(admin, "https://h:7860", None, lambda label, ok, detail="", info=False, skip=False: seen.append((label, ok, str(detail), skip)),
               opts(), prefix="accept-tf-")
    at.run(x, {"cpu": {"id": 1, "name": "cpu-a"}, "gpu": None})
    assert [(s[0], s[3]) for s in seen] == [("accept-tf-cpu-clip-vit-b-32-laion-embed: deploy on transformers", True),
                                            ("accept-tf-cpu-dinov2-small-embed: deploy on transformers", True)]
    assert all(s[1] and "vision_image_too_old" in s[2] and "1.5.4" in s[2] for s in seen)
    assert x.too_old and not x.served
    assert not any(c[:2] == ("POST", "/api/deployments") for c in admin.calls)
    estimates = [c for c in admin.calls if c[1] == "/api/deployments/estimate"]
    assert len(estimates) == 1 and estimates[0][2]["recipe"] == "transformers"   # the second is skipped without asking
    # A custom recipe by name; another refusal is a failure, not a skip.
    seen.clear()
    admin = FakeAdmin((422, {"detail": {"code": "incompatible", "message": "no"}}))
    x = at.Ctx(admin, "https://h:7860", None, lambda label, ok, detail="", info=False, skip=False: seen.append((label, ok, str(detail), skip)),
               opts(transformers_recipe="tf-copy", only="dinov2-small-embed"), prefix="accept-tf-")
    at.run(x, {"cpu": {"id": 1, "name": "cpu-a"}, "gpu": None})
    assert [c for c in admin.calls if c[1] == "/api/deployments/estimate"][0][2]["recipe"] == "tf-copy"
    assert seen and seen[0][1] is False and seen[0][3] is False and x.too_old is None
    # A recipe the server does not have: one SKIP, nothing else asked.
    seen.clear()
    admin = FakeAdmin(old)
    x = at.Ctx(admin, "https://h:7860", None, lambda label, ok, detail="", info=False, skip=False: seen.append((label, ok, str(detail), skip)),
               opts(transformers_recipe="nope"), prefix="accept-tf-")
    at.run(x, {"cpu": {"id": 1, "name": "cpu-a"}, "gpu": None})
    assert seen == [("transformers models", True, "the server has no nope recipe (older than 1.5.4, or no such custom recipe)", True)]


def test_fy_atf_005_what_it_creates_it_removes(at):
    """FY-ATF-005: the keys and the catalog entries a run made are removed by undo(), newest first, and only those."""
    admin = FakeAdmin((200, {}))
    x = at.Ctx(admin, "https://h:7860", None, lambda *a, **k: None, opts(), prefix="accept-zz-tf-")
    assert at.new_key(x, "accept-zz-tf-k1") == "fyk_x"
    at.new_key(x, "accept-zz-tf-k2", cloud="on")
    assert [c for c in admin.calls if c[:2] == ("POST", "/api/keys")][1][2] == {"name": "accept-zz-tf-k2", "cloud": "on"}
    admin.calls.clear()
    at.undo(x)
    assert [c[:2] for c in admin.calls] == [("DELETE", "/api/keys/9"), ("DELETE", "/api/keys/9")]
    assert x.undo == []
