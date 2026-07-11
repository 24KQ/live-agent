# 文档编码治理规范

> **版本**: v1.0  
> **更新日期**: 2026-07-11  
> **用途**: 防止中文文档再次出现乱码，并提供已有乱码的修复流程。

---

## 1. 写入规则

- 中文文档优先使用 `apply_patch` 修改。
- 不使用 PowerShell heredoc / 管道直接写大段中文。
- 不把终端里已经乱码的内容复制回文件。
- 新增或修改文档统一使用 UTF-8，无 BOM 优先。

## 2. 发现乱码后的处理顺序

1. 先确认是终端显示乱码，还是文件内容真的被写坏。
2. 运行 `python scripts/check_doc_encoding.py` 做只读扫描。
3. 能从 git 历史恢复的，优先从历史版本恢复。
4. 不能可靠恢复的，按当前项目事实重写，不要盲目转码。

## 3. 优先修复范围

1. `docs/project_guidance/`
2. `docs/worklog/`
3. `docs/superpowers/specs/`
4. `docs/superpowers/plans/`

## 4. 验证方式

```powershell
python scripts/check_doc_encoding.py
git diff --check
Get-Content docs/project_guidance/current_project_status_and_agent_roadmap.md -Encoding utf8 | Select-Object -First 20
```

## 5. 后续要求

- 每个阶段结束时补留迹：测试记录、反馈、遗留限制、后续迭代方向。
- 任何新增中文说明都要先检查 UTF-8，再提交。
- 如果扫描脚本报错，先修文档，不要继续堆阶段内容。
