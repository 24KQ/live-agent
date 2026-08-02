# 让大模型稳定输出符合 Schema 的 JSON：一次真实项目的实战经验分享

> 2026-08-02 · 经验分享文档 · 无需项目源码即可阅读
>
> 本文基于一个真实生产链路的完整收口经验：**13 个实验 campaign、12 次正式运行、
> 267 次真实模型调用、总成本 6.60 CNY（1,894,873 tokens）**。模型演进：
> deepseek-v4-pro（初版，JSON 频繁失败）→ gpt-5.6-luna（内容级失败）→
> **gpt-5.6-terra / 高推理强度（最终 72/72 全部通过 Schema + 语义校验）**。文中
> 所有代码切片均摘录自真实项目实现（标注了原始文件与行号），渠道名已匿名化处理。

---

## 0. 难点定义：为什么「稳定 JSON」是个真问题

大模型做结构化输出时，不稳定来自这几个层面，缺一不可地破坏生产链路：

| 破坏形态 | 典型表现 | 后果 |
|:--|:--|:--|
| 格式破坏 | 输出被 markdown 代码块包裹、带前缀说明、混入推理过程 | `json.loads` 直接抛错 |
| 结构破坏 | 多余字段（幻觉）、缺必填字段、字段类型错误 | 下游解析崩溃或静默失真 |
| 词表漂移 | 枚举值写错（`SWITCH_TO_BACKUP` 写成 `SwitchBackup`） | 领域逻辑无法消费 |
| 语义违规 | 引用了输入里不存在的 ID、数量超限、覆盖不全 | 结构合法但内容错误 |
| 传输层 | 连接失败、超时、限流（429）、服务端 5xx | 调用根本没成功 |

我们的核心方法论是：**先把失败分清楚，再决定每一类失败怎么处理**。整个体系建立
在下面这个三态分类上，后续每一层的设计都围绕它展开：

```
传输层失败  —— 连接/超时/429/5xx    → 可以重试、换渠道
结构层失败  —— JSON 无效/schema 违规 → 终态失败，不重试
内容层失败  —— 结构合法但语义不合格  → 终态失败，不重试
```

为什么「结构层/内容层不重试」是关键决策？因为**重试不会让模型输出变合格**——同样的
输入、同样的模型，下一次大概率输出同样的问题；重试只是在烧钱并污染审计。宁可
run 失败、如实记录，也不重试。这个原则贯穿全文。

---

## 1. 第一层：协议层 —— 打开 JSON Mode，关闭思考模式

### 机制

1. **请求体强制 `response_format: {"type": "json_object"}`**（OpenAI 兼容协议），
   让服务端进入 JSON 输出模式；
2. **关闭推理模式（thinking）**：我们怀疑推理文本混入输出正文是 JSON 被破坏的头号
   嫌疑，最终方案在请求顶层注入 `thinking: {"type": "disabled"}`。

### 代码切片 1（真实请求构造）

```python
payload = {
    "model": request.model_id,
    "messages": [message.model_dump(mode="json") for message in request.messages],
    "temperature": float(request.temperature),
    "max_tokens": request.max_output_tokens,
    "response_format": {"type": "json_object"},   # ← JSON Mode 开关
}
# 推理强度通过环境变量覆写（max / high / medium / low），不设置则按 API 默认
reasoning_effort = os.environ.get("LLM_API_REASONING_EFFORT", "").strip()
if reasoning_effort:
    payload["reasoning_effort"] = reasoning_effort
```

> 摘录自真实项目 `src/specialist_runtime/deepseek_adapter.py:134-145`。

### 关键方法论：先探针证明协议，再上完整系统

我们的「V4 协议探针」值得单独说——它只做一件事：**在最小规模下证明「关闭思考 +
JSON Mode + 严格解析」这条协议链路可行**，通过之后才把完整系统跑起来。

