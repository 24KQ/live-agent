# LiveAgent v1.0 踩坑与修复复盘（Lessons Learned）

## 文档说明

- **目的**：沉淀 LiveAgent Phase 17 holdout 工程执行中真实发生的踩坑、根因、修复与验证，作为面试方法论素材与完结报告「评估纪律」章节的展开支撑。
- **范围**：Phase 17 holdout 执行链（执行契约、校验器、预算账本、真实模型 run、分层核验）。
- **更新约定**：本文档随 Phase 17 进展**增量更新**——每条踩坑标注状态（已闭环 / 修复中 / 观察中）；batch1 重跑验证、batch2 执行完成后更新对应条目；最终版随 `liveagent-v1.0-completion.md` 一起定稿。
- **状态图例**：✅ 已闭环（根因确认 + 修复验证完成）；🔧 修复中（已批准方案，验证未完成）；👀 观察中（机制已建立，效果由后续 run 累积证据）。
- **编码**：UTF-8 / LF / 无 BOM。

---

## 踩坑 1：测试自洽 ≠ 系统正确（batch1 首次 run 0/10 事件）✅

### 现象

Phase 17 batch1 首次真实模型 run：10 例中 **9 例 ANALYST_VALIDATION_FAILED + 1 例 danmu-001 transport failure = 0/10**。而此前全量测试 **integration 43/43 全绿**、unit 全绿——测试与真实执行严重背离。

### 根因（逐层剥离）

1. 冻结 prompt/schema 教模型输出 `{"kind":"FINAL","final_output":{constraint_codes, risk_codes, explanation, evidence_ids}}` envelope；
2. 但 `Phase17HoldoutCampaignRunner._structure_valid` 是 **v2 同构手写校验器**（期望顶层 `trigger_codes` + `analysis`），与冻结协议错位；
3. integration 测试的 fake 输出同样沿用 v2 结构——**fake 数据与校验器同源，双双与真实协议错位**，测试自洽却系统性错误。

### 修复（commit 6f7c8de）

- `_final_output_mapping()` 单点解包（校验与投影共用，消除第二份协议）；
- `_structure_valid` 重写：字段级校验（codes 允许空数组匹配冻结 schema 无 minItems、explanation 非空、evidence_ids 非空）；
- PLANNER projection 只投影 `final_output`，不再嵌套外层 envelope；
- 新增一致性测试：**从冻结 prompt 提取形状示例**，做 prompt↔schema↔validator 三点闭合验证。

### 验证

- 9/9 真实 batch1 artifact 过新校验器全部 PASS——决定性证据，且**未放低任何字段要求**；
- 用户批准新 contract digest 后 batch1 重跑 **10/10 PASS**（0.317055 CNY）。

### 教训

对冻结契约的校验器必须**从冻结协议本身提取形状**（prompt 示例 / schema）做闭合验证；凭记忆手写校验器 = 制造第二份协议。integration 全绿只证明「fake 与校验器自洽」，不证明「真实输出与协议一致」。

### 面试应答

被问「测试全绿为什么还翻车」：*测试自洽 ≠ 系统正确——fake 数据与校验器同源，双双与冻结协议错位；修复后用真实模型 artifact 过校验器作为决定性证据，且逐字段核对未放宽任何要求。*

---

## 踩坑 2：LLM 指令遵守是概率性的——不可谈判约束必须系统强制 ✅

### 现象

batch1 重跑 10/10 PASS（结构层全过），但第 3 层 artifact 逐例审查发现：inventory-001 的 `evidence_ids = ["bundle-evidence-id"]`——**prompt 形状示例占位符被模型照抄进输出**。而 prompt 明确写着「evidence_ids 只能选择输入证据包中可见的 ID」。

实际遵守率：10 例中 9 例输入含真实 ID（SYN-P-XXXX），其中 8 例正确绑定、1 例抄袭；唯一无 ID 可引的 postlive-001 抄了输入首行。

### 根因

