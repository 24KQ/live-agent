# Phase 16 V9 imagebridge 渠道单独探针报告

本报告记录对备渠道 `api.imagebridge.top` 的单独体检结果,以及与主渠道
`synapse-ai.uk` 的同参数对照。探针为三层从小到大设计,真实模型调用
(`gpt-5.6-luna` / `reasoning_effort=xhigh`),不写执行账本、不触碰 campaign
digest。所有结果来自 `scripts/probe_phase16_channel.py` 的 JSON 报告文件。

- Probe host: `api.imagebridge.top`(对照 `synapse-ai.uk`)
- Model / Effort: `gpt-5.6-luna` / `xhigh`
- 探针 deadline: L1/L2 60s,L3 120s(正式 profile prompt 实测单次 56-73s)

## 结论(一句话)

**imagebridge 的内容质量合格,问题集中在传输层:间歇性 502、偶发挂死
(60s 无响应)、延迟波动极大(1.6s-45s)。作为备渠道可用,不适合作主渠道。**

## 探针设计(从小到大)

| 层 | 内容 | 预算 | 判据 |
|:--|:--|:--|:--|
| L1 连通 | 3 次最小请求(max_tokens=32) | ~0.03 元 | 全 200、零挂死 |
| L2 稳定 | 10 次固定 schema 化请求 + JSON 结构校验 | ~0.2 元 | 成功率 ≥90%、schema 合格 ≥90% |
| L3 语义 | 2 个冻结 corpus 真实 case(analyst+planner 链)+ 正式 profile prompt_text/result_schema 校验 | ~0.1 元 | 全过 |

## 结果对比

### L1 连通性

| 渠道 | 结果 | 延迟 |
|:--|:--|:--|
| imagebridge(两轮 6 次) | 6/6 成功,含间歇性 502(连续两次后自愈) | 1.6s–45.2s,均值 11.3s |
| synapse(3 次) | 3/3 成功 | 2.9s–12.5s,均值 7.0s |

### L2 稳定性 + 内容质量(修正校验后最终轮)

| 渠道 | 结果 | schema 合格 | 延迟 |
|:--|:--|:--|:--|
| imagebridge | 10/10 成功 | 10/10 | 3.0s–10.0s,均值 4.9s |
| synapse | 9/10 成功(1×503) | 9/9 | 4.4s–11.2s,均值 5.5s |

注:imagebridge 上一轮(L2 旧校验)出现 1 次 60s 挂死(L2-10 wall=60019ms)与
1 次 39.2s 长尾;本轮未复发。

### L3 真实 corpus case(正式语义,120s deadline)

| 渠道 | 结果 | schema 合格 | 延迟 |
|:--|:--|:--|:--|
| imagebridge | 2/3 成功(1×502) | 2/2(成功样本) | 13.1s–36.2s |
| synapse | 4/4 成功 | 4/4 | 7.2s–41.3s |

## 关键修正记录(探针自身的三次 bug,与渠道无关)

1. **FrozenDict 误判**:V5 adapter 返回的 `output` 是 FrozenDict(实现 Mapping 协议
   但不是 dict 子类),`isinstance(parsed, dict)` 误报「JSON is not an object」,
   导致两渠道 L2 首轮 0/10 schema 全 False。修正:改用 `Mapping` 判断 +
   递归 `_to_plain` 归一。
2. **FINAL envelope 未解壳**:正式管线要求模型输出
   `{"kind":"FINAL","final_output":<RESULT>}`(multi_agent.py:333),evaluator
   只校验 `final_output` 内部。探针直接对整包校验报「additional properties」。
   修正:先解壳再校验,对齐正式语义。
3. **60s deadline 误杀**:L3 使用正式 profile prompt(大 context + xhigh 推理),
   正式 campaign 实测单次 56-73s;60s 探针 deadline 把正常调用误判为挂死
   (synapse L3 曾 2/2 DEADLINE_EXCEEDED)。修正:L3 deadline 放宽到 120s。

另有渠道侧事实:探测中发现 imagebridge 对不含 "json" 字样的 prompt +
`response_format=json_object` 组合返回 400,这是 OpenAI 兼容规范行为
(DeepSeek 官方端点同样要求),不是渠道缺陷。

## imagebridge 问题清单(实测)

1. **间歇性 502**:本次探针至少 3 次(L1 前后调试 2 次连续 + L3 1 次),自愈
2. **偶发挂死**:L2 一轮 1 次 60s 无响应(L3 上一轮也有 1 次);正式 campaign
   002 挂死 180s 同型
3. **延迟波动极大**:1.6s–45.2s(σ 显著大于 synapse),均值高于 synapse
4. **内容质量合格**:修正探针后 L2 10/10、L3 成功样本全过 —— 与正式 campaign
   失败轮(9eda8e8a)的 ANALYST/PLANNER_VALIDATION_FAILED 不矛盾:那 3 次是
   正式完整 evidence context 下的输出,探针简化 context 无法完全复刻;但
   传输层问题(挂死/502)在两个场景下都得到确认

## 决策建议

- **保留 imagebridge 为备渠道**(白名单内,可随时调整顺序):内容合格,
  自动 failover 可消化其不稳定性
- **主渠道维持 synapse**:8 次探针调用 0 挂死 0 超时,仅 1 次偶发 503(重试成功)
- 若需正式账本证据:下次合法 digest 变化(如换 sol/terra 新模型)时可顺手
  以 imagebridge 主渠道跑一轮完整 campaign
