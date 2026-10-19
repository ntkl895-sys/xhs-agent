# -*- coding: utf-8 -*-
"""第 25 步 · 选题图「LLM 自主选工具」自检（**零 token、不联网**）。

覆盖（全部用脚本化假模型 / 纯函数，不发任何真实请求）：

1. 开关关闭：图节点集与接入前一致（无 tool_decide / tool_exec），提示词无工具段落；
2. 开关打开：图多出 2 个节点，且条件边可达（装配正确）；
3. 越权拒绝：模型请求白名单外工具（如 delete_note）→ 丢弃、记录、
   不执行、不写 ToolMessage；
4. 收敛：工具轮数到硬上限后，路由直接进入生成节点，不再多问一次模型；
5. 异常降级：工具内部抛错 → ToolNode 转错误消息，事件标 ok=False，
   节点状态 partial，流程不中断；
6. 入参 schema：空 query / top_k 越界被 pydantic 拒绝；
7. 白名单：工具名集合固定，且不含任何写操作命名。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts/selftest_topic_tool_calling.py

全部通过退出码 0；任一项 FAIL 退出码 1。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.agent.topics.graph import build_topic_graph  # noqa: E402
from app.agent.topics.prompts import build_messages  # noqa: E402
from app.agent.topics.state import TopicState  # noqa: E402
from app.agent.topics.tool_calling import (  # noqa: E402
    NODE_TOOL_DECIDE,
    NODE_TOOL_EXEC,
    TOOL_NAMES,
    SearchKnowledgeArgs,
    build_tool_loop,
)
from app.core.config import settings  # noqa: E402
from app.crud.knowledge import crud_knowledge  # noqa: E402


class Checker:
    """极简断言计数器（与其它 selftest 同样的 PASS/FAIL 形态）。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"[PASS] {label}")
        else:
            self.failures.append(label)
            print(f"[FAIL] {label}" + (f"  ->  {detail}" if detail else ""))


class ScriptedToolModel(BaseChatModel):
    """按脚本顺序返回 AIMessage 的假模型（bind_tools 返回自身，零网络）。"""

    responses: list[AIMessage] = []
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-model"

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        index = min(self.cursor, max(len(self.responses) - 1, 0))
        self.cursor += 1
        message = self.responses[index] if self.responses else AIMessage(content="{}")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: list[Any], **kwargs: Any) -> "ScriptedToolModel":
        return self


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _base_state(persona_id: str) -> dict[str, Any]:
    return {
        "persona_id": persona_id,
        "persona_snapshot": {
            "name": "测试人设",
            "positioning": "面向职场新人的效率博主",
            "target_audience": "职场新人",
            "tone": "直给",
            "content_pillars": ["效率", "工具"],
        },
        "hotspot_snapshot": {"title": "AI 写作工具爆火", "source": "zhihu"},
        "per_hotspot": 1,
        "feedback": "",
        "rag_context": None,
        "node_trace": [],
    }


# ---------------------------------------------------------------------------
def group1_switch_off(c: Checker) -> None:
    """关闭时图与提示词都不该出现任何工具痕迹。"""
    persona_id = str(uuid.uuid4())
    fake = ScriptedToolModel(responses=[AIMessage(content="{}")])
    graph = build_topic_graph(model=fake, use_checkpointer=False)
    nodes = set(graph.get_graph().nodes.keys())
    c.check(NODE_TOOL_DECIDE not in nodes, "关闭：图中无 tool_decide 节点", f"{sorted(nodes)}")
    c.check(NODE_TOOL_EXEC not in nodes, "关闭：图中无 tool_exec 节点")
    c.check(
        nodes == {"__start__", "load_context", "generate_topics", "validate_dedupe", "persist", "give_up", "__end__"},
        "关闭：节点集与接入前完全一致",
        f"实际={sorted(nodes)}",
    )

    base = dict(
        persona_snapshot={"name": "测试人设", "positioning": "定位", "content_pillars": ["效率"]},
        hotspot_snapshot={"title": "热点", "source": "zhihu"},
        per_hotspot=2,
        title_max_len=20,
        min_tags=3,
        max_tags=8,
    )
    with_default = build_messages(**base)
    explicit_none = build_messages(**base, tool_instruction=None, tool_context=None)
    c.check(
        [m.content for m in with_default] == [m.content for m in explicit_none],
        "关闭：提示词与显式传 None 逐字节一致",
    )
    c.check(
        "工具使用规则" not in with_default[0].content and "工具收集到的上下文" not in with_default[1].content,
        "关闭：提示词中没有任何工具段落",
    )


