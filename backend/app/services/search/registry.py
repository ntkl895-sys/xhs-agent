"""Provider 注册表（第 23 步，可插拔）。

- 路由 / service 只通过 :func:`get_provider` 取源，**不 import 具体实现**；
- 本期只默认注册 ``github``；其余源只留扩展位（实现 ``SearchProvider``
  后调用 :func:`register_provider` 即接入，路由与前端无需改动）；
- ``register_provider(..., replace=True)`` 供测试显式覆盖内置实现。
"""

from __future__ import annotations

from app.services.search.provider import SearchProvider

#: 已注册的 provider（source 标识小写 -> 实例）
_PROVIDERS: dict[str, SearchProvider] = {}


def register_provider(provider: SearchProvider, *, replace: bool = False) -> None:
    """注册一个 provider。

    - 重复注册默认抛 ``ValueError``（防手滑覆盖）；
    - ``replace=True`` 显式覆盖（测试注入 / 有意替换）。
    """
    key = (provider.source or "").strip().lower()
    if not key:
        raise ValueError("provider.source 不能为空")
    if not replace and key in _PROVIDERS:
        raise ValueError(f"provider 已注册：{key}")
    _PROVIDERS[key] = provider


def get_provider(source: str) -> SearchProvider | None:
    """按源标识取 provider；未注册返回 ``None``（路由据此转 400）。"""
    return _PROVIDERS.get((source or "").strip().lower())


def available_sources() -> list[str]:
    """已注册的源标识（排序，供错误提示展示）。"""
    return sorted(_PROVIDERS.keys())


def _register_builtin_providers() -> None:
    """注册内置源（本期仅 github）。

    函数内延迟 import：既避免 ``github`` 模块间接回指本模块时成环，
    也给测试留出先注册覆盖的窗口（``replace=True`` 可随时覆盖）。
    """
    from app.services.search.github import GitHubProvider

    register_provider(GitHubProvider.from_settings())


_register_builtin_providers()

__all__ = ["available_sources", "get_provider", "register_provider"]
