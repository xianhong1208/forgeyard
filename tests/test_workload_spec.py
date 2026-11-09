"""The workload spec: what is accepted, what is refused and why (FY-WSPEC-*)."""

import pytest
from pydantic import ValidationError

from forgeyard.workloads.spec import WorkloadSpec, parse_bytes
from tests.workload_helpers import container, service, task


def ok(spec: dict) -> WorkloadSpec:
    return WorkloadSpec.model_validate(spec)


def refused(spec: dict, text: str) -> None:
    with pytest.raises(ValidationError) as e:
        WorkloadSpec.model_validate(spec)
    assert text in str(e.value), str(e.value)


def test_fy_wspec_001_small_valid_specs_and_defaults():
    """FY-WSPEC-001"""
    s = ok(service())
    assert (s.kind, s.runtime_type, s.restart, s.replicas, s.machine_id) == ("service", "process", "always", 1, None)
    assert s.resources.gpus.wanted == 0 and s.env == {}
    t = ok(task(max_duration=60, retries=2))
    assert (t.retries, t.retry_backoff, t.max_duration) == (2, 5, 60)
    c = ok(container(health={"type": "http", "path": "/healthz"}))
    assert c.health.port == "http" and c.health.interval == 10  # the first port by default


@pytest.mark.parametrize("name", ["Web", "-web", "web-", "we b", "a" * 49, "", "web_1"])
def test_fy_wspec_002_names(name):
    """FY-WSPEC-002"""
    refused(service(name=name), "name")


@pytest.mark.parametrize("key", ["CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES",
                                 "ROCR_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "GPU_DEVICE_ORDINAL",
                                 "FORGEYARD_REPLICA", "FORGEYARD_ANYTHING", "PORT", "PORT_HTTP"])
def test_fy_wspec_003_reserved_env_cannot_be_declared(key):
    """FY-WSPEC-003: GPU visibility, ports and identity belong to Forgeyard; declaring them would
    undo the isolation."""
    refused(task(env={key: "0"}), "set by Forgeyard")


@pytest.mark.parametrize("env, text", [({"1BAD": "x"}, "not a valid variable name"), ({"A-B": "x"}, "not a valid"),
                                       ({"OK": "two\nlines"}, "single line"), ({"OK": "nul\x00"}, "single line")])
def test_fy_wspec_004_env_keys_and_values(env, text):
    """FY-WSPEC-004"""
    refused(task(env=env), text)
    assert ok(task(env={"HF_HOME": "/data/hf", "PATH": "/opt/bin:/usr/bin"})).env["HF_HOME"] == "/data/hf"


@pytest.mark.parametrize("image", ["--privileged", "-v/:/host", "python 3", "", "x" * 300, "img\x00"])
def test_fy_wspec_005_image_can_never_be_read_as_a_flag(image):
    """FY-WSPEC-005"""
    refused(container(image=image), "image")


def test_fy_wspec_006_container_command_and_args():
    """FY-WSPEC-006"""
    s = ok(container(runtime={"type": "container", "image": "ghcr.io/org/app:1.2@sha256:abc", "command": ["/bin/app", "--x"],
                              "args": ["serve"]}))
    assert s.runtime.command == ["/bin/app", "--x"] and s.runtime.args == ["serve"]
    refused(container(runtime={"type": "container", "image": "x", "command": []}), "command")
    refused(container(ports=[{"name": "http"}]), "container port")


def test_fy_wspec_007_process_command_and_workdir():
    """FY-WSPEC-007"""
    assert ok(task(runtime={"type": "process", "command": "/usr/bin/env"})).runtime.command == "/usr/bin/env"
    refused(task(runtime={"type": "process", "command": "./run.sh"}), "relative")
    refused(task(runtime={"type": "process", "command": "sh", "workdir": "data"}), "absolute")
    refused(task(runtime={"type": "process", "command": ""}), "command")


def test_fy_wspec_008_kind_rules():
    """FY-WSPEC-008: tasks have no ports or health; services no max_duration or retries."""
    refused(task(ports=[{"name": "http"}]), "ports are for services")
    refused(task(health={"type": "tcp"}), "health checks are for services")
    refused(service(max_duration=10), "max_duration is for tasks")
    refused(service(retries=1), "retries are for tasks")
    refused(service(ports=[], health={"type": "tcp"}), "needs a port")
    refused(service(health={"type": "http", "port": "nope"}), "not one of the ports")
    refused(service(ports=[{"name": "http"}, {"name": "http"}]), "unique")


def test_fy_wspec_009_limits():
    """FY-WSPEC-009"""
    refused(task(replicas=0), "replicas")
    refused(task(replicas=17), "replicas")
    refused(task(retries=21), "retries")
    refused(task(max_duration=0), "max_duration")
    refused(task(max_duration=31 * 86400), "max_duration")
    refused(task(resources={"cpu": 0}), "cpu")
    refused(task(resources={"gpus": {"count": 17}}), "count")
    refused(task(extra="x"), "extra")


def test_fy_wspec_010_gpus():
    """FY-WSPEC-010"""
    s = ok(task(machine_id=1, resources={"gpus": {"indices": [4], "vendor": "nvidia"}}))
    assert s.resources.gpus.indices == [4] and s.resources.gpus.wanted == 1
    refused(task(resources={"gpus": {"indices": [4]}}), "choose the machine")
    refused(task(machine_id=1, replicas=2, resources={"gpus": {"indices": [4]}}), "one replica")
    refused(task(resources={"gpus": {"count": 1, "indices": [1]}}), "not both")
    refused(task(machine_id=1, resources={"gpus": {"indices": [1, 1]}}), "duplicates")
    refused(task(resources={"gpus": {"count": 1, "vendor": "intel"}}), "vendor")
    assert ok(task(resources={"gpus": {"count": 0, "vendor": "amd"}})).resources.gpus.vendor is None


@pytest.mark.parametrize("value, n", [(536870912, 536870912), ("512Mi", 512 * 1024**2), ("4Gi", 4 * 1024**3),
                                      ("2G", 2 * 10**9), ("1.5Gi", int(1.5 * 1024**3)), ("64MiB", 64 * 1024**2)])
def test_fy_wspec_011_memory_sizes(value, n):
    """FY-WSPEC-011"""
    assert parse_bytes(value) == n
    assert ok(task(resources={"memory": value})).resources.memory == n


@pytest.mark.parametrize("value", ["lots", "1Pi", -5, True, "1M"])
def test_fy_wspec_012_bad_memory(value):
    """FY-WSPEC-012"""
    refused(task(resources={"memory": value}), "memory")