```python
class _ThinkingDisabledTransport:
    """V4 探针专用传输包装器：在请求栈帧内注入 thinking=disabled。"""

    async def post_json(self, url, headers, payload, timeout_seconds):
        payload = {**payload, "thinking": {"type": "disabled"}}
        response = await self._delegate.post_json(url, headers, payload, timeout_seconds)
        # ... 只消费最小字段：choices[0].message.content，其余自由文本丢弃
        return response
```

> 摘录自真实项目 `src/decision_support/v4_json_probe_adapter.py:74-135`（略作简化）。

**迁移要点**：换模型、换平台时，先用一个「最小探针」（一条请求、一个简单对象、
完整解析+校验）验证协议链路可行，再投入完整系统——探针 1 小时能跑完，完整系统
一轮实验的成本可能是它的 20 倍。

---

## 2. 第二层：提示层 —— 信封 + 形状示例 + 负面约束

协议层只保证「输出是 JSON」，不保证「JSON 是我们想要的结构」。提示层把结构约束
给模型。三个要素缺一不可：

1. **最终信封指令**：所有输出统一套信封 `{"kind":"FINAL","final_output":<RESULT>}`，
   解析层只认这个信封，模型无从「自由发挥」外层结构；
2. **形状示例**：给一个完整 JSON 形状示例（占位值），模型模仿形状远比理解 schema
   可靠；
3. **负面约束**：明确禁止常见破坏行为。

### 代码切片 2（提示构造）

```python
prompt_prefix=(
    "You are EvidenceAnalystAgent for a controlled E2E qualification. "
    "你只能分析给定证据，不得提出经营动作、调用 Skill、选择路由或声明权限。 "
    "finding_codes 与完整 EvidenceRef 均由系统管理，禁止输出它们；evidence_ids "
    "只能选择输入证据包内可见的 ID。只输出一个 JSON 对象，不得输出 Markdown、"
    "代码块、前缀或推理过程。无真实数据的形状示例："
    '{"kind":"FINAL","final_output":{"constraint_codes":[],"risk_codes":[],'
    '"explanation":"brief evidence-grounded explanation",'
    '"evidence_ids":["bundle-evidence-id"]}}. '
),
result_schema=_SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA,
```

> 摘录自真实项目 `src/decision_support/controlled_e2e_v5.py:522-531`。

### ⚠️ 我们踩过的坑：示例值必须用占位符

示例里的 `"bundle-evidence-id"`、`"placeholder-text"` 不是随便写的——**示例值如果
用真实数据，等于把答案泄漏给模型**，模型会抄示例值而不是根据输入推理。我们的
修正记录里专门有一条：V6 修正删除「仅含单个 risk_flag 的示例」，因为那个示例把
模型引向必然不满足覆盖规则的输出。**示例只示范形状，不示范答案**。

---

## 3. 第三层：结构层 —— 把 Schema 写「窄」

这一层是系统的第一道门。设计原则：**能表达的约束全写进 JSON Schema**——因为它
校验快、确定性、报错可定位。

### 代码切片 3（JSON Schema 定义）

```python
_SMOKE_V2_CONFLICT_ANALYSIS_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,          # ← 杜绝一切幻觉字段
    "properties": {
        "constraint_codes": ...,            # 受控词表（enum 表达）
        "risk_codes": ...,                  # 受控词表（enum 表达）
        "explanation": ...,                 # 文本长度/字符集边界
        "evidence_ids": {
            "type": "array",
            "minItems": 1,                  # ← 数量下界
            "maxItems": 6,                  # ← 数量上界
            "uniqueItems": True,            # ← 唯一性
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
        },
    },
    "required": ["constraint_codes", "risk_codes", "explanation", "evidence_ids"],
}
```

另一个典型例子——枚举约束 + 字符串模式（同一项目的 Planner 输出）：

```python
"product_strategy": {
    "enum": ["KEEP_CURRENT", "SWITCH_TO_BACKUP", "HOLD_AND_ESCALATE", "REPLY_DANMAKU"]
},
"option_id": {
    "type": "string", "minLength": 1, "maxLength": 80,
    "pattern": "^[a-z0-9][a-z0-9-]*$",      # ← 字符集/格式卡死
},
```

