"""pytest 公共测试配置。

本文件显式把仓库根目录加入 `sys.path`，确保无论从 IDE、命令行还是
后续 CI 运行测试，都可以稳定导入 `src` 包。这里不读取 `.env`，避免
测试被开发者本机真实配置影响。
"""

from pathlib import Path
import sys


# Windows 下不同 pytest 启动方式对导入路径处理略有差异；显式注入项目根
# 可以让测试命令保持可复现，不要求开发者提前设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
