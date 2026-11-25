"""scripts/check_engine_libs.py: what it reads from ldd inside the agent image, and its verdict (FY-ELIB-*). CI runs
the script itself against the built image (agent-image job)."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    spec = importlib.util.spec_from_file_location("check_engine_libs", ROOT / "scripts" / "check_engine_libs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_engine_libs"] = module
    spec.loader.exec_module(module)
    return module


libs = load()

# What the program prints in the image for an archive like stable-diffusion.cpp's, with libgomp1 missing (taken from a
# real run against the agent image's base, trimmed).
REPORT = """\
== /engine/libggml-cpu-haswell.so
\tlinux-vdso.so.1 (0x00007ffd2b3f2000)
\tlibggml-base.so.0 => /engine/libggml-base.so.0 (0x00007f2c1a2a1000)
\tlibgomp.so.1 => not found
\tlibstdc++.so.6 => /lib/x86_64-linux-gnu/libstdc++.so.6 (0x00007f2c1a000000)
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f2c19e0c000)
== /engine/sd-server
\tlinux-vdso.so.1 (0x00007ffcec3f8000)
\tlibstable-diffusion.so => /engine/libstable-diffusion.so (0x00007f1ec3f40000)
\tlibgomp.so.1 => not found
\tlibwebp.so.7 => not found
\tlibgomp.so.1 => not found
\t/lib64/ld-linux-x86-64.so.2 (0x00007f1ec6807000)
== /engine/static-tool
\tnot a dynamic executable
== /engine/bin/ok-tool
\tstatically linked
"""


def test_fy_elib_001_reads_each_files_missing_libraries():
    """FY-ELIB-001: every ELF file checked is listed, with the libraries ldd did not find (each once); a static file or
    one ldd cannot read has none."""
    assert libs.parse_report(REPORT) == {
        "/engine/libggml-cpu-haswell.so": ["libgomp.so.1"],
        "/engine/sd-server": ["libgomp.so.1", "libwebp.so.7"],
        "/engine/static-tool": [],
        "/engine/bin/ok-tool": [],
    }


def engine(binary="sd-server"):
    return libs.ProcessEngine("sd-cpp", "cpu-process", "https://example.invalid/sd.zip", "0" * 64, binary)


def test_fy_elib_002_verdict():
    """FY-ELIB-002: a missing library fails, named once with the files that need it; an engine program that was not
    among the files checked fails; an archive whose every library is found passes."""
    found = libs.problems(engine(), libs.parse_report(REPORT))
    assert found == ["libgomp.so.1 not found, needed by 2 files: libggml-cpu-haswell.so, sd-server",
                     "libwebp.so.7 not found, needed by 1 file: sd-server"]
    clean = {"/engine/sd-server": [], "/engine/libggml-base.so": []}
    assert libs.problems(engine(), clean) == []
    gone = libs.problems(engine("bin/sd-server"), clean)
    assert len(gone) == 1 and gone[0].startswith("bin/sd-server (the engine program) was not checked")
    many = {f"/engine/lib{i}.so": ["libgomp.so.1"] for i in range(5)} | {"/engine/sd-server": []}
    assert libs.problems(engine(), many) == [
        "libgomp.so.1 not found, needed by 5 files: lib0.so, lib1.so, lib2.so and 2 more"]


def test_fy_elib_003_checks_the_built_in_process_engines():
    """FY-ELIB-003: the archives checked are the built-in recipes' process variants for the architecture: today
    llama.cpp's and stable-diffusion.cpp's CPU archives on x86_64, none on aarch64 (no recipe has one there)."""
    x86 = libs.process_engines("x86_64")
    assert {e.label for e in x86} == {"llama-cpp/cpu-process", "sd-cpp/cpu-process"}
    for e in x86:
        assert e.url.startswith("https://") and len(e.sha256) == 64 and e.binary
    assert libs.process_engines("aarch64") == []


def test_fy_elib_004_ci_runs_the_check_on_the_built_agent_image():
    """FY-ELIB-004: the agent-image job runs the script on the image it built, after building it."""
    import yaml

    steps = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["agent-image"]["steps"]
    runs = [s.get("run", "") for s in steps]
    build = next(i for i, r in enumerate(runs) if "docker build -f Dockerfile.agent -t forgeyard-agent:ci" in r)
    check = next(i for i, r in enumerate(runs) if "scripts/check_engine_libs.py forgeyard-agent:ci" in r)
    assert check > build
