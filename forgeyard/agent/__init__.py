"""The Forgeyard agent: runs on each machine, reports its hardware, keeps a connection open.

M1 only reads inventory. Nothing here allocates GPU memory, creates a CUDA/HIP context or
touches running processes: NVML and AMD SMI are read-only management interfaces.
"""
