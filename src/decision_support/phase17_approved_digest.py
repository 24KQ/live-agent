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

#: 2026-08-04 用户批准的 terra/high Phase 17 执行契约 digest。
#: 该批准只解除契约 registry 的 fail-closed 拒绝；每个真实 batch 仍需单独批准。
PHASE17_APPROVED_CONTRACT_DIGEST = (
    "86a76ff8b81b9dfbe568cd1d03523a3deb3359833d75e1710057c0bbb14306a8"
)

#: 2026-08-04 用户批准的 30 例 holdout 数据集 manifest digest。
#: 该值只证明数据集身份已获批准；每次真实模型 run 仍需用户单独批准。
PHASE17_APPROVED_DATASET_MANIFEST_DIGEST: str | None = (
    "28b1499f403dd3f193cb128ef30621edf75be93cc36595e3312988d203698ef4"
)
