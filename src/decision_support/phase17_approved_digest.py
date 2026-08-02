"""Phase 17 已批准 contract digest 注册表（registry）。

本文件**不在** phase17 source closure 中——这是刻意的：approved digest 是
外部批准事实。若它存在于 closure 文件内，更新它会改变文件 digest 又改变
contract digest，形成自指循环（digest 依赖文件内容、文件内容依赖 digest）。

语义：
- 运行时 loader 只接受 ``PHASE17_APPROVED_CONTRACT_DIGEST`` 对应的 manifest；
- 参数变化 → closure 文件变 → 重新冻结 manifest（新 contract digest）→
  更新本注册值并**经用户明确批准**后生效（不可任意重签）；
- 只更新本文件（不重冻结 manifest）→ 旧 manifest 与注册值不匹配 → 拒绝
  加载（fail-closed）。
"""

PHASE17_APPROVED_CONTRACT_DIGEST = (
    "c2dc8b022607c80f069318eb0f6a69732647f1a27ed9c24ec5b734b381e19af4"
)