> 摘录自真实项目 `src/decision_support/multi_agent.py:157-231`（略作省略）。

### 设计原则速查

| 约束类型 | 用法 | 拦住什么 |
|:--|:--|:--|
| `additionalProperties: False` | 顶层与嵌套对象 | 幻觉字段（模型多输出即失败） |
| `enum` | 一切可枚举的字段 | 词表漂移（大小写/变体） |
| `minLength`/`maxLength` | 字符串字段 | 超长文本、空字符串 |
| `minItems`/`maxItems`/`uniqueItems` | 数组字段 | 数量越界、重复项 |
| `pattern` | 字符串字段 | 非法字符、格式漂移 |
| `required` | 对象 | 缺必填字段 |

**注意 schema 的边界**：schema 表达不了「evidence_ids 必须引用输入包内已给的 ID」
这类跨字段关系——那是第 5 层的活。把这两层的分工想清楚，校验代码才不会混乱。

---

## 4. 第四层：解析层 —— 防御性恢复 + fail-closed

### 机制

两层策略：先硬解析，失败走**有限的**恢复，恢复仍失败就**如实记失败**，绝不静默
接受。注意「有限」二字——恢复只处理常见形态（markdown 代码块包裹、前缀文本），
不做智能修复；恢复不了就是恢复不了。

### 代码切片 4（解析与失败分类）

```python
try:
    output = json.loads(content)
except (TypeError, json.JSONDecodeError, RecursionError):
    recovered = self._recover_json(content)   # 有限的防御性恢复
    if recovered is not None:
        output = recovered
    else:
        # 诊断：输出失败内容的片段和长度，帮助定位 JSON 模式返回非 JSON 的原因
        snippet = content[:120]
        print(f"  [JSON DECODE FAILED] len={len(content)} snippet={snippet}")
        return self._failure(
            request,
            ModelFailureCategory.INVALID_OUTPUT_JSON,   # ← fail-closed
            ...
        )
```

### 代码切片 5（恢复函数的完整逻辑）

```python
@classmethod
def _recover_json(cls, content: str):
    """尝试从模型返回的原始文本中恢复 JSON 对象。

    DeepSeek JSON mode 仍偶有返回空 content 或 markdown 包裹的情况。
    """
    if not content:
        return None
    # 尝试剥离 markdown 代码块标记（```json / ```）
    for delim in ("```json\n", "```json\r\n", "```\n", "```\r\n"):
        if content.startswith(delim) and content.rstrip().endswith("```"):
            inner = content[len(delim):].rstrip()[:-3]
            try:
                return json.loads(inner)
            except (json.JSONDecodeError, TypeError, RecursionError):
                return None
    # 尝试截取第一个 JSON 对象子串（容忍前缀/后缀自由文本）
    start_idx = content.find("{")
    if start_idx >= 0:
        try:
            return json.loads(content[start_idx:])
        except (json.JSONDecodeError, TypeError, RecursionError):
            pass
    return None
