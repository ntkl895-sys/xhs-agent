"""示例：离线冒烟图（不含业务逻辑）。"""

from __future__ import annotations

from app.agent.examples.smoke_graph import (
    DIRTY_JSON_RESPONSE,
    SMOKE_KIND,
    UNPARSEABLE_RESPONSE,
    ScriptedChatModel,
    SmokeDraft,
    SmokeState,
    build_smoke_graph,
    make_fake_model,
    make_usage_fake_model,
    smoke_build_fn,
    smoke_inputs,
)
__all__ = [
    "SMOKE_KIND",
    "SmokeState",
    "SmokeDraft",
    "ScriptedChatModel",
    "make_fake_model",
    "make_usage_fake_model",
    "build_smoke_graph",
    "smoke_build_fn",
    "smoke_inputs",
    "DIRTY_JSON_RESPONSE",
    "UNPARSEABLE_RESPONSE",
]
