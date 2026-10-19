"""数据回填 Agent（第 15 步）—— 在 LangGraph harness 上实现的**第三个业务图**。

对外入口：

- :func:`app.agent.metrics.service.recognize_metric_screenshot` —— service 层编排入口
  （单张截图 → 六指标候选，**不落库**；人工核对后才走 ``POST /api/v1/metrics``）；
- :func:`app.agent.metrics.graph.build_metric_graph` —— 单图业务图
  （多节点 + 条件边 + 有限回路 + 检查点）；
- :func:`app.agent.metrics.graph.normalize_payload` —— 本地确定性归一
  （别名对齐 + 数字换算 + evidence / missing / uncertain），纯函数可单测；
- :class:`app.agent.metrics.state.MetricState` —— 业务状态；
- :data:`app.agent.metrics.prompts.METRIC_PROMPT_VERSION` —— 提示词版本；
- :mod:`app.agent.metrics.contracts` —— 六字段口径 / 图像魔数探测 / 专属异常。

链路：``load_image → transcribe →（条件边）→ normalize_validate / 回炉重写 / give_up``。
"""

from __future__ import annotations

from app.agent.metrics.contracts import (
    METRIC_ALIASES,
    METRIC_FIELDS,
    SUFFIX_BY_MIME,
    ImageValidationError,
    MetricRecognitionError,
    MetricRecognitionPayload,
    MetricTranscription,
    NoteMissingError,
    RecognitionGiveUpError,
    VisionUnsupportedError,
    detect_image_mime,
)
from app.agent.metrics.graph import (
    NODE_GIVE_UP,
    NODE_LOAD_IMAGE,
    NODE_NORMALIZE,
    NODE_TRANSCRIBE,
    build_metric_graph,
    looks_like_vision_unsupported,
    normalize_payload,
)
from app.agent.metrics.prompts import METRIC_PROMPT_VERSION, build_messages, build_user_prompt
from app.agent.metrics.service import recognize_metric_screenshot
from app.agent.metrics.state import MetricState

__all__ = [
    "METRIC_ALIASES",
    "METRIC_FIELDS",
    "METRIC_PROMPT_VERSION",
    "SUFFIX_BY_MIME",
    "NODE_GIVE_UP",
    "NODE_LOAD_IMAGE",
    "NODE_NORMALIZE",
    "NODE_TRANSCRIBE",
    "ImageValidationError",
    "MetricRecognitionError",
    "MetricRecognitionPayload",
    "MetricState",
    "MetricTranscription",
    "NoteMissingError",
    "RecognitionGiveUpError",
    "VisionUnsupportedError",
    "build_metric_graph",
    "build_messages",
    "build_user_prompt",
    "detect_image_mime",
    "looks_like_vision_unsupported",
    "normalize_payload",
    "recognize_metric_screenshot",
]