```

> 摘录自真实项目 `src/specialist_runtime/deepseek_adapter.py:249-261` 与 `:318-360`
> （微缩为可读形式，逻辑一致）。

### 为什么这样设计

- 恢复函数的存在是因为「JSON Mode 仍偶有返回 markdown 包裹」——这是**防御性**
  容错，不是纵容；
- 恢复的范围刻意收窄：只剥离代码块、只截取第一个对象。**绝不尝试「修复」坏
  JSON**（补括号、猜字段）——修复出的「合格」JSON 是幻觉，会把错误固化进系统；
- 恢复不了 → `INVALID_OUTPUT_JSON` 终态失败。**让失败可见，好过让错误不可见。**

---

## 5. 第五层：语义层 —— JSON 合法 ≠ 合格

这是最容易漏掉的一层，也是我们交过学费的一层。

### 真实案例

我们的实验历史里，有一轮 run（12 次调用全部完成、**每个 JSON 都能解析**、结构
也合法）却整体 FAILED——因为输出是「内容级失败」：模型输出过短、语义验证挂掉、
引用了输入里不存在的 ID。**Schema 校验全部通过 ≠ 输出合格**。没有语义层，这些
问题会静默进入下游。

### 语义层拦什么（代码切片 6）

```python
def validate_v2_conflict_analysis_result(*, task, result, expected_profile,
                                         expected_evidence_refs, expected_finding_codes):
    # 1) 信封校验：输出必须是系统管理的 FINAL 信封
    output = _validate_system_managed_final_result_envelope(
        task=task, result=result, expected_profile=expected_profile,
        expected_evidence_refs=expected_evidence_refs,
    )
    # 2) 字段集合必须恰好等于预期 —— 不多不少
    if set(output) != {"constraint_codes", "risk_codes", "explanation", "evidence_ids"}:
        raise ValueError("V2 analysis output has unexpected fields")
    # 3) 引用有效性：evidence_ids 必须来自输入证据包
    _validated_v2_evidence_ids(output["evidence_ids"], expected_evidence_refs=...)
    # 4) 词表校验：codes 必须属于受控枚举（转换失败即拒绝）
    constraint_codes = tuple(ConflictConstraintCode(item) for item in output["constraint_codes"])
    risk_codes = tuple(ConflictRiskCode(item) for item in output["risk_codes"])
    # 5) 数量/唯一性/长度硬边界
    if (len(constraint_codes) != len(set(constraint_codes))
            or len(risk_codes) != len(set(risk_codes))
            or len(constraint_codes) > 3 or len(risk_codes) > 8
            or not explanation or len(explanation) > 500):
        raise ValueError("V2 analysis output does not close over governed facts")
    ...
```

> 摘录自真实项目 `src/decision_support/multi_agent.py:603-648`（略作简化，保留全部
> 校验类别）。

### schema 与语义层的分工

| 问题 | 由谁拦 |
|:--|:--|
| 字段类型错误、缺失、多余字段 | JSON Schema（第 3 层） |
| 词表漂移（枚举值写错） | JSON Schema `enum` |
| 引用有效性（ID 必须来自输入包） | 语义层（schema 无法表达） |
| 跨字段覆盖关系（输出必须覆盖输入全部风险码） | 语义层（schema 无法表达） |
| 文本安全（控制字符、前后空白） | 两层配合：schema 预筛 + 领域模型权威检查 |

**迁移要点**：写校验时先问「schema 能不能表达？」，不能的交给语义层；语义层的
校验规则**必须在 prompt 里告知模型**（我们 V6 修正的记录：有一条语义规则「必须
覆盖 analysis 中的全部 risk_codes」一直由代码强制，但 prompt 从未声明，导致模型
反复踩线——把校验规则如实告诉模型，不放松校验，但让模型有机会不犯）。

---

## 6. 第六层：失败策略 —— 只对传输层重试

### 失败分类（收口全链路）

| 类别 | 触发条件 | 策略 | 理由 |
|:--|:--|:--|:--|
| `TRANSPORT_ERROR` | 连接层失败 | **同端点重试 1 次**，再换下一条渠道 | 瞬态问题，重试大概率成功 |
| `HTTP 5xx` | 服务端错误 | 退避 1s 后**同端点重试 1 次** | 服务端瞬态 |
| `RATE_LIMITED (429)` | 限流 | **不重试同端点**，直接换下一条渠道 | 同端点重试必然再次 429 |
| `DEADLINE_EXCEEDED` | 绝对 deadline 耗尽 | **停止**，不再触碰网络 | 继续只会更晚 |
| 结构层/内容层失败 | JSON 无效、schema/语义违规 | **终态失败，永不重试** | 重试不会让输出变合格 |

### 重试决策伪代码

```
for 渠道 in 渠道链:                 # 渠道链按优先级排列，顺序即降级路径
    for attempt in 1..2:             # 每端点最多 2 次
        重建请求: 换 host + 独立 90s 窗口 deadline
        调用模型
        if 成功: return 结果(带 attempts/endpoint 事实)
        if 失败类别 in {TRANSPORT, 5xx, DEADLINE}: continue  # 同端点重试
        if 失败类别 == 429: break                             # 换端点
        else: return 终态失败                                 # 结构/内容失败
    换端前检查: 剩余时间 < 最小重试窗口(1s) 则整体停止
