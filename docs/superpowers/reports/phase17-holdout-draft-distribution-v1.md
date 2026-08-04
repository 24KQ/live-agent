# Phase 17 Holdout 30 例起草分布报告（待用户终审）

> 状态：DRAFT，尚未冻结，不包含 manifest_digest，不写入
> PHASE17_APPROVED_DATASET_MANIFEST_DIGEST，不得用于真实模型调用。
> 所有输入为合成数据；标签位于独立目录，运行器和模型输入路径不可读取标签。

## 数据布局

- 输入目录：evaluation/phase17_holdout/inputs/
- 标签文件：evaluation/phase17_holdout/labels/phase17-holdout-labels-v1.jsonl
- 每个输入文件：phase17-holdout-*.txt，UTF-8、LF、无 BOM。
- 每个标签记录只含身份、场景、评分路由和安全审查元数据；不把标签字段写入输入正文。
- 用户终审前不生成冻结 manifest；用户批准后才计算并登记数据集 digest。

## 场景分布

| scenario_tag | 例数 | batch 1 | batch 2 | hard-safety |
| --- | ---: | ---: | ---: | ---: |
| DANMAKU_CONFLICT | 5 | 2 | 3 | 2 |
| INVENTORY_ALERT | 5 | 2 | 3 | 0 |
| SOLD_OUT | 5 | 2 | 3 | 2 |
| PRICE_CHANGE | 5 | 2 | 3 | 2 |
| SUBSTITUTION | 5 | 1 | 4 | 0 |
| POST_LIVE_REVIEW | 5 | 1 | 4 | 0 |
| 合计 | 30 | 10 | 20 | 6 |

## Hard-safety 对照

| case_id | rubric | batch |
| --- | --- | ---: |
| phase17-holdout-danmu-001 | HS-DANMAKU-01 | 1 |
| phase17-holdout-danmu-002 | HS-DANMAKU-02 | 2 |
| phase17-holdout-soldout-001 | HS-SOLDOUT-01 | 1 |
| phase17-holdout-soldout-002 | HS-SOLDOUT-02 | 2 |
| phase17-holdout-price-001 | HS-PRICE-01 | 1 |
| phase17-holdout-price-002 | HS-PRICE-02 | 2 |

## 起草阶段预检记录

本报告随数据起草提交，预检由 Codex 在用户终审前执行并回填。样例包预检不
替代本次 30 例全量检查。仓库中的
`evaluation/phase16_qualification/development_cases.jsonl` 实际包含 18 条
development 记录，其中 Phase 16 真实高冲突 dev 是其中 12 条；本次交叉检查
使用全部 18 条，因而比仅检查 12 条更严格。

- [x] 30 条输入文件均可解析，标签记录与输入文件一一对应（30/30）。
- [x] case ID 唯一，且与真实 development corpus 的 18 个 case ID 零重叠；
  其中包含用户口径的 12 个高冲突 dev case。
- [x] 语义近重复初筛通过：规范化字符 3-gram Jaccard 与序列相似度的组合
  阈值为 0.80，30 例内部最高 0.435，与既有 dev/样例最高 0.447。
- [x] prompt、源码、文档长片段泄漏扫描通过：扫描 src、scripts、docs、
  evaluation 中既有文本，未发现 28 字符连续片段命中。
- [x] 6 个 hard-safety case 与 rubric ID 一一对应，并按 batch 1/2 各分布 3 例。
- [ ] 完整 30 例经用户终审。
- [ ] 用户批准后才生成 dataset manifest digest 并更新 registry。

上述近重复和泄漏数值是起草阶段的可复验预检，不是数据集冻结证明；完整
语义审阅、用户终审、manifest digest 和 registry 更新仍是后续门槛。
