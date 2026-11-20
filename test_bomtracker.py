import unittest
from datetime import datetime, timedelta

from bomtracker import BomDB, BomError, seed


class Base(unittest.TestCase):
    def setUp(self):
        self.db = BomDB(":memory:")

    def mk(self, name, cls="Mechanical", typ="Buy"):
        return self.db.create_part(name, cls, typ)

    def codes(self):
        return {c for _, c, _ in self.db.validate()}


class TestParts(Base):
    def test_auto_numbering_by_classification(self):
        self.assertEqual(self.mk("Bracket"), "MEC-000001")
        self.assertEqual(self.mk("Plate"), "MEC-000002")
        self.assertEqual(self.mk("LED", "Electrical"), "ELE-000001")

    def test_duplicate_part_blocked(self):
        self.mk("Screw M3x8")
        with self.assertRaises(BomError):
            self.mk("screw  m3x8")

    def test_required_fields(self):
        with self.assertRaises(BomError):
            self.mk("   ")
        with self.assertRaises(BomError):
            self.db.create_part("X", "Unknown", "Buy")


class TestBom(Base):
    def setUp(self):
        super().setUp()
        self.top = self.mk("Top", "Assembly", "Make")
        self.sub = self.mk("Sub", "Assembly", "Make")
        self.leaf = self.mk("Leaf")
        self.db.add_bom_line(self.top, self.sub, 2)
        self.db.add_bom_line(self.sub, self.leaf, 3)

    def test_explosion_extended_quantity(self):
        rows = self.db.explode(self.top)
        self.assertEqual([(r[0], r[1], r[4]) for r in rows],
                         [(0, self.top, 1), (1, self.sub, 2), (2, self.leaf, 6)])

    def test_where_used_is_multi_level(self):
        self.assertEqual(self.db.where_used(self.leaf), sorted([self.sub, self.top]))

    def test_circular_bom_blocked(self):
        with self.assertRaises(BomError):
            self.db.add_bom_line(self.leaf if False else self.sub, self.top, 1)

    def test_buy_part_cannot_have_bom(self):
        with self.assertRaises(BomError):
            self.db.add_bom_line(self.leaf, self.mk("Other"), 1)

    def test_duplicate_line_and_bad_qty(self):
        with self.assertRaises(BomError):
            self.db.add_bom_line(self.top, self.sub, 1)
        with self.assertRaises(BomError):
            self.db.add_bom_line(self.top, self.mk("Y"), 0)


class TestValidation(Base):
    def test_buy_part_without_aml_flagged_then_cleared(self):
        p = self.mk("Carton", "Packaging")
        self.assertIn("NO_APPROVED_SOURCE", self.codes())
        self.db.add_supplier_source(p, "BoxCo", "B-1")
        self.assertNotIn("NO_APPROVED_SOURCE", self.codes())

    def test_disqualified_source_does_not_count(self):
        p = self.mk("Carton", "Packaging")
        self.db.add_supplier_source(p, "BoxCo", "B-1", "Disqualified")
        self.assertIn("NO_APPROVED_SOURCE", self.codes())

    def test_empty_assembly_flagged(self):
        self.mk("Empty", "Assembly", "Make")
        self.assertIn("EMPTY_ASSEMBLY", self.codes())

    def test_missing_attribute_and_duplicate_from_raw_import(self):
        self.db.conn.execute("INSERT INTO parts(part_no,name,classification,part_type) "
                             "VALUES ('MEC-900001','','Mechanical','Buy')")
        self.db.conn.execute("INSERT INTO parts(part_no,name,classification,part_type) "
                             "VALUES ('MEC-900002','Widget','Mechanical','Buy')")
        self.db.conn.execute("INSERT INTO parts(part_no,name,classification,part_type) "
                             "VALUES ('MEC-900003','WIDGET','Mechanical','Buy')")
        self.assertTrue({"MISSING_ATTRIBUTE", "DUPLICATE_PART"} <= self.codes())

    def test_circular_reference_detected_in_raw_data(self):
        a, b = self.mk("A", "Assembly", "Make"), self.mk("B", "Assembly", "Make")
        self.db.conn.execute("INSERT INTO bom VALUES (?,?,1)", (a, b))
        self.db.conn.execute("INSERT INTO bom VALUES (?,?,1)", (b, a))
        self.assertIn("CIRCULAR_BOM", self.codes())


