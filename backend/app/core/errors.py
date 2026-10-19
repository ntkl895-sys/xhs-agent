"""统一业务异常与错误体（第 8 步确立，后续模块沿用）。

所有业务错误都返回同一结构，与第 6 步的全局 500 兜底保持同一「结构化 JSON」风格：

    {
      "status": "error",
      "code":   "not_found" | "conflict" | "validation" | "bad_request",
      "message": "人类可读的说明",
      "details": {...} 或 [...] 或 null
    }

约定：
- 路由 / CRUD 层**只抛 AppError**，不自己拼 JSONResponse，也不抛 HTTPException；
- 由 ``app.main`` 注册的异常处理器统一渲染成上面的结构；
- 时间字段一律由 Pydantic 序列化为 ISO 8601（带时区偏移）。
"""

from __future__ import annotations

from typing import Any

# 错误码常量（同时作为前端分支判断的依据）
CODE_NOT_FOUND = "not_found"
CODE_CONFLICT = "conflict"
CODE_VALIDATION = "validation"
CODE_BAD_REQUEST = "bad_request"


class AppError(Exception):
    """业务异常：携带错误码、HTTP 状态码与可选明细。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def to_body(self) -> dict[str, Any]:
        """渲染成统一错误体。"""
        return {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# --------------------------------------------------------------------------
# 语义化工厂：让调用处读起来像自然语言
# --------------------------------------------------------------------------
def not_found(message: str = "资源不存在", *, details: Any | None = None) -> AppError:
    """404：目标资源不存在。"""
    return AppError(CODE_NOT_FOUND, message, status_code=404, details=details)


def conflict(message: str = "资源冲突", *, details: Any | None = None) -> AppError:
    """409：唯一约束等冲突。"""
    return AppError(CODE_CONFLICT, message, status_code=409, details=details)


def validation_error(message: str = "请求参数不合法", *, details: Any | None = None) -> AppError:
    """422：字段校验失败（含框架校验失败时的统一转换）。"""
    return AppError(CODE_VALIDATION, message, status_code=422, details=details)


def bad_request(message: str = "请求不合法", *, details: Any | None = None) -> AppError:
    """400：语义上合法但业务上不成立的请求（如引用了不存在的账号）。"""
    return AppError(CODE_BAD_REQUEST, message, status_code=400, details=details)


# --------------------------------------------------------------------------
# 框架校验错误 -> 统一 details
# --------------------------------------------------------------------------
def format_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 FastAPI / Pydantic 的校验错误整理成字段级 details 列表。

    形如 ``[{"loc": ["body", "name"], "msg": "...", "type": "..."}]``。
    """
    formatted: list[dict[str, Any]] = []
    for err in errors:
        loc = err.get("loc", ())
        formatted.append(
            {
                "loc": [str(part) for part in loc],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return formatted


# HTTP 状态码 -> 错误码的兜底映射（用于框架自身抛出的 HTTPException）
_STATUS_TO_CODE = {
    400: CODE_BAD_REQUEST,
    404: CODE_NOT_FOUND,
    409: CODE_CONFLICT,
    422: CODE_VALIDATION,
}


def code_for_status(status_code: int) -> str:
    """把 HTTP 状态码映射为错误码，未知状态码归为 bad_request。"""
    return _STATUS_TO_CODE.get(status_code, CODE_BAD_REQUEST)
