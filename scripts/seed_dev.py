"""第 7 步 · 可选开发种子数据（**仅手动执行**，不会被任何流程自动调用）。

插入 **1 条明确标注为 dev 示例的 account + persona**，供第 8 步（账号 / 人设 CRUD）
联调使用。特征：

- 名称统一带 ``[DEV示例]`` 前缀，便于一眼识别并清理；
- 幂等：同名账号已存在则跳过，不重复插入；
- 不触碰任何真实账号信息（不填小红书号）。

用法（在**项目根目录**下，需数据库已启动）：

    backend\\.venv\\Scripts\\python.exe scripts\\seed_dev.py

清理：

    DELETE FROM personas WHERE name LIKE '[DEV示例]%';
    DELETE FROM accounts WHERE name LIKE '[DEV示例]%';
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models import Account, Persona  # noqa: E402

ACCOUNT_NAME = "[DEV示例] 探店号"
PERSONA_NAME = "[DEV示例] 探店碎碎念人设"


def main() -> int:
    with SessionLocal() as db:
        account = db.execute(select(Account).where(Account.name == ACCOUNT_NAME)).scalar_one_or_none()
        if account is None:
            account = Account(
                name=ACCOUNT_NAME,
                niche="本地探店",
                remark="由 scripts/seed_dev.py 生成，仅用于第 8 步联调，可随时删除",
            )
            db.add(account)
            db.flush()  # 取得 id
            print(f"[新增] account  id={account.id}  name={account.name}")
        else:
            print(f"[已存在] account  id={account.id}  name={account.name}（跳过）")

        persona = db.execute(select(Persona).where(Persona.name == PERSONA_NAME)).scalar_one_or_none()
        if persona is None:
            persona = Persona(
                account_id=account.id,
                name=PERSONA_NAME,
                positioning="我是一个爱挑刺的本地探店博主，帮读者避雷，只推真心觉得值的店。",
                target_audience="同城 20-35 岁、爱尝鲜但预算有限的上班族",
                tone="口语、带点吐槽、结尾给结论",
                content_pillars=["新店首探", "老店复购", "人均性价比榜", "踩雷复盘"],
                taboo=["不接虚假探店", "不写没有实际体验的店"],
                signature_style="先说结论，再说理由 —— 「值不值，看结尾」",
                is_default=True,
            )
            db.add(persona)
            db.flush()
            print(f"[新增] persona  id={persona.id}  name={persona.name}")
        else:
            print(f"[已存在] persona  id={persona.id}  name={persona.name}（跳过）")

        db.commit()

    print("\n完成。以上数据均带 [DEV示例] 前缀，仅用于本地联调。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
