# BOM & Change Order Tracker

A small PLM-style data management tool written in Python (standard library + SQLite, no dependencies). It models how a PLM system handles parts, Bills of Materials (BOM), Approved Manufacturer Lists (AML) and engineering change orders.

## Features

- **Part creation by classification:** auto-generated part numbers (`MEC-000001`, `ELE-000001`, ...), Make/Buy type, unit of measure, project, duplicate-part blocking.
- **Multi-level BOM:** parent-child lines with quantities, indented BOM explosion with extended quantities, multi-level where-used, circular-reference prevention.
- **AML:** supplier sources per Buy part with Approved / Disqualified status.
- **Change orders (ECR / ECO / MCO / Deviation):** Draft → Submitted → CCB Approved → Implemented (or Rejected). Actions: add child, remove child, change quantity, replace child, obsolete part.
- **Revision control:** implementing a change bumps the revision of every affected assembly (A → B → C).
- **Safe implementation:** a change is applied in one database transaction and rolled back if any item fails or introduces a validation error.
- **Validation checks:** missing attributes, duplicate parts, circular BOMs, obsolete parts in active BOMs, Buy parts with BOM lines, Buy parts with no approved AML source, empty assemblies.
- **KPIs:** change order counts by status, average cycle time, SLA compliance %, overdue open changes.
- **Audit log** of every change.

## Usage

```bash
python3 bomtracker.py seed                    # load a demo desk-lamp product + a draft ECO
python3 bomtracker.py bom ASY-000001          # indented multi-level BOM
python3 bomtracker.py validate                # data-quality report (the demo carton has no AML on purpose)
python3 bomtracker.py co-submit 1
python3 bomtracker.py co-approve 1 "CCB Chair"
python3 bomtracker.py co-implement 1          # screw replaced, lamp assembly goes to Rev B
python3 bomtracker.py where-used FAS-000002
python3 bomtracker.py report                  # SLA / cycle-time KPIs
python3 bomtracker.py audit
```

Other commands: `add-part`, `add-line`, `add-aml`, `co-new`, `co-add`, `co-reject`, `co-list`.

## Tests

```bash
python3 -m unittest -v      # 22 tests
```

## Limitations

This is a learning project, not a PLM replacement: single user, no authentication, no drawings or document management, and a flat one-letter revision scheme.
