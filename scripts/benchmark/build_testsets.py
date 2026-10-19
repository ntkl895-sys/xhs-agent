# -*- coding: utf-8 -*-
"""评测集生成器（带标准答案的 test set）· 冻结夹具落盘。

为 xhs-agent 的「带 ground truth 的评测」生成全部夹具，落盘到
``docs/benchmark/fixtures/testsets/<名称>/``，并写入 manifest（含内容 sha256 =
**冻结指纹**）。评测脚本读取夹具前会校验 sha256，任何改动都会被检测。

四套夹具（答案均**人工构造、独立于被测实现**，不由被测代码生成）：

1. ``json_robustness``：200 条 JSON 鲁棒性样本（8 类 × 25），每条标注
   label = direct（应一次解析）/ repaired（应兜底救回）/ failed（应正确拒绝）
   与期望字段（expected）；
2. ``compliance``：128 条合规扫描样本（正例 64 应命中 + 负例 64 期望不命中，
   含词表外变体 / 谐音 / 插入分隔符 / 空格 及易误伤语境）；
3. ``normalize``：108 条视觉数值归一化样本 (raw, expected)；
4. ``rag_golden``：30 条知识片段 + 40 条查询（每条标注应命中片段 id），
   含「主题相近但答案不同」的 hard negatives。

口语化约法（纪律）：

- **先冻结、后评测**：本脚本生成夹具一次；此后评测只读不改。重新生成需显式
  ``--force``（会更新 sha256 指纹），且报告须保留前后两版结果与说明；
- 答案独立：样本内容与 label 由人工设计的确定性规则构造，**不依赖被测函数**；
- 不删离群：评测脚本逐样本留档。

用法（项目根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\build_testsets.py
    backend\\.venv\\Scripts\\python.exe scripts\\benchmark\\build_testsets.py --force

退出码：0 = 生成完成；2 = 夹具已存在（冻结保护，未覆盖）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import bench_common as bc  # noqa: E402

TESTSETS_DIR = bc.FIXTURES_DIR / "testsets"
TESTSET_VERSION = "1.0"

# ===========================================================================
# 一、JSON 鲁棒性夹具（200 条，8 类 × 25）
# ===========================================================================
#: 基础素材池：25 个小型 JSON 负载（小红书运营场景 / 通用，取值中性）
_PAYLOADS: list[dict] = [
    {"title": "秋日通勤穿搭", "tags": ["毛衣", "通勤"]},
    {"note_id": "ab12-cd34", "score": 4.5, "ready": True},
    {"analysis": {"tone": "轻松", "length": 420}, "status": "done"},
    {"items": [{"name": "拿铁", "price": 28}, {"name": "美式", "price": 22}]},
    {"title": "周末露营清单", "body": "帐篷、防潮垫、头灯都要带齐。", "count": 12},
    {"reply": "收到，稍后回复你。", "latency_ms": 35},
    {"levels": [1, 2, 3, 4, 5], "labels": ["草稿", "待审", "已发布"]},
    {"user": {"name": "小北", "vip": False, "points": 1280}},
    {"summary": "三个要点：标题要具体；正文分段；标签别堆砌。"},
    {"text": "今天也要加油呀", "emoji": "😀"},
    {"candidates": ["选题A", "选题B", "选题C"], "picked": "选题B"},
    {"meta": {"source": "manual", "retry": 2}, "ok": True},
    {"range": {"start": "2026-10-01", "end": "2026-10-07"}, "days": 7},
    {"message": "已生成 3 个版本，请查收。", "variant_count": 3},
    {"kpis": {"views": 12800, "likes": 326, "collects": 145}},
    {"draft": "第一段：开场白。第二段：核心观点。", "words": 46},
    {"options": {"tags": ["干货", "教程"], "cover": False}},
    {"plan": ["周一发干货", "周三发育儿", "周五发vlog"], "week": 41},
    {"code": "A1", "detail": "库存充足", "quantity": 250},
    {"feedback": {"stars": 4, "comment": "整体不错，标题可以更聚焦。"}},
    {"counters": {"likes": "1,234", "shares": "56"}, "note": "字符串计数"},
    {"title": "三分钟快手早餐", "steps": ["打蛋", "热锅", "翻炒"], "minutes": 3},
    {"env": "staging", "debug": False, "version": "1.2.0"},
    {"pair": [{"q": "标题怎么起"}, {"a": "给具体数字"}], "score": 0.87},
    {"empty_list": [], "empty_obj": {}, "flag": True},
]

#: 控制字符类的 25 个标题文本（正文由多行拼接，dumps 后把 \n 还原为裸控制字符）
_CTRL_TITLES: list[str] = [
    "秋日穿搭指南", "露营装备清单", "快手早餐合集", "通勤包内好物", "平价护肤心得",
    "租房改造记录", "周末城市漫步", "读书笔记分享", "健身入门计划", "七天作息调整",
    "阳台种菜日记", "咖啡冲煮笔记", "桌面收纳方案", "旅行打包技巧", "护眼习惯清单",
    "早起流程拆解", "英语听力训练", "手机摄影构图", "情绪管理练习", "周末做饭菜单",
    "记账习惯养成", "衣物护理要点", "通勤路线优化", "小众书店地图", "夜间护肤步骤",
]

#: 彻底不可修复类的 25 条文本（无任何可提取的 JSON 结构 / 有残括号但无闭合）
_BROKEN_TEXTS: list[str] = [
    "今天天气不错，适合出门走走。",
    "抱歉，我无法完成这个请求。",
    "第一步：打开应用；第二步：填写资料。",
    "这是一段普通的中文说明文字。",
    "结论：整体效果不错，建议继续保持。",
    "目前没有更多信息可以补充。",
    "请稍后重试，或联系人工客服。",
    "推荐指数：五颗星。",
    "本段文字里没有任何结构化数据。",
    "明日计划：早起、运动、阅读。",
    "后台正在处理中，请耐心等待。",
    "说明文档已在附件中给出。",
    "以上内容仅供参考，不构成建议。",
    "待办：买牛奶、取快递、还书。",
    "系统提示：网络连接不稳定。",
    "示例：把商品放进购物车即可。",
    "更新完成，共调整了三处。",
    "祝你今天有个好心情。",
    "请注意查收最新的版本说明。",
    "这段描述没有包含任何花括号或方括号。",
    "未完待续……",
    "正在生成中，请不要关闭页面。",
    "记得把重要文件备份到本地。",
    "今天想聊聊时间管理这件小事。",
    "感谢阅读，下次再见。",
]


def _json_robustness_cases() -> list[dict]:
    """生成 200 条 JSON 鲁棒性样本（确定性构造，答案由设计规则决定）。"""
    cases: list[dict] = []
    counter = {"n": 0}

    def add(bucket: str, raw: str, label: str, expected: dict | None, note: str) -> None:
        counter["n"] += 1
        cases.append(
            {
                "id": f"json_{counter['n']:03d}",
                "bucket": bucket,
                "raw": raw,
                "label": label,
                "expected": expected,
                "note": note,
            }
        )

    dumps = lambda payload: json.dumps(payload, ensure_ascii=False)  # noqa: E731

    # ------------------------------------------------------------------ ① valid
    for payload in _PAYLOADS:
        add("valid", dumps(payload), "direct", payload, "合法 JSON：应一次解析成功")

    # ------------------------------------------------------------------ ② fence
    for index, payload in enumerate(_PAYLOADS):
        body = dumps(payload)
        form = index % 3
        if form == 0:
            raw = f"```json\n{body}\n```"
            note = "代码围栏（json 标记）：去围栏后应成功"
        elif form == 1:
            raw = f"```\n{body}\n```"
            note = "代码围栏（无语言标记）：去围栏后应成功"
        else:
            raw = f"以下是解析结果：\n```json\n{body}\n```\n以上内容仅供参考。"
            note = "围栏 + 前后说明文字：兜底（平衡扫描）后应成功"
        add("fence", raw, "repaired", payload, note)

    # ------------------------------------------------------------------ ③ ctrl
    for index, title in enumerate(_CTRL_TITLES):
        payload = {
            "title": title,
            "body": f"{title}的第一行要点。\n第二行：展开说明。\n第三行：收尾提醒。",
            "count": len(title) + index,
        }
        raw = dumps(payload).replace("\\n", "\n")  # 字符串内裸换行 → 非法控制字符
        add("ctrl", raw, "repaired", payload, "字符串内含裸换行（非法控制字符）：应修复后成功")

    # ------------------------------------------------------------------ ④ bracket
    forms = ["extra"] * 9 + ["missing"] * 8 + ["miscolon"] * 4 + ["miscparen"] * 4
    for index, payload in enumerate(_PAYLOADS):
        body = dumps(payload)
        form = forms[index]
        if form == "extra":
            raw, note = body + "}", "尾部多余括号：应兜底救回首个完整片段"
            label = "repaired"
        elif form == "missing":
            raw, note = body[:-1], "缺闭合括号：不可靠修复，应正确拒绝"
            label = "failed"
        elif form == "miscolon":
            raw, note = body.replace('": ', '" ', 1), "冒号缺失（结构错位）：应正确拒绝"
            label = "failed"
        else:
            raw, note = "(" + body[1:], "开括号错位（非 JSON 定界符）：应正确拒绝"
            label = "failed"
        add("bracket", raw, label, payload if label == "repaired" else None, note)

    # ------------------------------------------------------------------ ⑤ trailing
    for index, payload in enumerate(_PAYLOADS):
        body = dumps(payload)
        if body.endswith("}"):
            raw = body[:-1] + ",}"
        elif body.endswith("]"):
            raw = body[:-1] + ",]"
        else:  # pragma: no cover - 池内不存在该形态
            raw = body + ","
        add("trailing", raw, "repaired", payload, "尾随逗号：语义安全，期望兜底救回")

    # ------------------------------------------------------------------ ⑥ truncated
    for payload in _PAYLOADS:
        body = dumps(payload)
        cut = max(6, int(len(body) * 0.62))
        raw = body[:cut]
        add("truncated", raw, "failed", None, "JSON 被截断（内容不完整）：不得硬解析，应正确拒绝")

    # ------------------------------------------------------------------ ⑦ quotes
    for index, payload in enumerate(_PAYLOADS):
        body = dumps(payload)
        if index % 2 == 0:
            raw = body.replace('"', "'")
            note = "单引号 JSON：期望兜底救回"
        else:
            out = []
            flip = 0
            for char in body:
                if char == '"':
                    out.append("“" if flip % 2 == 0 else "”")
                    flip += 1
                else:
                    out.append(char)
            raw = "".join(out)
            note = "中文引号 JSON：期望兜底救回"
        add("quotes", raw, "repaired", payload, note)

    # ------------------------------------------------------------------ ⑧ broken
    for text in _BROKEN_TEXTS:
        add("broken", text, "failed", None, "无任何可提取的 JSON 结构：应正确拒绝")

    assert len(cases) == 200, f"JSON 夹具数量应为 200，实际 {len(cases)}"
    return cases


# ===========================================================================
# 通用：冻结落盘
# ===========================================================================
def _sha256_of(payload: object) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _freeze(name: str, files: dict[str, object], *, answer_source: str, stats: dict,
            notes: str = "", force: bool = False) -> bool:
    """把一套夹具落盘并写 manifest（含 sha256 冻结指纹）。

    Returns:
        True = 已写入；False = 已存在且未加 --force（冻结保护）。
    """
    target = TESTSETS_DIR / name
    if target.exists() and not force:
        print(f"[跳过] {name} 已存在（冻结保护）；如需重新生成请显式加 --force。")
        return False
    target.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    for filename, payload in files.items():
        path = target / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        digests[filename] = _sha256_of(payload)
    manifest = {
        "name": name,
        "version": TESTSET_VERSION,
        "generated_at": bc.now_iso(),
        "generator": "scripts/benchmark/build_testsets.py",
        "answer_source": answer_source,
        "stats": stats,
        "sha256": digests,
        "freeze_notice": (
            "本测试集已冻结：评测只读不改；任何修改须升 version 并在报告中保留前后两版结果。"
        ),
        "notes": notes,
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[写入] fixtures/testsets/{name}/（{', '.join(files)} + manifest.json）")
    return True


# ===========================================================================
# 二、合规扫描夹具（128 条：正例 64 应命中 + 负例 64 期望不命中）
# ===========================================================================
#: 正例·基础组（16 条）：词表词的**自然语境**（非孤立罗列；每条 note 注明期望命中词）
_CMP_POS_BASIC: list[tuple[str, str]] = [
    ("宝宝脸上的斑坚持调理了两个月，终于祛斑成功，太开心了！", "祛斑"),
    ("这款面霜主打抗老，居然还宣称能丰胸，纯属夸大其词。", "丰胸"),
    ("医生说先调整作息，再评估是否需要降血压的药物。", "降血压"),
    ("坚持打卡三个月，居然真的治愈了我的熬夜拖延症。", "治愈"),
    ("评论区扣1进群，今天下午统一发资料。", "扣1进群"),
    ("想学穿搭的姐妹加微信交流，记得备注来意。", "加微信"),
    ("全网最低价不敢说，但我们做到了同款里最省心。", "全网最低"),
    ("这款产品拿过行业第一的口碑，复购率一直很高。", "行业第一"),
    ("老粉都知道，这是本店唯一的常驻款。", "唯一"),
    ("号称稳赚不赔的项目，往往风险最大，先学会辨别。", "稳赚不赔"),
    ("月入十万的标题党太多了，真实收入我从不隐瞒。", "月入十万"),
    ("专家推荐可以参考，但要看清推荐背后的动机。", "专家推荐"),
    ("开业那天请了师傅来开光，图个吉利。", "开光"),
    ("账号被限流了，平台提示的原因是站外链接导流。", "站外链接"),
    ("刷单兼职群千万别进，十个里有九个是骗局。", "刷单"),
    ("据说这串手链能转运，我是不信的。", "转运"),
    ("这个『秘方』号称能药到病除，看到请顺手举报。", "药到病除"),
    ("这类课程号称 7 天让你日入过万，多半是割韭菜。", "日入过万"),
    ("这款茶饮宣称能降血糖，已经被监管部门点名。", "降血糖"),
    ("权威认证的牌子不是想印就能印的。", "权威认证"),
]

#: 正例·分隔符/空格组（14 条）：归一化（去标点空白）后应命中
_CMP_POS_SEPARATED: list[tuple[str, str]] = [
    ("想了解详情的宝子加-微-信，备注来意。", "加微信"),
    ("加 微 信 领取资料包，限今天。", "加微信"),
    ("全网·最·低的活动价，错过等一年。", "全网最低"),
    ("主打一个·最·强·防护，户外党狂喜。", "最强"),
    ("宝子们记得扣 1 进群，马上发模板。", "扣1进群"),
    ("全-网-最-低 的价格，仅限首单。", "全网最低"),
    ("加\u3000微\u3000信 同号，欢迎咨询。", "加微信"),
    ("行业·第·一 的测评数据，仅供参考。", "行业第一"),
    ("这个价格只能说不亏，稳-赚不敢当。", "稳赚"),
    ("日入·过万 的副业帖子，我举报了好几篇。", "日入过万"),
    ("私-聊-发-你 详细的对比表格。", "私聊发你"),
    ("扫码·进群 领取新课表。", "扫码进群"),
    ("唯一·不变的是变化本身。", "唯一"),
    ("权威·认证 四个字不能随便写。", "权威认证"),
]

#: 正例·大小写/写法变体组（10 条）
_CMP_POS_CASE: list[tuple[str, str]] = [
    ("需要资料的朋友加VX，秒回。", "加vx"),
    ("VX号放在主页简介里了。", "vx号"),
    ("加Wx 备注从主页来的。", "加wx 系词表外写法（w/v 书写差异）"),
    ("VX 号也可以搜到我们。", "vx号（空格隔开）"),
    ("直接加威信聊吧，方便发文件。（谐音）", "威信=微信谐音"),
    ("全网Zui低？不存在的。（拼音谐音）", "zui 拼音谐音"),
    ("WEI XIN 同号，欢迎咨询。（拼音）", "weixin 拼音"),
    ("加V❤️ 详聊。（emoji 插入）", "带 emoji 的写法"),
    ("kuai 加 vx 拿模板！", "加 vx（空格+小写）"),
    ("微信号：beibeistore01", "微信号"),
]

#: 正例·谐音/词表外近似组（20 条）：多数为「设计上会漏」的对抗变体（如实测出）
_CMP_POS_VARIANT: list[tuple[str, str]] = [
    ("全网蕞低价，蕞蕞划算。", "蕞=最 谐音"),
    ("稳撰不赔的理财课，看到请绕道。", "撰=赚 谐音"),
    ("直接加威信拿清单。", "信=信 谐音（威信）"),
    ("加薇信拉你进群。", "薇=微 谐音"),
    ("月入十萬？醒醒吧。", "繁体萬"),
    ("日入过萬的课程截图，多半是假的。", "繁体萬"),
    ("包治百疾的广告又出现了。", "疾=病 近似"),
    ("号称100％有效的喷剂。", "全角百分号"),
    ("蕞强防护，蕞长续航。", "蕞=最 谐音"),
    ("最顶级配置，价格还很克制。", "词表词「顶级」正常出现"),
    ("独家·首发，早鸟价仅三天。", "「独家」+分隔符"),
    ("转·运手串限时上架。", "「转运」+分隔符"),
    ("高回报？稳赚！看到这两种说法请警惕。", "含两个词表词"),
    ("转！运！搭子来组队。", "「转运」+分隔符"),
    ("一夜暴付的帖子，别信。", "付=富 谐音"),
    ("躺賺课程又换马甲了。", "繁体賺"),
    ("零風险理财，谨慎看待。", "繁体風"),
    ("绝无仅有·的版本，只印刷了一批。", "「绝无仅有」+分隔符"),
    ("改·运 小妙招合集。", "「改运」+分隔符"),
    ("招💰财摆件真有用吗？", "emoji 插入"),
]

#: 负例·明显干净（36 条）
_CMP_NEG_CLEAN: list[str] = [
    "今天的晚霞特别好看，随手拍了几张。",
    "分享一个提高效率的小方法：先做最重要的三件事。",
    "周末去公园跑了五公里，配速比上周快了一点。",
    "新买的绿植放在窗边，每天晒太阳。",
    "晚上读完了这本书的第三章，收获很多。",
    "中午做了一份番茄鸡蛋面，简单又好吃。",
    "最近在学做手冲咖啡，水温控制是关键。",
    "把书桌重新整理了一遍，舒服多了。",
    "给大家推荐一条安静的散步路线。",
    "第一次尝试用相机拍月亮，效果还不错。",
    "今天把衣柜里的衣服按季节分好了。",
    "学会了几个新的拉伸动作，肩膀舒服多了。",
    "小猫今天学会了用猫抓板。",
    "阳台的薄荷长得很好，剪了几枝泡水。",
    "尝试早睡一周，白天精神好了不少。",
    "把旧照片整理成了电子相册。",
    "今天的便当配色很满意，拍了张照。",
    "下载了一个记账应用，坚持了十天。",
    "给家里的椅子换了个新的坐垫。",
    "傍晚骑车沿河走了一圈，风很舒服。",
    "学着剪了个短视频，节奏还在摸索。",
    "周末烤了戚风蛋糕，第一次就很成功。",
    "把常用的公式做成了小卡片贴在墙上。",
    "换了新的键盘，打字手感不错。",
    "阳台的多肉换了盆，希望长得更好。",
    "今天尝试了新的通勤路线，少花了十分钟。",
    "图书馆借了三本书，都是讲沟通的。",
    "拍了一段延时摄影，云动得很有意思。",
    "把冰箱清理了一遍，扔掉不少过期食品。",
    "学了两句简单的法语问候语。",
    "今晚的月亮又圆又亮，适合散步。",
    "给笔电换了个新壁纸，心情都好了。",
    "试着用表格管理每周的待办事项。",
    "整理了一份常去的咖啡馆清单。",
    "练习了十分钟冥想，确实更放松。",
    "把去年旅行的手账翻出来看了看。",
]

#: 负例·字面含词表词但语义无害（14 条，预期最可能出现 FP，如实测出）
_CMP_NEG_BAIT_LITERAL: list[tuple[str, str]] = [
    ("暴雨过后，市政部门开始根治城区内涝问题。", "根治（治水语境）"),
    ("这次期末，我们小组排名第一，全组都很开心。", "排名第一（成绩语境）"),
    ("小区门口的快递转运中心今天开始运营。", "转运（物流语境）"),
    ("报名参加微积分竞赛的同学请举手。", "加微（数学语境）"),
    ("报名表在群文件里，参加微课的同学看第三条。", "加微（课程语境）"),
    ("公司决定修改运营节奏，先做小范围试点。", "改运（运营语境）"),
    ("修改运费模板的操作路径截图如下。", "改运（运费语境）"),
    ("种子在春雨里萌生发芽。", "生发（植物语境）"),
    ("确保本次出行的安全是首要任务。", "保本（确保本次）"),
    ("我们唯一要做的就是耐心等待花开花落。", "唯一（语义无害）"),
    ("历史展览上看到了那幅唯一的真迹。", "唯一（语义无害）"),
    ("这是一条通往山顶的独家小路。", "独家（路径语境）"),
    ("这是全市唯一一家盲人按摩公益店。", "唯一（公益语境）"),
    ("网上流传的「招财猫」摆件其实只是普通陶瓷。", "招财（话题讨论语境）"),
]

#: 负例·含敏感字但不在词表（14 条，测「不过度匹配」）
_CMP_NEG_BAIT_SENSITIVE: list[str] = [
    "最近天气转凉，注意添衣。",
    "第一次做戚风蛋糕就成功了。",
    "绝对不踩雷的咖啡店清单。",
    "这家面馆的味道最地道。",
    "最后的那个方案我觉得更稳妥。",
    "最好看的秋景就在这条路上。",
    "记得做好备份，以防万一。",
    "这个小工具的易用性一流。",
    "全家人一起动手包饺子，其乐融融。",
    "第一步先热身，避免拉伤。",
    "首屈一指的手艺？别夸了，熟能生巧而已。",
    "这家店的招牌菜堪称一绝。",
    "品质绝对过硬，用了三年没坏。",
    "全网都在夸的收纳方法，我也试了。",
]


def _compliance_cases() -> list[dict]:
    """生成 128 条合规扫描样本（正例 64 + 负例 64）。"""
    cases: list[dict] = []
    counter = {"n": 0}

    def add(group: str, text: str, expect_hit: bool, note: str) -> None:
        counter["n"] += 1
        cases.append(
            {
                "id": f"cmp_{counter['n']:03d}",
                "group": group,
                "text": text,
                "expect_hit": expect_hit,
                "note": note,
            }
        )

    for text, word in _CMP_POS_BASIC:
        add("pos_basic", text, True, f"期望命中：{word}")
    for text, word in _CMP_POS_SEPARATED:
        add("pos_separated", text, True, f"期望命中（分隔符/空格归一化）：{word}")
    for text, word in _CMP_POS_CASE:
        add("pos_case", text, True, f"写法变体：{word}")
    for text, note in _CMP_POS_VARIANT:
        add("pos_variant", text, True, f"谐音/词表外近似（如实测出是否命中）：{note}")
    for text in _CMP_NEG_CLEAN:
        add("neg_clean", text, False, "明显干净文案")
    for text, note in _CMP_NEG_BAIT_LITERAL:
        add("neg_bait_literal", text, False, f"易误伤（字面含词表词）：{note}")
    for text in _CMP_NEG_BAIT_SENSITIVE:
        add("neg_bait_sensitive", text, False, "含敏感字眼但不在词表：期望不误伤")

    assert len(cases) == 128, f"合规夹具数量应为 128，实际 {len(cases)}"
    return cases


# ===========================================================================
# 三、视觉数值归一化夹具（108 条：raw → expected）
# ===========================================================================
def _normalize_cases() -> list[dict]:
    """生成 108 条归一化样本（12 类 × 9；expected=None 表示应为空值）。"""
    cases: list[dict] = []
    counter = {"n": 0}

    def add(category: str, raw, expected, note: str) -> None:
        counter["n"] += 1
        cases.append(
            {
                "id": f"norm_{counter['n']:03d}",
                "category": category,
                "raw": raw,
                "expected": expected,
                "note": note,
            }
        )

    # ① 纯数字
    for raw in ["0", "7", "123", "4567", "95272", "1000000", "42", "8888", "2026"]:
        add("plain_int", raw, int(raw), "纯数字：原样解析")
    # ② 半角千分位
    for raw, expected in [("1,234", 1234), ("12,345", 12345), ("1,234,567", 1234567),
                          ("9,999", 9999), ("100,000", 100000), ("23,456", 23456),
                          ("7,007", 7007), ("500,000", 500000), ("3,141", 3141)]:
        add("thousand_sep", raw, expected, "半角千分位：去逗号")
    # ③ 全角逗号千分位
    for raw, expected in [("1，234", 1234), ("12，345", 12345), ("9，999", 9999),
                          ("100，000", 100000), ("5，678", 5678), ("88，888", 88888),
                          ("1，000，000", 1000000), ("66，666", 66666), ("3，000", 3000)]:
        add("fullwidth_comma", raw, expected, "中文逗号千分位：去逗号")
    # ④ 万
    for raw, expected in [("1.2万", 12000), ("3万", 30000), ("10.5万", 105000),
                          ("0.5万", 5000), ("12.34万", 123400), ("99万", 990000),
                          ("1万", 10000), ("2.5万", 25000), ("100万", 1000000)]:
        add("wan", raw, expected, "单位「万」：×10000")
    # ⑤ 亿
    for raw, expected in [("1.2亿", 120000000), ("2亿", 200000000), ("0.8亿", 80000000),
                          ("10亿", 1000000000), ("1亿", 100000000), ("3.5亿", 350000000),
                          ("0.05亿", 5000000), ("12亿", 1200000000), ("1.08亿", 108000000)]:
        add("yi", raw, expected, "单位「亿」：×100000000")
    # ⑥ w / W
    for raw, expected in [("1.5w", 15000), ("2w", 20000), ("3.2W", 32000),
                          ("0.8w", 8000), ("12w", 120000), ("1.2W", 12000),
                          ("100w", 1000000), ("0.5W", 5000), ("5.5w", 55000)]:
        add("letter_w", raw, expected, "单位「w/W」：×10000")
    # ⑦ 千 / k / K
    for raw, expected in [("3.4千", 3400), ("5k", 5000), ("1.5K", 1500),
                          ("0.6千", 600), ("12k", 12000), ("8.8K", 8800),
                          ("2千", 2000), ("20k", 20000), ("0.25千", 250)]:
        add("kilo", raw, expected, "单位「千/k/K」：×1000")
    # ⑧ 空格 / 空白混排
    for raw, expected, note in [
        ("1 234", 1234, "半角空格千分位"),
        ("12 345", 12345, "半角空格千分位"),
        (" 5678 ", 5678, "首尾空格"),
        ("1\u00a0234", 1234, "不换行空格（NBSP）"),
        (" 99 ", 99, "首尾空格"),
        ("7 007", 7007, "半角空格千分位"),
        ("1 2 3", 123, "数字间多处空格（全去）"),
        ("500 000", 500000, "半角空格千分位"),
        ("  42  ", 42, "首尾多空格"),
    ]:
        add("spaces", raw, expected, note)
    # ⑨ 小数（非半数，避免银行家舍入争议）
    for raw, expected in [("123.4", 123), ("99.9", 100), ("12.6", 13),
                          ("7.2", 7), ("345.8", 346), ("1000.1", 1000),
                          ("66.66", 67), ("0.4", 0), ("250.7", 251)]:
        add("decimal", raw, expected, "小数：四舍五入到整数")
    # ⑩ 混合文本（从文本中提取首个可解释数值）
    for raw, expected, note in [
        ("点赞 1234", 1234, "前缀文字"),
        ("曝光量：8567", 8567, "中文冒号"),
        ("共 234 条评论", 234, "句中数字"),
        ("收藏 12,345", 12345, "文本+千分位"),
        ("新增粉丝 806", 806, "前缀文字"),
        ("分享数 56", 56, "前缀文字"),
        ("阅读约 1.2万", 12000, "文本+单位"),
        ("总曝光 12.8万", 128000, "文本+单位"),
        ("评论 187 条", 187, "句中数字"),
    ]:
        add("mixed_text", raw, expected, note)
    # ⑪ 占位符与空（expected=None）
    for raw, note in [("-", "短横线占位"), ("--", "双短横线占位"), ("—", "em 长横占位"),
                      ("暂无", "中文占位"), ("?", "问号占位"), ("？", "全角问号占位"),
                      ("无", "中文「无」"), ("N/A", "n/a 占位"), ("", "空串")]:
        add("placeholder", raw, None, f"{note}：应为空值（None）")
    # ⑫ 非法与边界
    add("invalid", "abc", None, "非数字串：应为 None")
    add("invalid", "hello world", None, "英文串：应为 None")
    add("invalid", "待定", None, "中文非数字词：应为 None")
    add("invalid", "-5", None, "负数（半角减号）：计数语义下应为 None")
    add("invalid", "－5", None, "负数（全角减号）：语义同上（如实测出实现行为）")
    add("invalid", "85%", None, "百分比：计数语义下应为 None")
    add("invalid", "１２３", 123, "全角数字：期望等价 123（如实测出实现行为）")
    add("invalid", "1.2.3", None, "非法数（多点）：应为 None")
    add("invalid", "   ", None, "纯空白串：应为 None")

    assert len(cases) == 108, f"归一化夹具数量应为 108，实际 {len(cases)}"
    return cases


# ===========================================================================
# 四、RAG 黄金集（30 片段 + 40 查询；含「主题相近但答案不同」hard negatives）
# ===========================================================================
#: 知识片段（全部 synthetic；人工撰写，含明确答案点）
_RAG_CORPUS: list[dict] = [
    {"id": "kb_01", "topic": "内容运营", "title": "标题撰写",
     "content": "小红书标题 26 字以内更容易完整显示；加入具体数字或场景词（如「3 步」「通勤」）能提高点击率。"},
    {"id": "kb_02", "topic": "内容运营", "title": "封面设计",
     "content": "封面用高对比配色 + 不超过 12 个大字；人物出镜或前后对比图的点击率更高。"},
    {"id": "kb_03", "topic": "内容运营", "title": "发布时段",
     "content": "工作日 19:00-21:00、周末 10:00-12:00 的发文互动更集中；应避开凌晨发布。"},
    {"id": "kb_04", "topic": "内容运营", "title": "标签策略",
     "content": "单篇笔记配 3-10 个标签：1-2 个大词搭 3-6 个垂类小词，再加 1-2 个长尾词，避免堆砌无关标签。"},
    {"id": "kb_05", "topic": "内容运营", "title": "评论运营",
     "content": "发布后前 30 分钟是互动黄金窗口：优先回复提问类评论，并把优质评论置顶引导讨论。"},
    {"id": "kb_06", "topic": "内容运营", "title": "数据复盘漏斗",
     "content": "复盘看四层漏斗：曝光到点击看封面标题、点击到互动看内容、互动到转化看主页与私信；先定位掉得最多的一层。"},
    {"id": "kb_07", "topic": "健康生活", "title": "咖啡因与睡眠",
     "content": "咖啡因半衰期约 5 小时；睡前 6 小时内摄入会推迟入睡并减少深睡时段。"},
    {"id": "kb_08", "topic": "健康生活", "title": "咖啡因与运动表现",
     "content": "运动前 30-60 分钟摄入约 3mg/kg 体重的咖啡因，可提升耐力表现。"},
    {"id": "kb_09", "topic": "健康生活", "title": "肩颈拉伸",
     "content": "久坐每 60 分钟做 2 分钟肩颈拉伸：下巴微收、肩胛骨后收并下沉。"},
    {"id": "kb_10", "topic": "健康生活", "title": "冥想入门",
     "content": "从每天 5 分钟呼吸计数开始：吸气 4 秒、呼气 6 秒，走神后温柔地把注意力拉回呼吸。"},
    {"id": "kb_11", "topic": "健康生活", "title": "早睡调整",
     "content": "连续 7 天把上床时间每天提前 20 分钟，并保持起床时间固定；比周末补觉更有效。"},
    {"id": "kb_12", "topic": "健康生活", "title": "护眼 20-20-20",
     "content": "每用眼 20 分钟，看向 6 米（20 英尺）外 20 秒，可缓解视疲劳。"},
    {"id": "kb_13", "topic": "效率方法", "title": "番茄工作法",
     "content": "番茄钟 = 25 分钟专注 + 5 分钟休息；连续 4 轮后休息 15-30 分钟。"},
    {"id": "kb_14", "topic": "效率方法", "title": "待办清单法",
     "content": "待办清单每天最多列 6 件事，把「重要且难」的排在早上第一件，完成后再补新任务。"},
    {"id": "kb_15", "topic": "效率方法", "title": "邮件三段式",
     "content": "邮件三段式：第一句说明来意，中间分条列要点，结尾给出明确的下一步与时间。"},
    {"id": "kb_16", "topic": "效率方法", "title": "会议纪要四要素",
     "content": "会议纪要记四样：决议、待办（负责人 + 截止时间）、遗留问题、下次会议时间。"},
    {"id": "kb_17", "topic": "效率方法", "title": "康奈尔笔记法",
     "content": "康奈尔笔记把每页分三区：左侧线索栏、右侧笔记区、底部总结区。"},
    {"id": "kb_18", "topic": "效率方法", "title": "复盘四问",
     "content": "复盘四问：目标是什么、结果如何、差异出在哪里、下一步动作是什么。"},
    {"id": "kb_19", "topic": "烹饪咖啡", "title": "手冲水温",
     "content": "手冲咖啡水温 88-93℃：浅烘取偏高值，深烘取偏低值。"},
    {"id": "kb_20", "topic": "烹饪咖啡", "title": "手冲研磨度",
     "content": "手冲研磨度约为砂糖粗细（中细）：过细易苦涩，过粗易寡淡。"},
    {"id": "kb_21", "topic": "烘焙", "title": "戚风塌陷原因",
     "content": "戚风塌陷多因蛋白霜消泡或没有烤透：翻拌要轻、出炉立刻倒扣放凉。"},
    {"id": "kb_22", "topic": "烘焙", "title": "戚风脱模",
     "content": "戚风需要完全凉透再脱模：用薄刀贴模壁划一圈，再从底部顶出。"},
    {"id": "kb_23", "topic": "生活技能", "title": "冰箱收纳分区",
     "content": "冰箱上层放即食、中层放奶蛋、下层放生鲜；冷藏室后壁最冷，适合放奶制品。"},
    {"id": "kb_24", "topic": "生活技能", "title": "洗标符号速查",
     "content": "衣物护理先看洗标：圆圈表示可干洗、三角表示可漂白、熨斗点数表示温度上限。"},
    {"id": "kb_25", "topic": "理财安全", "title": "记账三账户",
     "content": "记账起步用三个账户：日常开销、固定储蓄、弹性娱乐；每笔支出只归属一个账户。"},
    {"id": "kb_26", "topic": "理财安全", "title": "预算 50-30-20",
     "content": "预算分配常用 50-30-20：必要支出 50%、想要 30%、储蓄与投资 20%。"},
    {"id": "kb_27", "topic": "理财安全", "title": "密码管理器",
     "content": "每个网站用独立的长密码，交由密码管理器保管；主密码只记在脑子里，不写在便签上。"},
    {"id": "kb_28", "topic": "理财安全", "title": "双重验证",
     "content": "能给账号开启双重验证就开启：优先验证器 App 或安全密钥，短信验证次之。"},
    {"id": "kb_29", "topic": "理财安全", "title": "网购比价",
     "content": "下单前做三件事：看历史价、算运费、比同款评价数；警惕只换包装的同款商品。"},
    {"id": "kb_30", "topic": "生活技能", "title": "快递验货",
     "content": "贵重物品当面验货：先看外包装、再拍开箱视频，发现异常直接拒收。"},
]

#: 查询（40 条；expected_ids = 应命中的片段，1-3 个；note 注明 hard negative 设计）
_RAG_QUERIES: list[tuple[str, list[str], str]] = [
    ("标题写多少字合适？", ["kb_01"], "direct"),
    ("封面放几个字比较好？", ["kb_02"], "direct"),
    ("什么时候发笔记流量好？", ["kb_03"], "direct"),
    ("一篇笔记配多少个标签？", ["kb_04"], "direct"),
    ("发布后多久要盯评论区？", ["kb_05"], "direct"),
    ("怎么看数据找出问题在哪层？", ["kb_06"], "direct"),
    ("睡前喝咖啡会影响睡眠吗？", ["kb_07"], "hard：干扰项 kb_08（咖啡因与运动）"),
    ("运动前喝咖啡有用吗？", ["kb_08"], "hard：干扰项 kb_07（咖啡因与睡眠）"),
    ("久坐肩颈怎么放松？", ["kb_09"], "direct"),
    ("冥想从几分钟开始？", ["kb_10"], "direct"),
    ("怎么把作息逐渐调早？", ["kb_11"], "direct"),
    ("看屏幕久了眼睛怎么休息？", ["kb_12"], "direct"),
    ("番茄钟是多长时间一轮？", ["kb_13"], "hard：干扰项 kb_14（同为效率方法）"),
    ("每天待办列多少件合适？", ["kb_14"], "hard：干扰项 kb_13（同为效率方法）"),
    ("写工作邮件怎么组织结构？", ["kb_15"], "direct"),
    ("会议纪要要记什么内容？", ["kb_16"], "hard：干扰项 kb_17（同为记录方法）"),
    ("康奈尔笔记怎么分区？", ["kb_17"], "hard：干扰项 kb_16（同为记录方法）"),
    ("复盘要问哪几个问题？", ["kb_18"], "direct"),
    ("手冲咖啡水温多少合适？", ["kb_19"], "hard：干扰项 kb_20（手冲参数）"),
    ("手冲咖啡磨多粗？", ["kb_20"], "hard：干扰项 kb_19（手冲参数）"),
    ("戚风蛋糕为什么会塌陷？", ["kb_21"], "hard：干扰项 kb_22（戚风流程）"),
    ("戚风蛋糕怎么完整脱模？", ["kb_22"], "hard：干扰项 kb_21（戚风流程）"),
    ("冰箱东西怎么分区放？", ["kb_23"], "direct"),
    ("衣服洗标上的符号怎么看？", ["kb_24"], "direct"),
    ("记账怎么开始做分类？", ["kb_25"], "hard：干扰项 kb_26（记账预算）"),
    ("每月收入按什么比例分配？", ["kb_26"], "hard：干扰项 kb_25（记账预算）"),
    ("密码太多记不住怎么办？", ["kb_27"], "direct"),
    ("账号怎么设置更安全？", ["kb_28"], "direct"),
    ("网购怎么避免买贵？", ["kb_29"], "direct"),
    ("收贵重快递要注意什么？", ["kb_30"], "direct"),
    ("咖啡因对睡眠和运动分别有什么说法？", ["kb_07", "kb_08"], "multi：跨两条近邻片段"),
    ("内容发布前中后分别要做什么？", ["kb_03", "kb_05"], "multi：发布时段 + 评论运营"),
    ("专注方法和任务清单怎么配合使用？", ["kb_13", "kb_14"], "multi：效率方法组合"),
    ("手冲咖啡从水温到研磨怎么调？", ["kb_19", "kb_20"], "multi：手冲参数组合"),
    ("烘焙翻车的排查思路？", ["kb_21", "kb_22"], "multi：戚风问题组合"),
    ("做笔记的标题和封面有什么要点？", ["kb_01", "kb_02"], "multi：内容运营组合"),
    ("账号安全最该先做哪两件事？", ["kb_27", "kb_28"], "multi：安全组合"),
    ("记账和预算怎么配套？", ["kb_25", "kb_26"], "multi：理财组合"),
    ("写邮件和写纪要的要点？", ["kb_15", "kb_16"], "multi：书面沟通组合"),
    ("护眼和肩颈的日常小习惯？", ["kb_09", "kb_12"], "multi：健康习惯组合"),
]


def _rag_golden_files() -> tuple[dict[str, object], dict[str, object]]:
    """返回 (corpus_payload, queries_payload)。"""
    corpus = {
        "purpose": "RAG 检索黄金集（合成语料）：验证检索管线「能不能找对」；不代表真实语料检索质量",
        "source": "synthetic",
        "entries": _RAG_CORPUS,
    }
    queries = {
        "purpose": "查询与期望命中片段（人工标注；含主题相近但答案不同的 hard negatives）",
        "entries": [
            {
                "id": f"ragq_{index:02d}",
                "query": query,
                "expected_ids": expected,
                "note": note,
            }
            for index, (query, expected, note) in enumerate(_RAG_QUERIES, start=1)
        ],
    }
    return corpus, queries


def main() -> int:
    parser = argparse.ArgumentParser(description="评测集生成器（冻结夹具落盘）")
    parser.add_argument("--force", action="store_true", help="已存在时覆盖（会更新冻结指纹）")
    args = parser.parse_args()

    bc.ensure_dirs()
    bc.print_banner("评测集生成（冻结夹具 · 答案独立于被测实现）")

    # ------------------------------------------------------------ 1. JSON
    json_cases = _json_robustness_cases()
    bucket_counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}
    for item in json_cases:
        bucket_counts[item["bucket"]] = bucket_counts.get(item["bucket"], 0) + 1
        label_counts[item["label"]] = label_counts.get(item["label"], 0) + 1
    _freeze(
        "json_robustness",
        {"cases.json": json_cases},
        answer_source=(
            "人工设计的确定性变换规则（8 类 × 25）：素材池 + 变形规则（围栏/控制字符/"
            "括号增删/尾随逗号/截断/引号替换）；label 按「语义是否可安全修复」判定，"
            "不依赖 json_repair 实现"
        ),
        stats={"total": len(json_cases), "buckets": bucket_counts, "labels": label_counts},
        notes=(
            "label 口径：direct=原始文本应一次解析成功；repaired=需兜底（去围栏/控制字符修复/"
            "平衡扫描）救回；failed=不可靠修复，应正确拒绝（不得硬解析出错误数据）。"
        ),
        force=args.force,
    )

    # ------------------------------------------------------------ 2. compliance
    cmp_cases = _compliance_cases()
    group_counts: dict[str, int] = {}
    for item in cmp_cases:
        group_counts[item["group"]] = group_counts.get(item["group"], 0) + 1
    _freeze(
        "compliance",
        {"cases.json": cmp_cases},
        answer_source=(
            "人工撰写 128 条文本（正例 64 含自然语境/分隔符/大小写/谐音变体；负例 64 含"
            "明显干净与易误伤语境）；expect_hit 由人工按语义标注，不复制扫描器词表"
        ),
        stats={
            "total": len(cmp_cases),
            "positives": sum(1 for c in cmp_cases if c["expect_hit"]),
            "negatives": sum(1 for c in cmp_cases if not c["expect_hit"]),
            "groups": group_counts,
        },
        notes=(
            "评测口径 = 内置保守词表（73 词）扫描器（scan_note，确定性、离线）；"
            "知识库扩展词表为运行态数据，不纳入本冻结集评测。"
        ),
        force=args.force,
    )

    # ------------------------------------------------------------ 3. normalize
    norm_cases = _normalize_cases()
    cat_counts: dict[str, int] = {}
    none_count = 0
    for item in norm_cases:
        cat_counts[item["category"]] = cat_counts.get(item["category"], 0) + 1
        if item["expected"] is None:
            none_count += 1
    _freeze(
        "normalize",
        {"cases.json": norm_cases},
        answer_source=(
            "人工设计的 12 类数值写法（108 条）+ 按「计数语义」标注 expected；"
            "expected=None 表示应为空值，不依赖 _parse_count 实现"
        ),
        stats={
            "total": len(norm_cases),
            "categories": cat_counts,
            "none_expected": none_count,
            "numeric_expected": len(norm_cases) - none_count,
        },
        notes=(
            "空对空不判命中：expected=None 且实测为 None 计「正确拒绝」，"
            "单独统计、不与数值解析正确率混算。"
        ),
        force=args.force,
    )

    # ------------------------------------------------------------ 4. rag_golden
    corpus, queries = _rag_golden_files()
    multi = sum(1 for _, expected, _ in _RAG_QUERIES if len(expected) > 1)
    hard = sum(1 for _, _, note in _RAG_QUERIES if note.startswith("hard"))
    _freeze(
        "rag_golden",
        {"corpus.json": corpus, "queries.json": queries},
        answer_source=(
            "人工撰写 30 条知识片段（全 synthetic）+ 人工标注 40 条查询的期望命中片段；"
            "片段含「主题相近但答案不同」近邻对（睡眠/运动、水温/研磨、塌陷/脱模 等）"
        ),
        stats={
            "corpus_total": len(_RAG_CORPUS),
            "queries_total": len(_RAG_QUERIES),
            "multi_expected_queries": multi,
            "hard_negative_queries": hard,
            "corpus_source": "synthetic（真实语料待用户提供后可升版本扩充）",
        },
        notes=(
            "评测需本地 bge 依赖（langchain-huggingface / sentence-transformers）与模型缓存；"
            "未就绪时按红线全项 N/A，禁止用假向量出率。"
        ),
        force=args.force,
    )

    print("-" * 66)
    print("四套夹具生成完毕（冻结指纹见各 manifest.json 的 sha256 字段）。")
    return bc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
