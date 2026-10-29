"""Synthetic, non-private application conversations; no network or credentials.

The live API matrix uses both variants, three real turns per conversation.
Kimi personal sessions use variant A, individually reviewed and triggered.
These are acceptance checks, not a benchmark of general model quality.
"""

# ruff: noqa: E501 -- Keep literal acceptance prompts readable as complete sentences.

import json
import re

SYSTEM = (
    "Help with this fictional personal task. Follow the latest user's explicit corrections, "
    "preserve facts and uncertainty, and never invent quotations or sources. "
    "Use the actual conversation history. When JSON is requested return only valid JSON; "
    "when code is requested return one complete Python function in one fenced block. "
    "Be substantive but finish within the output cap; do not restate the whole input."
)
SCENARIOS = ("sentiment", "translation", "coding", "article", "chat", "design")

MAIL_A = """我正在整理一段自己的协作邮件，请帮助理解各句话的语气，而不是推断作者人格。
S01: 谢谢你昨天耐心解释，新的步骤让我终于顺利完成了安装。
S02: 更新后又丢了草稿，我非常失望，也不愿意再尝试一次。
S03: 搜索速度确实快了，不过导出结果还是缺列，暂时不能放心使用。
S04: 我周三提交了表单，附件是版本 2.4 的截图。
S05: 这下可真方便，一次升级就让我多加了两个小时班。
S06: 没有想象中难用，第一次就找到了需要的记录，挺满意的。
S07: 不是不喜欢这个方案，只是我还没看过完整说明，暂不评价。
S08: 有人说“这个工具糟透了”，但我自己的体验一直很稳定，很喜欢。
S09: 就这样吧。
S10: 修复得很好，之前的问题都解决了，感谢及时处理。
S11: 我已经等了四天仍然没有回复，这样的处理令人恼火。
S12: 离线功能很实用，可惜同步冲突时的提示不够清楚。
语气标签约定：positive 明确赞许；negative 明确不满，包括反讽；mixed 同时明确赞许和不满；
neutral 事实陈述或明确暂不评价；unclear 信息不足。标注的是句子作者，不是句中引用他人的态度。
"""
MAIL_B = """我想整理自己的活动交流笔记，理解措辞，不诊断任何人的心理状态。
S01: 志愿者讲解得很清楚，我很愿意下次继续参加。
S02: 报名链接失效三次还没人处理，太让人失望了。
S03: 内容有收获，但迟到半小时才开始，体验一半一半。
S04: 活动在 10 月 12 日下午举行，签到表已经寄出。
S05: 真是高效，取消一个报名居然要填写五张重复表格。
S06: 原先担心看不懂，结果一点也不难，我非常开心。
S07: 我不是说活动差，只是没参加，所以不作评价。
S08: 朋友说“安排太差了”，可我对自己的参与体验很满意。
S09: 嗯，知道了。
S10: 你们细心安排了休息区，考虑得很周到，感谢。
S11: 连续两次没有通知变更，这种做法实在令人愤怒。
S12: 讨论很有趣，只是现场噪声影响了听清内容。
只分析作者态度，不能把引述的别人的态度当成作者态度。
标签定义：positive 明确赞许；negative 明确不满（包括反讽）；mixed 同时明确赞许与不满；
neutral 事实或暂不评价；unclear 信息不足。
"""
LABELS = [
    "positive",
    "negative",
    "mixed",
    "neutral",
    "negative",
    "positive",
    "neutral",
    "positive",
    "unclear",
    "positive",
    "negative",
    "mixed",
]