```

### 代码切片 7（真实实现要点）

```python
for delegate, host in self._chain:                      # 渠道链
    for _ in range(self._MAX_ATTEMPTS_PER_ENDPOINT):    # 每端点最多 2 次
        attempts += 1
        # 每次尝试重建请求：换 host + 独立窗口 deadline
        payload = request.model_dump(mode="json")
        payload["endpoint_host"] = host
        payload["deadline_at"] = self._attempt_deadline(request, self._clock).isoformat()
        endpoint_request = ModelRequest.model_validate(payload)
        outcome = await delegate.complete(endpoint_request)
        if isinstance(outcome, ModelSuccess):
            return _stamp_attempt(outcome, attempts=attempts, endpoint_host=host)
        last_outcome = outcome
        if self._retryable(outcome):      # TRANSPORT / DEADLINE / 5xx
            if self._remaining_seconds(request, self._clock) < self._MIN_RETRY_WINDOW_SECONDS:
                break                     # 剩余时间不足最小窗口 → 不再触碰网络
            if outcome.http_status is not None and outcome.http_status >= 500:
                await self._sleep(min(self._RETRY_BACKOFF_SECONDS, ...))  # 退避 1s
            continue
        if outcome.category is ModelFailureCategory.RATE_LIMITED:
            break                         # 429 不重试同端点 → 走渠道链下一端点
        return _stamp_attempt(outcome, attempts=attempts, endpoint_host=host)  # 终态
```

> 摘录自真实项目 `src/decision_support/controlled_e2e_adapter_v5.py:254-317`
> （略作简化）。两个关键常数：`_MAX_ATTEMPTS_PER_ENDPOINT = 2`、
> `_MIN_RETRY_WINDOW_SECONDS = 1.0`。

### 三个容易被抄错的细节

1. **每次尝试独立窗口（90s）**：前面的慢调用不挤压后面渠道的预算——每条渠道都
   获得完整窗口，而不是共享一个总沙漏；
2. **最小重试窗口门**：剩余时间不足 1s 就直接停止，绝不把重试调用发出在 deadline
   边缘（发出的请求要么必失败，要么算超时罚款）；
3. **429 换端不重试**：同端点重试 429 是必然再 429，白白增加 latency。

---

## 7. 评估体系 —— 怎么证明「输出合格」而不是「能解析」

### 7.1 逐例指标校验

每个 case 的输出都要过 7 项指标，每项 12/12 才算 run 通过（我们最终合格 run 的
真实结果）：

```
E2E_MULTI_AGENT_READY                 12/12   # 双 Agent 全链路执行完成
ANALYST_SCHEMA_AND_SEMANTIC_VALID     12/12   # schema + 语义双重校验
PLANNER_RISK_COVERAGE                 12/12   # 输出覆盖输入全部风险点
EXPLANATION_BOUND                     12/12   # 解释长度/内容受控
CONTROLLED_EVIDENCE_BINDING           12/12   # 证据引用全部来自输入包
HARD_SAFETY_CONFORMANCE               12/12   # 安全约束
OPTION_VALIDITY                       12/12   # 候选项合法性
```

**要点**：指标不是「跑完了」而是「每一例都通过独立校验」——逐例统计，不是抽样。
「12/12」的诚实含义是：没有任何一例靠运气通过。

### 7.2 失败归因：把校验失败钉到具体字段

结构校验失败时，我们的实现用 jsonschema 的 `json_path` 把失败钉到具体坐标，再
映射为**确定性失败码**（同一份输出必须映射到同一个码，否则同一失败会因迭代顺序
落成不同的账本事实）：

```python
validator = Draft202012Validator(_plain_json(result_schema))
errors = sorted(validator.iter_errors(_plain_json(final_output)),
                key=lambda item: item.json_path)
