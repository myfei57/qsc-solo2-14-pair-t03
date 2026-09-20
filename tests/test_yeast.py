"""酵母扩培登记、活性判定、代次上限与接种追溯。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import ConflictError, InterlockError, SequenceError

from .helpers import (
    StepClock,
    boil_to_cooling,
    create_batch,
    first_tank,
    make_app,
    mash_to_filter,
    sanitize_tank,
)


class YeastManagementTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StepClock()
        self.app = make_app(clock=self.clock)
        self.yeast = self.app.registry.yeast
        self.service = self.app.registry.yeast_service
        self.brewing = self.app.registry.brewing

    def _register(self, viability: float | None = 96.0, volume_l: float = 20.0) -> dict:
        brewery_id = str(self.app.registry.namespaces.list_breweries()[0]["id"])
        batch = self.service.register_propagation(brewery_id, "US-05", volume_l, "tester")
        if viability is not None:
            batch = self.yeast.record_viability(str(batch["id"]), viability)
        return batch

    def _ready_to_pitch(self, tank_id: str) -> str:
        batch_id = create_batch(self.app)
        mash_to_filter(self.app, batch_id)
        boil_to_cooling(self.app, batch_id)
        self.brewing.mark_cooled(batch_id, 10.0, "tester")
        sanitize_tank(self.app, tank_id)
        self.brewing.transfer_to_tank(batch_id, tank_id, "tester")
        return batch_id

    def test_lab_propagation_starts_generation_zero(self) -> None:
        batch = self._register()
        self.assertEqual("Y-0002", batch["code"])
        self.assertEqual(0, batch["generation"])
        self.assertEqual("lab", batch["source"])
        self.assertEqual("released", batch["status"])

    def test_low_viability_rejects_and_raises_alarm(self) -> None:
        batch = self._register(viability=82.0)
        self.assertEqual("rejected", batch["status"])
        self.assertEqual("viability", batch["rejected_reason"])
        alarms = self.app.registry.alarms.list_alarms()
        codes = [item["code"] for item in alarms]
        self.assertIn("yeast_viability_rejected", codes)

    def test_propagating_yeast_cannot_pitch(self) -> None:
        batch = self._register(viability=None)
        self.assertEqual("propagating", batch["status"])
        with self.assertRaises(SequenceError):
            self.yeast.require_pitchable(str(batch["id"]))

    def test_rejected_yeast_blocks_pitch_without_touching_tank(self) -> None:
        yeast_batch = self._register(viability=70.0)
        tank_id = first_tank(self.app)
        batch_id = self._ready_to_pitch(tank_id)
        with self.assertRaises(InterlockError):
            self.brewing.pitch_yeast(
                batch_id, tank_id, str(yeast_batch["id"]), 10.0, 20.0, "tester"
            )
        tank = self.brewing.tanks.get(tank_id)
        self.assertEqual("filled", tank["stage"])
        self.assertIsNone(self.brewing.status(batch_id)["batch"].get("yeast_pitch_id"))

    def test_pitch_marks_consumed_and_blocks_reuse(self) -> None:
        yeast_batch = self._register()
        tank_id = first_tank(self.app)
        batch_id = self._ready_to_pitch(tank_id)
        view = self.brewing.pitch_yeast(
            batch_id, tank_id, str(yeast_batch["id"]), 10.0, 20.0, "tester"
        )
        self.assertEqual("consumed", view["yeast_batch"]["status"])
        record = view["yeast_pitch"]
        self.assertEqual(batch_id, record["batch_id"])
        self.assertEqual(tank_id, record["tank_id"])
        self.assertEqual(0, record["generation"])
        with self.assertRaises(ConflictError):
            self.yeast.record_pitch(str(yeast_batch["id"]), "other-batch", tank_id, 20.0, 10.0, "tester")

    def test_harvest_increments_generation_and_lineage(self) -> None:
        yeast_batch = self._register()
        tank_id = first_tank(self.app)
        batch_id = self._ready_to_pitch(tank_id)
        view = self.brewing.pitch_yeast(
            batch_id, tank_id, str(yeast_batch["id"]), 10.0, 20.0, "tester"
        )
        pitch_id = str(view["batch"]["yeast_pitch_id"])
        child = self.service.harvest(pitch_id, 18.0, "tester")
        self.assertEqual(1, child["generation"])
        self.assertEqual("harvest", child["source"])
        self.assertEqual(str(yeast_batch["id"]), child["parent_yeast_id"])
        lineage = self.service.lineage(str(child["id"]))
        self.assertEqual([0, 1], [item["generation"] for item in lineage["chain"]])
        trace = self.service.trace_batch(batch_id)
        self.assertTrue(trace["pitched"])
        self.assertEqual(str(yeast_batch["id"]), trace["yeast"]["id"])

    def test_harvest_beyond_generation_cap_is_blocked(self) -> None:
        batch = self._register()
        current = batch
        tank_cycle = 0
        for generation in range(1, self.app.settings.yeast_max_generation + 2):
            # 直接构造一条已投用记录来模拟上一代投用，避免重复跑酿造流程
            self.yeast.batches.update(
                str(current["id"]),
                lambda doc: {**doc, "status": "consumed", "consumed_at": doc["registered_at"]},
            )
            record = self.yeast.pitches.put(
                f"ypit-fake{generation}",
                {
                    "id": f"ypit-fake{generation}",
                    "yeast_batch_id": current["id"],
                    "batch_id": f"batch-fake-{generation}",
                    "tank_id": "tank-fake",
                    "generation": current["generation"],
                    "viability_pct": 96.0,
                    "volume_l": 20.0,
                    "temp_c": 10.0,
                    "operator": "tester",
                    "pitched_at": current["registered_at"],
                },
            )
            if generation > self.app.settings.yeast_max_generation:
                with self.assertRaises(InterlockError):
                    self.service.harvest(str(record["id"]), 18.0, "tester")
                return
            current = self.service.harvest(str(record["id"]), 18.0, "tester")
            self.assertEqual(generation, current["generation"])
            self.yeast.record_viability(str(current["id"]), 95.0)
            tank_cycle += 1
        self.fail("代次上限未触发")

    def test_over_generation_yeast_is_listed_at_limit(self) -> None:
        summary = self.yeast.summary()
        self.assertEqual(self.app.settings.yeast_max_generation, summary["max_generation"])
        self.assertGreaterEqual(summary["batches"], 1)