def group2_switch_on(c: Checker) -> None:
    """打开时多两个节点，且路由函数齐备。"""
    persona_id = str(uuid.uuid4())
    fake = ScriptedToolModel(responses=[AIMessage(content="{}")])
    original = settings.TOPIC_TOOLCALLING_ENABLED
    try:
        settings.TOPIC_TOOLCALLING_ENABLED = True
        graph = build_topic_graph(model=fake, tool_model=fake, use_checkpointer=False)
        nodes = set(graph.get_graph().nodes.keys())
        c.check(NODE_TOOL_DECIDE in nodes and NODE_TOOL_EXEC in nodes, "开启：工具节点已挂入图")
        c.check(len(nodes) == 9, "开启：节点数为 9（7 业务 + start/end）", f"{sorted(nodes)}")
        loop = build_tool_loop(model=fake, max_rounds=2)
        c.check(
            set(loop) == {"decide", "exec", "route_after_decide", "route_after_exec", "tools_factory"},
            "开启：回路四件套齐备",
            f"{sorted(loop)}",
        )
    finally:
        settings.TOPIC_TOOLCALLING_ENABLED = original


def group3_unknown_tool(c: Checker) -> None:
    """越权：白名单外工具必须被丢弃，且不产生任何 ToolMessage。"""
    persona_id = str(uuid.uuid4())
    fake = ScriptedToolModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("delete_note", {"note_id": "note-1"}, "call_bad"),
                    _tool_call("search_hotspots", {"q": "AI"}, "call_ok"),
                ],
            )
        ]
    )
    loop = build_tool_loop(model=fake, max_rounds=3)
    update = loop["decide"](_base_state(persona_id))
    msgs = update["messages"]
    names = [item.get("name") for item in getattr(msgs[-1], "tool_calls", [])]
    c.check(names == ["search_hotspots"], "越权：只保留白名单内的调用", f"实际={names}")
    c.check(update.get("hallucinated_tools") == ["delete_note"], "越权：幻觉工具被记录")
    c.check(
        not any(isinstance(m, AIMessage) and any(x.get("name") == "delete_note" for x in m.tool_calls) for m in msgs),
        "越权：delete_note 没有进入消息通道（不会被执行）",
    )
    c.check(
        update["node_trace"][0]["hallucinated"] == ["delete_note"],
        "越权：node_trace 留痕",
    )


def group4_convergence(c: Checker) -> None:
    """轮数上限：到顶后直接进生成，不再多问一次模型。"""
    fake = ScriptedToolModel(responses=[AIMessage(content="", tool_calls=[_tool_call("search_hotspots", {}, "c1")])])
    loop = build_tool_loop(model=fake, max_rounds=1)
    state = _base_state(str(uuid.uuid4()))
    state["messages"] = [AIMessage(content="", tool_calls=[_tool_call("search_hotspots", {}, "c1")])]
    state["tool_rounds"] = 1
    c.check(loop["route_after_decide"](state) == "generate_topics", "收敛：到上限后 decide 不再执行工具")
    c.check(loop["route_after_exec"](state) == "generate_topics", "收敛：到上限后 exec 直接进生成")

    state2 = _base_state(str(uuid.uuid4()))
    state2["messages"] = [AIMessage(content="", tool_calls=[_tool_call("search_hotspots", {}, "c1")])]
    state2["tool_rounds"] = 0
    c.check(loop["route_after_decide"](state2) == NODE_TOOL_EXEC, "收敛：未到上限时进入工具执行")
    state2["tool_rounds"] = 0
    c.check(loop["route_after_exec"](state2) == NODE_TOOL_DECIDE, "收敛：未到上限时回到决策")

    state3 = _base_state(str(uuid.uuid4()))
    state3["messages"] = [AIMessage(content="[]")]  # 无 tool_calls
    c.check(loop["route_after_decide"](state3) == "generate_topics", "收敛：模型不再调用工具即进生成")