# 多处违规时按 json_path 取确定性的第一条
first = errors[0]
return str(first.validator), str(first.json_path)   # 例: ("enum", "$.options[0].product_strategy")
```

> 摘录自真实项目 `src/decision_support/controlled_e2e_v5.py:218-253`（略作简化）。

jsonschema 的 `message` 会内嵌被拒实例值（模型正文），所以**从不读取 message 拼
进账本**——只取 validator 名 + json_path，防正文泄漏进审计。

### 7.3 账本化：所有失败都是终态记录

每个 case 的执行结果（PASS/FAILED + 失败码 + 尝试次数 + 响应端点 + 成本）写入
append-only 账本。四个历史 FAILED run 保留为终态记录、不可重跑——这是防「刷分」
设计：**账本只说发生过什么，不允许「重跑直到成功」**。这对评估类系统是底线。

---

## 8. 成本与预算控制

### 8.1 真实账本（12 次正式运行）

- 总成本：**6.60 CNY**（1,894,873 tokens），单 run 成本 0.45 ~ 0.62 CNY
- 单 run 预算上限：4.00 CNY；每个 stage 调用前先「预约」0.10 CNY
- 成本随规模线性可估：24 次调用/run ≈ 0.5 CNY（gpt-5.6-terra / high）；换更高规格
  组合成本上升但换质量

### 8.2 三个预算机制（都值得迁移）

1. **单 run 预算上限**：跑飞即停，不靠人工盯；
2. **预约-结算（reserve/settle）**：调用前先扣预算，响应回来再按实际 token 结算
   差额——防止并发/崩溃导致预算账目漂移；
3. **成本进账本**：每个 receipt 记录 cost + tokens，与执行结果同表——「贵且失败」
   和「便宜且成功」是可查询的事实，不是印象。

**迁移要点**：评估一个模型的组合时，别只看「成功/失败」，要看**每成功一例花多少
钱**——我们最终选定的组合不是最便宜的，是「每成功例成本」最优的。

---

## 9. 排障流程 —— 遇到不稳定时按什么顺序查

```
遇到输出不稳定
  │
  ├─ 先看失败分类（你的日志/账本必须先有这一列！）
  │    ├─ 结构层失败（JSON 无效 / schema 违规）
  │    │     → 顺序查：协议层（JSON mode 开没开、思考模式关没关）
  │    │              → 提示层（信封/示例/负面约束在不在，示例是不是泄漏答案）
  │    │              → schema 是否写窄（additionalProperties:False 等）
  │    │              → 解析层恢复是否越权（恢复不出就该 fail，不该猜）
  │    ├─ 内容层失败（JSON 合法但语义挂）
  │    │     → 查语义层规则是否在 prompt 里声明过（我们 V6 的真实教训）
  │    │     → 换模型组合（这是内容层的正解，不是结构层的手段）
  │    └─ 传输层失败（超时/429/5xx）
  │          → 查重试参数（次数/窗口/退避/换端策略），与模型质量无关
  │
  └─ 结论：协议/提示/schema 都到位仍不稳 → 换模型组合，而不是继续调 JSON 机制
