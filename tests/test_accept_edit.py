"""The acceptance scripts' checks of images given to a model (1.5.5, #143), run here against Forgeyard on a real uvicorn
server with fake engines: scripts/accept_img.py's edit checks (--edit) and scripts/accept_video.py's start-image checks
(--image), and their place in scripts/accept_all.py's img and video groups (FY-AED-*). The same functions run against a
real server in the lab acceptance."""

from __future__ import annotations

import importlib.util
import sys
import types
import zlib
from pathlib import Path

import pytest

from tests.conftest import OWNER
from tests.gateway_helpers import LiveServer
from tests.test_image_edits import ed  # noqa: F401  (the fixture)
from tests.test_image_to_video import iv  # noqa: F401  (the fixture)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


NAMES = ("accept_img", "accept_video", "accept_all")


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod      # accept_all registers itself under its name (accept_scale imports it)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def scripts():
    sys.path.insert(0, str(SCRIPTS))
    try:
        import accept_m3

        yield types.SimpleNamespace(img=load("accept_img"), video=load("accept_video"), m3=accept_m3, all=load("accept_all"))
    finally:
        sys.path.remove(str(SCRIPTS))
        for name in NAMES:
            sys.modules.pop(name, None)


class Checks:
    """What a script's check() was told, by status."""

    def __init__(self):
        self.all: list[tuple[str, bool, str, bool]] = []

    def __call__(self, name, ok, detail="", info=False, skip=False):
        self.all.append((name, bool(ok), str(detail), bool(info or skip)))

    @property
    def failed(self):
        return [c for c in self.all if not c[1] and not c[3]]

    def passed(self, part: str) -> bool:
        return any(part in c[0] and c[1] and not c[3] for c in self.all)


def signed_in(m3, url: str):
    m3.TLS = None
    c = m3.Client(url)
    c.ok("POST", "/api/auth/login", {"username": OWNER[0], "password": OWNER[1]})
    return c


def deployment(c, name: str) -> dict:
    d = next(x for x in c.ok("GET", "/api/deployments")["deployments"] if x["name"] == name)
    return c.ok("GET", f"/api/deployments/{d['id']}")["deployment"]


def test_fy_aed_001_the_pictures_and_answers_it_reads(scripts):
    """FY-AED-001: the input pictures accept_img.py draws (no Pillow) are whole PNGs of the size asked (any size, a square
    or a circle, the centre painted, a corner white); data_url and code_of read and write what the gateway takes and
    answers."""
    img = scripts.img
    for w, h, shape in ((768, 512, "square"), (512, 512, "circle"), (2080, 64, "square"), (1, 1, "square")):
        data = img.scene_png(w, h, (30, 60, 216), shape)
        assert img.decode_image(data) == (w, h, "png")
    # The pixels: 64 x 32, a red square in the middle, white around it.
    data = img.scene_png(64, 32)
    n = int.from_bytes(data[33:37], "big")
    raw = zlib.decompress(data[41:41 + n])
    row = 1 + 3 * 64
    assert len(raw) == 32 * row and raw[16 * row + 1 + 3 * 32:16 * row + 4 + 3 * 32] == bytes((216, 30, 30))
    assert raw[1:4] == b"\xff\xff\xff" and raw[16 * row + 1:16 * row + 4] == b"\xff\xff\xff"
    assert img.data_url(b"\x89PNG").startswith("data:image/png;base64,iVBO")
    # What it says about the images an edit made: the size asked, else the first input's shape within the largest side;
    # never an input given back; each must decode. The kilopixels are counted as usage counts them.
    a, b = img.scene_png(1024, 672), img.scene_png(1024, 1024)
    assert img.edit_problems([a, a], None, (768, 512), 1024, []) == ([], 2 * round(1024 * 672 / 1000))
    assert img.edit_problems([b], (1024, 1024), (768, 512), 1024, []) == ([], round(1024 * 1024 / 1000))
    assert img.edit_problems([b], (768, 768), (768, 512), 1024, [])[0] == ["image 0 is 1024x1024, 768x768 asked"]
    assert "not the first input's shape" in img.edit_problems([b], None, (768, 512), 1024, [])[0][0]
    assert "not the first input's shape" in img.edit_problems([img.scene_png(1536, 1008)], None, (768, 512), 1024, [])[0][0]
    assert img.edit_problems([a], None, (768, 512), 1024, [a])[0] == ["image 0 is an input, unchanged"]
    assert img.edit_problems([b"not an image"], None, (768, 512), 1024, [])[0] == ["image 0: not a PNG, JPEG or WebP"]
    assert img.code_of('{"error": {"code": "mask_not_supported", "message": "x"}}') == "mask_not_supported"
    assert img.code_of("not json") is None and img.code_of('{"detail": "x"}') is None