TRANSLATION = {
    "a": {
        "direction": "English to natural Simplified Chinese",
        "strings": {
            "title": "Scheduled maintenance for {workspace_name}",
            "window": "On 2026-11-03, editing will be paused from 09:30 to 10:15 UTC+8.",
            "backup": "Create a local backup at /data/notes before upgrading to v2.7.0.",
            "safety": "Do not delete local drafts. Retry does not guarantee that a charge will be reversed.",
            "retention": "Unsynced drafts remain on this device for 14 days; administrators cannot read them.",
            "action": "If the connection fails, choose 'Work offline' and contact {support_email}.",
        },
        "terms": "workspace must be translated as 工作区; drafts as 草稿; Work offline as 离线工作",
    },
    "b": {
        "direction": "Simplified Chinese to natural English for a small desktop application",
        "strings": {
            "title": "{workspace_name} 的计划维护",
            "window": "2026-11-03 的 09:30 至 10:15 UTC+8 暂停编辑。",
            "backup": "升级到 v2.7.0 前，请在 /data/notes 创建本地备份。",
            "safety": "不要删除本地草稿。重试不保证已发生的费用会被退还。",
            "retention": "未同步草稿在此设备保留 14 天；管理员无法读取这些内容。",
            "action": "连接失败时，请选择“离线工作”，并联系 {support_email}。",
        },
        "terms": "工作区 must be workspace; 草稿 must be drafts; 离线工作 must be Work offline",
    },
}

LIBRARY = """虚构项目“青禾阅读角”事实档案，用于我的社区通讯练习，不是真实新闻。
试运行发生在 2026-09-06 至 2026-09-20，共两周；有 118 名参与者，登记借阅 286 册次。
匿名意见卡收到 43 张，这不是 43 位不同受访者，也不能由此算出支持率。
现场值班由 12 名志愿者轮换，每周二、四 18:00—20:00 开放，周末不开门。
借阅期 14 天，不收押金；每人同时最多借 2 册。遗失处理仍在讨论，不能写成已经免赔。
阅读角位于虚构的青禾服务站一楼，只有 18 个座位，不接受团体包场。
大字版书目目前只有 27 种；新书建议通过纸质卡提交，不能承诺收到建议就立即购买。
试运行结束后仍由志愿者保管未归还登记，登记只包含化名和借书编号，不公布实名名单。
不保存身份证号码，不收集电话号码；不是一个需要用户注册的线上产品。
财务方面：启动预算尚未公布，未获得长期资助承诺，不能写为“长期免费且永久开放”。
志愿者反馈通道与借书登记分开保存，活动结束 30 天后销毁纸质意见卡。
没有正式评估过阅读能力、心理健康或社区凝聚力，因此不能宣称活动已经改善这些指标。
文章读者是附近居民，语气具体、友好，避免宏大口号。可描述参与流程和现实不足。
编辑要求：不要创造人物、采访引语、捐款数额、领导评价、赞助单位或不存在的报名链接。
可以清楚标明哪些仍待决定：未来开放安排、长期预算、遗失处理方式。
计划补充的工作是整理目录、检查书籍完好程度、汇总匿名意见；不是已完成结果。
"""
SOFTWARE = """虚构的个人工具“纸舟便笺”发布事实档案，仅用于我的写作草稿。
版本 v1.4.0 的试用期为 2026-09-06 至 2026-09-20，共收到 43 张反馈表。
有 118 名参与者下载了软件，新增 286 条笔记；下载人数不等于活跃人数，反馈表不等于独立用户。
工具在 Linux 桌面离线运行，使用本地 SQLite；不提供云同步、不需要账号。
12 名志愿者帮忙检查了导出结果；这不是安全审计，不能称为“绝对安全”或“零漏洞”。
支持 Markdown 导出，一次最多选择 2 个笔记本；大批量导出尚未实现。
回收站保留删除的笔记 14 天；磁盘损坏仍可能导致数据丢失，请自行备份。
文件夹可读权限依赖用户的操作系统设置，没有实现应用层端到端加密。
已知限制：同一数据目录只允许一个实例，不能直接放到共享网络磁盘上。
27 个菜单项支持键盘操作，但屏幕阅读器适配仍需验证，不能宣称已通过无障碍认证。
经用户主动同意可以把脱敏反馈寄给维护者，没有后台遥测，不上传完整笔记。
免费提供此试用版本，未来维护预算尚未公布，也没有承诺永久维护。
未来计划是改善导入错误提示和备份说明；不承诺发布时间，不把计划当成已发布功能。
说明面向非技术读者：讲清可做什么、不能做什么、如何备份和在哪里看到失败原因。
写作不得虚构第三方评价、资助金额、法律合规认证、性能倍数或用户引语。
如果引用数量，保持参与者、笔记、反馈表各自的单位，不将它们混为满意度指标。
"""