LLM 指令遵守是**概率性行为**（本批约 9/10）；模型在聚焦满足 JSON 格式时，可能从上下文最近示例复制 token（few-shot 格式仿写），与内容语义绑定解耦。这是当前主流模型的已知行为特征，不是单点能力问题。

### 修复（用户已批准「生产级完整修复」，d569371 已落地）

1. prompt 占位符改为明显元形态（消除「示例太像真 ID」的诱导）；
2. 数据集为复盘类 case 补证据 ID 体系（消除「无 ID 可引」的困境，5 个 postlive 输入用户终审通过，dataset digest `28b1499f` 冻结）；
3. **runner 增加 evidence 引用有效性校验**（`evidence_ids ⊆ 输入中可见 ID 集合`，fail-closed）——把「模型必须记住规则」升级为「系统强制执行规则」。

### 验证

batch1 重跑 10/10 PASS（0.327462 CNY）；第 3 层重审新增 evidence 引用有效性检查项：独立校验 20/20 attempt 的 evidence_ids 均为输入可见 SYN-P-XXXX 集合子集，占位符抄袭零复现——软约束→系统强制闭环 ✅。

### 教训

对不可谈判的验收指标（如证据绑定真实性），**概率性遵守不成立**——LLM 指令是培训手册，确定性代码校验才是外键约束。生产级系统不指望模型「更聪明」，而是把约束从指令层下沉到系统层。

### 面试应答

被问「为什么不换更强的模型解决」：*这不是模型能力问题，是系统设计问题——对 100% 要求的约束，任何模型的概率性遵守都不成立；把约束变成确定性校验（fail-closed）才是生产级答案。*

---

## 踩坑 3：few-shot 占位符设计——占位符不能长得像真实数据 ✅

### 现象

冻结 prompt 形状示例用 `"bundle-evidence-id"` 作为 evidence_ids 占位符，与真实 ID（如 `SYN-P-4205`）形态无区别——连字符命名、名词短语同构。模型在 inventory-001 输出中把占位符原样抄进 `evidence_ids`（位置：`src/decision_support/phase16_qualification_candidate.py` `_ANALYST_PROMPT_PREFIX` / `_PLANNER_PROMPT_PREFIX`）。

### 根因

占位符「太像真数据」：模型无法从形态上区分「示例占位符」与「可用 ID」，格式仿写时把最近示例的 token 复制进输出。

### 修复（d569371 已落地）

占位符改为明显元形态（`<evidence-id-from-input>`）——照抄即产生格式错误，模型复制概率骤降。

### 教训

prompt 示例中的占位符必须是**不可能与真实数据同构**的元形态；示例文本会被模型当作可复制的表层模式，设计时按「最坏情况会被照抄」来写。

---

## 踩坑 4：数据集设计缺口——每种 case 类型都要有证据 ID 体系 ✅

### 现象

postlive-001（直播复盘类）输入**没有商品级 ID**（只有「某款商品在 18:34 出现讲解」），模型无 ID 可引，把输入首行文本当作 `evidence_id` 输出。

### 根因

数据集为弹幕/库存/售罄/改价/替补类 case 设计了 `SYN-P-XXXX` 商品 ID 体系，**复盘类 case 没有对应 ID 体系**。prompt 要求「只能选择输入中可见的 ID」，但输入中不存在 ID——模型陷入「必须填 ID 却无 ID 可用」的困境。

### 修复（d569371 已落地，用户终审通过）

为 5 个 postlive case 输入补充证据 ID 体系（`SYN-P-XXXX`，30 例 50 个 ID 全无冲突）；dataset digest `28b1499f` 冻结，等待 batch1 重跑验证。

### 教训

prompt 施加的约束（引用可见 ID），**输入必须真的提供**；数据集设计要为每种 case 类型规划证据标识，而不是默认「模型自己知道引什么」。

---

## 踩坑 5：预算跨池口径——预算数字必须带池身份 ✅

### 现象

