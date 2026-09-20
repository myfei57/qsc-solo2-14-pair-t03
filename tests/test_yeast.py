"""酵母扩培：登记、活性判定、代次联锁与投用追溯。"""

from __future__ import annotations

import unittest

from breweryctl.core.errors import ConflictError, InterlockError, SequenceError, ValidationError

from .helpers import (
    StepClock,
    boil_to_cooling,
    create_batch,
    first_tank,
    make_app,
    mash_to_filter,
    sanitize_tank,
)


def brewery_id(app) -> str:
    return str(app.registry.namespaces.seed_default()["id"])


def prepare_filled_tank(app) -> tuple[str, str]:
    """跑完糖化煮沸降温，转罐后返回 (batch_id, tank_id)。"""

    batch_id = create_batch(app)
    mash_to_filter(app, batch_id)
    boil_to_cooling(app, batch_id)
    brewing = app.registry.brewing
    brewing.mark_cooled(batch_id, 10.0, "tester")
    tank_id = first_tank(app)
    sanitize_tank(app, tank_id)
    brewing.transfer_to_tank(batch_id, tank_id, "tester")
    return batch_id, tank_id


def register_qualified(app, *, generation=0, viability=95.0, source="propagated", parent=None):
    library = app.registry.yeast
    culture = library.register_culture(
        brewery_id(app),
        "WLP001",
        source=source,
        volume_l=100.0,
        operator="tester",
        generation=generation,
        parent_id=parent,
    )
    return library.assay(str(culture["id"]), viability, "tester")


class YeastLibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(clock=StepClock(), yeast_max_generation=2)

    def tearDown(self) -> None:
        self.app.close()

    def test_register_and_qualified_assay(self) -> None:
        library = self.app.registry.yeast
        culture = library.register_culture(
            brewery_id(self.app), "WLP001", source="propagated", volume_l=100.0, operator="tester"
        )
        self.assertEqual("YST-001", culture["code"])
        self.assertEqual(0, culture["generation"])
        self.assertEqual("registered", culture["status"])

        tested = library.assay(str(culture["id"]), 96.5, "tester", cell_count_m_ml=150.0)
        self.assertEqual("qualified", tested["status"])
        self.assertEqual(96.5, tested["viability_pct"])
        self.assertEqual(150.0, tested["cell_count_m_ml"])

    def test_low_viability_quarantines_and_alarms(self) -> None:
        library = self.app.registry.yeast
        culture = register_qualified(self.app, viability=82.0)
        self.assertEqual("quarantined", culture["status"])
        self.assertEqual("活性低于投用阈值", culture["reject_reason"])
        low_viability = [
            item
            for item in self.app.registry.alarms.list_alarms()
            if item.get("code") == "yeast_low_viability"
        ]
        self.assertEqual(1, len(low_viability))
        with self.assertRaises(InterlockError) as caught:
            library.require_pitchable(str(culture["id"]))
        self.assertEqual("interlock_blocked", caught.exception.code)

    def test_unregistered_assay_cannot_pitch(self) -> None:
        library = self.app.registry.yeast
        culture = library.register_culture(
            brewery_id(self.app), "WLP001", source="pitched", volume_l=100.0, operator="tester"
        )
        with self.assertRaises(SequenceError):
            library.mark_pitched(str(culture["id"]), "batch-x", "tank-x", 10.0, 90.0, "tester")

    def test_cropped_generation_increments_with_lineage(self) -> None:
        library = self.app.registry.yeast
        first = register_qualified(self.app, generation=0)
        cropped = library.register_culture(
            brewery_id(self.app),
            "WLP001",
            source="cropped",
            volume_l=90.0,
            operator="tester",
            parent_id=str(first["id"]),
        )
        self.assertEqual(1, cropped["generation"])
        self.assertEqual([first["id"]], cropped["lineage"])
        chain = library.lineage(str(cropped["id"]))
        self.assertEqual([first["id"], cropped["id"]], [item["id"] for item in chain])

    def test_generation_limit_blocks_registration_and_pitch(self) -> None:
        library = self.app.registry.yeast
        with self.assertRaises(InterlockError):
            library.register_culture(
                brewery_id(self.app),
                "WLP001",
                source="propagated",
                volume_l=100.0,
                operator="tester",
                generation=3,
            )
        # 先在放宽后的代数上限下登记一代合格酵母
        culture = library.register_culture(
            brewery_id(self.app),
            "WLP001",
            source="propagated",
            volume_l=100.0,
            operator="tester",
            generation=2,
        )
        data_dir = self.app.settings.data_dir
        self.app.close()
        self.app = make_app(clock=StepClock(), yeast_max_generation=1, data_dir=data_dir)
        qualified = self.app.registry.yeast.assay(str(culture["id"]), 99.0, "tester")
        self.assertEqual("qualified", qualified["status"])
        # 投用时仍要被第二道联锁拦住
        with self.assertRaises(InterlockError):
            self.app.registry.yeast.require_pitchable(str(culture["id"]))

    def test_cropped_requires_parent(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.registry.yeast.register_culture(
                brewery_id(self.app),
                "WLP001",
                source="cropped",
                volume_l=100.0,
                operator="tester",
            )

    def test_discarded_culture_cannot_pitch(self) -> None:
        library = self.app.registry.yeast
        culture = register_qualified(self.app)
        library.discard(str(culture["id"]), "染菌废弃", "tester")
        with self.assertRaises(ConflictError):
            library.mark_pitched(str(culture["id"]), "b", "t", 10.0, 90.0, "tester")


class YeastPitchFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = make_app(clock=StepClock(), yeast_max_generation=3)

    def tearDown(self) -> None:
        self.app.close()

    def test_pitch_records_traceability(self) -> None:
        batch_id, tank_id = prepare_filled_tank(self.app)
        culture = register_qualified(self.app, generation=1, viability=97.0)
        view = self.app.registry.brewing.pitch_yeast(
            batch_id, tank_id, 10.0, 90.0, "tester", culture_id=str(culture["id"])
        )
        pitches = view["yeast_pitches"]
        self.assertEqual(1, len(pitches))
        self.assertEqual(culture["id"], pitches[0]["culture_id"])
        self.assertEqual(batch_id, pitches[0]["batch_id"])
        self.assertEqual(tank_id, pitches[0]["tank_id"])
        self.assertEqual(1, pitches[0]["generation"])

        consumed = self.app.registry.yeast.get(str(culture["id"]))
        self.assertEqual("consumed", consumed["status"])
        self.assertEqual(batch_id, consumed["consumed_batch_id"])
        tank = self.app.registry.tanks.get(tank_id)
        self.assertEqual(culture["id"], tank["yeast_culture_id"])
        self.assertEqual("WLP001", tank["yeast_strain"])
        batch = view["batch"]
        self.assertEqual(culture["id"], batch["yeast_culture_id"])
        # 同一罐不能投第二个批次
        with self.assertRaises(ConflictError):
            self.app.registry.yeast.require_pitchable(str(culture["id"]))

    def test_pitch_without_culture_is_rejected(self) -> None:
        batch_id, tank_id = prepare_filled_tank(self.app)
        with self.assertRaises(ValidationError):
            self.app.registry.brewing.pitch_yeast(
                batch_id, tank_id, 10.0, 90.0, "tester", culture_id=None
            )

    def test_failed_pitch_rolls_culture_back(self) -> None:
        batch_id, tank_id = prepare_filled_tank(self.app)
        culture = register_qualified(self.app)
        # 把罐推进到已接种状态，再尝试投另一批：罐状态机必然失败
        self.app.registry.brewing.pitch_yeast(
            batch_id, tank_id, 10.0, 90.0, "tester", culture_id=str(culture["id"])
        )
        second = register_qualified(self.app)
        with self.assertRaises(ConflictError):
            self.app.registry.brewing.pitch_yeast(
                batch_id, tank_id, 10.0, 90.0, "tester", culture_id=str(second["id"])
            )
        restored = self.app.registry.yeast.get(str(second["id"]))
        self.assertEqual("qualified", restored["status"])
        self.assertEqual([], self.app.registry.yeast.pitches_for_batch(batch_id)[1:])

    def test_quarantined_culture_blocked_at_pitch(self) -> None:
        batch_id, tank_id = prepare_filled_tank(self.app)
        culture = register_qualified(self.app, viability=70.0)
        with self.assertRaises(InterlockError) as caught:
            self.app.registry.brewing.pitch_yeast(
                batch_id, tank_id, 10.0, 90.0, "tester", culture_id=str(culture["id"])
            )
        self.assertEqual("interlock_blocked", caught.exception.code)
        # 批次与酵母都未被改动
        self.assertEqual("cooling", self.app.registry.brewing.status(batch_id)["batch"]["stage"])
        self.assertEqual("quarantined", self.app.registry.yeast.get(str(culture["id"]))["status"])

    def test_summary_counts(self) -> None:
        register_qualified(self.app, viability=95.0)
        register_qualified(self.app, viability=60.0)
        summary = self.app.registry.yeast.summary()
        self.assertEqual(2, summary["cultures"])
        self.assertEqual(1, summary["qualified"])
        self.assertEqual(1, summary["quarantined"])
        self.assertEqual(3, summary["generation_limit"])


if __name__ == "__main__":
    unittest.main()
