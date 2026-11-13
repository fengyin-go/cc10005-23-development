"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

发行包工作区（solution 模块）需要在种子表之外维护自己的状态表（方案包、接收记录、
凭证绑定等），它们不是普通业务表，因此放在 EXTRA_TABLES 里单独初始化。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS

# 发行包工作区专用状态表：键为表名，值为初始行列表
EXTRA_TABLES: dict[str, list[dict[str, Any]]] = {
    "solution_package": [],       # 制作端的方案包（含冻结快照与内容摘要）
    "solution_receipt": [],       # 接收端的接收记录（差异树、映射、接收状态）
    "solution_credential": [],    # 目标环境对迁入方案的凭证绑定
    "solution_job_evidence": [],  # 在途作业保留的原方案证据
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name, rows in EXTRA_TABLES.items():
            self._tables[name] = [dict(row) for row in rows]

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def business_module_names(self) -> list[str]:
        """对外可见的业务模块：排除发行包工作区内部状态表。"""
        return [name for name in self.module_names() if name not in EXTRA_TABLES]

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """生成指定表内自增主键，保证新建记录不与已有 id 撞车。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        # 发行包内部状态表不属于业务模块看板，避免污染运营概览
        hidden = set(EXTRA_TABLES)
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            if name in hidden:
                continue
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
