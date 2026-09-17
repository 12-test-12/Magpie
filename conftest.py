r"""让 magpie.* 可导入（仓库根目录加入 sys.path）。

注意: conftest.py 自身就在仓库根目录，因此 parents[0] 才是根目录。
旧实现用 parents[1]（= 盘符根，如 D:\ ）插入 sys.path，属于错误路径。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
