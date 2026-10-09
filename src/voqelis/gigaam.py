from __future__ import annotations

from typing import Any

SUPPORTED_GIGAAM_MODELS = frozenset(
    {"gigaam-v3-rnnt-int8", "gigaam-v3-ctc-int8"}
)


def load_gigaam_model(model_name: str) -> Any:
    """Load a supported GigaAM v3 model with benchmarked CPU INT8 settings."""
    if model_name not in SUPPORTED_GIGAAM_MODELS:
        raise ValueError(f"Unsupported GigaAM model: {model_name!r}")

    import onnx_asr
    import onnxruntime as ort

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = 1
    session_options.inter_op_num_threads = 1
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    return onnx_asr.load_model(
        model_name.removesuffix("-int8"),
        quantization="int8",
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
