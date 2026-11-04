# 本轮真实输出片段（0.6.1）

以下来自 2026-10-29 的真实 API 回答，只节选最终回答，不含思考正文、凭据或用户私有数据。
输入为本次编写的虚构材料。记录 ID 可在 Selark 本地记录中对应查询。
详细结果见 [验收记录](workload-live-verification.md)。这些是测试产物，不是已合入项目的实现。

## 编程：真实错误与后续修正

中转站 `claude-sonnet-4-6` 扩展半开区间合并时产生了以下错误：

```python
uncovered_gap = start - previous_end - 1
```

对 `[(0, 1), (3, 4)]`、`gap=1`，预期不合并，原回答错误地合并。
原请求 `422251be2d2e48f185df3d9adae938ee` 的 703 项检查有 97 项失败。
明确反馈最小失败样例后，新请求 `dc86a8e3a41d4574928d614177b54f45` 返回以下函数，703 项全部通过：

```python
def merge_intervals(intervals, *, gap=0):
    if type(gap) is not int or gap < 0:
        raise ValueError("gap must be an exact nonnegative int")

    if not isinstance(intervals, (list, tuple)):
        raise ValueError("intervals must be a list or tuple")

    ranges = []
    for interval in intervals:
        if not isinstance(interval, (list, tuple)) or len(interval) != 2:
            raise ValueError("each interval must contain exactly two endpoints")

        start, end = interval
        if type(start) is not int or type(end) is not int:
            raise ValueError("endpoints must be exact Python ints")
        if start > end:
            raise ValueError("interval start cannot exceed end")
        if start == end:
            continue

        ranges.append((start, end))

    ranges.sort()

    merged = []
    for start, end in ranges:
        if not merged:
            merged.append((start, end))
            continue

        previous_start, previous_end = merged[-1]
        uncovered_gap = start - previous_end

        if uncovered_gap <= gap:
            if end > previous_end:
                merged[-1] = (previous_start, end)
        else:
            merged.append((start, end))

    return merged
```

## 编程：Kimi 的截断后续写

第一条 Kimi 请求 `b1c999fdbcb34ae6a079a95b4ae6022b` 在 512 个输出 Token 处结束，
其中思考 Token 为 509，未返回代码。手动携带真实思考历史续写后，请求
`bccac08f7969447783a0392c97ac233e` 返回以下函数，81 项用量、未知值、零值与非法值检查通过：

```python
def summarize_usage(rows):
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    sums = dict.fromkeys(fields)
    known = missing = 0
    for row in rows:
        ok = True
        for f in fields:
            v = row.get(f)
            if v is None:
                ok = False
                continue
            if type(v) is not int or v < 0:
                raise ValueError(f)
            sums[f] = v if sums[f] is None else sums[f] + v
        if ok:
            known += 1
        else:
            missing += 1
    return {"known_tokens": sums, "known_usage_requests": known, "missing_usage_requests": missing}
```

## 设计：第三轮真实文本线框

中转站请求 `b675822ad1c54c0fb125d415763265f6`，使用实际前两轮对话及修订后的约束。
以下线框字符串原样节选，仅移入 Markdown 代码块：

```text
+-- Folio 本地研究笔记 ---------------- 离线可用 | 遥测关闭 --+
| [资料库] [编辑器] [设置] [状态]    焦点：Tab / Shift+Tab  --+
+---------------------------------------------------------------+
| 资料库                                                        |
| 搜索本地笔记       [新建集合] [导入文本]                      |
| > 集合 0   3 条笔记   已保存       Enter 打开                   |
|   集合 1   1 条笔记   有未同步修改                              |
| 底部：本地文件归你所有；保留 7 天；同步仅手动                |
+---------------------------------------------------------------+
| 编辑器                                                        |
| 集合 0 / 笔记标题                 [保存] [来源] [安排分析]     |
| +---------------- 编辑区（纯文本） ----------------+          |
| | 本地内容……                                      |          |
| +--------------------------------------------------+          |
| 状态：已保存到本地 | 来源：来源文件、导入时间、哈希     |
| 下方：来源 | 分析队列 | 导出副本                               |
+---------------------------------------------------------------+
| 设置                                                        |
| 存储位置：[用户选择目录]       保留期：[7 天]                  |
| 遥测：[关闭]  同步：[仅手动]  自动付费回退：[关闭]            |
| 模型输入：[仅文本]            [保存设置]                      |
+---------------------------------------------------------------+
| 状态                                                        |
| 任务：处理中 2/4，等待 3                                   |
| 文本分析 #12  处理中                                           |
| 文本分析 #13  等待（位置 1）                                  |
| 消耗：供应商报告 1,240 tokens | 本地估算约 1,100 | 未报告：未知 |
+---------------------------------------------------------------+
```

该回答中的状态处理节选：

| 状态 | 文案 | 用户动作 | 自动重试 |
| --- | --- | --- | --- |
| offline | 离线：本地编辑、保存、来源查看和导出可用；分析需等待联网。未报告消耗：未知。 | 继续编辑或手动同步；如需分析，可保留任务在等待队列。 | False |
| rate_limited | 供应商限流：请求未完成。未报告消耗：未知。 | 手动选择稍后重试、更换明确选择的模型，或取消此任务；不会自动改用付费模型。 | False |
| quota_unknown | 配额未知：供应商未提供可用额度或消耗报告；未知不等于零。 | 手动查看供应商设置、保留当前任务并稍后重试，或取消任务。 | False |
| timeout | 请求超时：本地输入快照已保留，未确认供应商是否处理完成。未报告消耗：未知。 | 手动重试所选任务或取消；重试前可检查重复处理风险。 | False |
| revoked_token | 凭据已撤销：供应商拒绝访问；不会切换到其他账户或自动重试。未报告消耗：未知。 | 打开设置，由用户手动更新或移除凭据，然后重新安排任务。 | False |

## 内容契约失败：未转义的 JSON 引号

MiMo 首轮设计请求 `6cce2183e92e4e85b2209dc839053a8e` 的回答包含这类原始片段：

```text
"sync": "仅手动触发，界面上提供"立即同步"按钮，绝不后台自动上传"
```

HTTP 和用量均正常，但内层引号未转义，严格 JSON 解析失败。
第二轮带具体反馈后得到可解析 JSON。服务文本转发成功不能替代应用层内容校验。
