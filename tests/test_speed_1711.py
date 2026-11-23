"""Speed-ups of 17.11.26 (synthetic data only):

* #3 a larger answer limit (llm.num_predict) is part of the run hash, except at the old 1024;
* #4 several questions at once (llm.parallel): the database ends up exactly as after a one-at-a-time run;
* #5 `skip_repeated_lines`: list lines already read in an earlier note of the patient are not read again, prose is
  never cut, and a note with nothing new for the group is not read at all.
"""

from __future__ import annotations

import io
import random
import sqlite3
import time

from nmosd.extract.dedupe import line_keys, repeated_lines
from nmosd.extract.ollama_client import OllamaClient
from nmosd.extract.run import plan_work, run_config_hash, run_extraction
from nmosd.extract.synthetic_oracle import SyntheticOracleClient
from nmosd.store.db import connect
from nmosd.textproc.view import TextView

NOTE_1 = """Clinic note

Past Medical History:
Migraine
Osteopenia
Hypertension

HPI: She reports numbness in both legs since the spring and was
seen by her primary care doctor, who started gabapentin.
"""
NOTE_2 = """Clinic note

Past Medical History:
Migraine
Osteopenia
Hypertension
PTSD

HPI: She reports numbness in both legs since the spring and was
treated with steroids in the emergency department last week.
"""
NOTE_3 = """Clinic note

Past Medical History:
Migraine
Osteopenia
Hypertension
PTSD
"""


def test_repeated_list_lines_go_and_prose_stays_whole():
    seen = line_keys(TextView.full(NOTE_1))
    v2 = TextView.full(NOTE_2)
    drop, new, n = repeated_lines(v2, seen)
    left = v2.remove(drop).text
    assert new and n == 4, "the title line and the three repeated conditions"
    assert "PTSD" in left and "Migraine" not in left and "Hypertension" not in left
    assert "Past Medical History:" in left, "the list's title stays"
    # the HPI paragraph has a repeated first line but a new second one: prose is never cut
    assert "She reports numbness in both legs since the spring and was" in left and "steroids" in left
    seen |= line_keys(v2)
    drop3, new3, _ = repeated_lines(TextView.full(NOTE_3), seen)
    assert not new3 and TextView.full(NOTE_3).remove(drop3).text.strip() == "", "every line was read before"


def _deid_db(path, notes: list[str]):
    con = connect(path, schema="deid")
    con.execute("INSERT INTO patients (study_id) VALUES ('NMO-TEST01')")
    for i, text in enumerate(notes, 1):
        con.execute("INSERT INTO documents (doc_id, study_id, source_table, report_date, report_description, "
                    "relevance_class, text) VALUES (?,?,?,?,?,?,?)",
                    (f"DOC-{i:010d}", "NMO-TEST01", "notes", f"2020-0{i}-01", "Progress Note", "clinical_note", text))
    con.commit()
    return con


def test_plan_reads_only_what_is_new_for_the_groups_that_skip_repeats(tmp_path):
    con = _deid_db(tmp_path / "deid.sqlite", [NOTE_1, NOTE_2, NOTE_3])
    work, _, stats = plan_work(con, ["comorbidity"], 20000, 400, {"low_value"})
    con.close()
    docs = [w["doc"]["doc_id"] for w in work]
    assert docs == ["DOC-0000000001", "DOC-0000000002"], "note 3 has nothing new for the conditions list"
    second = work[1]["chunks"][0].text
    assert "PTSD" in second and "Osteopenia" not in second
    assert stats["comorbidity:only_repeats"] == 1 and stats["comorbidity:repeated_lines"] >= 3


def test_answer_limit_is_in_the_run_hash_except_at_the_old_value(pipeline):
    class C:
        model, num_ctx = "synthetic", 8192

        def digest(self):
            return None

    s = pipeline["settings"]
    a, b = C(), C()
    a.num_predict, b.num_predict = 1024, 3072
    old = run_config_hash(C(), {"x": 1}, s, "treatment")   # a client without num_predict: as before 17.11.26
    assert run_config_hash(a, {"x": 1}, s, "treatment") == old
    assert run_config_hash(b, {"x": 1}, s, "treatment") != old


class _Parallel(SyntheticOracleClient):
    """The synthetic oracle, slowed down at random so answers come back out of order, with a clone per worker."""

    def __init__(self, annotations):
        super().__init__(annotations)
        self.annotations = annotations

    def clone(self):
        return _Parallel(self.annotations)

    def generate_json(self, system, prompt, schema):
        time.sleep(random.random() / 200)
        return super().generate_json(system, prompt, schema)


class _Settings:
    def __init__(self, base, **over):
        self.base, self.over = base, over

    def get(self, key, default=None):
        return self.over.get(key, self.base.get(key, default))

    def path(self, key):
        return self.base.path(key)


def test_parallel_run_writes_the_same_database_as_one_at_a_time(pipeline, tmp_path):
    out = tmp_path / "deid" / "parallel.sqlite"
    buf = io.StringIO()
    s = _Settings(pipeline["settings"], **{"llm.parallel": 3})
    assert run_extraction(pipeline["deid"], out, s, client=_Parallel(pipeline["synthetic"] / "annotations.json"),
                          stdout=buf) == 0
    assert "3 questions at a time" in buf.getvalue() and "(3 at a time)" in buf.getvalue()
    sig = ("SELECT doc_id, field_group, field, value, event_date, quote, char_start FROM evidence "
           "WHERE run_id IN (SELECT run_config_hash FROM done) ORDER BY rowid")
    c1, c2 = sqlite3.connect(out), sqlite3.connect(pipeline["extract"])
    try:
        assert c1.execute(sig).fetchall() == c2.execute(sig).fetchall(), "same rows, in the same order"
        done = "SELECT run_config_hash, doc_id, field_group FROM done ORDER BY 1, 2, 3"
        assert c1.execute(done).fetchall() == c2.execute(done).fetchall()
    finally:
        c1.close()
        c2.close()


def test_ollama_client_clone_has_its_own_connection():
    c = OllamaClient()
    d = c.clone()
    assert d.client is not c.client and (d.model, d.num_ctx, d.num_predict) == (c.model, c.num_ctx, c.num_predict)
    injected = OllamaClient(_client=object())
    assert injected.clone().client is injected.client, "a test's injected client is shared"


class _Gone(_Parallel):
    """Ollama has gone away: every question fails."""

    def clone(self):
        return _Gone(self.annotations)

    def generate_json(self, system, prompt, schema):
        from nmosd.extract.ollama_client import LLMResult

        time.sleep(random.random() / 200)
        return LLMResult(content="", parsed=None, model=self.model, digest=None, options={},
                         error="ConnectError: [Errno 61] Connection refused")


def test_parallel_run_still_stops_when_ollama_goes_away(pipeline, tmp_path):
    out = tmp_path / "deid" / "gone.sqlite"
    buf = io.StringIO()
    s = _Settings(pipeline["settings"], **{"llm.parallel": 4})
    rc = run_extraction(pipeline["deid"], out, s, groups=["treatment"],
                        client=_Gone(pipeline["synthetic"] / "annotations.json"), stdout=buf)
    text = buf.getvalue()
    assert rc == 1 and "Stopped: the model did not answer 3 documents in a row" in text
    con = sqlite3.connect(out)
    assert con.execute("SELECT COUNT(*) FROM done").fetchone()[0] == 0
    con.close()
