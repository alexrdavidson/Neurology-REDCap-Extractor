"""The real RPDR header layouts (fitted 11.11.26 to `nmosd header-shapes` on the pilot data, masked shapes only).

Every fake line below has exactly the masked shape the pilot data showed, so the parser is tested on the real
layout without any real value.
"""

from __future__ import annotations

import pytest

from nmosd.deid.header import parse_header
from nmosd.deid.header_shapes import shape

BANNER = ("***This report contains confidential information about a patient and is released for authorized "
          "research use only, '123456.txt'.")

# (fake line, the shape seen in the pilot data)
INPATIENT = [
    ("MGH Neurology Clinic Doe, Jane", "A+ Aa+ Aa+ Aa+, Aa+"),
    ("42 Yrs Old MRN: 1234567, DOB: 3/4/1980, Legal Sex: F", "9+ Aa+ Aa+ MRN: 9+, DOB: 9+/9+/9+, Aa+ Sex: A"),
    ("Boston MA 02114-1234 Acct #: 98765432", "Aa+ A+ 9+-9+ Acct #: 9+"),
    ("ADM: 1/2/2020, D/C: ?", "A+: 9+/9+/9+, A/A: ?"),
]
VISIT = [
    ("Doe, Jane (MR # 1234567) DOB: 3/4/1980 Encounter Date: 5/6/2021",
     "Aa+, Aa+ (MR # 9+) DOB: 9+/9+/9+ Encounter Date: 9+/9+/9+"),
    ("Jane Doe Gender: Female DOB:", "Aa+ Aa+ Aa+: Female DOB:"),
    ("5/6/2021 10:30 AM Office Visit 5/6/2021", "9+/9+/9+ 9+:9+ AM Aa+ Visit 9+/9+/9+"),
    ("MRN: 1234567 Provider: Smith, John, MD", "MRN: 9+ Aa+: Aa+, Aa+, A+"),
    ("CSN: 123456789 Dept: MGH NEURO", "A+: 9+ Aa+: A+ A+"),
]
BODY = "Progress Note\r\nHPI: 42F with NMOSD, AQP4-IgG positive, seen for follow-up.\r\n"
# A banner with the pilot data's shape but none of the codebook's wording ("report contains").
REAL_BANNER = ("***This information has been released under the research agreement, '123456.txt'. Do not share "
               "this file with anyone outside the study team .txt. Use is made of the data under protocol "
               "(irb), see the terms.")
REAL_BANNER_SHAPE = ("***Aa+ a+ a+ a+ a+ a+ a+ a+ a+, '9+.a+'. Aa+ a+ a+ a+ a+ a+ a+ a+ a+ a+ a+ .a+. Aa+ a+ a+ of a+ a+ a+ "
                     "a+ (a+), a+ a+ a+ …")


def _note(lines: list[str], banner: str = BANNER) -> str:
    return "\r\n".join([banner, *lines]) + "\r\n" + BODY


def test_fake_lines_have_the_pilot_shapes():
    for line, seen in INPATIENT + VISIT:
        assert shape(line) == seen, line
    assert shape(REAL_BANNER)[:100] == REAL_BANNER_SHAPE[:100]


@pytest.mark.parametrize("table, lines, layout", [("ProgressNote", INPATIENT, "inpatient_rpdr"),
                                                  ("VisitNote", VISIT, "visit_rpdr")])
def test_banner_wording_does_not_matter(table, lines, layout):
    """The pilot banner does not say "report contains": it must still be recognised and removed."""
    text = _note([x for x, _ in lines], banner=REAL_BANNER)
    h = parse_header(text, table)
    assert h.banner == (0, len(REAL_BANNER) + 2)
    assert h.matched and h.replace and h.layout == layout and h.start == 0


@pytest.mark.parametrize("table", ["ProgressNote", "HistoryAndPhysical", "Discharge_Summaries", "VisitNote"])
def test_inpatient_layout_is_replaced_and_harvested(table):
    text = _note([x for x, _ in INPATIENT])
    h = parse_header(text, table)
    assert h.matched and h.replace and h.layout == "inpatient_rpdr" and h.lines_matched == 4
    assert text[h.end:].startswith("Progress Note")          # the header block ends after the ADM line
    assert h.harvested["lastname"] == ["Doe"] and h.harvested["firstname"] == ["Jane"]
    assert h.harvested["mrn"] == ["1234567"] and h.harvested["dob"] == ["3/4/1980"]
    assert h.harvested["town"] == ["Boston"] and h.harvested["zip"] == ["02114-1234"]
    assert h.harvested["account"] == ["98765432"]


@pytest.mark.parametrize("variant, n", [
    # the shapes of the less common variants in the pilot data
    (["MGH Neurology Clinic Doe, Jane M", "42 Yrs Old MRN: 1234567, DOB: 3/4/1980, Legal Sex: F",
      "New Bedford MA 02740-1234Acct #: 98765432", "Encounter Date: 5/6/2021"], 4),
    (["MGH Neurology Clinic Doe, Jane", "Unknown. MRN: 1234567, DOB: 3/4/1980, Legal Sex: F",
      "Boston MA 02114-1234 Encounter Date: 5/6/2021"], 3),
    (["MGH Neuro Infusion Center Doe, Jane", "42 Yrs Old MRN: 1234567, DOB: 3/4/1980, Legal Sex: F",
      "12 Elm Street Acct #: 98765432", "ADM: 1/2/2020, D/C: 1/9/2020"], 4),
])
def test_inpatient_variants(variant, n):
    h = parse_header(_note(variant), "ProgressNote")
    assert h.matched and h.replace and h.lines_matched == n
    assert h.harvested["mrn"] == ["1234567"]