```

**什么时候换模型**：协议层、提示层、结构层、语义层全部验证过之后仍不稳定——尤其
是**内容层失败占比高**——就该换模型组合（模型名 × 推理强度）。我们的完整演进：

1. `deepseek-v4-pro`（V1–V3 时代，默认配置）→ **全失败**；到 V3 已是字面
   `INVALID_OUTPUT_JSON`（`json.loads` 直接抛错，JSON 无法解析）
2. `deepseek-v4-pro` + 关闭思考模式（V4 协议探针）→ 最小 JSON 协议验证通过
3. `gpt-5.6-luna` / 超高推理强度（V5 时代）→ **内容级失败**：JSON 能解析、结构也
   合法，但输出过短/语义校验挂掉（`INVALID_RESPONSE` + 语义验证失败）
4. **`gpt-5.6-terra` / 高推理强度（最终解）→ 72/72 全部通过**（Schema + 语义
   双重校验逐例通过）

对比 3 和 4 可以看清两件事：换组合解决的是**内容层失败**；而 JSON 格式问题
（步骤 1 → 2）是思考模式关闭 + JSON Mode 解决的（第 1 层）。两者不能混为一谈。

但注意顺序：**换模型是最后一块拼图，不是第一手段**——协议/提示/schema 没做对，
换什么模型都是碰运气。

---

## 10. 最小可运行演示

完整演示一个六层管线如何拦住坏输出。复制即跑（Python 3.10+，需
`pip install jsonschema`）。

````python
"""最小可运行演示：让 LLM 输出稳定符合 schema 的 JSON —— 六层管线。
模拟一个「模型」，它会交替输出五种真实世界里我们见过的问题输出，
跑完整管线后观察每一层拦住了什么。
"""
import json
import re
from jsonschema.validators import Draft202012Validator

# ── 第 3 层：窄 Schema ──────────────────────────────────────────────
RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "category": {"enum": ["SAFE", "WARN", "ESCALATE"]},
        "evidence_ids": {
            "type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "explanation": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["category", "evidence_ids", "explanation"],
}
VALIDATOR = Draft202012Validator(RESULT_SCHEMA)

# ── 第 1/2 层：模拟模型输出（真实世界见过的五种坏输出）─────────────
BAD_OUTPUTS = [
    '```json\n{"category": "SAFE", "evidence_ids": ["e1"], "explanation": "ok"}\n```',
    'Sure! Here is the result:\n{"category": "SAFE", "evidence_ids": ["e1"], "explanation": "ok"}',
    '{"category": "SAFE", "evidence_ids": ["e1"], "explanation": "ok", "extra_field": true}',
    '{"category": "safe", "evidence_ids": ["e1"], "explanation": "ok"}',
    '{"category": "SAFE", "evidence_ids": ["ghost-id"], "explanation": "ok"}',
]
GOOD_OUTPUT = '{"category": "SAFE", "evidence_ids": ["e1"], "explanation": "ok"}'
KNOWN_EVIDENCE_IDS = {"e1", "e2", "e3"}   # 第 5 层语义校验的引用白名单

# ── 第 4 层：解析 + 有限恢复 + fail-closed ──────────────────────────
def parse_output(content: str):
    """硬解析 → 有限恢复（剥代码块/截首对象）→ fail-closed。"""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    for delim in ("```json\n", "```\n"):
        if content.startswith(delim) and content.rstrip().endswith("```"):
            try:
                return json.loads(content[len(delim):].rstrip()[:-3])
            except (json.JSONDecodeError, TypeError):
                return None
    idx = content.find("{")
    if idx >= 0:
        try:
            return json.loads(content[idx:])
        except (json.JSONDecodeError, TypeError):
            return None
    return None

# ── 第 5 层：语义校验 ───────────────────────────────────────────────
def semantic_validate(obj):
    if set(obj) != {"category", "evidence_ids", "explanation"}:
        raise ValueError("字段集合不恰等")
    if not set(obj["evidence_ids"]) <= KNOWN_EVIDENCE_IDS:
        raise ValueError(f"引用未知 ID: {set(obj['evidence_ids']) - KNOWN_EVIDENCE_IDS}")
    return obj

# ── 第 6 层：失败分类 ───────────────────────────────────────────────
def classify(failure):
    # 传输层失败（超时/429/5xx）在此返回 retryable=True；结构/内容层一律 False。
    # 演示里模拟模型只产生结构/内容层失败 —— 它们是终态，不重试。
    return False

