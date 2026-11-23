# Date shifting rules

Implementation: `src/nmosd/deid/date_shift.py`. Tests: `tests/test_phase3_deid.py`.

## One offset per patient, three derived units

Each patient gets one random offset `days`, drawn uniformly from ±[30, 365] (never 0), stored only in the
encrypted linkage key. Two derived offsets follow from it:

| Unit | Formula | Example (days = +200) |
|---|---|---|
| `days` | drawn | +200 |
| `months` | `round(days / 30.4375)`, never 0 | +7 |
| `years` | `floor((6 + months) / 12)`: the year a 1 July anchor lands in after the month shift | +1 |

## What gets shifted, and by which unit

| Written in the note | Kind | Shifted by | Example (+200 days) |
|---|---|---|---|
| `3/4/2019`, `03/04/19`, `2019-03-04`, `March 4, 2019`, `4 March 2019`, `04-MAR-2019` | full date | days | `9/20/2019` … |
| `Monday, March 4, 2019` | full date + weekday | days; weekday recomputed | `Friday, September 20, 2019` |
| `March 4` (no year) | month/day | days (anchored to the note's year) | `September 20` |
| `March 2019`, `March '19`, `9/2016`, `3/98` | month precision | months | `October 2019`, `4/2017`, `10/98` |
| `in 2016`, `(2011)`, `2016-2018`, bare years in lists (`TM 1998`) | year precision | years | `in 2017` |
| `summer 2016` | season | label by months (mid-season anchor), year by years | `winter 2017` |
| `3/19` (19 March **or** March 2019) | ambiguous short form | see below | `10/19` |
| `Christmas`, `Thanksgiving`, … | holiday | replaced by `[HOLIDAY]`, flagged | |
| `birthday` | relative anchor | flagged for review (not changed) | |

The written format is kept: zero padding, separators, 2- or 4-digit years, full or abbreviated month names,
letter case, trailing periods, ordinal suffixes (`4th` → `20th`) and commas. Date ranges written with a dash
(`10/2016-3/2018`, `3/4/2019-3/8/2019`, `3/19-5/19`) shift both ends.

### Why different units for different precisions

Each precision is shifted by a whole number of its own unit, so:

* **intervals** between dates of the same precision are preserved exactly (between precisions, to within
  half a unit);
* **re-identification is exact**: `nmosd reidentify` un-shifts each exported date by the unit matching its
  REDCap estimate code (1 and 4 → days, 2 → months, 3 → years).

A month-precision mention shifted by days would land on a different month boundary after un-shifting about
half the time. Shifting by whole months avoids this.

## The ambiguous short form `a/b` (a ≤ 12, b ≤ 31)

`3/19` can mean 19 March (month/day) or March 2019 (month/2-digit-year). The rule:

1. Shift the first number by `months`.
2. If that wraps past December or January, move the second number by the carry (±1) as well, and **flag the
   mention for review** (`ambiguous_short_date_year_wrap`).

This is exact for the month/year reading. For the month/day reading it is within ±15 days (+1 day on a wrap)
of a day shift. Forms with b > 31 (`3/98`) can only be month/year and are shifted as such.

## Values that look like dates but are never shifted

| Look-alike | Rule |
|---|---|
| Visual acuity `20/20`, `20/400`, `CF`, `HM` | first number > 12 is never a month; metric acuity (`6/12`) with an eye/acuity cue |
| Strength `5/5`, `4+/5`, `4-/5` | `x/5` with x ≤ 5, unless a strong date cue precedes ("on", "since", "started") |
| Blood pressure `120/80`, BP tables | first number > 12; 3-digit numerators never match |
| Reflexes `2+`, `2/4` | no slash; `x/4` with a reflex cue (biceps, patellar, DTR…) |
| Doses `1/2 tab`, `1 1/2 tablets` | a dose unit or "of" follows, or a bare integer precedes |
| Ratios/titres `1:100` | colon forms are never dates |
| Spinal levels `C2-C5`, `T4-T9`, `C5/6` | letter-prefixed; a range dash is only treated as a separator between date-shaped tokens |
| Ranges `3-4` | 2-part dash forms are never dates |
| Scores `EDSS 3.5`, `7/10 pain`, `2/6 murmur` | decimals never; `x/10` with a pain/score cue; `x/6` with a murmur cue |
| Doses/times next to years: `2000 units`, `at 1930` | unit follows / time cue precedes |

## Review flags (counts appear in the `deid` summary and the HTML report)

`ambiguous_short_date_year_wrap`, `holiday`, `relative_anchor` (birthday), `ner_date_unparsed` (an NER
DATE span the shifter cannot parse: redacted as `[DATE]`), `header_unmatched`.

## Ages

Ages ≥ 90 become `90+` (e.g. `91-year-old` → `90+-year-old`, `aged 93` → `aged 90+`). The structured
`Age` column is capped the same way.