def test_visit_layout_is_replaced_and_harvested():
    text = _note([x for x, _ in VISIT])
    h = parse_header(text, "VisitNote")
    assert h.matched and h.replace and h.layout == "visit_rpdr" and h.lines_matched == 5
    assert text[h.end:].startswith("Progress Note")
    assert h.harvested["lastname"] == ["Doe"] and h.harvested["firstname"] == ["Jane"]
    assert h.harvested["mrn"] == ["1234567", "1234567"] and h.harvested["dob"] == ["3/4/1980"]
    assert "provider" not in h.harvested       # the MRN line's label is unknown: never harvest it as a name


VISIT_MIDDLE_NAME = [
    ("Doe, Jane Marie (MR # 1234567) DOB: Encounter Date: 5/6/2021",
     "Aa+, Aa+ Aa+ (MR # 9+) DOB: Encounter Date: 9+/9+/9+"),
    ("5/6/2021", "9+/9+/9+"),
    ("Jane Marie Doe Gender: Female DOB:", "Aa+ Aa+ Aa+ Aa+: Female DOB:"),
    ("5/6/2021 10:30 AM Office Visit 5/6/2021", "9+/9+/9+ 9+:9+ AM Aa+ Visit 9+/9+/9+"),
    ("MRN: 1234567 Provider: Smith, John,", "MRN: 9+ Aa+: Aa+, Aa+,"),
]


def test_visit_variant_with_middle_name_empty_dob_and_date_first():
    """3 pilot visit notes (11.11.26): full middle name, empty DOB, the lone date before the name/sex line."""
    for line, seen in VISIT_MIDDLE_NAME:
        assert shape(line) == seen, line
    text = _note([x for x, _ in VISIT_MIDDLE_NAME], banner=REAL_BANNER)
    h = parse_header(text, "VisitNote")
    assert h.matched and h.replace and h.layout == "visit_rpdr" and h.lines_matched == 5
    assert text[h.end:].startswith("Progress Note")
    assert h.harvested["middle"] == ["Marie"] and "dob" not in h.harvested


@pytest.mark.parametrize("lines, seen, n", [
    # only the name/MRN line, then text (no date-and-visit line)
    (["Doe, Jane M (MRN 1234567) Encounter Date: 5/6/2021"],
     ["Aa+, Aa+ A (MRN 9+) Encounter Date: 9+/9+/9+"], 1),
    # the date-and-visit line without a time
    (["Doe, Jane (MR # 1234567) DOB: 3/4/1980 Encounter Date: 5/6/2021", "Jane Doe Gender: Female DOB:",
      "5/6/2021 Telephone 5/6/2021", "MRN: 1234567 Provider: Smith, John"],
     ["Aa+, Aa+ (MR # 9+) DOB: 9+/9+/9+ Encounter Date: 9+/9+/9+", "Aa+ Aa+ Aa+: Female DOB:",
      "9+/9+/9+ Aa+ 9+/9+/9+", "MRN: 9+ Aa+: Aa+, Aa+"], 4),
])
def test_last_two_pilot_visit_variants(lines, seen, n):
    assert [shape(x) for x in lines] == seen
    text = _note(lines, banner=REAL_BANNER)
    h = parse_header(text, "VisitNote")
    assert h.matched and h.replace and h.layout == "visit_rpdr" and h.lines_matched == n
    assert text[h.end:].startswith("Progress Note")


def test_a_clinical_line_starting_with_a_date_stays_in_the_note():
    text = _note(["Doe, Jane (MR # 1234567) DOB: 3/4/1980 Encounter Date: 5/6/2021",
                  "5/6/2021 MRI brain showed a new enhancing lesion"], banner=REAL_BANNER)
    h = parse_header(text, "VisitNote")
    assert h.matched and h.lines_matched == 1
    assert text[h.end:].startswith("5/6/2021 MRI brain")


def test_radiology_report_without_exam_title():
    text = "Reason for exam (eg ICD code): Myelitis\r\nHISTORY: Weakness.\r\nFINDINGS: normal.\r\n"
    assert shape(text.splitlines()[0]) == "Reason a+ a+ (a+ A+ a+): Aa+"
    h = parse_header(text, "Radiology")
    assert h.matched and not h.replace and h.layout == "radiology"


def test_harvested_middle_names_are_scrubbed():
    from nmosd.deid.known_identifiers import from_harvest
    s = from_harvest({"lastname": ["DOE"], "firstname": ["JANE"], "middle": ["MARIE", "Q"]})
    assert s.middle == {"Marie"} and s.last == {"Doe"} and s.first == {"Jane"}


def test_visit_layout_optional_lines():
    """Without the name/sex line, the MRN line and the ID line, only the two required lines remain."""
    text = _note(["Doe, Jane M (MR # 1234567) DOB: 3/4/1980 Encounter Date: 5/6/2021",
                  "5/6/2021 2:15 PM Telephone encounter 5/6/2021"])
    h = parse_header(text, "VisitNote")
    assert h.matched and h.replace and h.lines_matched == 2 and text[h.end:].startswith("Progress Note")


def test_a_wrong_line_order_is_not_a_header():
    text = _note(["Boston MA 02114-1234 Acct #: 98765432", "MGH Neurology Clinic Doe, Jane"])
    h = parse_header(text, "ProgressNote")
    assert not h.matched and not h.replace