DESIGN = {
    "a": """为我设计虚构的“木匣清单”个人物品管理程序。一个人使用，Linux 单机，最多 3000 件物品，
每件有编号、名称、位置、可选备注；需要搜索、CSV 导出、撤销最近一次修改。
SQLite 只由单实例访问，备份必须可恢复；界面可以是 CLI，不需要网页或登录。
必须离线可用，默认无遥测，不能上传完整物品清单。未来可显式接入我自己的模型 API，
首版不调用模型、不自动切换收费来源。项目只保存必要元数据，没有用户行为追踪。
删除记录保留 30 天后清理；使用者关闭程序时必须明确未完成导出的状态。
预算和开发周期未知，不能保证两天完成。模型/API 剩余额度无可靠来源时显示未知。
处理数据库忙、磁盘满、权限拒绝和导出中断；不能重试一个已经不确定完成的付费请求。
首次迭代目标是一个能恢复数据的个人工具，不做分布式集群、多租户、支付或自动整理物品。
""",
    "b": """为我设计虚构的“竹签排练”小组排练日程程序。3 名成员通过同一台 Linux 机器轮流使用，
最多 200 个预约，场地 2 个。每条预约有开始时间、结束时间、场地、化名和可选备注。
首版是本地 CLI，不要网络账号、多租户服务、收款或短信发送。SQLite 单实例，禁止时间重叠。
必须离线可用，默认不收遥测，不能把完整预约发给上游模型。未来如接入模型也要用户主动确认，
首版不调用模型、不自动切换付费来源。已取消预约保留 30 天，化名不是实名认证。
需要日历导出、预览冲突、显式取消和本地备份。导出失败不能显示成功；未知用量不能写成免费。
预算与交付日期未知，不做虚构的效率百分比承诺。必须说明时区和夏令时歧义处理方案。
备份要能在另一个本地目录恢复，不以复制正在变化的 SQLite 主文件当作完整备份。
关注数据库忙、磁盘满、权限拒绝和进程意外退出；只用个人规模能维护的方案。
""",
}


