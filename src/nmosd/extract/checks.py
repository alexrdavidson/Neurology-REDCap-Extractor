"""Code checks on what the model extracted (18.11.26): fixed rules, no model, applied when the results are combined.

The pilot's reference review found values the notes do not support: a drug the quoted sentence never names
(azathioprine from "Continue duloxetine", a drug guessed from a dose line), and the end of one infusion read as the end
of the treatment. A check compares a value with the passage it was taken from and gives the same answer every time.
A value that fails is not removed silently: it is left out of the dates and marked for the reviewer, with the reason.

* `drug_named`: the drug of a treatment mention is named (generic or brand name) in its quote, or within
  `PASSAGE_MARGIN` characters of it in the note ("Med: Ravulizumab" two lines above "Dose: 3,000 mg");
* `says_first_dose`: the quote speaks of starting the drug, not of one more administration;
* `infusion_end`: a "stop" on the day of the note, with a "start" of the same drug on that day in the same note, is
  the end of that day's infusion.
"""

from __future__ import annotations

import re

PASSAGE_MARGIN = 250

# REDCap `maintenance_therapy` code → the names a passage may use for it. 15 (Other) is checked against the name
# the model gave (`other_maintenance_tx`).
DRUG_NAMES: dict[str, tuple[str, ...]] = {
    "1": ("rituximab", "rituxan", "truxima", "ruxience", "riabni", "ritux", "RTX"),
    "2": ("mycophenolate", "cellcept", "myfortic", "MMF"),
    "3": ("eculizumab", "soliris"),
    "4": ("ravulizumab", "ultomiris"),
    "5": ("inebilizumab", "uplizna"),
    "6": ("satralizumab", "enspryng"),
    "7": ("azathioprine", "imuran", "azasan", "AZA"),
    "8": ("tocilizumab", "actemra"),
    "9": ("interferon", "avonex", "rebif", "betaseron", "plegridy", "extavia", "copaxone", "glatiramer", "glatopa"),
    "10": ("ofatumumab", "kesimpta", "ocrelizumab", "ocrevus"),
    "11": ("fumarate", "tecfidera", "vumerity", "bafiertam", "fingolimod", "gilenya", "siponimod", "mayzent",
           "ozanimod", "zeposia", "ponesimod", "ponvory", "teriflunomide", "aubagio", "cladribine", "mavenclad"),
    "12": ("natalizumab", "tysabri"),
    "13": ("methotrexate", "MTX"),
    "14": ("cyclophosphamide", "cytoxan"),
    "16": ("prednisone", "prednisolone", "methylprednisolone", "dexamethasone", "medrol", "deltasone", "steroid"),
}


def _name_rx(names: tuple[str, ...]) -> re.Pattern:
    """Whole words: a name starts a word and may carry an ending ("steroids", "rituximab-abbs"); an abbreviation
    (written in capitals above) must stand alone."""
    parts = [rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])" if n.isupper() else rf"(?<![A-Za-z]){re.escape(n)}" for n in names]
    return re.compile("|".join(parts), re.I)


_DRUG_RX = {code: _name_rx(names) for code, names in DRUG_NAMES.items()}


def names_drug(text: str | None, code: str | None, other_name: str | None = None) -> bool:
    """True when `text` names the drug with REDCap code `code` (for 15, Other: the name the model gave)."""
    if not text or code in (None, ""):
        return False
    code = str(code)
    if code in _DRUG_RX:
        return bool(_DRUG_RX[code].search(text))
    words = [w for w in re.findall(r"[A-Za-z]{4,}", other_name or "")]
    return any(re.search(rf"(?<![A-Za-z]){re.escape(w)}", text, re.I) for w in words)


def passage(note_text: str | None, start: int | None, end: int | None, margin: int = PASSAGE_MARGIN) -> str:
    if not note_text or start is None:
        return ""
    end = end if end is not None and end >= start else start
    return note_text[max(0, start - margin):min(len(note_text), end + margin)]


def drug_named(code, quote: str | None, note_text: str | None = None, start: int | None = None, end: int | None = None,
               other_name: str | None = None) -> bool:
    """The drug is named in the quote, or close to it in the note."""
    return names_drug(quote, code, other_name) or names_drug(passage(note_text, start, end), code, other_name)


FIRST_DOSE_RX = re.compile(r"(?i)\b(start(?:ed|ing|s)?|initiat\w*|beg[ai]n\w*|commenc\w*|first (?:dose|infusion|cycle)|"
                           r"loading|induction|dose\s*#?\s*1\b|#\s*1\b|1st (?:dose|infusion))")


def says_first_dose(quote: str | None) -> bool:
    return bool(FIRST_DOSE_RX.search(quote or ""))


def mark_unnamed(items: list, texts: dict[str, str]) -> int:
    """Set `unnamed` on the treatment mentions whose passage does not name their drug. `texts`: {doc_id: note text}.
    Returns how many were marked."""
    n = 0
    for i in items:
        if i.group not in ("treatment", "treatment_detail"):
            continue
        code = i.fields.get("maintenance_therapy")
        if code in (None, "", "UNK", "not_established"):
            continue
        end = i.char_start + len(i.quote) if i.char_start is not None and i.quote else None
        i.unnamed = not drug_named(code, i.quote, texts.get(i.doc_id), i.char_start, end,
                                   i.fields.get("other_maintenance_tx"))
        n += int(i.unnamed)
    return n