campaign 按 `contract_digest` 唯一（防刷分设计）→ **预算事件不跨池结转**。修复/重冻结产生新 contract digest = 新预算池：旧池 6cbb9029（settled=1.0、available=7.395869）、新池 75ac54c8（实际 available=8.395869）。批准与重跑申请时引用旧池数字（7.395869），与实际新池（8.395869）错位——codex fail-closed 暂停执行，抓出该缺口。

### 根因

预算池与 contract_digest 绑定，跨池数字混用导致口径分裂；「可用余额」没有池身份时是歧义数字。

### 修复（口径裁决）

契约字段 `forward_budget_remaining_cny` 是**唯一权威**；跨池汇总（总盘 15 CNY = phase16 历史 6.604131 + 旧池 1.0 + 新池花费）由完结报告核对。总盘恒不超支。

### 教训

任何预算/余额数字必须指明池归属；多池并存时，引用数字前先确认「说的是哪个池」。

---

## 踩坑 6：fail-closed registry——批准前全红是预期，不是故障 ✅

### 现象

registry（`phase17_approved_digest.py`）未同步新 contract digest 时，全量测试系统性变红：`phase17 execution contract digest is not the approved registry digest`（ValueError）；用户批准更新 registry 后**确定性转绿**。

### 根因

设计使然：loader fail-closed（digest 与 registry 不一致 → 拒绝加载），防止未批准的契约被执行。

### 价值（双证机制）

「批准前红、批准后绿」的确定性差异本身是可独立验证的机制：独立重跑全量 gate，逐一核验每个失败均为同一 ValueError，即可证明执行者自报数字无水分——**机制正确性可被第三方复现**。

### 教训

fail-closed 拒绝是评估框架的信任基础设施，不是缺陷；批准前后的确定性颜色反转是「执行无水分」的客观证据。

---

## 踩坑 7：分层核验机制——降级的是重复劳动，不是独立视角 ✅

### 现象

三轮独立全量重跑（unit 1739 / integration 295）与 codex 自报逐项吻合（唯一差异全部归因于 registry 未批准的同一 ValueError）——全量重跑边际价值递减；但 0/10 事件证明「测试自洽 ≠ 系统正确」，独立视角不能撤。

### 机制（用户认可）

1. **全量 gate 独立重跑**（unit+integration）：只在契约批准前后、checkpoint 验收两个关键节点执行（降频）；
2. **针对性独立核验**（diff 审读 + digest 复算 + DB 核对 + 关键数字）：每个 checkpoint 必做（保持）；
3. **真实 run artifact 逐例审查**：每次真实模型 run 后必做，**永不降级**——唯一 codex 无法自证的环节。

### 价值实证

本次证据引用瑕疵（踩坑 2/3/4）正是第 3 层 artifact 审查抓到的——结构层（runner 校验 20/20 PASS）看不见的语义问题。

### 教训

分层核验的关键不是「多少层」，而是**每一层不可替代的视角**：结构校验器保证形状，独立审查保证语义与真实性。

---

## 踩坑 8：append-only 账本纪律——失败终态保留的价值 > 刷分 ✅

### 现象

首轮 batch1 的 danmu-001 为 INCONCLUSIVE（transport failure），无法写入 `safety_reviews`——函数 64-hex 强制 + NOT NULL + 触发器**三重阻止**；历史 run（0/10 BLOCKED、1.0 CNY）在账本中保留、不重跑、不修改。后续新 run 中 danmu-001 正常 PASS（0.033213 CNY，非 transport failure）。

### 处理

不批准 schema 修正（涉 3 个冻结路径文件、需重冻结），以**文档级记录**承载 INCONCLUSIVE 事实；预算口径裁决按「不扩预算、不重冻结」执行。

### 教训

数据库级阻止倒逼诚实：失败与不可判定的事实**如实入账（或如实记录无法入账的原因）**，比伪造干净账本更有评估价值；账本 append-only 是防刷分的最后防线。

---

## 踩坑 9：审查者独立视角——reviewer 只看原始文件与 rubric，不看执行者摘要 ✅

### 现象与机制

Phase 17 角色分工：codex = 执行，Claude = **独立第三方审查**（reviewer 值 `claude-independent-review`），用户 = 唯一批准权威。审查只依据原始文件 + rubric（artifact 文件、账本行、冻结契约），不看执行者自报摘要。

