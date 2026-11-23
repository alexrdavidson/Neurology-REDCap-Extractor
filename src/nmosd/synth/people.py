"""Five fictional NMOSD patients (plus one non-pilot control) with full clinical histories.

Every name, number, address and date here is invented. Codes follow the REDCap codebook for
PID 47765 (maintenance_therapy, relapse_phenotype, acute_rx, reason_for_stopping_tx, ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Relapse:
    onset: date
    precision: str            # day | month | season | year  (how the notes usually describe it)
    estimate: int             # REDCap date_attack_estimate (1-4)
    phenotype: list[int]      # relapse_phenotype codes
    pheno_text: str           # narrative description
    pheno_short: str          # Dx-line abbreviation
    admitted: int             # 1 / 0
    admit: date | None = None
    discharge: date | None = None
    hospital: str | None = None   # where admitted / first seen
    acute_rx: list[int] = field(default_factory=list)
    acute_text: str = ""
    mri_new: int | None = None
    mri_text: str = ""
    mri_region: str = "BRAIN"     # BRAIN | CERVICAL SPINE | THORACIC SPINE | ORBITS


@dataclass
class Treatment:
    code: int                 # maintenance_therapy
    name: str
    start: date
    start_est: int
    stop: date | None = None
    stop_est: int | None = None
    reason_stop: int | None = None
    reason_text: str = ""
    dose_text: str = ""
    planned_note_date: date | None = None   # a note before `start` says "plan to start ..."


@dataclass
class AntibodyTest:
    test: str                 # aqp4 | mog
    drawn: date
    positive: bool
    lab: str
    titer: str | None = None


@dataclass
class Person:
    key: str
    empi: str
    first: str
    middle: str
    last: str
    preferred: str | None
    sex: str
    dob: date
    mrns: dict[str, str]
    town: str
    zip5: str
    zip4: str
    street: str
    phone: str
    email: str
    relatives: list[tuple[str, str]]          # (relation, full name)
    race1: str
    race_group: str
    ethnic_group: str
    language: str
    religion: str
    marital: str
    vital_status: str
    date_of_death: date | None
    otherdx: int
    dx_other: str
    dx_date: date
    dx_est: int
    dx_phrase: str
    relapses: list[Relapse]
    treatments: list[Treatment]
    antibodies: list[AntibodyTest]
    first_seen: date
    last_seen: date
    pilot: bool = True
    notes_profile: str = "standard"           # standard | elderly | control
    pregnancy: dict | None = None
    planned_only: list[tuple[str, date]] = field(default_factory=list)  # (drug, note date) never started

    @property
    def title(self) -> str:
        return "Ms." if self.sex == "Female" else "Mr."

    @property
    def pronoun(self) -> str:
        return "she" if self.sex == "Female" else "he"

    @property
    def possessive(self) -> str:
        return "her" if self.sex == "Female" else "his"

    @property
    def noun(self) -> str:
        return "woman" if self.sex == "Female" else "man"

    def age_on(self, d: date) -> int:
        return d.year - self.dob.year - ((d.month, d.day) < (self.dob.month, self.dob.day))

    @property
    def mrn(self) -> str:
        return next(iter(self.mrns.values()))

    @property
    def dx_category(self) -> str:
        """Diagnosis-check category (config/eligibility.yaml). P3 has MOGAD, not NMOSD: excluded at the check."""
        return "mogad" if "MOGAD" in self.dx_phrase else "nmosd"

    @property
    def dx_word(self) -> str:
        return {"mogad": "MOGAD", "nmosd": "NMOSD"}[self.dx_category]


CLINICIANS = {
    "neuro": "Theodora Blackwood, MD",
    "np": "Cornelius Fairweather, NP",
    "resident": "Seraphina Goldwyn, MD",
    "hospitalist": "Percival Oakhart, MD",
    "radiologist": "Imogen Thistlewood, MD",
    "pathologist": "Lysander Crowe, MD",
    "nurse": "Wilhelmina Starling, RN",
    "lactation": "Octavia Fenwright, IBCLC",
    "pcp": "Ambrose Pennywhistle, MD",
    "ed": "Barnaby Quenneville, MD",
}
PCP_PRACTICE = "Lakeside Family Practice"
OUTSIDE_HOSPITALS = ["Wexley Falls Community Hospital", "St. Brendan's Medical Center", "Abbotsford Regional Hospital"]


def build_people() -> list[Person]:
    p1 = Person(
        key="P1", empi="100004417", first="Rosalind", middle="M", last="Quillfeather", preferred="Roz",
        sex="Female", dob=date(1988, 3, 4), mrns={"MGH": "4417263", "BWH": "10982734"},
        town="Wexley Falls", zip5="01987", zip4="2231", street="14 Juniper Hollow Road",
        phone="(978) 555-0193", email="roz.quillfeather@examplemail.com",
        relatives=[("husband", "Bartholomew Quillfeather"), ("mother", "Gwendolyn Ashby"), ("sister", "Hope Ashby")],
        race1="White", race_group="White", ethnic_group="Non Hispanic", language="English",
        religion="Catholic", marital="Married", vital_status="Not reported as deceased", date_of_death=None,
        otherdx=1, dx_other="", dx_date=date(2016, 9, 14), dx_est=1,
        dx_phrase="AQP4-IgG seropositive NMOSD",
        relapses=[
            Relapse(date(2016, 7, 15), "season", 3, [1], "painful vision loss in the left eye consistent with optic neuritis",
                    "ON OS", 0, hospital=OUTSIDE_HOSPITALS[0], acute_rx=[1],
                    acute_text="IV methylprednisolone 1 g daily for 5 days", mri_new=0,
                    mri_text="no new T2 lesions in the brain; enhancement of the left optic nerve", mri_region="ORBITS"),
            Relapse(date(2018, 3, 4), "day", 1, [2], "bilateral leg numbness and weakness with urinary retention",
                    "LETM C2-C5", 1, admit=date(2018, 3, 5), discharge=date(2018, 3, 12), hospital="MGH",
                    acute_rx=[1, 2], acute_text="IV methylprednisolone followed by 5 sessions of PLEX", mri_new=1,
                    mri_text="longitudinally extensive T2 hyperintense lesion from C2-C5 with patchy enhancement",
                    mri_region="CERVICAL SPINE"),
            Relapse(date(2020, 11, 20), "day", 1, [1], "blurred vision and pain on eye movement in the right eye",
                    "ON OD", 0, acute_rx=[1], acute_text="IV methylprednisolone 1 g daily for 5 days at the infusion center",
                    mri_new=1, mri_text="new T2 hyperintensity and enhancement of the right optic nerve",
                    mri_region="ORBITS"),
        ],
        treatments=[
            Treatment(7, "azathioprine", date(2016, 10, 3), 1, date(2018, 3, 10), 1, 1,
                      "stopped after the myelitis relapse", "150 mg daily"),
            Treatment(1, "rituximab", date(2018, 4, 16), 1, date(2021, 10, 18), 1, 2,
                      "stopped because of hypogammaglobulinemia", "1000 mg IV every 6 months",
                      planned_note_date=date(2018, 3, 28)),
            Treatment(5, "inebilizumab", date(2022, 2, 7), 1, None, None, None, "", "300 mg IV every 6 months"),
        ],
        antibodies=[AntibodyTest("aqp4", date(2016, 9, 1), True, "Mayo Clinic", "1:100"),
                    AntibodyTest("mog", date(2016, 9, 1), False, "Mayo Clinic")],
        first_seen=date(2016, 9, 14), last_seen=date(2026, 5, 11),
    )
    p2 = Person(
        key="P2", empi="100007731", first="Adaeze", middle="N", last="Okafor-Whitlock", preferred="Daze",
        sex="Female", dob=date(1992, 11, 23), mrns={"MGH": "5530981"},
        town="North Abbotsford", zip5="01864", zip4="1102", street="7 Larkspur Terrace",
        phone="781-555-0147", email="a.okaforwhitlock@examplemail.org",
        relatives=[("husband", "Desmond Whitlock"), ("mother", "Ngozi Okafor"), ("daughter", "Zuri Whitlock")],
        race1="Black", race_group="Black or African American", ethnic_group="Non Hispanic", language="English",
        religion="Christian", marital="Married", vital_status="Not reported as deceased", date_of_death=None,
        otherdx=5, dx_other="", dx_date=date(2014, 5, 20), dx_est=1, dx_phrase="AQP4-IgG positive NMOSD",
        relapses=[
            Relapse(date(2014, 4, 28), "day", 1, [2], "ascending numbness to the chest and leg weakness",
                    "LETM T2-T8", 1, admit=date(2014, 4, 30), discharge=date(2014, 5, 9), hospital="MGH",
                    acute_rx=[1, 2], acute_text="IV methylprednisolone and 5 sessions of PLEX", mri_new=1,
                    mri_text="longitudinally extensive T2 hyperintense lesion T2-T8", mri_region="THORACIC SPINE"),
            Relapse(date(2019, 2, 10), "month", 2, [4], "two weeks of intractable hiccups, nausea and vomiting",
                    "APS", 1, admit=date(2019, 2, 18), discharge=date(2019, 2, 23), hospital="MGH",
                    acute_rx=[1], acute_text="IV methylprednisolone 1 g daily for 5 days", mri_new=1,
                    mri_text="new T2 hyperintensity in the dorsal medulla at the area postrema", mri_region="BRAIN"),
            Relapse(date(2021, 9, 5), "day", 1, [1], "subacute vision loss in the left eye six weeks postpartum",
                    "ON OS", 0, acute_rx=[1], acute_text="oral prednisone 1250 mg daily for 5 days",
                    mri_new=0, mri_text="no new lesions", mri_region="ORBITS"),
        ],
        treatments=[
            Treatment(2, "mycophenolate", date(2014, 6, 2), 1, date(2019, 2, 20), 1, 1,
                      "stopped after the area postrema relapse", "1000 mg twice daily"),
            Treatment(3, "eculizumab", date(2019, 3, 11), 1, date(2023, 1, 9), 1, 4,
                      "switched to ravulizumab for less frequent infusions", "1200 mg IV every 2 weeks"),
            Treatment(4, "ravulizumab", date(2023, 1, 23), 1, None, None, None, "", "3300 mg IV every 8 weeks"),
        ],
        antibodies=[AntibodyTest("aqp4", date(2014, 5, 12), True, "Mayo Clinic", "1:1000")],
        first_seen=date(2014, 4, 30), last_seen=date(2026, 3, 2),
        pregnancy={"delivered": date(2021, 7, 24)},
    )
    p3 = Person(
        key="P3", empi="100002286", first="Thaddeus", middle="J", last="Brightwater", preferred="Tad",
        sex="Male", dob=date(1979, 6, 17), mrns={"MGH": "0662918", "BWH": "20334187"},
        town="Port Elsinore", zip5="01944", zip4="3310", street="233 Marrowbone Lane",
        phone="617.555.0128", email="tbrightwater@examplemail.net",
        relatives=[("wife", "Marisol Brightwater"), ("brother", "Ignatius Brightwater")],
        race1="White", race_group="White", ethnic_group="Hispanic", language="English",
        religion="None", marital="Married", vital_status="Not reported as deceased", date_of_death=None,
        otherdx=3, dx_other="", dx_date=date(2021, 6, 8), dx_est=1, dx_phrase="MOG antibody-associated disease (MOGAD)",
        relapses=[
            Relapse(date(2019, 3, 12), "month", 2, [1], "painful vision loss in the right eye",
                    "ON OD", 0, hospital=OUTSIDE_HOSPITALS[1], acute_rx=[1],
                    acute_text="IV methylprednisolone for 3 days", mri_new=0, mri_text="unremarkable brain MRI",
                    mri_region="BRAIN"),
            Relapse(date(2021, 5, 24), "day", 1, [1], "bilateral simultaneous optic neuritis with severe vision loss",
                    "bilateral ON", 1, admit=date(2021, 5, 25), discharge=date(2021, 5, 28), hospital="MGH",
                    acute_rx=[1, 2], acute_text="IV methylprednisolone followed by PLEX", mri_new=1,
                    mri_text="long-segment enhancement of both optic nerves with perineural enhancement",
                    mri_region="ORBITS"),
        ],
        treatments=[
            Treatment(2, "mycophenolate", date(2021, 7, 12), 1, None, None, None, "", "1000 mg twice daily",
                      planned_note_date=None),
        ],
        antibodies=[AntibodyTest("aqp4", date(2019, 4, 2), False, "Quest"),
                    AntibodyTest("mog", date(2021, 5, 26), True, "Mayo Clinic", "1:100")],
        first_seen=date(2019, 4, 2), last_seen=date(2026, 1, 20),
        planned_only=[("rituximab", date(2021, 6, 8))],
    )
    p4 = Person(
        key="P4", empi="100009912", first="Euphemia", middle="R", last="Castellanos-Pike", preferred=None,
        sex="Female", dob=date(1934, 5, 12), mrns={"MGH": "1129384"},
        town="Lower Fenwick", zip5="02032", zip4="0004", street="9 Quarry Bend Road",
        phone="(508) 555-0176", email="ambrose.pike@examplemail.com",
        relatives=[("son", "Ambrose Pike"), ("daughter-in-law", "Philippa Pike")],
        race1="Other", race_group="Other", ethnic_group="Hispanic", language="Spanish",
        religion="Catholic", marital="Widowed", vital_status="Deceased per Social Security",
        date_of_death=date(2025, 12, 2),
        otherdx=4, dx_other="idiopathic transverse myelitis", dx_date=date(2009, 10, 15), dx_est=2,
        dx_phrase="AQP4-IgG positive NMOSD",
        relapses=[
            Relapse(date(1998, 7, 1), "year", 3, [2], "an episode of transverse myelitis with leg weakness",
                    "TM", 1, hospital=OUTSIDE_HOSPITALS[2], acute_rx=[1], acute_text="IV steroids",
                    mri_new=None, mri_text="", mri_region="THORACIC SPINE"),
            Relapse(date(2009, 9, 14), "day", 1, [2, 3], "leg weakness, numbness and diplopia",
                    "LETM+brainstem", 1, admit=date(2009, 9, 15), discharge=date(2009, 9, 29), hospital="MGH",
                    acute_rx=[1, 2], acute_text="IV methylprednisolone and PLEX", mri_new=1,
                    mri_text="longitudinally extensive cervicothoracic lesion and a new pontine lesion",
                    mri_region="CERVICAL SPINE"),
            Relapse(date(2015, 1, 8), "day", 1, [3], "vertigo, diplopia and gait ataxia",
                    "brainstem", 1, admit=date(2015, 1, 9), discharge=date(2015, 1, 14), hospital="MGH",
                    acute_rx=[1], acute_text="IV methylprednisolone 1 g daily for 5 days", mri_new=1,
                    mri_text="new enhancing T2 lesion in the left middle cerebellar peduncle", mri_region="BRAIN"),
        ],
        treatments=[
            Treatment(7, "azathioprine", date(2009, 10, 15), 2, date(2015, 1, 15), 2, 1,
                      "stopped after the brainstem relapse", "100 mg daily"),
            Treatment(1, "rituximab", date(2015, 2, 2), 1, date(2025, 6, 16), 1, 3,
                      "stopped when she transitioned to comfort-focused care", "1000 mg IV every 6 months"),
        ],
        antibodies=[AntibodyTest("aqp4", date(2009, 10, 5), True, "Mayo Clinic")],
        first_seen=date(2009, 9, 15), last_seen=date(2025, 10, 6), notes_profile="elderly",
    )
    p5 = Person(
        key="P5", empi="100005563", first="Lucinda", middle="A", last="Vantongeren", preferred="Lucy",
        sex="Female", dob=date(1999, 1, 30), mrns={"MGH": "8871102", "BWH": "31125790"},
        town="Hollisford", zip5="01810", zip4="4471", street="52 Bramblewood Drive",
        phone="978-555-0164", email="lucy.v99@examplemail.com",
        relatives=[("partner", "Ezekiel Marchetti"), ("father", "Hendrik Vantongeren")],
        race1="White", race_group="White", ethnic_group="Hispanic", language="English",
        religion="Unknown", marital="Single", vital_status="Not reported as deceased", date_of_death=None,
        otherdx=5, dx_other="", dx_date=date(2021, 6, 23), dx_est=1, dx_phrase="AQP4-IgG positive NMOSD",
        relapses=[
            Relapse(date(2021, 6, 15), "day", 1, [2], "rapidly progressive leg weakness and a T6 sensory level",
                    "LETM T4-T9", 1, admit=date(2021, 6, 16), discharge=date(2021, 6, 24), hospital="MGH",
                    acute_rx=[1, 2], acute_text="IV methylprednisolone and 5 sessions of PLEX", mri_new=1,
                    mri_text="longitudinally extensive T2 hyperintense lesion T4-T9 with enhancement",
                    mri_region="THORACIC SPINE"),
            Relapse(date(2023, 4, 30), "day", 1, [3], "new diplopia and right facial numbness",
                    "brainstem", 1, admit=date(2023, 5, 1), discharge=date(2023, 5, 5), hospital="MGH",
                    acute_rx=[1], acute_text="IV methylprednisolone 1 g daily for 5 days", mri_new=1,
                    mri_text="new T2 hyperintense lesion in the right dorsal pons", mri_region="BRAIN"),
        ],
        treatments=[
            Treatment(1, "rituximab", date(2021, 7, 19), 1, date(2022, 12, 12), 1, 1,
                      "stopped after a breakthrough brainstem relapse", "1000 mg IV every 6 months",
                      planned_note_date=date(2021, 6, 30)),
            Treatment(6, "satralizumab", date(2023, 6, 5), 1, None, None, None, "", "120 mg SC every 4 weeks"),
        ],
        antibodies=[AntibodyTest("aqp4", date(2021, 6, 17), True, "Mayo Clinic", "1:1000"),
                    AntibodyTest("mog", date(2021, 6, 17), False, "Mayo Clinic")],
        first_seen=date(2021, 6, 16), last_seen=date(2026, 4, 14),
    )
    p6 = Person(
        key="P6", empi="100001111", first="Cosimo", middle="L", last="Farthingale", preferred=None,
        sex="Male", dob=date(1970, 8, 8), mrns={"MGH": "7712300"},
        town="Eastbrook Mills", zip5="01701", zip4="5500", street="88 Tanner Row Road",
        phone="(617) 555-0109", email="cfarthingale@examplemail.com",
        relatives=[("wife", "Ottilie Farthingale")],
        race1="White", race_group="White", ethnic_group="Non Hispanic", language="English",
        religion="Unknown", marital="Married", vital_status="Not reported as deceased", date_of_death=None,
        otherdx=5, dx_other="", dx_date=date(2020, 2, 3), dx_est=1, dx_phrase="AQP4-IgG positive NMOSD",
        relapses=[Relapse(date(2020, 1, 20), "day", 1, [2], "leg numbness", "LETM", 0, acute_rx=[1],
                          acute_text="IV methylprednisolone", mri_new=1, mri_text="T2 lesion T3-T7",
                          mri_region="THORACIC SPINE")],
        treatments=[Treatment(1, "rituximab", date(2020, 3, 2), 1, None, None, None, "", "1000 mg IV every 6 months")],
        antibodies=[AntibodyTest("aqp4", date(2020, 1, 25), True, "Mayo Clinic")],
        first_seen=date(2020, 1, 25), last_seen=date(2024, 6, 1), pilot=False, notes_profile="control",
    )
    return [p1, p2, p3, p4, p5, p6]
