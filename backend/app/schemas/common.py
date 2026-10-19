"""公共 schema：分页容器、错误体、可复用的校验器。

第 8 步确立的范式：所有「列表」接口统一返回
``{"items": [...], "total": int, "page": int, "page_size": int}``。
"""

from __future__ import annotations

from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, Field
from pydantic.functional_validators import AfterValidator

T = TypeVar("T")

# 字符串数组（标签 / 内容支柱 / 禁忌）的硬性上限
MAX_TAG_COUNT = 10
MAX_TAG_LENGTH = 50


class Page(BaseModel, Generic[T]):
    """统一分页响应。"""

    items: list[T]
    total: int = Field(description="满足过滤条件的总条数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")


class ErrorOut(BaseModel):
    """统一错误体（仅用于 OpenAPI 文档展示）。"""

    status: str = "error"
    code: str
    message: str
    details: Any | None = None


def _error_response(description: str) -> dict[str, Any]:
    return {"description": description, "model": ErrorOut}


class ErrorOutSpec:
    """常见错误响应的 OpenAPI 声明，供路由 ``responses=`` 复用。"""

    NOT_FOUND = {404: _error_response("资源不存在")}
    CONFLICT = {409: _error_response("唯一约束冲突")}
    VALIDATION = {422: _error_response("字段校验失败")}
    BAD_REQUEST = {400: _error_response("业务上不成立的请求")}


def blank_to_none(value: str) -> str | None:
    """把「全是空白」的字符串归一为 ``None``。

    用于可空文本字段：前端清空输入框时会送来 ``""``，
    直接落库会让「未填写」和「填了个空串」两种语义混在一起。
    """
    return None if not value.strip() else value


def clean_tag_list(value: list[str] | None) -> list[str] | None:
    """校验并归一化字符串数组（内容支柱 / 人设禁忌）。

    规则：元素 strip 后非空、每项 ≤50 字、最多 10 项、自动去重。
    非法元素抛 ``ValueError``，由 Pydantic 汇总为 422 的字段级错误。
    """
    if value is None:
        return None
    if len(value) > MAX_TAG_COUNT:
        raise ValueError(f"最多 {MAX_TAG_COUNT} 项")
    result: list[str] = []
    for item in value:
        stripped = item.strip()
        if not stripped:
            raise ValueError("列表元素不能为空")
        if len(stripped) > MAX_TAG_LENGTH:
            raise ValueError(f"每个元素不超过 {MAX_TAG_LENGTH} 字")
        if stripped not in result:  # 自动去重（保留首次出现顺序）
            result.append(stripped)
    return result


# 可复用的 Annotated 类型别名
OptText = Annotated[str, AfterValidator(blank_to_none)]
TagList = Annotated[list[str], AfterValidator(clean_tag_list)]
OptTagList = Annotated[list[str], AfterValidator(clean_tag_list)] | None
