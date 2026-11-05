# SettleTrace · Agent 编排实现 v1

> 对应「提示词 7：Agent 编排开发」。
> 状态：已实现并通过真实模型端到端验证（2026-11-05）。代码在 `project/app/core/agent/`。

---

## 一、框架选型：自建轻量 harness，不引第三方框架

提示词建议"推荐 Harness"。这里的判断是：**不引入 LangGraph / AutoGen / CrewAI，自己写编排外壳**。

| 维度 | 自建（现状） | 引入框架 |
|---|---|---|
| 代码量 | 编排 + 工具 + 账本 约 1000 行 | 框架依赖 + 适配层，净增 300+ 行 |
| 审计要求 | 每一步直接 `INSERT INTO tool_call` | 中间状态在框架的 checkpointer 里，要再写一层落表 |
| 部署 | 一个 venv，无额外服务 | 部分框架要求 Redis/Postgres checkpointer、甚至独立服务 |
| 能力匹配 | 单 agent、线性循环、9 个工具 | 状态图、多 agent、断点续跑——本项目都用不上 |

判断依据是：**框架提供的能力我们不需要，而我们需要的能力（每步可查、可关、可拦）框架不直接给。**

如果将来要做多 agent 协作或长事务人工介入（比如核对暂停三天后继续），再换 LangGraph 也不吃亏：
`execute()` 和 `tool_call` 表可以原样保留，只换编排那一段。

---

## 二、执行循环

```
模型决策 ──▶ 执行工具 ──▶ 结果回填 ──▶ 再决策
   ▲                                        │
   └────────── 最多 5 轮（AGENT_MAX_ROUNDS）┘
```

一轮 = 一次模型决策 + 该轮内所有工具执行。实现见 `loop.py:run_agent()`。

### 怎么把 8 行单据压进 5 轮

初版照直做需要 8 轮（每行取证 + 映射各一轮），根本跑不完。三个措施：

| 措施 | 省下的轮次 | 说明 |
|---|---|---|
| `scan_settlement` 复合技能 | 5 轮 → 1 轮 | 内部按固定顺序跑 读单据→读行→批量取证→批量映射→查重 |
| 扫描结果内联 `evidence_refs` | 再省 1 轮 | 否则模型还得单独查一次凭证才能填 evidence_refs |
| 扫描后隐藏搬运类工具 | 再省 1-2 轮 | 模型"想复查一遍"时无工具可调，而不是调用后被拒（被拒也吃一轮） |

最终实测：**4 轮**完成（1 次 scan + 2 轮登记 6 条问题 + 1 轮出草稿）。

### 五条中止条件

| # | 条件 | 结果状态 |
|---|---|---|
| 1 | 模型不再请求工具 | `ok` |
| 2 | 轮数达上限 | `failed` + `E5001` |
| 3 | 同一工具同参数重复 3 次 | `failed` + `E5005` |
| 4 | 已生成待确认草稿 | `awaiting_confirmation` |
| 5 | 已转人工 | `escalated` |

---

## 三、每轮决策记录到日志表

两张表：

```
agent_run (id, settlement_no, status, rounds, model, error, started_at, finished_at)
tool_call (id, run_id, seq, round, tool, args, ok, error_code, result_chars, ms, via)
```

`round` 与 `via` 是阶段 5 分析的关键：

- `round`：第几轮模型决策。可以算"平均几轮完成""哪一轮最容易出错"
- `via`：`NULL` = 模型直接调用；`scan_settlement` = 复合技能内部调用。
  分开才能算清"skill 内部哪一步最慢"，否则只有一个总耗时

**真实轨迹**（4 轮 8 次顶层调用 + 6 条子调用）：

```
seq round tool                  ok    via               ms
 1   R1  scan_settlement       True  -                2341
 2   R1  get_settlement        True  scan_settlement   812
 3   R1  list_fee_lines        True  scan_settlement  1083
 4   R1  get_evidence          True  scan_settlement  1704
 5   R1  map_service_item      True  scan_settlement  3823
 6   R1  check_duplicates      True  scan_settlement   982
 7   R2  record_finding        True  -                 452
……
13   R4  build_review_draft    True  -                2270
```

阶段 5 可直接跑的三个分析：各工具 P50/P95 耗时、按 `round` 统计收敛轮数、按 `error_code` 统计模型踩坑类型。

---

## 四、建草稿前强制中断 + 人工确认

