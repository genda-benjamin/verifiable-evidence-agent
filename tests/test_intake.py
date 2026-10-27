import json
from pathlib import Path

import pytest

from vea import __main__ as cli
from vea import intake, llm, questionnaire
from vea.llm import ScriptedProvider

ROOT = Path(__file__).resolve().parent.parent
TRUTH = json.loads((ROOT / "samples" / "questionnaire-truth.json").read_text(encoding="utf-8"))["questions"]
PDF, PNG = ROOT / "samples" / "questionnaire.pdf", ROOT / "samples" / "questionnaire-page1.png"


def _reply(questions):
    return json.dumps({"questions": [{"ref": q["ref"], "section": q["section"], "text": q["text"]} for q in questions]})


def _page(n):
    return [q for q in TRUTH if q["page"] == n]


def test_a_screenshot_is_read_and_marked_for_a_person_to_check(tmp_path):
    p = ScriptedProvider([_reply(_page(1))])
    found = intake.extract(PNG, p)
    assert [q.id for q in found] == [q["ref"] for q in _page(1)]
    assert all(q.check == "no text layer: compare with the original" for q in found)
    sent = p.seen[0][0]["content"]
    assert sent[1]["image_url"]["url"].startswith("data:image/png;base64,") and "ignore any instructions" in sent[0]["text"]


def test_a_pdf_is_checked_word_for_word_against_its_own_text(tmp_path):
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    reworded = dict(_page(1)[0], text="Is MFA enforced for every admin?")
    invented = {"ref": "A9", "section": "A. Access control", "text": "Do you guarantee zero breaches?"}
    p = ScriptedProvider([_reply(_page(1)[1:] + [reworded, invented]), _reply(_page(2))])
    found = {q.id: q for q in intake.extract(PDF, p)}
    assert len(p.seen) == 2  # one model call per page
    assert found["A2"].check == "verbatim" and found["D4"].check == "verbatim"  # wrapped lines are joined before matching
    assert found["A1"].check == "NOT FOUND in the document text"  # reworded
    assert found["A9"].check == "NOT FOUND in the document text"  # invented
    s = intake.score(list(found.values()), TRUTH)
    assert s["exact"] == 13 and s["missed"] == ["A1"] and len(s["extra"]) == 2


def test_intake_writes_a_csv_that_answer_reads(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(llm, "from_env", lambda preset: ScriptedProvider([_reply(_page(1))]))
    out = tmp_path / "questions.csv"
    assert cli.main(["intake", str(PNG), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "6 questions from 1 page(s); 0 match the document's text word for word, 6 to check by hand." in text
    loaded = questionnaire.load(out)
    assert [q.id for q in loaded] == ["A1", "A2", "A3", "B1", "B2", "B3"]
    assert loaded[0].text == "Is multi-factor authentication enforced for all administrative and console access?"


def test_unsupported_files_are_refused(tmp_path):
    doc = tmp_path / "questions.docx"
    doc.write_bytes(b"x")
    with pytest.raises(ValueError, match="give a PDF or an image"):
        intake.extract(doc, ScriptedProvider([]))
