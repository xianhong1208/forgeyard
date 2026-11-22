"""The gateway's proxy and the agent's supervisor in parts (1.4.5, #173): every runtime is a `Runtime` (and the
container one a `ContainerEngine`), the supervisor calls nothing on a runtime the protocols leave out, its state
keeps its old names, and every name forgeyard.gateway.proxy had is still there (FY-SPL-*)."""

import ast
import inspect
import typing
from pathlib import Path

import pytest

from forgeyard.agent.workloads.container import ContainerRuntime
from forgeyard.agent.workloads.process import ProcessRuntime
from forgeyard.agent.workloads.runtime import ContainerEngine, Runtime

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ROOT / "forgeyard" / "agent" / "workloads"


def _members(proto) -> list[str]:
    return sorted(n for n, v in vars(proto).items() if inspect.isfunction(v) and not n.startswith("_"))


def test_fy_spl_001_every_runtime_is_a_runtime():
    """FY-SPL-001: the process and container runtimes have every method of `Runtime`; only the container one
    is a `ContainerEngine` (the supervisor pulls an image only for a runtime that has one)."""
    assert _members(Runtime) == ["cleanup", "info", "observe", "owned", "remove", "start", "stop", "tail", "trim"]
    for cls in (ProcessRuntime, ContainerRuntime):
        assert issubclass(cls, Runtime), cls
    assert issubclass(ContainerRuntime, ContainerEngine)
    assert not issubclass(ProcessRuntime, ContainerEngine)
    assert not hasattr(ProcessRuntime, "pull")


@pytest.mark.parametrize("proto,cls", [(Runtime, ProcessRuntime), (Runtime, ContainerRuntime),
                                       (ContainerEngine, ContainerRuntime)])
def test_fy_spl_002_the_runtimes_take_what_the_protocol_passes(proto, cls):
    """FY-SPL-002: each method of a runtime takes the protocol's parameters, by the same names, of the same
    kinds, with a default wherever the protocol has one: a call written against the protocol works on it."""
    for name in _members(proto):
        want = inspect.signature(getattr(proto, name)).parameters
        have = inspect.signature(getattr(cls, name)).parameters
        assert list(want) == list(have), (cls.__name__, name, list(want), list(have))
        for p, q in zip(want.values(), have.values()):
            assert p.kind == q.kind, (cls.__name__, name, p.name)
            assert (p.default is inspect.Parameter.empty) == (q.default is inspect.Parameter.empty), \
                (cls.__name__, name, p.name)


