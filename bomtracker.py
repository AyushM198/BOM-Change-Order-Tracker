#!/usr/bin/env python3
"""BOM & Change Order Tracker.

A small PLM-style tool (Python + SQLite) for managing parts, multi-level
Bills of Materials (BOM), Approved Manufacturer Lists (AML) and engineering
change orders (ECR / ECO / MCO / Deviation) with a CCB-style approval flow.
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta

CLASSES = {"Electrical": "ELE", "Mechanical": "MEC", "Fastener": "FAS",
           "Packaging": "PKG", "Assembly": "ASY"}
PART_TYPES = ("Make", "Buy")
CHANGE_KINDS = ("ECR", "ECO", "MCO", "Deviation")
ACTIONS = ("ADD_CHILD", "REMOVE_CHILD", "SET_QTY", "REPLACE_CHILD", "OBSOLETE_PART")
OPEN_STATES = ("Draft", "Submitted", "Approved")
TRANSITIONS = {
    "Draft": ("Submitted",),
    "Submitted": ("Approved", "Rejected"),
    "Approved": ("Implemented",),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS parts(
    part_no TEXT PRIMARY KEY, name TEXT NOT NULL, classification TEXT NOT NULL,
    part_type TEXT NOT NULL, uom TEXT NOT NULL DEFAULT 'EA',
    revision TEXT NOT NULL DEFAULT 'A', status TEXT NOT NULL DEFAULT 'Active',
    project TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS bom(
    parent TEXT NOT NULL REFERENCES parts(part_no),
    child TEXT NOT NULL REFERENCES parts(part_no),
    qty REAL NOT NULL CHECK(qty > 0), PRIMARY KEY(parent, child));
CREATE TABLE IF NOT EXISTS suppliers(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS aml(
    part_no TEXT REFERENCES parts(part_no),
    supplier_id INTEGER REFERENCES suppliers(id),
    mpn TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Approved',
    PRIMARY KEY(part_no, supplier_id, mpn));
CREATE TABLE IF NOT EXISTS change_orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft', created_at TEXT NOT NULL,
    due_at TEXT NOT NULL, closed_at TEXT, approver TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS change_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    co_id INTEGER NOT NULL REFERENCES change_orders(id),
    action TEXT NOT NULL, parent TEXT NOT NULL, child TEXT, new_child TEXT, qty REAL);
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    event TEXT NOT NULL, detail TEXT NOT NULL);
"""