def group5_tool_error(c: Checker) -> None:
    """异常降级：工具抛错不阻断，事件标 ok=False，节点 partial。"""
    persona_id = str(uuid.uuid4())
    fake = ScriptedToolModel(responses=[AIMessage(content="{}")])
    loop = build_tool_loop(model=fake, max_rounds=3)
    state = _base_state(persona_id)
    state["messages"] = [
        AIMessage(
            content="",
            tool_calls=[_tool_call("search_knowledge", {"query": "选题模板", "top_k": 2}, "call_kb")],
        )
    ]
    original = crud_knowledge.search_snippets

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("embedding 不可用（自检注入）")

    crud_knowledge.search_snippets = _boom  # type: ignore[method-assign]
    try:
        # ToolNode 需要图注入的 runtime 配置，因此这里用一张最小图跑执行节点
        mini = StateGraph(TopicState)
        mini.add_node(NODE_TOOL_EXEC, loop["exec"])
        mini.add_edge(START, NODE_TOOL_EXEC)
        mini.add_edge(NODE_TOOL_EXEC, END)
        update = mini.compile().invoke(state)
    finally:
        crud_knowledge.search_snippets = original  # type: ignore[method-assign]

    events = update.get("tool_events") or []
    c.check(len(events) == 1, "降级：记录 1 条工具事件", f"{events}")
    c.check(events and events[0]["ok"] is False, "降级：失败被如实标记 ok=False")
    c.check(events and "RuntimeError" in (events[0].get("error") or ""), "降级：错误摘要可读")
    c.check(update["node_trace"][0]["status"] == "partial", "降级：节点状态 partial（不中断整个图）")
    c.check(update.get("tool_rounds") == 1, "降级：轮数照常累加（不会无限重试）")
    c.check(
        any(getattr(m, "status", "") == "error" for m in update.get("messages") or []),
        "降级：错误以 ToolMessage(status=error) 回给模型",
    )


def group6_schema(c: Checker) -> None:
    """入参 schema 校验。"""
    for label, kwargs in [
        ("空 query 被拒", {"query": ""}),
        ("top_k 越界被拒", {"query": "模板", "top_k": 9}),
    ]:
        try:
            SearchKnowledgeArgs(**kwargs)
        except ValidationError:
            c.check(True, label)
        else:
            c.check(False, label, "未抛 ValidationError")
    c.check(SearchKnowledgeArgs(query="模板").top_k == 3, "合法入参取默认值 top_k=3")


def group7_whitelist(c: Checker) -> None:
    """白名单固定且纯只读。"""
    fake = ScriptedToolModel(responses=[AIMessage(content="{}")])
    loop = build_tool_loop(model=fake, max_rounds=3)
    tools = loop["tools_factory"](str(uuid.uuid4()))
    names = tuple(item.name for item in tools)
    c.check(names == TOOL_NAMES, "白名单：工具名与常量一致", f"{names}")
    banned = ("save", "delete", "update", "publish", "insert", "create", "write")
    c.check(
        not any(word in name.lower() for name in names for word in banned),
        "白名单：不含任何写操作命名",
        f"{names}",
    )
    c.check(all(getattr(item, "args_schema", None) is not None for item in tools), "白名单：每个工具都有入参 schema")


def main() -> int:
    print("=" * 72)
    print("选题图 · 工具调用自检（假模型 / 零 token / 不联网）")
    print("=" * 72)
    c = Checker()
    group1_switch_off(c)
    group2_switch_on(c)
    group3_unknown_tool(c)
    group4_convergence(c)
    group5_tool_error(c)
    group6_schema(c)
    group7_whitelist(c)

    print("-" * 72)
    if c.failures:
        print(f"[FAIL] {len(c.failures)} 项失败 / 共 {c.passed + len(c.failures)} 项")
        for item in c.failures:
            print(f"  - {item}")
        return 1
    print(f"[OK] 全部 {c.passed} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