这是提示词 7 里我最重视的一条，因为它是"三处人工闸口"之外新增的第四处。

### 状态机

```
（模型生成）preview ──人工点「确认草稿」──▶ draft / needs_review
                │
                └─ 未确认时：无任何下游效力，可被新草稿取代
```

- 模型调用 `build_review_draft` 后，库里落的是 `status='preview'`
- **`confirm_draft()` 没有注册成工具**，模型在 Function Calling 清单里看不到它
- 只有 `POST /api/agent/drafts/{id}/confirm` 能触发，也就只有人点按钮才会执行
- 重复确认返回 `E4012`

这不是"用提示词禁止模型确认"，而是让"确认"这个动作根本不在它的可触达范围里。

### 流式预览

草稿一生成，SSE 立刻推 `draft_preview` 事件，携带完整 summary：

```text
event: draft_preview
data: {"draft_id":"…","status":"preview","lines_total":8,"lines_gap":2,
       "findings_total":5,"summary":{"lines":[…],"stats":{…}},
       "note":"草稿已生成，等待确认。确认前它只是预览，不产生任何效力。"}
```

用户看着内容决定点不点确认，而不是先点确认再问"草稿里写了什么"。

### 三个状态都不是"已确认"

`draft` / `needs_review` 的语义是"草稿已被人复核过"，**不等于结算单已确认或已审批**。
`review_draft.status` 的 CHECK 里没有 `confirmed` 这个值，任何代码都写不出来。

---

## 五、三个典型轨迹（真实运行，非设想）

### a. 只问口径：「跨币种重复费用应该怎么处理？」

```
R1  search_corpus("跨币种重复费用怎么处理")
R2  search_corpus("跨币种重复 只提示 不删除 保留原行")
→ 文字回答（带 3 处引用定位）
```

工具序列：**只碰检索，不碰任何单据工具**。2 轮，约 12 秒。
对应报销模板里的"出差住宿标准是多少"——只查制度，不建单。

### b. 整单核对：「帮我核对 JS-2026-0318」

```
R1  scan_settlement(JS-2026-0318)          → 8 行 / 7 条证据 / 1 组疑似重复
R2  record_finding ×4（duplicate×2、missing_evidence、unmapped）
R3  record_finding ×2（line7 unmapped、line8 unmapped）
R4  build_review_draft                     → preview 草稿
→ SSE draft_preview → 人工点「确认草稿」→ needs_review
```

4 轮 13 次调用（含 6 条子调用），约 50 秒。
对应模板里的"帮我报上个月的差旅"——但本项目的终点是**待确认草稿**，不是报销单。

### c. 针对具体行：「第 5 行和第 6 行是不是重复了？」

```
R1  scan_settlement(JS-2026-0318)
R2  search_corpus("跨币种重复 判定口径")
R3  record_finding（duplicate，把结论留痕）
R4  → 文字结论
```

关键输出：**"是，系统标记为高置信度疑似重复，但这是提示，不是认定的重复"**，
并列出三条判定信号（名称归一后相同、业务日期差 0 天、折算后金额差 2.6%）。

提问模式下 `build_review_draft` 从工具清单里移除，所以它不会顺手把整单核完。
对应模板里的"这个发票能报吗"——给结论，不建单。

---

## 六、踩到的坑（都已在代码注释里写明原因）

| 现象 | 根因 | 处理 |
|---|---|---|
| 模型 scan 后又把单步工具重跑一遍，5 轮全耗在搬数据 | 提示词管不住"重查一遍"的冲动 | 扫描后把 5 个搬运类工具从**工具清单**里移除（不是调用后拒绝，拒绝也吃一轮） |
| `build_review_draft` 总被闸口拦，提示"尚未查重" | `scan_settlement` 内部直接调 `_check_duplicates()`，它返回纯数据没有 `ok` 字段，被当成失败 | 内部调用统一补 `{"ok": True, **data}`；`_settlement` / `_list_fee_lines` 同病 |
| run 收尾报 `E5001 + CheckViolationError` | `agent_run.status` 的 CHECK 没有 `awaiting_confirmation` | 迁移里补该状态 |
| 用户问"两行算重复吗"，模型把整单核完还出草稿 | 提问被当成任务 | 提问模式下把 `build_review_draft` 永久移出工具清单 + 提示词明确"这是提问不是核对" |

---

## 七、已知边界