class BomError(Exception):
    """Raised for any business-rule violation."""


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class BomDB:
    def __init__(self, path=":memory:", clock=datetime.now):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.clock = clock

    # ---------------------------------------------------------- helpers
    def _log(self, event, detail):
        self.conn.execute("INSERT INTO audit(ts, event, detail) VALUES (?,?,?)",
                          (self.clock().isoformat(timespec="seconds"), event, detail))

    def _part(self, part_no):
        row = self.conn.execute("SELECT * FROM parts WHERE part_no=?", (part_no,)).fetchone()
        if not row:
            raise BomError(f"Part {part_no} does not exist")
        return row

    def _related(self, part_no, down=True):
        """All descendants (down) or ancestors (up) of a part."""
        sql = "SELECT child AS n FROM bom WHERE parent=?" if down else \
              "SELECT parent AS n FROM bom WHERE child=?"
        seen, stack = set(), [part_no]
        while stack:
            for r in self.conn.execute(sql, (stack.pop(),)):
                if r["n"] not in seen:
                    seen.add(r["n"])
                    stack.append(r["n"])
        return seen

    # ------------------------------------------------------------ parts
    def create_part(self, name, classification, part_type, uom="EA", project=""):
        name = (name or "").strip()
        if not name:
            raise BomError("Part name is required")
        if classification not in CLASSES:
            raise BomError(f"Classification must be one of {sorted(CLASSES)}")
        if part_type not in PART_TYPES:
            raise BomError(f"Part type must be one of {PART_TYPES}")
        for r in self.conn.execute(
                "SELECT part_no, name FROM parts WHERE classification=?", (classification,)):
            if _norm(r["name"]) == _norm(name):
                raise BomError(f"Possible duplicate of {r['part_no']} ({r['name']})")
        prefix = CLASSES[classification]
        last = self.conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(part_no, 5) AS INTEGER)), 0) "
            "FROM parts WHERE part_no LIKE ?", (prefix + "-%",)).fetchone()[0]
        part_no = f"{prefix}-{last + 1:06d}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO parts(part_no,name,classification,part_type,uom,project) "
                "VALUES (?,?,?,?,?,?)", (part_no, name, classification, part_type, uom, project))
            self._log("PART_CREATED", f"{part_no} {name}")
        return part_no

    def add_supplier_source(self, part_no, supplier, mpn, status="Approved"):
        self._part(part_no)
        if status not in ("Approved", "Disqualified"):
            raise BomError("AML status must be Approved or Disqualified")
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO suppliers(name) VALUES (?)", (supplier,))
            sid = self.conn.execute("SELECT id FROM suppliers WHERE name=?", (supplier,)).fetchone()[0]
            self.conn.execute("INSERT OR REPLACE INTO aml VALUES (?,?,?,?)", (part_no, sid, mpn, status))
            self._log("AML_UPDATED", f"{part_no} {supplier} {mpn} {status}")

    # -------------------------------------------------------------- BOM
    def _add_line(self, parent, child, qty):
        p, c = self._part(parent), self._part(child)
        if p["part_type"] != "Make":
            raise BomError(f"{parent} is a Buy part and cannot have a BOM")
        if p["status"] != "Active" or c["status"] != "Active":
            raise BomError("Obsolete parts cannot be added to a BOM")
        if qty is None or qty <= 0:
            raise BomError("Quantity must be greater than zero")
        if parent == child or parent in self._related(child, down=True):
            raise BomError(f"Adding {child} under {parent} would create a circular BOM")
        if self.conn.execute("SELECT 1 FROM bom WHERE parent=? AND child=?", (parent, child)).fetchone():
            raise BomError(f"{child} is already on the BOM of {parent}")
        self.conn.execute("INSERT INTO bom VALUES (?,?,?)", (parent, child, qty))

    def add_bom_line(self, parent, child, qty):
        with self.conn:
            self._add_line(parent, child, qty)
            self._log("BOM_LINE_ADDED", f"{parent} -> {child} x{qty:g}")

    def explode(self, part_no, qty=1.0):
        """Indented multi-level BOM: (level, part_no, name, rev, extended_qty)."""
        self._part(part_no)
        out = []

        def walk(pn, mult, level):
            r = self._part(pn)
            out.append((level, pn, r["name"], r["revision"], mult))
            for line in self.conn.execute(
                    "SELECT child, qty FROM bom WHERE parent=? ORDER BY child", (pn,)):
                walk(line["child"], mult * line["qty"], level + 1)

        walk(part_no, qty, 0)
        return out

    def where_used(self, part_no):
        self._part(part_no)
        return sorted(self._related(part_no, down=False))

    # ------------------------------------------------------- validation
    def validate(self):
        """Run data-quality checks. Returns [(severity, code, message)]."""
        issues, seen = [], {}
        for p in self.conn.execute("SELECT * FROM parts ORDER BY part_no"):
            pn = p["part_no"]
            for f in ("name", "classification", "part_type", "uom"):
                if not (p[f] or "").strip():
                    issues.append(("ERROR", "MISSING_ATTRIBUTE", f"{pn}: missing {f}"))
            if p["status"] != "Active":
                continue
            key = (p["classification"], _norm(p["name"]))
            if key in seen:
                issues.append(("WARNING", "DUPLICATE_PART", f"{pn} duplicates {seen[key]}"))
            seen.setdefault(key, pn)
            n_children = self.conn.execute(
                "SELECT COUNT(*) FROM bom WHERE parent=?", (pn,)).fetchone()[0]
            if p["part_type"] == "Buy":
                if n_children:
                    issues.append(("ERROR", "BUY_PART_WITH_BOM", f"{pn}: Buy part has BOM lines"))
                ok = self.conn.execute(
                    "SELECT 1 FROM aml WHERE part_no=? AND status='Approved'", (pn,)).fetchone()
                if not ok:
                    issues.append(("WARNING", "NO_APPROVED_SOURCE", f"{pn}: no approved AML source"))
            elif n_children == 0:
                issues.append(("WARNING", "EMPTY_ASSEMBLY", f"{pn}: Make part has no BOM lines"))
            if pn in self._related(pn, down=True):
                issues.append(("ERROR", "CIRCULAR_BOM", f"{pn}: circular BOM reference"))
        for r in self.conn.execute(
                "SELECT b.parent, b.child FROM bom b JOIN parts c ON c.part_no=b.child "
                "JOIN parts p ON p.part_no=b.parent "
                "WHERE c.status!='Active' AND p.status='Active' ORDER BY 1,2"):
            issues.append(("ERROR", "OBSOLETE_IN_BOM",
                           f"{r['parent']}: uses obsolete part {r['child']}"))
        return issues

    def _error_set(self):
        return {f"{c}: {m}" for s, c, m in self.validate() if s == "ERROR"}

    # ----------------------------------------------------- change orders
    def _co(self, co_id):
        row = self.conn.execute("SELECT * FROM change_orders WHERE id=?", (co_id,)).fetchone()
        if not row:
            raise BomError(f"Change order {co_id} does not exist")
        return row

    def new_change(self, kind, title, sla_days=5):
        if kind not in CHANGE_KINDS:
            raise BomError(f"Change type must be one of {CHANGE_KINDS}")
        if not (title or "").strip():
            raise BomError("Change title is required")
        now = self.clock()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO change_orders(kind,title,created_at,due_at) VALUES (?,?,?,?)",
                (kind, title.strip(), now.isoformat(timespec="seconds"),
                 (now + timedelta(days=sla_days)).isoformat(timespec="seconds")))
            self._log("CO_CREATED", f"{kind} #{cur.lastrowid} {title}")
        return cur.lastrowid

    def add_item(self, co_id, action, parent, child=None, qty=None, new_child=None):
        co = self._co(co_id)
        if co["status"] != "Draft":
            raise BomError("Items can only be added while the change order is in Draft")
        if action not in ACTIONS:
            raise BomError(f"Action must be one of {ACTIONS}")
        need = {"ADD_CHILD": (child, qty), "REMOVE_CHILD": (child,), "SET_QTY": (child, qty),
                "REPLACE_CHILD": (child, new_child), "OBSOLETE_PART": ()}[action]
        if any(v is None for v in need):
            raise BomError(f"{action} is missing required fields")
        for pn in (parent, child, new_child):
            if pn:
                self._part(pn)
        with self.conn:
            self.conn.execute(
                "INSERT INTO change_items(co_id,action,parent,child,new_child,qty) VALUES (?,?,?,?,?,?)",
                (co_id, action, parent, child, new_child, qty))

    def _move(self, co_id, new_status, **fields):
        co = self._co(co_id)
        if new_status not in TRANSITIONS.get(co["status"], ()):
            raise BomError(f"CO {co_id}: cannot move from {co['status']} to {new_status}")
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE change_orders SET status=?{', ' + sets if sets else ''} WHERE id=?"
        self.conn.execute(sql, (new_status, *fields.values(), co_id))
        self._log("CO_" + new_status.upper(), f"#{co_id}")

    def submit(self, co_id):
        if not self.conn.execute("SELECT 1 FROM change_items WHERE co_id=?", (co_id,)).fetchone():
            raise BomError("Cannot submit a change order with no items")
        with self.conn:
            self._move(co_id, "Submitted")

    def approve(self, co_id, approver):
        if not (approver or "").strip():
            raise BomError("CCB approver name is required")
        with self.conn:
            self._move(co_id, "Approved", approver=approver.strip())

    def reject(self, co_id, reason):
        if not (reason or "").strip():
            raise BomError("A rejection reason is required")
        with self.conn:
            self._move(co_id, "Rejected", reason=reason.strip(),
                       closed_at=self.clock().isoformat(timespec="seconds"))

    def _apply(self, it, affected):
        action, parent, child = it["action"], it["parent"], it["child"]
        if action == "ADD_CHILD":
            self._add_line(parent, child, it["qty"])
            affected.add(parent)
        elif action == "REMOVE_CHILD":
            if not self.conn.execute("DELETE FROM bom WHERE parent=? AND child=?",
                                     (parent, child)).rowcount:
                raise BomError(f"{child} is not on the BOM of {parent}")
            affected.add(parent)
        elif action == "SET_QTY":
            if it["qty"] <= 0:
                raise BomError("Quantity must be greater than zero")
            if not self.conn.execute("UPDATE bom SET qty=? WHERE parent=? AND child=?",
                                     (it["qty"], parent, child)).rowcount:
                raise BomError(f"{child} is not on the BOM of {parent}")
            affected.add(parent)
        elif action == "REPLACE_CHILD":
            line = self.conn.execute("SELECT qty FROM bom WHERE parent=? AND child=?",
                                     (parent, child)).fetchone()
            if not line:
                raise BomError(f"{child} is not on the BOM of {parent}")
            self.conn.execute("DELETE FROM bom WHERE parent=? AND child=?", (parent, child))
            self._add_line(parent, it["new_child"], line["qty"])
            affected.add(parent)
        elif action == "OBSOLETE_PART":
            users = self._related(parent, down=False)
            if users:
                raise BomError(f"{parent} is still used on: {', '.join(sorted(users))}")
            self.conn.execute("UPDATE parts SET status='Obsolete' WHERE part_no=?", (parent,))

    def _bump_revision(self, part_no):
        rev = self._part(part_no)["revision"]
        if len(rev) != 1 or not "A" <= rev < "Z":
            raise BomError(f"{part_no}: cannot bump revision '{rev}'")
        self.conn.execute("UPDATE parts SET revision=? WHERE part_no=?", (chr(ord(rev) + 1), part_no))

    def implement(self, co_id):
        """Apply an approved change atomically; roll back if it breaks data quality."""
        co = self._co(co_id)
        if co["status"] != "Approved":
            raise BomError(f"CO {co_id} must be Approved by the CCB before implementation")
        items = self.conn.execute("SELECT * FROM change_items WHERE co_id=? ORDER BY id",
                                  (co_id,)).fetchall()
        before, affected = self._error_set(), set()
        with self.conn:  # any exception below rolls the whole change back
            for it in items:
                self._apply(it, affected)
            for pn in sorted(affected):
                self._bump_revision(pn)
            new_errors = self._error_set() - before
            if new_errors:
                raise BomError("Blocked by validation errors: " + "; ".join(sorted(new_errors)))
            self._move(co_id, "Implemented", closed_at=self.clock().isoformat(timespec="seconds"))

    def metrics(self):
        rows = self.conn.execute("SELECT * FROM change_orders").fetchall()
        by_status, cycle, met = {}, [], 0
        now = self.clock()
        overdue = 0
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            created, due = datetime.fromisoformat(r["created_at"]), datetime.fromisoformat(r["due_at"])
            if r["status"] == "Implemented":
                closed = datetime.fromisoformat(r["closed_at"])
                cycle.append((closed - created).total_seconds() / 86400)
                met += closed <= due
            elif r["status"] in OPEN_STATES and now > due:
                overdue += 1
        return {"total": len(rows), "by_status": by_status, "open_overdue": overdue,
                "avg_cycle_days": round(sum(cycle) / len(cycle), 2) if cycle else None,
                "sla_met_pct": round(100 * met / len(cycle), 1) if cycle else None}


