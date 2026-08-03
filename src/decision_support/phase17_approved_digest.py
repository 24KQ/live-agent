"""Phase 17 已批准 contract / dataset manifest digest 注册表（registry）。

本文件**不在** phase17 source closure 中——这是刻意的：approved digest 是
外部批准事实。若它存在于 closure 文件内，更新它会改变文件 digest 又改变
contract digest，形成自指循环（digest 依赖文件内容、文件内容依赖 digest）。

语义：
- 运行时 loader 只接受 ``PHASE17_APPROVED_CONTRACT_DIGEST`` 对应的 manifest；
- 参数变化 → closure 文件变 → 重新冻结 manifest（新 contract digest）→
  更新本注册值并**经用户明确批准**后生效（不可任意重签）；
- 只更新本文件（不重冻结 manifest）→ 旧 manifest 与注册值不匹配 → 拒绝
  加载（fail-closed）；
- ``PHASE17_APPROVED_DATASET_MANIFEST_DIGEST`` 是 30 例 holdout 数据集
  manifest 的批准事实（codex 第十八轮 P0-2：``--manifest`` 不得指向任意
  自洽 manifest）。数据起草完成、用户终审并冻结后才填入真实 digest；
  冻结前保持 ``None``（未批准），任何 manifest 都被拒绝（fail-closed）。
"""

PHASE17_APPROVED_CONTRACT_DIGEST = (
    "462cd590d171bf71d3c94099c7bddee1c85d3aea05da94dfdab91de38d55afd9"
)

#: 冻结的 holdout 数据集 manifest digest；None = 尚未有获批数据集。
PHASE17_APPROVED_DATASET_MANIFEST_DIGEST: str | None = None
