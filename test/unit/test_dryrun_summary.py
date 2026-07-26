from main import FileResult, Outcome, write_dryrun_summary


def test_dryrun_summary_counts_and_change_breakdown(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(f"{input_dir}/a.jpg", Outcome.UPDATED, "mtime, EXIF timestamp"),
        FileResult(f"{input_dir}/b.jpg", Outcome.UPDATED, "mtime, EXIF GPS"),
        FileResult(
            f"{input_dir}/c.jpg", Outcome.UPDATED, "mtime, EXIF timestamp, EXIF GPS"
        ),
        FileResult(f"{input_dir}/d.jpg", Outcome.GOOD_EXIF, "mtime synced"),
        FileResult(f"{input_dir}/e.png", Outcome.NO_JSON),
        FileResult(f"{input_dir}/f.mp4", Outcome.ERROR, error="boom"),
    ]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "DRY RUN" in text
    assert "| Would update (EXIF/mtime from JSON) | 3 |" in text
    assert "| Good EXIF already (no change needed) | 1 |" in text
    assert "| No JSON sidecar found | 1 |" in text
    assert "| Errors | 1 |" in text
    assert "**Total** | **6**" in text

    assert "| Timestamp only | 1 |" in text
    assert "| GPS only | 1 |" in text
    assert "| Timestamp + GPS | 1 |" in text


def test_dryrun_summary_breaks_down_by_file_extension(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(f"{input_dir}/a.jpg", Outcome.UPDATED, "mtime, EXIF timestamp"),
        FileResult(f"{input_dir}/b.jpg", Outcome.NO_JSON),
        FileResult(f"{input_dir}/c.mp4", Outcome.NO_JSON),
        FileResult(f"{input_dir}/noext", Outcome.NO_JSON),
    ]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "## By file type" in text
    assert "| `.jpg` | 1 | 0 | 1 | 0 | 2 |" in text
    assert "| `.mp4` | 0 | 0 | 1 | 0 | 1 |" in text
    assert "| `(no extension)` | 0 | 0 | 1 | 0 | 1 |" in text


def test_dryrun_summary_groups_no_json_by_extension_with_capped_examples(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(f"{input_dir}/img{i}.heic", Outcome.NO_JSON) for i in range(8)
    ]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "## No JSON sidecar found — by file type" in text
    assert "- `.heic`: 8 files" in text
    assert "and 3 more" in text  # 8 files, only 5 examples shown


def test_dryrun_summary_groups_errors_by_message_with_capped_examples(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(f"{input_dir}/a.jpg", Outcome.ERROR, error="disk full"),
        FileResult(f"{input_dir}/b.jpg", Outcome.ERROR, error="disk full"),
        FileResult(f"{input_dir}/c.jpg", Outcome.ERROR, error="permission denied"),
    ]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "## Errors — grouped by message" in text
    assert "- **2×** disk full" in text
    assert "- **1×** permission denied" in text


def test_dryrun_summary_surveys_sidecar_fields_not_already_used(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(
            f"{input_dir}/a.jpg",
            Outcome.UPDATED,
            "mtime, EXIF timestamp",
            sidecar_data={
                "photoTakenTime": {"timestamp": "100"},
                "description": "A lovely sunset",
                "favorited": True,
                "people": [{"name": "Alice"}],
            },
        ),
        FileResult(
            f"{input_dir}/b.jpg",
            Outcome.UPDATED,
            "mtime, EXIF timestamp",
            sidecar_data={
                "photoTakenTime": {"timestamp": "200"},
                "description": "",
                "favorited": False,
            },
        ),
    ]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "## Sidecar fields seen" in text
    assert "Surveyed 2 JSON sidecars" in text
    # photoTakenTime is already used by the script.
    assert "| `photoTakenTime` | 2/2 | 2 | yes |" in text
    # description is present in both but only meaningful (non-empty) in one.
    assert "| `description` | 2/2 | 1 |  |" in text
    # favorited is present in both but only true in one.
    assert "| `favorited` | 2/2 | 1 |  |" in text
    # people only appears once but is meaningful.
    assert "| `people` | 1/2 | 1 |  |" in text


def test_dryrun_summary_omits_sections_with_nothing_to_report(tmp_path):
    input_dir = str(tmp_path)
    results = [FileResult(f"{input_dir}/a.jpg", Outcome.GOOD_EXIF, "mtime synced")]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "## What would change" not in text
    assert "## No JSON sidecar found" not in text
    assert "## Errors" not in text
    assert "## Sidecar fields seen" not in text