1. **5 轮是硬上限**：行数超过约 20 行的单据，登记问题会挤掉出草稿的轮次。生产建议按行数分批（每批 10 行）或把轮次上限做成按单据规模动态调整。
2. **提问模式与核对模式共用一份系统提示词**：靠工具可见性区分，而不是两套提示词。好处是不用维护两份，代价是提示词里"整单核对"的段落对提问场景略有干扰（实测可接受）。
3. **没有断点续跑**：run 结束即结束，中途停了要重跑。真要做"暂停三天后继续确认"，`preview` 状态已经留好了位置，缺的是把 messages 持久化。
4. **`tool_call` 只增不减**：没有清理策略，按月归档需要在运维侧补。

---

## 八、轨迹接入后台审计页

`/admin/audit` 从单一视角改成两个标签，`#runs` 可直接链接：

| 标签 | 看什么 | 回答的问题 |
|---|---|---|
| 对话记录 | 会话 / 问答 / 引用 / 人工标记 | 用户问了什么，系统怎么答的 |
| **智能体运行** | 运行列表 / 工具轨迹 / 工具耗时概览 | 模型调了什么，卡在哪一步，贵在哪 |

**运行列表**：开始时间、结算单、状态、轮数、调用次数（含失败数）、耗时、模型。
支持按单据号、状态、日期筛选，可导出 CSV（一行 = 一次工具调用，带 `round`/`via`/耗时/错误码，UTF-8 BOM）。

**轨迹详情**：按 `seq` 展开，`round` 列只在同轮首行显示（分组视觉），
`via` 区分"模型直接调用"与"scan 内部调用"，参数只留前 80 字符。

**工具耗时概览**（最近 7 天）：按工具聚合调用次数、平均/最慢耗时、失败数、来源分布。
这一块直接就是"阶段 5 分析"的第一张图——实测数据里立刻能看出两件事：

```
scan_settlement   10 次  均 8799ms  最慢 13631ms   ← 一轮跑完五个步骤，必然最慢
map_service_item  21 次  均 2863ms  最慢  5631ms   ← 字面匹配要遍历全部服务项
record_finding    74 次  均  442ms  失败 26        ← 失败多是模型传参不合规（被闸口拦）
```

`scan_settlement` 占了单次核对一半以上的时间。要提速度，先优化它内部的服务项映射，
而不是去调模型参数——这是这张表的直接用处。

---

## 九、证据引用自动补齐（降低模型出错面）

**问题**：`record_finding` 原来强制要求模型手填 `evidence_refs`（voucher + page + field）。
历史失败统计显示，`record_finding` 的 28 次失败里有 **15 次与 refs 有关**：
`E5003 evidence_refs 不能为空` 12 次、`E5002 缺少 voucher/page` 3 次。
模型并不是不愿意填，而是"从扫描结果里把 UUID 和页码对应回正确的那一行"这件事本身容易错。

**改法**：把 refs 变成可选，系统按三级回退自动补齐。

| 级别 | 条件 | 用的定位 | 返回的 `evidence_refs_source` |
|---|---|---|---|
| 1 | 模型显式传了 | 模型的 | `model` |
| 2 | 该行有凭证 | 凭证文件 + 页码 + 字段 | `auto_evidence` |
| 3 | 该行没有凭证（缺证据的典型情形） | 结算单文件 + 第 1 页 + `kind: 费用名` | `auto_settlement` |
| — | 连单据都没读过 | 拒绝，`E5003` | `none` |

第 3 级是关键取舍：一行完全没有凭证时，"这行在结算单里存在但没有凭证"这个**事实本身是有来源的**，
来源就是结算单文件。用结算单文件定位并不违反"无来源定位视为无效"，
反而比让模型编一个凭证名更诚实。

**硬约束没有被削弱**：门槛从"模型没填"变成"系统也拿不到"，
没扫过单就直接登记依然被拒（实测 `E5003`）。

**效果**（同一条演示单据，各跑一次）：

| | 改前 | 改后 |
|---|---|---|
| 调用序列 | scan → findings×6 → build | 同（少了一次补查凭证的往返） |
| record_finding 失败 | 多为 refs 类（E5003/E5002） | **refs 类为 0** |
| 剩余失败 | — | 仅模型偶发空参数调用（`{}` → `E5002`，模型自行重试即通过） |

顺带把空参数调用的报错从 `E5001 工具执行异常` 改成 `E5002 note 不能为空`——
模型看到后者才知道该补什么，看到前者只会换个工具再试。
