import os

from main import FileResult, Outcome, write_report


def test_write_report_counts_and_sections(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(os.path.join(input_dir, "a.jpg"), Outcome.UPDATED, "mtime, EXIF"),
        FileResult(os.path.join(input_dir, "b.jpg"), Outcome.GOOD_EXIF, "fine"),
        FileResult(os.path.join(input_dir, "c.jpg"), Outcome.NO_JSON),
        FileResult(os.path.join(input_dir, "d.jpg"), Outcome.ERROR, error="oops"),
    ]
    report_path = os.path.join(input_dir, "timestamper_report.md")

    write_report(results, report_path, input_dir)

    text = open(report_path).read()
    assert "| Updated (EXIF/mtime written from JSON) | 1 |" in text
    assert "| Good EXIF (not changed) | 1 |" in text
    assert "| No JSON sidecar found | 1 |" in text
    assert "| Errors | 1 |" in text
    assert "**Total** | **4**" in text
    assert "a.jpg" in text
    assert "oops" in text
