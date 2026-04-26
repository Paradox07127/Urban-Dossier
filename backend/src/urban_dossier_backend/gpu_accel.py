"""GPU acceleration utilities for DGX Spark."""
from __future__ import annotations

import os
import logging

logger = logging.getLogger(__name__)

_gpu_status: dict[str, object] = {
    "cuda_available": False,
    "cudf_available": False,
    "cudf_pandas_active": False,
    "cuvs_available": False,
    "device_name": None,
}


def detect_gpu() -> dict[str, object]:
    try:
        import cupy
        _gpu_status["cuda_available"] = True
        _gpu_status["device_name"] = str(
            cupy.cuda.runtime.getDeviceProperties(0).get("name", b"unknown"), "utf-8"
        )
    except Exception:
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                _gpu_status["cuda_available"] = True
                _gpu_status["device_name"] = result.stdout.strip().split("\n")[0]
        except Exception:
            pass
    try:
        import cudf  # noqa: F401
        _gpu_status["cudf_available"] = True
    except ImportError:
        pass
    try:
        import cuvs  # noqa: F401
        _gpu_status["cuvs_available"] = True
    except ImportError:
        pass
    return dict(_gpu_status)


def enable_cudf_pandas() -> bool:
    try:
        import cudf.pandas
        cudf.pandas.install()
        _gpu_status["cudf_pandas_active"] = True
        logger.info("cudf.pandas accelerator enabled")
        return True
    except Exception:
        return False


def get_gpu_status() -> dict[str, object]:
    return dict(_gpu_status)


if os.getenv("URBAN_DOSSIER_GPU_DETECT", "1") != "0":
    detect_gpu()
    if _gpu_status["cuda_available"]:
        logger.info("GPU detected: %s", _gpu_status["device_name"])
    if os.getenv("URBAN_DOSSIER_GPU_ACCEL", "1") == "1" and _gpu_status["cudf_available"]:
        enable_cudf_pandas()
