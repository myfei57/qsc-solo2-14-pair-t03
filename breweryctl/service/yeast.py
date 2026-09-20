"""酵母扩培与代次管理的应用服务：审计包装与代次链查询。"""

from __future__ import annotations

from typing import Any

from ..core.validators import require_number
from ..domain.audit import AuditLog
from ..domain.yeast import YeastCenter


class YeastService:
    """面向控制台的酵母管理用例，领域校验由 :class:`YeastCenter` 完成。"""

    def __init__(self, yeast: YeastCenter, audit: AuditLog) -> None:
        self.yeast = yeast
        self.audit = audit

    def register_propagation(
        self,
        brewery_id: str,
        strain: str,
        propagated_volume_l: float,
        actor: str,
    ) -> dict[str, Any]:
        """登记一罐实验室接种的扩培酵母（第 0 代）。"""

        volume = require_number(
            propagated_volume_l, field="propagated_volume_l", minimum=0.5, maximum=100_000.0
        )
        document = self.yeast.register_propagation(
            brewery_id=brewery_id,
            strain=strain,
            propagated_volume_l=volume,
        )
        self.audit.record(
            str(document["brewery_id"]),
            None,
            actor,
            "yeast.propagation_registered",
            {
                "yeast_batch_id": document["id"],
                "code": document["code"],
                "strain": document["strain"],
                "generation": document["generation"],
                "volume_l": volume,
            },
        )
        return document

    def record_viability(self, yeast_batch_id: str, viability_pct: float, actor: str) -> dict[str, Any]:
        """录入活性检测结果，由领域层自动放行或淘汰。"""

        document = self.yeast.record_viability(yeast_batch_id, viability_pct)
        self.audit.record(
            str(document["brewery_id"]),
            None,
            actor,
            "yeast.viability_checked",
            {
                "yeast_batch_id": yeast_batch_id,
                "code": document.get("code"),
                "viability_pct": document.get("viability_pct"),
                "status": document.get("status"),
            },
        )
        return document

    def harvest(self, pitch_id: str, propagated_volume_l: float, actor: str) -> dict[str, Any]:
        """从一次接种的酵母泥回收扩培下一代。"""

        volume = require_number(
            propagated_volume_l, field="propagated_volume_l", minimum=0.5, maximum=100_000.0
        )
        record = self.yeast.pitches.require(pitch_id, label="接种记录")
        document = self.yeast.harvest(
            parent_yeast_id=str(record["yeast_batch_id"]),
            pitch_id=pitch_id,
            propagated_volume_l=volume,
        )
        self.audit.record(
            str(document["brewery_id"]),
            str(record.get("batch_id")),
            actor,
            "yeast.harvested",
            {
                "yeast_batch_id": document["id"],
                "code": document["code"],
                "parent_yeast_id": document.get("parent_yeast_id"),
                "generation": document["generation"],
                "volume_l": volume,
            },
        )
        return document

    def lineage(self, yeast_batch_id: str) -> dict[str, Any]:
        """追溯酵母代次链。"""

        return self.yeast.lineage(yeast_batch_id)

    def trace_batch(self, batch_id: str) -> dict[str, Any]:
        """查清某个酿造批次用了哪罐酵母及其代次链。"""

        record = self.yeast.pitch_for_batch(batch_id)
        if record is None:
            return {"batch_id": batch_id, "pitched": False}
        yeast = self.yeast.get(str(record["yeast_batch_id"]))
        lineage = self.yeast.lineage(str(record["yeast_batch_id"]))
        return {
            "batch_id": batch_id,
            "pitched": True,
            "record": record,
            "yeast": {
                "id": yeast.get("id"),
                "code": yeast.get("code"),
                "strain": yeast.get("strain"),
                "generation": yeast.get("generation"),
                "source": yeast.get("source"),
                "status": yeast.get("status"),
            },
            "lineage": lineage,
        }
