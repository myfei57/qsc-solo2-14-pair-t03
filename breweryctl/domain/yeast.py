"""酵母扩培、活性判定、代次管理与接种追踪。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import (
    ConflictError,
    InterlockError,
    NotFoundError,
    SequenceError,
    ValidationError,
)
from ..core.ids import new_id, yeast_code
from ..core.validators import require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .models import YeastBatch, YeastPitchRecord, YeastSource, YeastStatus

YEAST_BATCHES = "yeast_batches"
YEAST_PITCHES = "yeast_pitches"


class YeastCenter:
    """登记扩培批次、按活性与代次放行接种，并留痕罐批对应关系。"""

    def __init__(
        self,
        store: FileStore,
        settings: Settings,
        clock: Clock,
        alarms: AlarmCenter,
    ) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.batches = store.collection(YEAST_BATCHES)
        self.pitches = store.collection(YEAST_PITCHES)

    # ------------------------------------------------------------------
    # 扩培登记
    # ------------------------------------------------------------------

    def register_propagation(
        self,
        brewery_id: str,
        strain: str,
        propagated_volume_l: float,
        source: str = YeastSource.LAB.value,
        parent_yeast_id: str | None = None,
        harvest_pitch_id: str | None = None,
    ) -> dict[str, Any]:
        """登记一罐扩培中的酵母；实验室接种为第 0 代，回收扩培代次加一。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_strain = require_text(strain, field="strain", max_length=80)
        volume = require_number(
            propagated_volume_l, field="propagated_volume_l", minimum=0.5, maximum=100_000.0
        )
        clean_source = source if source in (item.value for item in YeastSource) else None
        if clean_source is None:
            raise ValidationError(
                "酵母来源取值不合法",
                field="source",
                value=source,
                allowed=[item.value for item in YeastSource],
            )

        generation = 0
        if clean_source == YeastSource.HARVEST.value:
            parent = self._require_batch(require_text(parent_yeast_id, field="parent_yeast_id", max_length=64))
            if parent.get("status") != YeastStatus.CONSUMED.value:
                raise SequenceError(
                    "只能回收已经投用完毕的酵母泥",
                    yeast_batch_id=parent_yeast_id,
                    status=parent.get("status"),
                )
            clean_pitch = require_text(harvest_pitch_id, field="harvest_pitch_id", max_length=64)
            record = self.pitches.get(clean_pitch)
            if record is None or record.get("yeast_batch_id") != parent["id"]:
                raise ValidationError(
                    "回收接种记录与母代酵母不匹配",
                    harvest_pitch_id=clean_pitch,
                    parent_yeast_id=parent["id"],
                )
            generation = int(parent.get("generation", 0)) + 1
            clean_strain = str(parent.get("strain"))
            if generation > self.settings.yeast_max_generation:
                raise InterlockError(
                    "酵母已超过允许的最大代数，禁止继续扩培",
                    yeast_batch_id=parent["id"],
                    generation=generation,
                    max_generation=self.settings.yeast_max_generation,
                )

        sequence = self.batches.count() + 1
        now = format_moment(self.clock.now())
        batch = YeastBatch(
            id=new_id("yst"),
            code=yeast_code(sequence),
            brewery_id=clean_brewery,
            strain=clean_strain,
            generation=generation,
            source=clean_source,
            parent_yeast_id=parent_yeast_id if clean_source == YeastSource.HARVEST.value else None,
            harvest_pitch_id=harvest_pitch_id if clean_source == YeastSource.HARVEST.value else None,
            propagated_volume_l=volume,
            registered_at=now,
            updated_at=now,
        )
        return self.batches.put(batch.id, batch.to_doc())

    def harvest(self, parent_yeast_id: str, pitch_id: str, propagated_volume_l: float) -> dict[str, Any]:
        """从一次接种的母代酵母泥回收再扩培一代。"""

        parent = self._require_batch(parent_yeast_id)
        return self.register_propagation(
            brewery_id=str(parent.get("brewery_id")),
            strain=str(parent.get("strain")),
            propagated_volume_l=propagated_volume_l,
            source=YeastSource.HARVEST.value,
            parent_yeast_id=parent_yeast_id,
            harvest_pitch_id=pitch_id,
        )

    # ------------------------------------------------------------------
    # 活性判定
    # ------------------------------------------------------------------

    def record_viability(self, yeast_batch_id: str, viability_pct: float) -> dict[str, Any]:
        """录入活性检测结果并自动放行或淘汰。"""

        viability = require_number(
            viability_pct, field="viability_pct", minimum=0.0, maximum=100.0
        )

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") == YeastStatus.CONSUMED.value:
                raise SequenceError(
                    "酵母已经投用完毕，不能再录入活性", yeast_batch_id=yeast_batch_id
                )
            now = format_moment(self.clock.now())
            acceptable = viability >= self.settings.yeast_min_viability_pct
            patch: list[tuple[str, Any]] = [
                ("viability_pct", viability),
                ("viability_checked_at", now),
                ("updated_at", now),
            ]
            if acceptable:
                patch.append(("status", YeastStatus.RELEASED.value))
                patch.append(("released_at", now))
                patch.append(("rejected_reason", None))
                patch.append(("rejected_at", None))
            else:
                patch.append(("status", YeastStatus.REJECTED.value))
                patch.append(("rejected_reason", "viability"))
                patch.append(("rejected_at", now))
            return merge_documents(document, patch)

        with self.store.locks.guard(f"yeast:{yeast_batch_id}"):
            document = self.batches.update(yeast_batch_id, mutate)
        if document.get("status") == YeastStatus.REJECTED.value:
            self.alarms.raise_alarm(
                brewery_id=str(document.get("brewery_id")),
                source=f"yeast:{document['id']}",
                severity="warning",
                code="yeast_viability_rejected",
                message=(
                    f"酵母 {document.get('code')} 活性 {viability:g}% 低于 "
                    f"{self.settings.yeast_min_viability_pct:g}%，已淘汰禁止投用"
                ),
                context={
                    "yeast_batch_id": document["id"],
                    "viability_pct": viability,
                    "limit_pct": self.settings.yeast_min_viability_pct,
                },
            )
        return document

    # ------------------------------------------------------------------
    # 接种放行与追踪
    # ------------------------------------------------------------------

    def require_pitchable(self, yeast_batch_id: str) -> dict[str, Any]:
        """接种前校验代次与活性状态，不满足即联锁拦截。"""

        document = self._require_batch(yeast_batch_id)
        generation = int(document.get("generation", 0))
        if generation > self.settings.yeast_max_generation:
            raise InterlockError(
                "酵母已超过允许的最大代数，禁止接种",
                yeast_batch_id=yeast_batch_id,
                generation=generation,
                max_generation=self.settings.yeast_max_generation,
            )
        status = document.get("status")
        if status == YeastStatus.REJECTED.value:
            raise InterlockError(
                "酵母活性检测不合格，禁止接种",
                yeast_batch_id=yeast_batch_id,
                viability_pct=document.get("viability_pct"),
                limit_pct=self.settings.yeast_min_viability_pct,
            )
        if status != YeastStatus.RELEASED.value:
            raise SequenceError(
                "酵母尚未完成活性检测放行，禁止接种",
                yeast_batch_id=yeast_batch_id,
                status=status,
            )
        return document

    def record_pitch(
        self,
        yeast_batch_id: str,
        batch_id: str,
        tank_id: str,
        volume_l: float,
        temp_c: float,
        operator: str,
    ) -> dict[str, Any]:
        """记录哪罐酵母投给了哪个批次，并把该罐酵母标记为已耗尽。"""

        volume = require_number(volume_l, field="volume_l", minimum=0.5, maximum=10_000.0)
        temperature = require_number(temp_c, field="temp_c", minimum=-5.0, maximum=60.0)
        clean_operator = require_text(operator, field="operator", max_length=60)
        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        clean_tank = require_text(tank_id, field="tank_id", max_length=64)

        with self.store.locks.guard(f"yeast:{yeast_batch_id}"):
            yeast = self.require_pitchable(yeast_batch_id)
            duplicate = self.pitches.find(lambda item: item.get("batch_id") == clean_batch)
            if duplicate:
                raise ConflictError(
                    "该批次已经接种过酵母",
                    batch_id=clean_batch,
                    yeast_pitch_id=duplicate[0].get("id"),
                )
            now = format_moment(self.clock.now())
            record = YeastPitchRecord(
                id=new_id("ypit"),
                yeast_batch_id=yeast_batch_id,
                batch_id=clean_batch,
                tank_id=clean_tank,
                generation=int(yeast.get("generation", 0)),
                viability_pct=yeast.get("viability_pct"),
                volume_l=volume,
                temp_c=temperature,
                operator=clean_operator,
                pitched_at=now,
            )
            document = self.pitches.put(record.id, record.to_doc())
            self.batches.update(
                yeast_batch_id,
                lambda current: merge_documents(
                    current,
                    [
                        ("status", YeastStatus.CONSUMED.value),
                        ("consumed_at", now),
                        ("pitched_count", int(current.get("pitched_count", 0)) + 1),
                        ("updated_at", now),
                    ],
                ),
            )
        return document

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, yeast_batch_id: str) -> dict[str, Any]:
        """读取酵母扩培批次。"""

        return self._require_batch(yeast_batch_id)

    def list_batches(
        self,
        brewery_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出酵母扩培批次，可按工厂与状态过滤。"""

        items = self.batches.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        if status:
            items = [item for item in items if item.get("status") == status]
        return sorted(items, key=lambda item: str(item.get("registered_at", "")), reverse=True)

    def pitch_for_batch(self, batch_id: str) -> dict[str, Any] | None:
        """返回某个酿造批次的接种事实。"""

        records = self.pitches.find(lambda item: item.get("batch_id") == batch_id)
        if not records:
            return None
        records.sort(key=lambda item: str(item.get("pitched_at", "")), reverse=True)
        return records[0]

    def pitches_for(self, yeast_batch_id: str) -> list[dict[str, Any]]:
        """返回某罐酵母的全部接种记录（通常只有一条）。"""

        records = self.pitches.find(lambda item: item.get("yeast_batch_id") == yeast_batch_id)
        return sorted(records, key=lambda item: str(item.get("pitched_at", "")))

    def lineage(self, yeast_batch_id: str) -> dict[str, Any]:
        """沿 parent_yeast_id 向上追溯酵母代次链。"""

        chain: list[dict[str, Any]] = []
        current_id: str | None = yeast_batch_id
        seen: set[str] = set()
        while current_id:
            if current_id in seen:
                raise ConflictError("酵母代次链出现环路", yeast_batch_id=current_id)
            seen.add(current_id)
            document = self._require_batch(current_id)
            chain.append(
                {
                    "id": document.get("id"),
                    "code": document.get("code"),
                    "strain": document.get("strain"),
                    "generation": document.get("generation"),
                    "source": document.get("source"),
                    "status": document.get("status"),
                }
            )
            current_id = document.get("parent_yeast_id")
        chain.reverse()
        return {"yeast_batch_id": yeast_batch_id, "depth": len(chain), "chain": chain}

    def summary(self) -> dict[str, Any]:
        """汇总酵母库存与代次风险。"""

        items = self.batches.all()
        counts: dict[str, int] = {}
        for item in items:
            key = str(item.get("status"))
            counts[key] = counts.get(key, 0) + 1
        at_limit = [
            item
            for item in items
            if int(item.get("generation", 0)) >= self.settings.yeast_max_generation
        ]
        return {
            "batches": len(items),
            "by_status": counts,
            "pitches": self.pitches.count(),
            "max_generation": self.settings.yeast_max_generation,
            "min_viability_pct": self.settings.yeast_min_viability_pct,
            "at_generation_limit": len(at_limit),
        }

    def _require_batch(self, yeast_batch_id: str) -> dict[str, Any]:
        document = self.batches.get(yeast_batch_id)
        if document is None:
            raise NotFoundError("酵母扩培批次不存在", yeast_batch_id=yeast_batch_id)
        return document