# ------------------------------------------------------------------ demo
def seed(db):
    """Create a small demo product (a desk lamp) with a draft ECO."""
    P = {}
    spec = [
        ("lamp", "Desk Lamp Assembly", "Assembly", "Make"),
        ("pcb", "LED Driver PCB Assembly", "Assembly", "Make"),
        ("base", "Lamp Base Casting", "Mechanical", "Buy"),
        ("arm", "Lamp Arm Tube", "Mechanical", "Buy"),
        ("led", "LED Module 5W", "Electrical", "Buy"),
        ("ic", "Driver IC", "Electrical", "Buy"),
        ("res", "Resistor 10k 0603", "Electrical", "Buy"),
        ("scr8", "Screw M3x8 Pan Head", "Fastener", "Buy"),
        ("scr10", "Screw M3x10 Pan Head", "Fastener", "Buy"),
        ("box", "Retail Carton", "Packaging", "Buy"),
    ]
    for key, name, cls, typ in spec:
        P[key] = db.create_part(name, cls, typ, project="DeskLamp")
    for parent, child, qty in [("lamp", "base", 1), ("lamp", "arm", 1), ("lamp", "pcb", 1),
                               ("lamp", "led", 1), ("lamp", "scr8", 4), ("lamp", "box", 1),
                               ("pcb", "ic", 1), ("pcb", "res", 4)]:
        db.add_bom_line(P[parent], P[child], qty)
    for key, sup, mpn in [("base", "Precision Castings Ltd", "PC-1100"), ("arm", "TubeWorks", "TW-22"),
                          ("led", "BrightChip", "BC-5W"), ("ic", "DriverSemi", "DS-300"),
                          ("res", "PassiveCo", "PR-10K"), ("scr8", "FastenIt", "FI-M3-8"),
                          ("scr10", "FastenIt", "FI-M3-10")]:
        db.add_supplier_source(P[key], sup, mpn)
    # "Retail Carton" deliberately has no AML so validation shows a finding.
    co = db.new_change("ECO", "Replace M3x8 screw with M3x10 for better thread engagement")
    db.add_item(co, "REPLACE_CHILD", P["lamp"], child=P["scr8"], new_child=P["scr10"])
    return P


