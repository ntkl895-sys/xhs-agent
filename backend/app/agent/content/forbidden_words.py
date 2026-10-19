"""违禁词合规自检模块（第 12 步 · 创作 Agent；第 19 步增强）。

⚠️ 免责声明（务必先读）：

- 本模块内置的《基础风险词表》**仅为示例**，用于演示「合规自检 → 命中提示」这条链路，
  **不是完整清单，也不构成任何法律 / 平台规则意见**；
- 使用者需自行核实并补充所在行业、所在平台的最新规则（本表刻意保守，宁多勿漏）；
- 第 19 步起词表**统一由知识库管理**（``knowledge_snippets.chunk_type='forbidden_word'``）：
  创作图 ``validate_compliance`` 优先使用知识库词表（见 ``services/forbidden_source.py``），
  本模块保留「无知识库 / 知识库异常时的保守兜底」角色，并支持**传入自定义词表**
  （``scan_text`` / ``scan_note`` 的 ``wordlist`` 参数）；缺省行为与第 12 步**完全一致**。

实现口径（刻意简单、可解释、可复现）：

- **大小写不敏感 + 去常见标点 / 空白**后做**精确匹配**——不做分词、不做复杂 NLP；
- 去掉标点这一动作会顺带覆盖「用符号隔开规避」的写法（如「加-微-信」归一后命中「加微信」）；
- 命中 ⇒ ``ComplianceStatus.warning``；0 命中 ⇒ ``ComplianceStatus.passed``；
- **不新增 blocked 状态、不丢弃草稿、不静默改写正文**——只提示，最终由人工决定。

安全约定：

- 内置词表**只内置在代码里**：不写 env、不写日志；
- 自定义词表由调用方（服务层）从知识库读取后传入，本模块**不做任何数据库访问**；
- ``scan_*`` 返回的命中项只包含「词 / 类别 / 字段 / 片段」，不含任何密钥或隐私信息。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.models.enums import ComplianceStatus

# --------------------------------------------------------------------------
# 分类标签（展示用；类别 key 稳定，便于后续替换为知识库来源）
# --------------------------------------------------------------------------
CATEGORY_LABELS: dict[str, str] = {
    "absolute_wording": "广告法绝对化用语",
    "medical_claim": "医疗 / 功效断言",
    "financial_promise": "收益 / 保本承诺",
    "fake_authority": "虚假权威背书",
    "superstition": "迷信 / 玄学功效",
    "platform_diversion": "站外导流 / 联系方式",
    "vulgar_risk": "低俗 / 高风险导向",
}

#: 内置基础风险词表：类别 -> 示例词（**示例性质，非完整清单**）。
#: 追加 / 替换词表时只需改这里；后续接入知识库时以知识库为唯一来源。
_RAW_WORDS: dict[str, tuple[str, ...]] = {
    "absolute_wording": (
        "国家级",
        "世界级",
        "最高级",
        "最佳",
        "最强",
        "全国第一",
        "行业第一",
        "排名第一",
        "销量第一",
        "唯一",
        "独家",
        "顶级",
        "极致",
        "王牌",
        "史上最",
        "绝无仅有",
        "独一无二",
        "全网最低",
        "100%有效",
    ),
    "medical_claim": (
        "包治百病",
        "根治",
        "治愈",
        "药到病除",
        "疗效显著",
        "壮阳",
        "丰胸",
        "祛斑",
        "生发",
        "降血压",
        "降血糖",
        "抗肿瘤",
        "增强免疫力",
    ),
    "financial_promise": (
        "稳赚",
        "稳赚不赔",
        "保本",
        "保收益",
        "零风险",
        "躺赚",
        "日入过万",
        "月入十万",
        "高回报",
        "包赚",
        "一夜暴富",
    ),
    "fake_authority": (
        "官方指定",
        "央视推荐",
        "央视上榜",
        "国家认证",
        "专家推荐",
        "权威认证",
        "诺贝尔奖级",
    ),
    "superstition": (
        "转运",
        "招财",
        "辟邪",
        "开光",
        "风水改命",
        "改运",
        "消灾",
    ),
    "platform_diversion": (
        "加微信",
        "加vx",
        "加微",
        "微信号",
        "vx号",
        "私聊发你",
        "扫码进群",
        "扣1进群",
        "代购加",
        "站外链接",
    ),
    "vulgar_risk": (
        "博彩",
        "赌场",
        "网贷",
        "刷单",
        "套现",
        "灰色产业",
    ),
}


class ForbiddenWord:
    """一条风险词（词 + 类别）。``__slots__`` 让它足够轻量。"""

    __slots__ = ("word", "category")

    def __init__(self, word: str, category: str) -> None:
        self.word = word
        self.category = category

    def __repr__(self) -> str:  # pragma: no cover
        return f"ForbiddenWord(word={self.word!r}, category={self.category!r})"


#: 展开后的词表（构建顺序 = 追加 / 替换顺序，保持稳定）
FORBIDDEN_WORDS: tuple[ForbiddenWord, ...] = tuple(
    ForbiddenWord(word=word, category=category)
    for category, words in _RAW_WORDS.items()
    for word in words
)

#: 归一化后的「词 -> 类别」映射（扫描主索引）
_NORMALIZED_INDEX: dict[str, str] = {}

#: 需要被剥离的标点 / 空白（半角 + 全角常见标点）
_PUNCTUATION = set(
    " \t\r\n\u3000"
    "，。！？；：、（）()【】[]{}<>《》“”‘’\"'´`"
    "…—–-·~～!?;:,./\\|@#$%^&*_+=^"
)

#: 命中片段左右各保留的字符数
_SNIPPET_PADDING = 12

#: 单次扫描最多返回的命中项（防止超长文本产生巨型 content_check）
MAX_HITS = 50


def normalize_text(text: str) -> str:
    """归一化：转小写 + 去掉常见标点与空白（保留其余字符）。

    归一化后的文本仅用于**匹配**；返回的片段也取自归一化视图（口径见模块文档）。
    """
    return "".join(char for char in text.lower() if char not in _PUNCTUATION)


def build_word_index(
    items: Iterable[tuple[str, str] | ForbiddenWord],
) -> dict[str, str]:
    """把「词 + 类别」序列构建成归一化索引（``归一化词 -> 类别``）。

    - 与内置索引（:data:`_NORMALIZED_INDEX`）**同一套构建逻辑**（第 19 步：
      自定义词表与内置词表共用扫描实现，不复制第二份）；
    - 归一化后为空的词跳过；同一归一化词的重复项**首次出现优先**（顺序稳定）。
    """
    index: dict[str, str] = {}
    for item in items:
        if isinstance(item, ForbiddenWord):
            word, category = item.word, item.category
        else:
            word, category = str(item[0]), str(item[1])
        key = normalize_text(word)
        if key and key not in index:
            index[key] = category
    return index


def _build_index() -> None:
    """惰性构建内置词表的归一化索引（模块导入时不执行额外工作）。"""
    if _NORMALIZED_INDEX:
        return
    _NORMALIZED_INDEX.update(build_word_index(FORBIDDEN_WORDS))


def _scan_one_field(
    text: str | None,
    field: str,
    seen: set[tuple[str, str]],
    *,
    index: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """扫描单个字段，返回该字段内的命中项。

    ``index=None`` 时用**内置词表**索引；否则用调用方传入的自定义索引
    （词表来源由上层决定——第 19 步：知识库 active 违禁词）。

    去重 / 剪枝口径：

    - 同一「词 + 字段」只报一次（取首次出现位置的片段）；
    - **被同字段更长命中词包含的短词不再单列**（如「稳赚」被「稳赚不赔」覆盖，
      只报后者），避免同一句话被重复计数。
    """
    if not text:
        return []
    normalized = normalize_text(text)
    if not normalized:
        return []
    if index is None:
        _build_index()
        active_index = _NORMALIZED_INDEX
    else:
        active_index = index

    matched: list[tuple[str, str, int]] = []
    for word, category in active_index.items():
        if (word, field) in seen:
            continue
        position = normalized.find(word)
        if position >= 0:
            matched.append((word, category, position))

    pruned = [
        item
        for item in matched
        if not any(item[0] != other[0] and item[0] in other[0] for other in matched)
    ]

    hits: list[dict[str, Any]] = []
    for word, category, position in pruned:
        if len(hits) >= MAX_HITS:
            break
        seen.add((word, field))
        start = max(0, position - _SNIPPET_PADDING)
        end = min(len(normalized), position + len(word) + _SNIPPET_PADDING)
        hits.append(
            {
                "word": word,
                "category": category,
                "field": field,
                "snippet": normalized[start:end],
            }
        )
    return hits


def scan_text(
    text: str | None,
    *,
    field: str = "body",
    wordlist: Iterable[tuple[str, str] | ForbiddenWord] | None = None,
) -> list[dict[str, Any]]:
    """扫描一段文本，返回命中列表 ``[{word, category, field, snippet}]``（无命中返空表）。

    ``wordlist``：可选自定义词表（词 + 类别的序列）；``None``（缺省）时用内置词表。
    """
    index = build_word_index(wordlist) if wordlist is not None else None
    return _scan_one_field(text, field, seen=set(), index=index)


def scan_note(
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    *,
    wordlist: Iterable[tuple[str, str] | ForbiddenWord] | None = None,
) -> list[dict[str, Any]]:
    """扫描整篇笔记（标题 / 正文 / 标签），按字段顺序返回去重后的命中列表。

    ``wordlist``：可选自定义词表（词 + 类别的序列，如知识库 active 违禁词）；
    ``None``（缺省）时用内置词表 —— 缺省行为与第 12 步**完全一致**。

    去重口径：同一「词 + 字段」只报一次；跨字段重复（标题与正文各命中一次）会各报一次。
    """
    index = build_word_index(wordlist) if wordlist is not None else None
    seen: set[tuple[str, str]] = set()
    hits: list[dict[str, Any]] = []
    hits.extend(_scan_one_field(title, "title", seen, index=index))
    hits.extend(_scan_one_field(body, "body", seen, index=index))
    if tags:
        joined = " ".join(str(item) for item in tags if str(item).strip())
        hits.extend(_scan_one_field(joined, "tags", seen, index=index))
    return hits[:MAX_HITS]


def compliance_of(hits: list[dict[str, Any]]) -> ComplianceStatus:
    """命中列表 → 合规结论：0 命中 = passed；≥1 命中 = warning（**不引入 blocked**）。"""
    return ComplianceStatus.WARNING if hits else ComplianceStatus.PASSED


def build_content_check(
    hits: list[dict[str, Any]],
    *,
    checked_fields: tuple[str, ...] = ("title", "body", "tags"),
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """组装落 ``notes.content_check`` 的结构（可 JSON 序列化）。"""
    status = compliance_of(hits)
    return {
        "status": status.value,
        "hits": hits,
        "checked_fields": list(checked_fields),
        "checked_at": (checked_at or datetime.now(timezone.utc)).isoformat(),
    }


def describe_wordlist() -> dict[str, Any]:
    """给出词表概况（数量 / 分类），供文档与自检引用。**不含任何密钥**。"""
    counts: dict[str, int] = {key: 0 for key in CATEGORY_LABELS}
    for item in FORBIDDEN_WORDS:
        counts[item.category] = counts.get(item.category, 0) + 1
    return {
        "total": len(FORBIDDEN_WORDS),
        "by_category": counts,
        "note": "内置基础风险词表仅为示例、非完整清单、不构成法律意见；第 19 步起由知识库统一管理。",
    }


__all__ = [
    "CATEGORY_LABELS",
    "FORBIDDEN_WORDS",
    "MAX_HITS",
    "ForbiddenWord",
    "build_content_check",
    "build_word_index",
    "compliance_of",
    "describe_wordlist",
    "normalize_text",
    "scan_note",
    "scan_text",
]
