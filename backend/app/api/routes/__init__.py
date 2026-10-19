"""路由包：``/api/v1`` 下的业务路由。

- ``accounts``：账号 CRUD
- ``personas``：人设 CRUD

健康检查（``/health``、``/health/ready``）**不带 v1 前缀**，仍在 ``app.api.health``。
"""

from __future__ import annotations

__all__: list[str] = []