# ------------------------------------------------------------------- CLI
def _print_issues(issues):
    for sev, code, msg in issues:
        print(f"[{sev}] {code}: {msg}")
    print(f"{len(issues)} finding(s), {sum(i[0] == 'ERROR' for i in issues)} error(s)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="bom.db", help="SQLite file (default bom.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed", help="load a demo product and a draft ECO")
    s = sub.add_parser("add-part"); s.add_argument("name"); s.add_argument("classification")
    s.add_argument("part_type"); s.add_argument("--uom", default="EA"); s.add_argument("--project", default="")
    s = sub.add_parser("add-line"); s.add_argument("parent"); s.add_argument("child"); s.add_argument("qty", type=float)
    s = sub.add_parser("add-aml"); s.add_argument("part_no"); s.add_argument("supplier"); s.add_argument("mpn")
    s = sub.add_parser("bom"); s.add_argument("part_no"); s.add_argument("--qty", type=float, default=1)
    s = sub.add_parser("where-used"); s.add_argument("part_no")
    sub.add_parser("validate")
    s = sub.add_parser("co-new"); s.add_argument("kind"); s.add_argument("title")
    s.add_argument("--sla-days", type=int, default=5)
    s = sub.add_parser("co-add"); s.add_argument("co_id", type=int); s.add_argument("action")
    s.add_argument("parent"); s.add_argument("--child"); s.add_argument("--new-child")
    s.add_argument("--qty", type=float)
    for name in ("co-submit", "co-implement"):
        s = sub.add_parser(name); s.add_argument("co_id", type=int)
    s = sub.add_parser("co-approve"); s.add_argument("co_id", type=int); s.add_argument("approver")
    s = sub.add_parser("co-reject"); s.add_argument("co_id", type=int); s.add_argument("reason")
    sub.add_parser("co-list")
    sub.add_parser("report")
    sub.add_parser("audit")
    a = ap.parse_args(argv)
    db = BomDB(a.db)
    try:
        if a.cmd == "seed":
            for k, v in seed(db).items():
                print(f"{v}  {k}")
        elif a.cmd == "add-part":
            print(db.create_part(a.name, a.classification, a.part_type, a.uom, a.project))
        elif a.cmd == "add-line":
            db.add_bom_line(a.parent, a.child, a.qty)
        elif a.cmd == "add-aml":
            db.add_supplier_source(a.part_no, a.supplier, a.mpn)
        elif a.cmd == "bom":
            for lvl, pn, name, rev, q in db.explode(a.part_no, a.qty):
                print(f"{'  ' * lvl}{pn}  {name}  (Rev {rev})  x{q:g}")
        elif a.cmd == "where-used":
            print("\n".join(db.where_used(a.part_no)) or "Not used on any BOM")
        elif a.cmd == "validate":
            issues = db.validate()
            _print_issues(issues)
            return 1 if any(i[0] == "ERROR" for i in issues) else 0
        elif a.cmd == "co-new":
            print(f"Created change order #{db.new_change(a.kind, a.title, a.sla_days)}")
        elif a.cmd == "co-add":
            db.add_item(a.co_id, a.action, a.parent, a.child, a.qty, a.new_child)
        elif a.cmd == "co-submit":
            db.submit(a.co_id)
        elif a.cmd == "co-approve":
            db.approve(a.co_id, a.approver)
        elif a.cmd == "co-reject":
            db.reject(a.co_id, a.reason)
        elif a.cmd == "co-implement":
            db.implement(a.co_id)
        elif a.cmd == "co-list":
            for r in db.conn.execute("SELECT * FROM change_orders ORDER BY id"):
                print(f"#{r['id']}  {r['kind']:<9} {r['status']:<12} due {r['due_at'][:10]}  {r['title']}")
        elif a.cmd == "report":
            for k, v in db.metrics().items():
                print(f"{k}: {v}")
        elif a.cmd == "audit":
            for r in db.conn.execute("SELECT * FROM audit ORDER BY id"):
                print(f"{r['ts']}  {r['event']:<16} {r['detail']}")
    except BomError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
