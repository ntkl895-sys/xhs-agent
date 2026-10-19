# -*- coding: utf-8 -*-
"""第 27 步 · 候选经验库（工作目录 JSON，零迁移）。

状态机（与 `evolve_common.CANDIDATE_TRANSITIONS` 一致）：

```
pending ──► approved ──► applied ──► closed
   │                        ▲
   ├──► rejected（终态）      │
   └──► xfail ───────────────┘（也可直接 closed）
```

为什么要一个存储而不是继续用「候选文件 + 决定文件」两套 JSON：

- v1 的候选与决定是两个时间点的快照，中间没有身份——同一个候选被 review 两次会产生两条互不相干的记录；
- v2 要给候选挂 `reflection 类型 / 证据 / 置信度 / 对照数字`，并追「谁在什么时候把它推进到下一步」；
- 但**不建表**：一张 `evolution_candidates` 需要迁移，而本轮仍守零迁移基线。JSON 存储够用，
  且天然可读、可 diff、可整份备份。

写入策略：先写临时文件再 `os.replace`（原子），避免半截文件。

用法（项目根目录）：

    :: 把 reflect 产出的候选灌进库（按 candidate_id 去重）
    ... evolve_store.py --ingest D:\\项目1\\evolution\\reflect\\reflect-xxxx.json

    :: 列表 / 详情 / 统计
    ... evolve_store.py --list
    ... evolve_store.py --list --state pending
    ... evolve_store.py --show refl_0001

    :: 人工推进状态（唯一改状态入口）
    ... evolve_store.py --transition refl_0001 --to approved --reason "证据充分"

退出码：0 完成；1 非法迁移；3 参数问题。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evolve_common as ec  # noqa: E402

STORE_VERSION = 1


def store_path(workdir: Path) -> Path:
    return workdir / "candidates" / "store.json"


def load_store(workdir: Path) -> dict:
    path = store_path(workdir)
    if not path.is_file():
        return {"version": STORE_VERSION, "updated_at": ec.now_iso(), "candidates": {}}
    payload = ec.read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), dict):
        raise RuntimeError(f"候选库格式不对：{path}")
    return payload


def save_store(workdir: Path, store: dict) -> Path:
    store["updated_at"] = ec.now_iso()
    path = ec.ensure_dir(workdir / "candidates") / "store.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        __import__("json").dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def ingest(workdir: Path, candidates: list[dict]) -> dict:
    store = load_store(workdir)
    added: list[str] = []
    skipped: list[str] = []
    for candidate in candidates:
        cid = str(candidate.get("candidate_id") or "").strip()
        if not cid:
            continue
        if cid in store["candidates"]:
            skipped.append(cid)
            continue
        record = dict(candidate)
        record.setdefault("state", "pending")
        record.setdefault("created_at", ec.now_iso())
        record["history"] = [
            {"at": record["created_at"], "from": None, "to": record["state"], "reason": "ingested"}
        ]
        store["candidates"][cid] = record
        added.append(cid)
    save_store(workdir, store)
    return {"added": added, "skipped": skipped, "total": len(store["candidates"])}


def transition(workdir: Path, candidate_id: str, to_state: str, reason: str | None) -> dict:
    store = load_store(workdir)
    record = store["candidates"].get(candidate_id)
    if record is None:
        return {"ok": False, "error": f"候选不存在：{candidate_id}"}
    current = str(record.get("state") or "pending")
    if to_state not in ec.CANDIDATE_STATES:
        return {"ok": False, "error": f"非法目标状态：{to_state}"}
    allowed = ec.CANDIDATE_TRANSITIONS.get(current, ())
    if to_state not in allowed:
        return {
            "ok": False,
            "error": f"{candidate_id} 不能从 {current} 迁到 {to_state}（允许：{list(allowed) or '无，终态'}）",
        }
    record["state"] = to_state
    record.setdefault("history", []).append(
        {"at": ec.now_iso(), "from": current, "to": to_state, "reason": reason}
    )
    save_store(workdir, store)
    return {"ok": True, "candidate_id": candidate_id, "from": current, "to": to_state}


def summarize(workdir: Path) -> dict:
    store = load_store(workdir)
    by_state: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for record in store["candidates"].values():
        state = str(record.get("state") or "pending")
        by_state[state] = by_state.get(state, 0) + 1
        ctype = str(record.get("type") or "unknown")
        by_type[ctype] = by_type.get(ctype, 0) + 1
    return {
        "total": len(store["candidates"]),
        "by_state": dict(sorted(by_state.items())),
        "by_type": dict(sorted(by_type.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="候选经验库（状态机，JSON 存储）")
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--ingest", type=Path, default=None, help="reflect 产出的候选文件")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--state", default=None, help="与 --list 连用，按状态过滤")
    parser.add_argument("--show", default=None, help="按 candidate_id 查看详情")
    parser.add_argument("--transition", default=None, help="候选 id")
    parser.add_argument("--to", default=None, help="目标状态")
    parser.add_argument("--reason", default=None)
    args = parser.parse_args(argv)

    workdir = args.workdir or ec.default_workdir()
    print("=" * 78)
    print("  候选经验库（状态机 · JSON · 零迁移）")
    print("=" * 78)

    if args.ingest is not None:
        if not args.ingest.is_file():
            print(f"[参数错误] 找不到候选文件：{args.ingest}")
            return ec.EXIT_ENV
        payload = ec.read_json(args.ingest)
        candidates = payload.get("candidates") if isinstance(payload, dict) else payload
        report = ingest(workdir, list(candidates or []))
        print(f"入库：新增 {len(report['added'])}，已存在跳过 {len(report['skipped'])}，库内共 {report['total']}")
        print(f"存储：{store_path(workdir)}")
        return ec.EXIT_OK

    if args.transition:
        if not args.to:
            print("[参数错误] --transition 需要配 --to")
            return ec.EXIT_ENV
        report = transition(workdir, args.transition, args.to, args.reason)
        if not report.get("ok"):
            print(f"[拒绝] {report['error']}")
            return ec.EXIT_FAIL
        print(f"{report['candidate_id']}：{report['from']} → {report['to']}")
        return ec.EXIT_OK

    if args.show:
        store = load_store(workdir)
        record = store["candidates"].get(args.show)
        if record is None:
            print(f"[未找到] {args.show}")
            return ec.EXIT_ENV
        print(__import__("json").dumps(record, ensure_ascii=False, indent=2))
        return ec.EXIT_OK

    # 走到这里只可能是 --list 或完全无参数（其余分支都已 return）
    store = load_store(workdir)
    records = list(store["candidates"].values())
    if args.state:
        records = [item for item in records if str(item.get("state")) == args.state]
    stats = summarize(workdir)
    print(f"共 {stats['total']} 条；状态 {stats['by_state']}；类型 {stats['by_type']}")
    print("-" * 78)
    if not records:
        print("（空）")
    for record in records:
        print(
            f"  {record.get('candidate_id'):<14}{str(record.get('state')):<10}"
            f"{str(record.get('type')):<14}{str(record.get('confidence')):<8}"
            f"{str(record.get('suggestion'))[:40]}"
        )
    return ec.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