class TestChangeOrders(Base):
    def setUp(self):
        super().setUp()
        self.P = seed(self.db)
        self.co = 1  # draft ECO created by seed()

    def test_cannot_skip_approval(self):
        with self.assertRaises(BomError):
            self.db.implement(self.co)
        self.db.submit(self.co)
        with self.assertRaises(BomError):
            self.db.implement(self.co)

    def test_cannot_submit_empty_or_edit_after_submit(self):
        empty = self.db.new_change("ECR", "Request")
        with self.assertRaises(BomError):
            self.db.submit(empty)
        self.db.submit(self.co)
        with self.assertRaises(BomError):
            self.db.add_item(self.co, "SET_QTY", self.P["lamp"], child=self.P["scr8"], qty=2)

    def test_approval_requires_approver_and_rejection_reason(self):
        self.db.submit(self.co)
        with self.assertRaises(BomError):
            self.db.approve(self.co, " ")
        with self.assertRaises(BomError):
            self.db.reject(self.co, "")

    def test_full_flow_replaces_part_and_bumps_revision(self):
        P = self.P
        self.db.submit(self.co)
        self.db.approve(self.co, "CCB Chair")
        self.db.implement(self.co)
        kids = {r[1] for r in self.db.explode(P["lamp"])}
        self.assertIn(P["scr10"], kids)
        self.assertNotIn(P["scr8"], kids)
        self.assertEqual(self.db._part(P["lamp"])["revision"], "B")
        self.assertEqual(self.db._part(P["pcb"])["revision"], "A")
        self.assertEqual(self.db.conn.execute(
            "SELECT status FROM change_orders WHERE id=?", (self.co,)).fetchone()[0], "Implemented")

    def test_failed_implementation_rolls_back_everything(self):
        P = self.P
        co = self.db.new_change("ECO", "Bad change")
        self.db.add_item(co, "SET_QTY", P["lamp"], child=P["arm"], qty=9)  # valid
        self.db.add_item(co, "REMOVE_CHILD", P["lamp"], child=P["box"])
        self.db.add_item(co, "REMOVE_CHILD", P["lamp"], child=P["box"])  # fails: already gone
        self.db.submit(co)
        self.db.approve(co, "CCB")
        with self.assertRaises(BomError):
            self.db.implement(co)
        qty = self.db.conn.execute("SELECT qty FROM bom WHERE parent=? AND child=?",
                                   (P["lamp"], P["arm"])).fetchone()[0]
        self.assertEqual(qty, 1)  # first item was rolled back
        self.assertEqual(self.db._part(P["lamp"])["revision"], "A")
        self.assertEqual(self.db._co(co)["status"], "Approved")

    def test_cannot_obsolete_part_still_in_use(self):
        co = self.db.new_change("MCO", "Obsolete screw")
        self.db.add_item(co, "OBSOLETE_PART", self.P["scr8"])
        self.db.submit(co)
        self.db.approve(co, "CCB")
        with self.assertRaises(BomError):
            self.db.implement(co)

    def test_obsolete_after_replacement_is_allowed(self):
        self.db.submit(self.co); self.db.approve(self.co, "CCB"); self.db.implement(self.co)
        co = self.db.new_change("MCO", "Obsolete screw")
        self.db.add_item(co, "OBSOLETE_PART", self.P["scr8"])
        self.db.submit(co); self.db.approve(co, "CCB"); self.db.implement(co)
        self.assertEqual(self.db._part(self.P["scr8"])["status"], "Obsolete")

    def test_rejected_change_is_closed_and_final(self):
        self.db.submit(self.co)
        self.db.reject(self.co, "Not justified")
        with self.assertRaises(BomError):
            self.db.approve(self.co, "CCB")


class TestMetrics(Base):
    def test_sla_and_cycle_time(self):
        now = [datetime(2026, 1, 1)]
        db = BomDB(":memory:", clock=lambda: now[0])
        P = seed(db)
        # CO 1: close on day 2 (within 5-day SLA)
        db.submit(1); db.approve(1, "CCB")
        now[0] += timedelta(days=2); db.implement(1)
        # CO 2: close on day 9 (late)
        now[0] = datetime(2026, 1, 3)
        co = db.new_change("ECO", "Late change", sla_days=1)
        db.add_item(co, "SET_QTY", P["lamp"], child=P["arm"], qty=2)
        db.submit(co); db.approve(co, "CCB")
        now[0] += timedelta(days=4); db.implement(co)
        m = db.metrics()
        self.assertEqual(m["by_status"], {"Implemented": 2})
        self.assertEqual(m["sla_met_pct"], 50.0)
        self.assertEqual(m["avg_cycle_days"], 3.0)


if __name__ == "__main__":
    unittest.main()