### 价值实证

- 0/10 根因定位（校验器错位）由审查链的独立重跑与逐一核验完成；
- 预算跨池缺口（踩坑 5）由执行侧 fail-closed 暂停、审查侧确认口径错位；
- evidence 引用瑕疵（踩坑 2/3/4）由第 3 层 artifact 逐例审查发现。

### 教训

执行者与审查者信息通道必须刻意隔离（不看摘要、只看原始证据）；「信任但验证」在评估框架中是制度设计，不是人际信任。

## 踩坑 10：预算会计闭合复核——「数字一致」≠「语义自洽」✅

### 现象

第三版契约修正（2026-08-03/04）暴露的审查盲点：两次批准（`75ac54c8` 的 8.395869、`f24c5437` 的 8.078814）时，审查侧复核了「自报值与文件字段值一致」，但**未复核会计语义闭合**——等式 `forward == project − retrospective` 的基数漏扣跨池已结算 1.0（`6cbb9029` 池 BLOCKED run）。执行侧 fail-closed 在真实调用前二次拦截（`BUDGET_ENVELOPE_INCONSISTENT`）。

### 根因

分层核验的「关键数字」检查只核对**数字一致性**（A 处与 B 处相同），没核对**关系式成立**（数字之间的会计语义是否真的闭合）。审查清单里缺「forward 基数是否扣清跨池 settled」这一项。

### 修复（第三版契约，用户批准）

1. 跨池已结算**显式建模**：新增常量 `PHASE17_HOLDOUT_POOL_SETTLED_BEFORE_V3_CNY = 1.317055`（6cbb9029 池 1.0 + 75ac54c8 池 0.317055），直接入式；
2. 正确口径：`forward = 15.000000 − 6.604131 − 1.317055 = 7.078814`（8.395869/8.078814 均为未扣 1.0 的超发声明，作废）；
3. **不变量语义重构**：从「等式快照断言」改为「不等式上限断言」（`forward ≤ project − retrospective − settled`，超限 → `BUDGET_ENVELOPE_EXCEEDED`）——字段精确防篡改交给 contract digest + approved registry（loader 层），admission 只做不超支语义检查。结算后不再需要重冻结契约，**预算从此不成为流程拦截点**。

### 验证

独立复算闭合：`15.000000 − 6.604131 − 1.317055 == 7.078814` ✓；总盘核对 `6.604131 + 1.0 + 0.317055 + 7.078814 == 15.000000` ✓；第三版 contract digest `86a76ff8` 与 dataset digest `28b1499f` 独立 canonical MATCH ✓；全量 gate 全绿 ✓。

### 教训

分层核验的「关键数字」复核必须包含**语义自洽**（关系式真的成立、基数真的扣清），不只是「字段值与自报一致」。预算数字必须指明池归属并显式建模跨池已结算，等式不变量是「每笔结算后都要重冻结」的摩擦源——上限不等式 + digest 链分层才是可维护的预算防线。

### 面试应答

被问「预算口径为什么反复出问题」：*根因是契约字段的会计语义不闭合——等式不变量把跨池已结算金额漏在基数外；修复分两层：把已结算显式建模为常量入式（数学闭合），并把不变量从快照等式改为上限不等式（精确防篡改交给 digest 链）——预算从流程阻塞点变成纯账本约束。*

---

## 面试使用建议

- **主线叙事**：0/10 → 根因定位 → 修复 → 真实 artifact 验证 → 10/10 → 第 3 层发现新瑕疵 → 生产级完整修复决策——这是一条完整的「发现-修复-验证」故事线，比任何单个技术点都有说服力。
- **方法论点**：fail-closed registry、append-only 账本、契约 digest 链、分层核验、软约束→系统强制——五个方法论关键词，每个都有真实事件背书。
- **边界声明**：30 例 holdout 是预声明的工程验收线，不是统计显著性声明；本复盘不构成「模型总体成功率 90%」的声明。
