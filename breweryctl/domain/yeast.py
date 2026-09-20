"""酵母扩培批次：登记、活性检测、代次联锁与投用追溯。"""

from __future__ import annotations

from typing import Any

from ..core.clock import Clock, format_moment
from ..core.config import Settings
from ..core.errors import ConflictError, InterlockError, NotFoundError, SequenceError, ValidationError
from ..core.ids import new_id, yeast_code
from ..core.validators import require_choice, require_int, require_number, require_text
from ..persistence.store import FileStore, merge_documents
from .alarms import AlarmCenter
from .models import YeastCulture, YeastCultureStatus, YeastPitch, YeastSource

CULTURES = "yeast_cultures"
PITCHES = "yeast_pitches"

_ASSAY_STATUSES = (YeastCultureStatus.REGISTERED.value, YeastCultureStatus.QUARANTINED.value)


class YeastLibrary:
    """管理扩培酵母批次的活性、代次与去向。"""

    def __init__(self, store: FileStore, settings: Settings, clock: Clock, alarms: AlarmCenter) -> None:
        self.store = store
        self.settings = settings
        self.clock = clock
        self.alarms = alarms
        self.cultures = store.collection(CULTURES)
        self.pitches = store.collection(PITCHES)

    def register_culture(
        self,
        brewery_id: str,
        strain: str,
        source: str,
        volume_l: float,
        operator: str,
        generation: int | None = None,
        parent_id: str | None = None,
        cell_count_m_ml: float | None = None,
    ) -> dict[str, Any]:
        """登记一罐扩培酵母；回收扩培必须挂母罐，代次自动 +1。"""

        clean_brewery = require_text(brewery_id, field="brewery_id", max_length=64)
        clean_strain = require_text(strain, field="strain", max_length=60)
        clean_source = require_choice(
            source, field="source", choices=[item.value for item in YeastSource]
        )
        volume = require_number(volume_l, field="volume_l", minimum=0.5, maximum=50_000.0)
        clean_operator = require_text(operator, field="operator", max_length=60)
        clean_parent = (
            require_text(parent_id, field="parent_id", max_length=64) if parent_id else None
        )
        cell_count = (
            require_number(cell_count_m_ml, field="cell_count_m_ml", minimum=0.1, maximum=100_000.0)
            if cell_count_m_ml is not None
            else None
        )

        parent: dict[str, Any] | None = None
        if clean_source == YeastSource.CROPPED.value:
            if not clean_parent:
                raise ValidationError("回收扩培必须指定母罐酵母", field="parent_id")
            parent = self.require(clean_parent)
            if parent.get("brewery_id") != clean_brewery:
                raise ValidationError("母罐酵母不属于同一酒厂", parent_id=clean_parent)
            resolved_generation = int(parent.get("generation", 0)) + 1
        else:
            resolved_generation = require_int(
                0 if generation is None else generation,
                field="generation",
                minimum=0,
                maximum=99,
            )
            if clean_parent:
                parent = self.require(clean_parent)
                if parent.get("brewery_id") != clean_brewery:
                    raise ValidationError("母罐酵母不属于同一酒厂", parent_id=clean_parent)
        self._require_generation_allowed(resolved_generation, strain=clean_strain)

        index = len([item for item in self.cultures.all() if item.get("brewery_id") == clean_brewery]) + 1
        code = yeast_code(index)
        lineage = list(parent.get("lineage", [])) + [parent["id"]] if parent else []
        now = format_moment(self.clock.now())
        culture = YeastCulture(
            id=new_id("yst"),
            code=code,
            brewery_id=clean_brewery,
            strain=clean_strain,
            source=clean_source,
            generation=resolved_generation,
            volume_l=volume,
            parent_id=parent["id"] if parent else None,
            lineage=lineage,
            cell_count_m_ml=cell_count,
            registered_at=now,
            registered_by=clean_operator,
            updated_at=now,
        )
        document = self.cultures.put(culture.id, culture.to_doc())
        self.store.append_event(
            "yeast.registered",
            {
                "culture_id": document["id"],
                "code": code,
                "strain": clean_strain,
                "generation": resolved_generation,
                "parent_id": document.get("parent_id"),
                "source": clean_source,
            },
        )
        return document

    def assay(
        self,
        culture_id: str,
        viability_pct: float,
        operator: str,
        cell_count_m_ml: float | None = None,
        reject_reason: str | None = None,
    ) -> dict[str, Any]:
        """录入活性检测：达到阈值判合格，否则隔离并告警。"""

        viability = require_number(viability_pct, field="viability_pct", minimum=0.0, maximum=100.0)
        clean_operator = require_text(operator, field="operator", max_length=60)
        cell_count = (
            require_number(cell_count_m_ml, field="cell_count_m_ml", minimum=0.1, maximum=100_000.0)
            if cell_count_m_ml is not None
            else None
        )
        clean_reason = (
            require_text(reject_reason, field="reject_reason", max_length=200)
            if reject_reason
            else None
        )

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") not in _ASSAY_STATUSES:
                raise SequenceError(
                    "酵母批次当前状态不允许活性检测",
                    culture_id=culture_id,
                    status=document.get("status"),
                )
            qualified = viability >= self.settings.yeast_viability_min_pct
            new_status = (
                YeastCultureStatus.QUALIFIED.value
                if qualified
                else YeastCultureStatus.QUARANTINED.value
            )
            reason = None
            if not qualified:
                reason = clean_reason or "活性低于投用阈值"
            now = format_moment(self.clock.now())
            patch = [
                ("status", new_status),
                ("viability_pct", viability),
                ("assay_at", now),
                ("assay_operator", clean_operator),
                ("reject_reason", reason),
                ("updated_at", now),
            ]
            if cell_count is not None:
                patch.append(("cell_count_m_ml", cell_count))
            return merge_documents(document, patch)

        with self.store.locks.guard(f"yeast:{culture_id}"):
            document = self.cultures.update(culture_id, mutate)
        if document["status"] == YeastCultureStatus.QUARANTINED.value:
            self.alarms.raise_alarm(
                brewery_id=str(document.get("brewery_id")),
                source=f"yeast:{culture_id}",
                severity="warning",
                code="yeast_low_viability",
                message=(
                    f"酵母 {document.get('code')}（{document.get('strain')}）活性 "
                    f"{viability:.1f}% 低于阈值，已隔离禁止投用"
                ),
                context={
                    "culture_id": culture_id,
                    "viability_pct": viability,
                    "limit_pct": self.settings.yeast_viability_min_pct,
                },
            )
        return document

    def discard(self, culture_id: str, reason: str, operator: str) -> dict[str, Any]:
        """废弃未投用的酵母批次。"""

        clean_reason = require_text(reason, field="reason", max_length=200)
        clean_operator = require_text(operator, field="operator", max_length=60)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") in (
                YeastCultureStatus.CONSUMED.value,
                YeastCultureStatus.DISCARDED.value,
            ):
                raise ConflictError(
                    "酵母批次已经退出库存，无法废弃",
                    culture_id=culture_id,
                    status=document.get("status"),
                )
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("status", YeastCultureStatus.DISCARDED.value),
                    ("reject_reason", clean_reason),
                    ("updated_at", now),
                    ("discarded_by", clean_operator),
                    ("discarded_at", now),
                ],
            )

        with self.store.locks.guard(f"yeast:{culture_id}"):
            return self.cultures.update(culture_id, mutate)

    def mark_pitched(
        self,
        culture_id: str,
        batch_id: str,
        tank_id: str,
        temp_c: float,
        volume_l: float,
        operator: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """把合格酵母标记为已投用并写投用记录；失败时用 :meth:`cancel_pitch` 回滚。"""

        clean_batch = require_text(batch_id, field="batch_id", max_length=64)
        clean_tank = require_text(tank_id, field="tank_id", max_length=64)
        temperature = require_number(temp_c, field="temp_c", minimum=-5.0, maximum=60.0)
        volume = require_number(volume_l, field="volume_l", minimum=0.1, maximum=50_000.0)
        clean_operator = require_text(operator, field="operator", max_length=60)

        def mutate(document: dict[str, Any]) -> dict[str, Any]:
            self._require_pitchable(document)
            now = format_moment(self.clock.now())
            return merge_documents(
                document,
                [
                    ("status", YeastCultureStatus.CONSUMED.value),
                    ("consumed_batch_id", clean_batch),
                    ("consumed_tank_id", clean_tank),
                    ("consumed_at", now),
                    ("updated_at", now),
                ],
            )

        with self.store.locks.guard(f"yeast:{culture_id}"):
            culture = self.cultures.update(culture_id, mutate)
            now = format_moment(self.clock.now())
            record = YeastPitch(
                id=new_id("yp"),
                culture_id=culture_id,
                batch_id=clean_batch,
                tank_id=clean_tank,
                brewery_id=str(culture["brewery_id"]),
                strain=str(culture["strain"]),
                generation=int(culture["generation"]),
                volume_l=volume,
                temp_c=temperature,
                operator=clean_operator,
                pitched_at=now,
            )
            pitch = self.pitches.put(record.id, record.to_doc())
        self.store.append_event(
            "yeast.pitched",
            {
                "pitch_id": pitch["id"],
                "culture_id": culture_id,
                "batch_id": clean_batch,
                "tank_id": clean_tank,
                "generation": int(culture["generation"]),
            },
        )
        return culture, pitch

    def cancel_pitch(self, pitch_id: str) -> None:
        """投用后续步骤失败时回滚：删除投用记录并把酵母恢复为合格。"""

        pitch = self.pitches.get(pitch_id)
        if pitch is None:
            return
        culture_id = str(pitch["culture_id"])

        def restore(document: dict[str, Any]) -> dict[str, Any]:
            if document.get("status") != YeastCultureStatus.CONSUMED.value:
                return document
            return merge_documents(
                document,
                [
                    ("status", YeastCultureStatus.QUALIFIED.value),
                    ("consumed_batch_id", None),
                    ("consumed_tank_id", None),
                    ("consumed_at", None),
                ],
            )

        with self.store.locks.guard(f"yeast:{culture_id}"):
            self.cultures.update(culture_id, restore)
            self.pitches.delete(pitch_id)

    def require_pitchable(self, culture_id: str, brewery_id: str | None = None) -> dict[str, Any]:
        """预检：酵母必须存在、合格且未超代，返回酵母文档。"""

        culture = self.require(culture_id)
        if brewery_id and culture.get("brewery_id") != brewery_id:
            raise ValidationError("酵母批次不属于该批次所属酒厂", culture_id=culture_id)
        self._require_pitchable(culture)
        return culture

    def get(self, culture_id: str) -> dict[str, Any]:
        """读取酵母批次。"""

        return self.require(culture_id)

    def list_cultures(
        self,
        brewery_id: str | None = None,
        status: str | None = None,
        strain: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出酵母批次，可按酒厂、状态、菌株过滤。"""

        items = self.cultures.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        if status:
            clean_status = require_choice(
                status, field="status", choices=[item.value for item in YeastCultureStatus]
            )
            items = [item for item in items if item.get("status") == clean_status]
        if strain:
            clean_strain = require_text(strain, field="strain", max_length=60)
            items = [item for item in items if item.get("strain") == clean_strain]
        return sorted(items, key=lambda item: str(item.get("code", "")))

    def available(self, brewery_id: str | None = None) -> list[dict[str, Any]]:
        """返回当前可投用的合格酵母。"""

        return [
            item
            for item in self.list_cultures(brewery_id, YeastCultureStatus.QUALIFIED.value)
        ]

    def lineage(self, culture_id: str) -> list[dict[str, Any]]:
        """沿母罐链返回祖先酵母批次，从最早一代开始。"""

        culture = self.require(culture_id)
        chain: list[dict[str, Any]] = []
        for ancestor_id in culture.get("lineage", []):
            ancestor = self.cultures.get(ancestor_id)
            if ancestor is not None:
                chain.append(ancestor)
        chain.append(culture)
        return chain

    def pitches_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """返回某个批次投用了哪些酵母。"""

        items = [item for item in self.pitches.all() if item.get("batch_id") == batch_id]
        return sorted(items, key=lambda item: str(item.get("pitched_at", "")))

    def pitches_for_culture(self, culture_id: str) -> list[dict[str, Any]]:
        """返回某罐酵母的投用去向。"""

        items = [item for item in self.pitches.all() if item.get("culture_id") == culture_id]
        return sorted(items, key=lambda item: str(item.get("pitched_at", "")))

    def summary(self, brewery_id: str | None = None) -> dict[str, Any]:
        """汇总酵母库存与代次占用。"""

        items = self.cultures.all()
        if brewery_id:
            items = [item for item in items if item.get("brewery_id") == brewery_id]
        by_status: dict[str, int] = {}
        generations: list[int] = []
        for item in items:
            key = str(item.get("status"))
            by_status[key] = by_status.get(key, 0) + 1
            generations.append(int(item.get("generation", 0)))
        return {
            "cultures": len(items),
            "by_status": by_status,
            "qualified": by_status.get(YeastCultureStatus.QUALIFIED.value, 0),
            "quarantined": by_status.get(YeastCultureStatus.QUARANTINED.value, 0),
            "max_generation": max(generations, default=0),
            "generation_limit": self.settings.yeast_max_generation,
            "viability_min_pct": self.settings.yeast_viability_min_pct,
            "pitches": len(self.pitches.all()),
        }

    def require(self, culture_id: str) -> dict[str, Any]:
        document = self.cultures.get(culture_id)
        if document is None:
            raise NotFoundError("酵母批次不存在", culture_id=culture_id)
        return document

    def _require_pitchable(self, culture: dict[str, Any]) -> None:
        culture_id = str(culture.get("id"))
        generation = int(culture.get("generation", 0))
        self._require_generation_allowed(generation, code=culture.get("code"))
        status = culture.get("status")
        if status == YeastCultureStatus.CONSUMED.value:
            raise ConflictError(
                "该罐酵母已经投用，不能重复投用",
                culture_id=culture_id,
                code=culture.get("code"),
                consumed_batch_id=culture.get("consumed_batch_id"),
            )
        if status == YeastCultureStatus.QUARANTINED.value:
            raise InterlockError(
                "酵母活性不合格已隔离，禁止投用",
                culture_id=culture_id,
                code=culture.get("code"),
                viability_pct=culture.get("viability_pct"),
                limit_pct=self.settings.yeast_viability_min_pct,
                reason=culture.get("reject_reason"),
            )
        if status != YeastCultureStatus.QUALIFIED.value:
            raise SequenceError(
                "酵母批次尚未检测合格，禁止投用",
                culture_id=culture_id,
                code=culture.get("code"),
                status=status,
            )

    def _require_generation_allowed(self, generation: int, **context: Any) -> None:
        if generation > self.settings.yeast_max_generation:
            raise InterlockError(
                "酵母已超过允许扩培的最大代数，禁止继续使用",
                generation=generation,
                limit=self.settings.yeast_max_generation,
                **context,
            )