def make(scenario, variant, turn):
    assert scenario in SCENARIOS and variant in ("a", "b") and turn in (1, 2, 3)
    key = (scenario, variant, turn)
    expect = {}
    if scenario == "sentiment":
        prompt = [
            (MAIL_A if variant == "a" else MAIL_B)
            + '请逐句分析，只返回 JSON {"items":[{"id":"S01","label":"positive",'
            '"evidence":"该句的连续原文片段","confidence":0.9}],"note":"简短说明不确定性"}。'
            "必须包含 S01 到 S12，每条 evidence 不超过 30 个汉字。",
            "补充上下文：S09 的作者明确说明自己很不满意，是失望地结束对话；"
            "因此将 S09 改为 negative。S05 确认是反讽，不是赞扬。其余句子无新信息。"
            "请返回所有 12 条最新分析，保持上一轮 JSON 结构，不能把引用的别人的不满当成 S08 作者态度。",
            "再更正：S03 的原作者说，后半句指出的问题是转述旧版报告；自己只评价新版，"
            "新版体验完全满意。按这个明确新上下文把 S03 改为 positive；S09 仍为 negative。"
            "返回最终 12 条 JSON 分析。为 S03/S09 的 evidence 可以引用本轮或上轮补充，"
            "其余仍引用原句；不要从有限句子推断人格。",
        ][turn - 1]
        labels = LABELS.copy()
        if turn >= 2:
            labels[8] = "negative"
        else:
            # A short acknowledgment has no unique neutral/unclear gold label.
            labels[8] = ["neutral", "unclear"]
        if turn == 3:
            labels[2] = "positive"
        expect = {"labels": dict(zip((f"S{i:02}" for i in range(1, 13)), labels, strict=True))}
    elif scenario == "translation":
        spec = TRANSLATION[variant]
        prompt = [
            f"I am translating my own application's notice: {spec['direction']}. "
            f"Terminology: {spec['terms']}. Preserve every placeholder, path, version, date, "
            "time, duration and negation. Do not promise free retries or remote access to drafts. "
            "Return a JSON object with exactly the same six keys and translated string values. "
            + json.dumps(spec["strings"], ensure_ascii=False),
            "Revision from the author: maintenance date is now 2026-11-05 (not 2026-11-03); "
            "retention is now 7 days (not 14). All other times, version, path, placeholders and "
            "security statements stay unchanged. Adopt a warm, calm tone without adding promises. "
            "Return the complete revised six-key translation JSON in the same target language.",
            "Now make the text easy for a first-time user to understand without technical jargon. "
            "Keep the latest corrected date and retention, all six keys, both placeholders, exact "
            "times/timezone/path/version, the warning not to delete local drafts, and the distinction "
            "that a retry does not guarantee a refund. Administrators still cannot read the drafts. "
            "Return the final six-key translation JSON, not a commentary about your changes.",
        ][turn - 1]
        expect = {
            "keys": list(spec["strings"]),
            "date": "2026-11-03" if turn == 1 else "2026-11-05",
            "days": 14 if turn == 1 else 7,
        }
    elif scenario == "coding":
        if variant == "a":
            prompt = [
                "Write a complete Python function paginate(items, page, page_size, *, max_page_size=50). "
                "items must be a list or tuple; page, page_size and max_page_size must be exact int "
                "(reject bool), positive, and page_size <= max_page_size. Raise ValueError for all "
                "invalid inputs. Return a dict with items (always a new list), page, page_size, total, "
                "total_pages. Pages are 1-based. Empty input has total_pages=0. Out-of-range pages "
                "return an empty items list but preserve the requested page. Never mutate input. "
                "No imports or I/O. Provide the function in one Python fence, then explain boundaries.",
                "Extend that function with keyword-only clamp=False. clamp must be exact bool or "
                "raise ValueError. When true and input is nonempty, a page beyond the last page "
                "is clamped to the last page. When true and input is empty, effective page is 1. "
                "Existing type and positive-integer validation still happens before clamping. "
                "When false preserve previous behavior. Return the complete function, no imports.",
                "Keep every contract from both earlier rounds. Add has_next and has_previous to the "
                "result. For nonempty input: has_next = effective_page < total_pages, "
                "has_previous = effective_page > 1 (including out-of-range requests when clamp=false). "
                "For empty input both flags are false. Return the complete implementation, then "
                "three concise boundary examples. Do not mutate items or accept bool as an integer.",
            ][turn - 1]
        else:
            prompt = [
                "Write dedupe_events(events, *, prefer='first'). events must be a list of dicts, each "
                "with a nonempty string id; reject whitespace-only ids but otherwise preserve exact "
                "id text. prefer is exactly 'first' or 'last'. Raise ValueError for invalid inputs. "
                "Deduplicate by exact id. Output groups remain in first occurrence order, even when "
                "prefer='last' chooses later data. Return shallow copies in a new list; never mutate "
                "input. No imports or I/O. One complete Python function in a fence plus a short explanation.",
                "Extend with keyword-only scoped=False, requiring exact bool. If true, each event also "
                "requires a nonempty non-whitespace string caller_id, and the grouping key is "
                "(caller_id, id). If false group by id as before and caller_id is optional. Preserve "
                "first occurrence group order, prefer='first'/'last', shallow-copy and error contracts. "
                "Return the complete function with no imports.",
                "Keep scoped and old behavior. Add prefer='highest_revision'. In this new mode every "
                "event must have revision of exact int (not bool), >=0. Choose the highest revision "
                "per group; ties choose the LAST occurrence. Group order remains first occurrence "
                "order. Other prefer modes need not validate revision. All invalid inputs raise "
                "ValueError. Return the complete function, no imports, plus three short boundary cases.",
            ][turn - 1]
    elif scenario == "article":
        prompt = [
            (LIBRARY if variant == "a" else SOFTWARE)
            + "请写一篇 500—700 汉字的中文介绍，含标题、具体参与/使用步骤、限制与待定事项。"
            "必须保留 118、286、43 的各自单位，明确预算未知；不要添加引语和外部来源。",
            "编辑更正：参与者应为 121 人（原 118 有误），反馈表/意见卡应为 41 张（原 43 有误）；"
            "286 的记录不变，其他事实不变。请重写为 500—700 汉字，语气朴实，减少宣传形容词。"
            "含标题和小标题，不能把样本数量解释成满意率。仍须列出重要限制和待定预算。",
            "基于更正后的事实，改写成给初次读者的 5 问 5 答，约 400—600 汉字，保留标题。"
            "包括如何使用、具体限制、隐私/备份、当前参与数据、未来安排。保留 121 人、286 对应单位、"
            "41 张反馈及预算未知；不再出现旧数字 118 或 43，不创造采访、链接或确定性承诺。",
        ][turn - 1]
        expect = {"numbers": [118, 286, 43] if turn == 1 else [121, 286, 41]}
    elif scenario == "chat":
        topic = "个人阅读和散步计划" if variant == "a" else "学习 Python 的每周计划"
        prompt = [
            f"请作为我的日常助手帮我做{topic}。我是阿岚，称呼我小岚即可。"
            "我只在周二和周四各有 25 分钟，周末不安排。我喜欢安静、室内开始，避免跑步。"
            "预算还没决定，所以不要替我决定消费或推荐收费订阅。我的城市和年龄没有告诉你，"
            "也不要猜测。先给一个可执行的小计划，然后只问一个必要问题。"
            "用 JSON 返回 profile(含 name, days, minutes_per_day, budget, city)、schedule、question。"
            "days 使用 ['周二','周四']，未知 budget 和 city 使用 null，name 使用小岚。",
            "回答你的问题：我只想先坚持两周，不追求速度。另作更正：周二不方便，改为周三；"
            "周四只剩 15 分钟，周三仍 25 分钟。周末和预算保持原约束。请给新的两周小计划，"
            "保留相同 JSON 根字段；profile.minutes_per_day 这次是 {'周三':25,'周四':15}，"
            "profile.days 为 ['周三','周四']。不要复活已取消的周二安排。",
            "今天不新增目标，请检查记忆：我现在叫什么、哪几天多少分钟、预算和城市是否已知？"
            "返回与上一轮相同的 JSON 字段，保持更正后的计划。question 改为 null，因为这轮不追问。"
            "schedule 中不要安排周二或周末，也不要虚构我的城市、年龄或职业。",
        ][turn - 1]
        expect = {"days": ["周二", "周四"] if turn == 1 else ["周三", "周四"]}
    else:
        prompt = [
            DESIGN[variant]
            + "返回有效 JSON，根字段恰好为 constraints, architecture, workflows, failures, acceptance。"
            "constraints 含 offline=true, telemetry=false, paid_fallback=false, retention_days=30, "
            "budget=null；其余字段可为简洁数组。用中文具体说明设计、备份恢复及错误处理，约 600 汉字。",
            "需求变更：删除/取消记录保留期改为 7 天；导出改成先写临时文件、成功后原子替换。"
            "进程意外退出时清理不完整导出，但不能删除已提交数据；数据库繁忙要有界等待并显示原因。"
            "保留其他约束和上一轮 JSON 根字段。不要把自动重试付费请求作为恢复策略，"
            "预算仍未知。描述最小的数据表和恢复步骤，约 600 汉字。",
            "做最终设计评审：保留最新 7 天保留期、离线、无遥测、禁止付费来源自动切换和未知预算。"
            "保留相同根字段；acceptance 必须至少 6 条，其中覆盖磁盘满、数据库锁、退出后恢复、"
            "导出原子替换、敏感信息不外发、实际备份恢复演练。给出可观察结果，"
            "不能仅说“正常工作”。约 600 汉字，无需写整个业务程序。",
        ][turn - 1]
        expect = {"retention_days": 30 if turn == 1 else 7}
    return {
        "scenario": key[0],
        "variant": key[1],
        "turn": key[2],
        "prompt": prompt,
        "max_tokens": 2048,
        "stream": turn == 3 or (variant == "b" and turn == 1),
        "expect": expect,
    }


