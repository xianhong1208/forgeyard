"""1.4.5 (#202): what the server makes of a machine's GPU access check (agent/gpu_check.py), kept in
`nodes.runtime_info["gpu_check"]`.

While new processes there cannot open the GPUs of a vendor, no *process* workload or process engine is placed on
those GPUs: it would fail at once (`cuInit: CUDA_ERROR_NO_DEVICE`). Containers are not affected (a new container gets
the device access afresh), and neither is anything already running. Agents before 1.4.5 report nothing: nothing is
held back there.
"""

from __future__ import annotations

from typing import Optional

from forgeyard.db import Node

VENDOR_LABEL = {"nvidia": "NVIDIA", "amd": "AMD"}


def check(node: Node) -> Optional[dict]:
    value = (node.runtime_info or {}).get("gpu_check")
    return value if isinstance(value, dict) and isinstance(value.get("ok"), bool) else None


def failing_vendors(node: Node) -> set[str]:
    """The GPU vendors whose devices a new process on this machine cannot open (empty: fine, or not known)."""
    c = check(node)
    if c is None or c["ok"]:
        return set()
    failed = {d.get("vendor") for d in c.get("devices") or [] if not d.get("ok")} & set(VENDOR_LABEL)
    if failed:
        return failed
    return {a.vendor for a in node.accelerators if a.vendor in VENDOR_LABEL}  # failed, but not said where: all


def in_container(node: Node) -> bool:
    install = (node.runtime_info or {}).get("install")
    return isinstance(install, dict) and install.get("method") == "container"


def process_problem(node: Node, vendors: set[str]) -> str:
    """Why a process workload is not placed on these GPUs here: said in placement reasons and deployment pages."""
    label = " and ".join(VENDOR_LABEL[v] for v in sorted(vendors) if v in VENDOR_LABEL)
    where = ("its agent container lost its device access, e.g. after the host ran systemctl daemon-reload; restart "
             "the agent container" if in_container(node) else "see Setup health")
    gpus = f"{label} GPUs" if label else "GPUs"
    return (f"new processes there cannot open its {gpus} ({where}): process workloads are not placed on them "
            "meanwhile; containers still run there")
