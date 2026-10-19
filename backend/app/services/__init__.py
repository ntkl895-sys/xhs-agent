"""服务层（第 9 步起）。

``app/services/`` 承载**跨 crud 的编排逻辑**（多源并发采集、落库、审计、定时）。
与 ``app/crud/`` 的分工：

- ``crud``     ：单一实体的数据访问与业务规则（不感知 HTTP、不做编排）；
- ``services`` ：把多个 crud / 外部 IO 串成一个用例（可被路由或调度器调用）。

既有的第 8 步分层约定（``api/routes → crud → models → db``）不变：
本层位于 ``crud`` 之上、``api/routes`` 之下。
"""