def evaluate(text, case):
    """Only deterministic claims; semantic nuance is reviewed separately."""
    checks = []
    review = []

    def check(name, passed):
        checks.append({"check": name, "passed": bool(passed)})

    scenario = case["scenario"]
    expect = case["expect"]
    if scenario in ("sentiment", "translation", "chat", "design"):
        try:
            obj = json.loads(text)
            check("strict_json_object", isinstance(obj, dict))
            if not isinstance(obj, dict):
                raise ValueError
        except (ValueError, TypeError):
            return {"status": "fail", "checks": [{"check": "strict_json_object", "passed": False}]}
        try:
            if scenario == "sentiment":
                items = obj["items"]
                actual = {i["id"]: i for i in items}
                check("all_ids_once", len(items) == 12 and set(actual) == set(expect["labels"]))
                for identity, label in expect["labels"].items():
                    check(
                        identity + "_label",
                        actual[identity]["label"]
                        in (label if isinstance(label, list) else [label]),
                    )
                    check(identity + "_confidence", 0 <= actual[identity]["confidence"] <= 1)
                    check(identity + "_evidence", bool(actual[identity]["evidence"]))
                    if not (identity == "S09" and case["turn"] >= 2) and not (
                        identity == "S03" and case["turn"] == 3
                    ):
                        source = MAIL_A if case["variant"] == "a" else MAIL_B
                        sentence = next(
                            line for line in source.splitlines() if line.startswith(identity + ":")
                        )
                        check(identity + "_literal_quote", actual[identity]["evidence"] in sentence)
            elif scenario == "translation":
                check("same_keys", set(obj) == set(expect["keys"]))
                check("string_values", all(isinstance(v, str) for v in obj.values()))
                for token in [
                    "{workspace_name}",
                    "{support_email}",
                    "/data/notes",
                    "v2.7.0",
                    "09:30",
                    "10:15",
                    "UTC+8",
                    expect["date"],
                ]:
                    check("preserve_" + token, token in text)
                check(
                    "correct_retention",
                    bool(
                        re.search(
                            r"\b" + str(expect["days"]) + r"\s*(?:天|days?)", obj["retention"]
                        )
                    ),
                )
                if case["turn"] > 1:
                    check("old_date_removed", "2026-11-03" not in text)
            elif scenario == "chat":
                p = obj["profile"]
                check("name", p["name"] == "小岚")
                check("days", p["days"] == expect["days"])
                check("unknown_budget_and_city", p["budget"] is None and p["city"] is None)
                if case["turn"] > 1:
                    check("revised_minutes", p["minutes_per_day"] == {"周三": 25, "周四": 15})
                if case["turn"] == 3:
                    check("no_extra_question", obj["question"] is None)
            else:
                check(
                    "root_keys",
                    set(obj)
                    == {"constraints", "architecture", "workflows", "failures", "acceptance"},
                )
                p = obj["constraints"]
                for field, value in {
                    "offline": True,
                    "telemetry": False,
                    "paid_fallback": False,
                    "budget": None,
                    "retention_days": expect["retention_days"],
                }.items():
                    if isinstance(p, dict):
                        check(field, type(p[field]) is type(value) and p[field] == value)
                    else:
                        review.append(field)
                if case["turn"] == 3:
                    check("six_acceptance_checks", len(obj["acceptance"]) >= 6)
        except (KeyError, TypeError, ValueError, AttributeError):
            check("required_fields_and_types", False)
    elif scenario == "article":
        for number in expect["numbers"]:
            check(
                "fact_" + str(number), bool(re.search(r"(?<!\d)" + str(number) + r"(?!\d)", text))
            )
        check(
            "budget_uncertainty",
            "预算" in text
            and any(v in text for v in ["未知", "尚未", "未公布", "待定", "没有公布"]),
        )
        check("substantive_length", len(text) >= 350)
        low, high = (400, 600) if case["turn"] == 3 else (500, 700)
        check("requested_hanzi_range", low <= len(re.findall(r"[\u4e00-\u9fff]", text)) <= high)
        if case["turn"] == 3:
            check("old_numbers_removed", not re.search(r"(?<!\d)(118|43)(?!\d)", text))
    elif scenario == "coding":
        check("python_fence", bool(re.search(r"```python\s*\n.*?```", text, re.S)))
    return {
        "status": ("needs_review" if review else "pass")
        if checks and all(c["passed"] for c in checks)
        else "fail",
        "checks": checks,
        "needs_semantic_review": review,
        "scope": "deterministic requirements only; not complete semantic certification",
    }
