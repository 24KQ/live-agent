# Phase 16 V9 ai.vote520.com 渠道单独探针报告

本报告记录对新渠道 `ai.vote520.com` 的单独体检结果(白名单新增 +
资产链重冻结后,与 imagebridge/synapse 同参数)。探针为三层从小到大设计,
真实模型调用(`gpt-5.6-luna` / `reasoning_effort=xhigh`),不写执行账本、
不触碰 campaign digest。所有结果来自 `scripts/probe_phase16_channel.py` 的
JSON 报告文件(5 轮,20260801T061656 ~ 20260801T063511)。

- Probe host: `ai.vote520.com`(对照 `api.imagebridge.top` / `synapse-ai.uk`)
- Model / Effort: `gpt-5.6-luna` / `xhigh`
- 探针 deadline: L1/L2 60s,L3 120s(正式 profile prompt 实测单次 56-73s)

## 结论(一句话)

**vote520 的内容质量合格(schema 全过),但存在 60 秒断连硬墙:推理耗时
超过 ~60s 的请求被上游直接断开,V5 重试一次后仍撞 120s 整体超时 ——
而 luna xhigh 正式 profile 调用耗时经常 56-73s,大部分慢调用必死。
传输层比 imagebridge 更不可预测,不适合做主渠道。**

## 前置:白名单 + 资产链重冻结

`ai.vote520.com` 原不在 `FORMAL_ENDPOINT_HOSTS`(profiles.py:35),V5 adapter
构造器强制校验。新增白名单触发合法 digest 变化,完整重认证:

| 资产 | 新 digest |
|:--|:--|
| qualification policy | `1db584a0` |
| qualification corpus | `22707734`(4 个 jsonl 逐字节不变,仅 manifest.json) |
| V2 official smoke evidence | `2d3623de` |
| V5 controlled-e2e manifest | `73195f0d` |
| coverage source closure | `015e2030` |
| V2 ledger 触发器 manifest_digest / schema contract digest | 同步更新 |

- 迁移 `run_db_migrations.py` 29/29 PASS;unit 61/61 PASS
- commit: `e0b6f55`(白名单)+ `03644a5`(重冻结资产)

## 探针结果

### L1 连通性(3 次最小请求)

| 轮 | 结果 | 延迟 |
|:--|:--|:--|
| vote520(1 轮) | 3/3 成功 | 1.5s–3.6s,均值 2.6s |

L1 全过,延迟是三个渠道中最优的。

### L2 稳定性 + 内容质量(10 次固定 schema 化请求)

| 轮 | 结果 | schema 合格 | 延迟 |
|:--|:--|:--|:--|
| vote520 首轮 | **4/10 成功(前 6 次全部 60s 挂死,后 4 次正常)** | 4/4 | 3.8s–60s |
| vote520 重跑 | 10/10 成功 | 10/10 | 3.9s–8.8s,均值 5.5s |
| imagebridge(对照) | 10/10 | 10/10 | 3.0s–10.0s |
| synapse(对照) | 9/10(1×503) | 9/9 | 4.4s–11.2s |

首轮前 6 次连续 60s 挂死(deadline 精确触发,非错误返回),随后自愈;
重跑全部正常。疑似首波限流/上游排队窗口,恢复后稳定 —— 但首轮 60%
挂死是真实风险窗口。

### L3 真实 corpus case(正式语义,120s deadline)

| 轮 | 结果 | schema 合格 | 延迟 |
|:--|:--|:--|:--|
| vote520 首轮 | 2/3 成功 | 2/2(成功样本) | 9.7s–59.7s |
| vote520 重跑 | 2/3 成功 | 2/2(成功样本) | 12.2s–35.5s |
| imagebridge(对照) | 2/3(1×502) | 2/2 | 13.1s–36.2s |
| synapse(对照) | 4/4 | 4/4 | 7.2s–41.3s |

**两轮失败的都是同一个 case-001 的 ANALYST 阶段,且均为 wall=120s
(第一次尝试 ~59s 被断 + V5 重试第二次拖满 120s 整体超时)** —— 非随机,
是 case-001 推理耗时必然超过 60s 断连点。case-002 两轮全过。

## 关键发现:60s 断连墙

证据链:

1. **L2 首轮**:前 6 次请求全部精确 60s 挂死(deadline 触发,`http_status=None`)
2. **L3 两轮 case-001 ANALYST**:第一次尝试 latency≈59.2s 后失败
   (上游断连,无 HTTP 状态码,归 TRANSPORT_ERROR 类),V5 立即重试,
   第二次从 ~59.2s 起拖满 120s 整体 deadline → DEADLINE_EXCEEDED
   (两轮 wall 均为 120.0s)
3. **L3-2 ANALYST 首轮 59.7s 成功** —— 擦线过墙,距断连点仅 0.3s

结合实测:luna xhigh 正式 profile 单次调用(imagebridge/synapse 上)
56-73s;vote520 上 case-001 的调用稳定 >60s,必然撞墙。**该渠道对
耗时 >60s 的请求存在确定性断连,重试救不回来(120s 内再断)**。

对比 imagebridge:imagebridge 是随机性挂死/502(慢调用 36.2s 可成功,
无 60s 硬墙);vote520 是**确定性 60s 硬墙**(慢调用必死)。后者对正式
campaign(xhigh 慢推理)更致命。

## 决策建议

- **不建议 vote520 作主渠道**:正式 campaign 的 xhigh 调用 56-73s 常态,
  该渠道 >60s 必断,analyst 阶段将系统性失败
- **不建议放在渠道链前端**:60s 墙会让每次慢调用烧掉整个 120s 重试窗口,
  比 imagebridge 的随机 502 更难被 failover 消化
- 渠道链维持现状(synapse 主 / imagebridge 备);vote520 保留白名单
  (零成本,后续若有更快模型如 deepseek-v4-flash 短耗时场景可再评估)
- 若需正式账本证据:等下次合法 digest 变化时,可观察一次慢调用在
  vote520 上的确定性失败,但**不建议为此消耗 campaign 预算**