def test_fy_aed_002_the_edit_checks_pass_against_a_server_that_edits(scripts, ed):  # noqa: F811
    """FY-AED-002: exercise_edit on a FLUX.2 [klein] deployment that takes 2 input images (fake vLLM-Omni engine): every
    check passes. One input by the SDK without a size keeps the input's landscape shape, two inputs and two images at
    the size asked, JSON with a data: URL and with an http(s) URL the gateway fetches; the five refusals; usage counts 5
    input images (4 sent + 1 fetched), their bytes and the images made. The engine got the inputs as image[] files and
    never the URL. On a model that edits nothing it is one SKIP and nothing is sent."""
    live = LiveServer()
    try:
        c = signed_in(scripts.m3, live.url)
        httpx = scripts.m3.sdk_httpx()
        import openai

        checks = Checks()
        report = {"deployments": {}}
        args = types.SimpleNamespace(image_url=f"{ed.media.url}/img.png")
        scripts.img.exercise_edit(c, deployment(c, "klein"), "klein", 1024, args, checks, httpx, openai, report, None)
        assert checks.failed == [], checks.failed
        for part in ("one input, multipart with the SDK, the input's shape", "two inputs and two images at 1024x1024",
                     "JSON with a data: URL", "JSON with an http(s) URL the gateway fetches", "an edit without an image is refused",
                     "a mask is refused", "a file_id is refused", "3 input images are refused (it takes 2)",
                     "an input wider than 2048 px is refused", "usage counts the input images of the edits",
                     "usage counts their bytes", "usage counts the edited images"):
            assert checks.passed(part), part
        edits = ed.e["klein"].edits()
        assert [len(e["files"]) for e in edits] == [1, 2, 1, 1]
        assert all(name == "image[]" for e in edits for name, _, _ in e["files"])
        assert edits[0]["fields"]["size"] == "1024x672"      # the landscape input's shape at the default pixels
        assert not any("img.png" in str(e["fields"]) for e in edits)
        assert report["deployments"]["klein"]["edit_inputs"] == 5
        # A model that edits nothing: one SKIP.
        checks = Checks()
        scripts.img.exercise_edit(c, deployment(c, "sd"), "sd", 512, args, checks, httpx, openai, {"deployments": {}}, None)
        assert [(x[0], x[3]) for x in checks.all] == [("sd: edits", True)] and not ed.e["sd"].seen
    finally:
        live.close()


def test_fy_aed_003_the_start_image_checks_pass_for_optional_required_and_none(scripts, iv):  # noqa: F811
    """FY-AED-003: exercise_image against the three fake video deployments: Wan2.2-TI2V-5B (optional) and an
    image-to-video model (required) make an MP4 of the size and length asked from a start image uploaded with the SDK
    and from a data: URL, refuse a file_id, a start image wider than the deployment takes and a file that is not a
    picture; the required one refuses a request without an image; the usage counts one media item and the image's
    bytes per job. Wan2.1 (none) refuses a start image uploaded and by URL. Every check passes."""
    live = LiveServer()
    try:
        c = signed_in(scripts.m3, live.url)
        httpx = scripts.m3.sdk_httpx()
        import openai

        args = types.SimpleNamespace(seconds=1, steps=None, job_timeout=60)
        spec = {"fps": 16}
        for name, size, takes in (("ti2v", "832x480", "optional"), ("i2v", "832x480", "required"), ("t2v", "832x480", "none")):
            checks = Checks()
            report = {"deployments": {}}
            d = deployment(c, name)
            d["params"] = dict(d.get("params") or {}, max_seconds=1)
            scripts.video.exercise_image(c, d, name, spec, size, args, checks, httpx, openai, report, None)
            assert checks.failed == [], (name, checks.failed)
            assert report["deployments"][name]["image_input"] == takes
            if takes == "none":
                assert [x[0] for x in checks.all] == [f"{name}: a start image is refused (it makes videos from text only), uploaded",
                                                      f"{name}: a start image is refused (it makes videos from text only), by URL"]
                assert not iv.engines[name].posts
                continue
            for part in ("start image uploaded with the SDK: an MP4 of the size and length asked",
                         "start image as a data: URL (JSON): an MP4 of the size and length asked", "a file_id start image is refused",
                         "wider than", "not a picture is refused", "one media item per start image", "the start images' bytes"):
                assert checks.passed(part), (name, part)
            assert checks.passed("without a start image is refused") == (takes == "required")
            posted = iv.engines[name].posts
            assert len(posted) == 2
    finally:
        live.close()


def test_fy_aed_004_the_full_regression_runs_them(scripts):
    """FY-AED-004: accept_all's img group edits with both recommended FLUX.2 [klein] entries (vLLM-Omni on the GPU
    machine, stable-diffusion.cpp on the CPU machine at 512 px) with two input images, and its video group checks start
    images with Wan2.2-TI2V-5B on the GPU machine; every model it names is one the scripts know, pinned to the revision
    the recommended list holds; --img-edit-url names the address the server fetches."""
    from forgeyard.models import recommended as rec

    acc, img, video = scripts.all, scripts.img, scripts.video
    pinned = {e.name: (e.source, e.revision) for e in rec.load().models}
    assert [(r, n) for r, n, _ in acc.IMG_EDIT_MODELS] == [("gpu", "flux.2-klein-4b"), ("cpu", "flux.2-klein-4b-sdcpp")]
    assert dict((n, s) for _, n, s in acc.IMG_EDIT_MODELS)["flux.2-klein-4b-sdcpp"] == 512 and acc.EDIT_INPUTS == 2
    for _, name, _ in acc.IMG_EDIT_MODELS:
        assert img.MODELS[name]["edit"] and (img.MODELS[name]["source"], img.MODELS[name]["revision"]) == pinned[name]
        if "file" in img.MODELS[name]:
            entry = next(e for e in rec.load().models if e.name == name)
            assert (img.MODELS[name]["file"], img.MODELS[name]["components"], img.MODELS[name]["engine_args"]) == (
                entry.file, entry.components, entry.engine_args)
    assert acc.VIDEO_START_MODELS == (("gpu", "wan2.2-ti2v-5b"),)
    assert (video.MODELS["wan2.2-ti2v-5b"]["source"], video.MODELS["wan2.2-ti2v-5b"]["revision"]) == pinned["wan2.2-ti2v-5b"]
    args = acc.parse_args(["--server", "https://127.0.0.1:9"])
    assert args.img_edit_url.startswith("http://images.cocodataset.org/")
    assert acc.parse_args(["--server", "https://127.0.0.1:9", "--img-edit-url", "none"]).img_edit_url == "none"
