"""`nmosd header-shapes`: masked shapes only, never a value."""

from __future__ import annotations

import io
import json

from nmosd.deid.header_shapes import header_shapes, shape
from nmosd.store.db import connect


def test_shape_masks_every_value_and_collapses_lengths():
    assert shape("42 Years F MRN: 1234567 DOB: 3/4/1980") == "9+ Years A MRN: 9+ DOB: 9+/9+/9+"
    # a name keeps neither its letters nor its length
    assert shape("Smith, Jonathan Q.") == shape("Li, Al B.") == "Aa+, Aa+ A."
    assert shape("  lots   the   space ") == "a+ a+ a+"


def test_header_shapes_report_prints_no_seeded_identifier(pipeline):
    """On the synthetic staging DB (fake PHI seeded in every header), none of it appears in the report."""
    out = io.StringIO()
    r = header_shapes(pipeline["staging"], stdout=out)
    text = out.getvalue()
    assert set(r) >= {"VisitNote", "ProgressNote", "Radiology"}
    assert all(v["header_recognised"] <= v["notes"] for v in r.values())
    con = connect(pipeline["staging"], readonly=True)
    seeded = {str(row[0]) for row in con.execute("SELECT empi FROM documents")}
    for (raw,) in con.execute("SELECT raw_json FROM demographics"):
        seeded |= {str(v) for v in json.loads(raw).values() if v and len(str(v)) > 3}
    con.close()
    assert seeded
    for value in seeded:
        assert value not in text


def test_unrecognised_headers_are_listed_with_counts(tmp_path):
    db = tmp_path / "staging.sqlite"
    con = connect(db, "staging")
    notes = [("ProgressNote", "JANE DOE   55 Years F\nMRN 123\nBoston MA 02115\nbody\n") for _ in range(3)]
    notes.append(("ProgressNote", "one-off line\nbody\n"))
    for i, (table, text) in enumerate(notes):
        con.execute("INSERT INTO documents (empi, source_table, text, text_hash) VALUES (?,?,?,?)",
                    (f"E{i}", table, text, f"h{i}"))
    con.commit()
    con.close()
    out = io.StringIO()
    r = header_shapes(db, stdout=out)
    text = out.getvalue()
    assert r["ProgressNote"] == {"notes": 4, "header_recognised": 0, "banner_recognised": 0}
    assert "- line 1 (3×): `A+ A+ 9+ Years A`" in text
    assert "Not recognised: 4; banner line recognised in 0; closest layout:" in text
    assert "JANE" not in text and "123" not in text and "one-off" not in text
