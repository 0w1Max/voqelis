from __future__ import annotations

from typing import Any

SUPPORTED_GIGAAM_MODELS = frozenset(
    {"gigaam-v3-rnnt-int8", "gigaam-v3-ctc-int8"}
)


def _cpu_session_options() -> Any:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return options


def load_gigaam_model(model_name: str) -> Any:
    """Load a supported GigaAM v3 model with benchmarked CPU INT8 settings."""
    if model_name not in SUPPORTED_GIGAAM_MODELS:
        raise ValueError(f"Unsupported GigaAM model: {model_name!r}")

    import onnx_asr

    return onnx_asr.load_model(
        model_name.removesuffix("-int8"),
        quantization="int8",
        sess_options=_cpu_session_options(),
        providers=["CPUExecutionProvider"],
    )


def load_gigaam_vad() -> Any:
    """Load Silero VAD for pause-aware speech segmentation."""
    import onnx_asr

    return onnx_asr.load_vad(
        "silero",
        sess_options=_cpu_session_options(),
        providers=["CPUExecutionProvider"],
    )