def _runtime_calls(path: Path) -> set[str]:
    """What a module calls on a runtime: `rt.x`, `container.x`, `self.runtimes[...].x`, `hasattr(rt, "x")`."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute):
            v = node.value
            if isinstance(v, ast.Name) and v.id in ("rt", "container"):
                names.add(node.attr)
            elif isinstance(v, ast.Subscript) and isinstance(v.value, ast.Attribute) and v.value.attr == "runtimes":
                names.add(node.attr)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "hasattr"
              and isinstance(node.args[0], ast.Name) and node.args[0].id in ("rt", "container")):
            names.add(node.args[1].value)
    return names


def test_fy_spl_003_the_supervisor_calls_only_what_the_protocols_name():
    """FY-SPL-003: everything the supervisor and its parts call on a runtime is a method of `Runtime` or
    `ContainerEngine` (the runtimes are no longer only duck-typed), and the scan sees the calls."""
    allowed = set(_members(Runtime)) | set(_members(ContainerEngine))
    seen = set()
    for name in ("supervisor", "lifecycle", "fetching", "checks", "keyguards", "admin", "prefetch"):
        calls = _runtime_calls(WORKLOADS / f"{name}.py")
        assert calls <= allowed, (name, calls - allowed)
        seen |= calls
    assert {"start", "stop", "observe", "tail", "pull", "owned", "remove_name", "refers"} <= seen, seen
    hints = typing.get_type_hints(__import__("forgeyard.agent.workloads.supervisor",
                                             fromlist=["Supervisor"]).Supervisor.__init__)
    assert hints["runtimes"] == dict[str, Runtime]


def test_fy_spl_004_the_supervisor_state_keeps_its_names(tmp_path):
    """FY-SPL-004: the supervisor's state is in its typed structures; each field is also its attribute under
    the old name, the same object, and assigning it reaches the structure."""
    from forgeyard.agent.workloads import state
    from forgeyard.agent.workloads.supervisor import Supervisor

    sup = Supervisor(tmp_path, {})
    parts = {"secrets": state.Secrets, "fetches": state.Fetches, "guarding": state.Guarding,
             "work": state.Operations, "outbox": state.Outbox}
    aliased = {}
    for attr, value in vars(Supervisor).items():
        if isinstance(value, state.Alias):
            aliased[(value.part, value.name)] = attr
    for part, cls in parts.items():
        held = getattr(sup, part)
        assert isinstance(held, cls)
        for f in cls.__dataclass_fields__:
            attr = aliased.pop((part, f))      # every field has its old name
            assert getattr(sup, attr) is getattr(held, f), attr
            assert attr not in vars(sup), attr  # never shadowed by an instance attribute
    assert not aliased, aliased
    assert len(parts) == 5 and sum(len(c.__dataclass_fields__) for c in parts.values()) == 18
    sup.full_ready = True
    sup.removed = [{"id": "x"}]
    sup.keys["r1"] = (None, "k")
    assert sup.outbox.full_ready is True and sup.outbox.removed == [{"id": "x"}]
    assert sup.secrets.keys == {"r1": (None, "k")} and sup.guard_errors is sup.guarding.errors


def _defined(path: Path) -> set[str]:
    """The names a module defines at its top level (not the ones it imports)."""
    out = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


# Every name forgeyard/gateway/proxy.py defined before it was split (dev 4f8a437).
PROXY_NAMES = [
    "AUDIO_HEADERS", "AudioResponse", "BYTES_PER_TOKEN", "CEILING_TTL", "CLIENT_CLOSED", "CLOUD_APIS",
    "CLOUD_ENDPOINTS", "ClientGone", "CloudTarget", "DROPPED_HEADER", "ENDPOINTS", "EngineUnreachable",
    "FIRST_ANSWER_DEFAULT", "Failed", "Job", "KIND_WORDS", "LOCAL_BUSY_RETRY_AFTER", "LOCAL_ONLY_HEADER", "LocalGroup",
    "MAX_WAIT_MS", "MEDIA_ENDPOINTS", "NOT_TAKEN_HEADER", "Parsed", "RETRYABLE", "RETRY_AFTER_CAP", "Route",
    "SENT_BROKE", "SSEFilter", "SpeechSSE", "StreamResponse", "Tier", "UPSTREAM_PATHS", "USAGE_ENDPOINTS",
    "_SECRETISH", "_ceiling", "_clients", "_empty", "_int", "_ready", "_route_query", "_takes", "_until_disconnect",
    "allowed_tiers", "body_ceiling", "body_size", "build_route", "check_access", "client", "close_client",
    "cloud_target", "endpoint_for", "fit_classify", "fit_images", "fit_route", "fit_speech", "forget_ceiling",
    "forward", "load_route", "local_group", "log", "model_names", "not_ready_reason", "not_taken", "order_clouds",
    "parse_body", "parse_vision", "prompt_estimate", "provider_error", "read_body", "record", "retry_after", "rng",
    "scan_media", "serving", "streams", "tokens_of", "unless_gone", "upstream_body", "upstream_retry_after",
    "visible", "wants_local_only",
]


def test_fy_spl_005_every_proxy_name_is_still_there():
    """FY-SPL-005: every name the proxy module had is still importable from it, the very object of the part it
    moved to (a module-level setting read at call time, such as `rng` or MAX_WAIT_MS, is that part's), and a
    request is still one `Job` with its replica, provider and answer halves."""
    from forgeyard.gateway import answer, body, cloud, fit, proxy, replicas, route, streaming, upstream

    parts = (route, body, fit, upstream, streaming, cloud, replicas, answer)
    defined = {m: _defined(Path(m.__file__)) for m in parts}
    for name in PROXY_NAMES:
        assert hasattr(proxy, name), name
        homes = [m for m in parts if name in defined[m]]
        if name not in ("Job", "forward", "record", "log"):
            assert len(homes) == 1, (name, homes)   # defined in one part only
            assert getattr(proxy, name) is getattr(homes[0], name), name
    assert proxy.rng is cloud.rng and proxy._clients is upstream._clients and proxy._ceiling is body._ceiling
    assert route.MAX_WAIT_MS == proxy.MAX_WAIT_MS and route.FIRST_ANSWER_DEFAULT == proxy.FIRST_ANSWER_DEFAULT
    assert proxy.Job.__mro__[1:4] == (replicas.ReplicaSending, cloud.CloudSending, answer.Answering)
    assert len(Path(proxy.__file__).read_text().splitlines()) < 500
