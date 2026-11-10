#!/usr/bin/env python3
"""Which NVIDIA GPU can this workload see, and can it use it?

Run as a Forgeyard process task to check GPU isolation on a real machine. It needs only the
NVIDIA driver (libcuda.so.1, through ctypes): no torch, cupy or CUDA toolkit.

1. prints the GPU variables Forgeyard set (CUDA_VISIBLE_DEVICES, NVIDIA_VISIBLE_DEVICES, ...);
2. cuInit, then counts the visible devices: there must be exactly --expect of them (default 1);
3. prints each visible device's index, name, UUID and memory;
4. when CUDA_VISIBLE_DEVICES names one UUID, the device CUDA sees must be that card;
5. on the first device: allocates --gib GiB (default 1), writes to it, holds it for --hold
   seconds (default 20) so `nvidia-smi` shows it, frees it and exits.

Exit codes: 0 all good; 2 no NVIDIA driver here; 3 the wrong number of devices or the wrong
card; 4 a CUDA call failed.

    python3 scripts/gpu_probe.py --hold 20 --gib 1
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time

GPU_VARS = ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "ROCR_VISIBLE_DEVICES",
            "HIP_VISIBLE_DEVICES", "FORGEYARD_REPLICA", "FORGEYARD_WORKLOAD")


class CudaError(Exception):
    pass


def format_uuid(raw: bytes) -> str:
    """16 bytes -> GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (as nvidia-smi prints it)."""
    h = raw.hex()
    return f"GPU-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def load_cuda():
    try:
        return ctypes.CDLL("libcuda.so.1")
    except OSError:
        return None


class Cuda:
    def __init__(self, lib):
        self.lib = lib

    def call(self, name: str, *args) -> None:
        fn = getattr(self.lib, name)
        result = fn(*args)
        if result != 0:
            text = ctypes.c_char_p()
            try:
                self.lib.cuGetErrorName(result, ctypes.byref(text))
                label = text.value.decode() if text.value else str(result)
            except AttributeError:
                label = str(result)
            raise CudaError(f"{name} failed: {label} ({result})")

    def has(self, name: str) -> bool:
        return hasattr(self.lib, name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--expect", type=int, default=1, help="visible devices there must be (default 1)")
    parser.add_argument("--gib", type=float, default=1.0, help="GiB to allocate on the first device (default 1)")
    parser.add_argument("--hold", type=float, default=20.0, help="seconds to hold the memory (default 20)")
    args = parser.parse_args(argv)

    for var in GPU_VARS:
        print(f"{var}={os.environ.get(var, '<unset>')}")
    sys.stdout.flush()

    lib = load_cuda()
    if lib is None:
        print("libcuda.so.1 not found: no NVIDIA driver here (or it is hidden from this workload).")
        return 2
    cuda = Cuda(lib)
    try:
        cuda.call("cuInit", ctypes.c_uint(0))
        count = ctypes.c_int()
        cuda.call("cuDeviceGetCount", ctypes.byref(count))
        print(f"visible devices: {count.value}")
        devices = []
        for i in range(count.value):
            dev = ctypes.c_int()
            cuda.call("cuDeviceGet", ctypes.byref(dev), ctypes.c_int(i))
            name = ctypes.create_string_buffer(256)
            cuda.call("cuDeviceGetName", name, ctypes.c_int(256), dev)
            raw = (ctypes.c_ubyte * 16)()
            cuda.call("cuDeviceGetUuid_v2" if cuda.has("cuDeviceGetUuid_v2") else "cuDeviceGetUuid", ctypes.byref(raw), dev)
            total = ctypes.c_size_t()
            cuda.call("cuDeviceTotalMem_v2", ctypes.byref(total), dev)
            uuid = format_uuid(bytes(raw))
            devices.append((dev, uuid))
            print(f"device {i}: {name.value.decode()} {uuid} {total.value / 1024**3:.1f} GiB")
        sys.stdout.flush()
        if count.value != args.expect:
            print(f"FAIL: expected exactly {args.expect} visible device(s), found {count.value}.")
            return 3
        wanted = os.environ.get("CUDA_VISIBLE_DEVICES", "")
        if wanted.startswith("GPU-") and "," not in wanted and devices and devices[0][1] != wanted:
            print(f"FAIL: CUDA_VISIBLE_DEVICES is {wanted}, but CUDA sees {devices[0][1]}.")
            return 3
        if not devices or args.gib <= 0:
            print("OK (nothing allocated).")
            return 0

        dev = devices[0][0]
        ctx = ctypes.c_void_p()
        cuda.call("cuDevicePrimaryCtxRetain", ctypes.byref(ctx), dev)
        cuda.call("cuCtxSetCurrent", ctx)
        size = int(args.gib * 1024**3)
        ptr = ctypes.c_uint64()
        cuda.call("cuMemAlloc_v2", ctypes.byref(ptr), ctypes.c_size_t(size))
        cuda.call("cuMemsetD8_v2", ptr, ctypes.c_ubyte(0x5A), ctypes.c_size_t(size))
        cuda.call("cuCtxSynchronize")
        print(f"allocated and wrote {args.gib:g} GiB on {devices[0][1]}; holding it for {args.hold:g} s "
              "(watch nvidia-smi)")
        sys.stdout.flush()
        time.sleep(args.hold)
        cuda.call("cuMemFree_v2", ptr)
        release = "cuDevicePrimaryCtxRelease_v2" if cuda.has("cuDevicePrimaryCtxRelease_v2") else "cuDevicePrimaryCtxRelease"
        cuda.call(release, dev)
        print("freed; OK")
        return 0
    except CudaError as e:
        print(f"FAIL: {e}")
        return 4


if __name__ == "__main__":
    sys.exit(main())