# ── 管线 ────────────────────────────────────────────────────────────
def run_pipeline(content: str):
    print(f"\n模型输出: {content!r}")
    obj = parse_output(content)
    if obj is None:
        print("  ✗ 结构层: JSON 无效且恢复失败 → INVALID_OUTPUT_JSON（终态，不重试）")
        return False
    errors = sorted(VALIDATOR.iter_errors(obj), key=lambda e: e.json_path)
    if errors:
        e = errors[0]
        print(f"  ✗ 结构层: schema 违规 @ {e.json_path} ({e.validator}) → 终态失败")
        return False
    try:
        semantic_validate(obj)
    except ValueError as err:
        print(f"  ✗ 内容层: 语义违规 —— {err} → 终态失败")
        return False
    print("  ✓ 全部通过（结构 + 语义）")
    return True

if __name__ == "__main__":
    print("=== 六层管线演示：5 种坏输出 + 1 种好输出 ===")
    for bad in BAD_OUTPUTS:
        run_pipeline(bad)
    run_pipeline(GOOD_OUTPUT)
````

预期输出（读者可自行验证）：

````
=== 六层管线演示：5 种坏输出 + 1 种好输出 ===
模型输出: '```json\n{"category": "SAFE", ...}\n```'
  ✓ 全部通过（结构 + 语义）          ← markdown 包裹被恢复层处理
模型输出: 'Sure! Here is the result:\n{...}'
  ✓ 全部通过（结构 + 语义）          ← 前缀文本被截取恢复处理
模型输出: '{... "extra_field": true}'
  ✗ 结构层: schema 违规 @ $ (additionalProperties) → 终态失败
模型输出: '{"category": "safe", ...}'
  ✗ 结构层: schema 违规 @ $.category (enum) → 终态失败
模型输出: '{"category": "SAFE", "evidence_ids": ["ghost-id"], ...}'
  ✗ 内容层: 语义违规 —— 引用未知 ID: {'ghost-id'} → 终态失败
模型输出: '{"category": "SAFE", "evidence_ids": ["e1"], "explanation": "ok"}'
  ✓ 全部通过（结构 + 语义）
````

接真实 API 的改动只有一处——把 `BAD_OUTPUTS`/`GOOD_OUTPUT` 换成真实响应，并在
请求体加上 `response_format: {"type": "json_object"}`（见第 1 层代码切片）。

---

## 11. 迁移检查清单（10 条）

1. 请求里开 JSON Mode；平台支持则关闭思考模式，先跑「最小探针」证明协议可行
2. 提示三件套：信封指令 + **占位符**形状示例 + 负面约束（禁 markdown/前缀/推理）
3. Schema 写窄：`additionalProperties: False`、enum、长度/数量/唯一性边界
4. 解析 fail-closed：有限防御性恢复（剥代码块、截首对象）+ 恢复不了诚实记失败
5. 独立语义校验层：词表、引用有效性、跨字段关系、文本安全
6. 失败分类三态：传输可重试、结构/内容终态不重试；429 换端不重试
7. 示例值永远用占位符，不泄漏真实答案
8. 每次输出记录验证结果（哪层挂的、挂在哪个字段 json_path），可审计、可归因
9. 出问题先判断「结构失败」还是「内容失败」——处理方式完全不同（见第 9 节）
10. 以上全做好仍不稳 → 换模型组合，而不是放松校验

## 12. 诚实边界（哪些我们还没证明）

- **429 / 5xx / deadline 路径只被单元测试覆盖**，未被真实 provider 事件触发过；
  如果读者环境真实触发，请以实测为准（我们的账本有 attempt_count 列可事后验证）；
- 恢复函数只覆盖 markdown 包裹、前缀文本这两种形态——**不是万能恢复器**；
- 我们的验证规模是 267 次调用、72/72 合格；这个样本量能证明「该组合可行」，
  **不能证明「永远不会失败」**——所以 fail-closed + 账本才是最终保障：失败可见、
  可归因、可换组合；
- 「换模型组合」在我们体系里是白名单内的声明组合切换（模型名 × 推理强度），
  不是无边界试错——**试错要有预算上限和账本**，否则是烧钱赌博。

---

*本文基于真实项目的完整收口经验撰写；渠道域名已匿名化，成本数字与模型名保留
原样。所有代码切片可在真实项目源码中按标注定位（项目内部可用）。*
