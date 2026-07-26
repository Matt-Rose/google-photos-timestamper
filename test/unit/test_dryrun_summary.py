from main import (
    FileResult,
    Outcome,
    _describe_geo_anomaly,
    _describe_timestamp_anomaly,
    write_dryrun_summary,
)


def test_describe_timestamp_anomaly_none_for_expected_shape():
    assert _describe_timestamp_anomaly({"photoTakenTime": {"timestamp": "100"}}) is None


def test_describe_timestamp_anomaly_flags_missing_photo_taken_time():
    issue = _describe_timestamp_anomaly({"title": "x"})
    assert "no `photoTakenTime` key" in issue


def test_describe_timestamp_anomaly_flags_renamed_sub_key():
    issue = _describe_timestamp_anomaly({"photoTakenTime": {"time-stamp": "100"}})
    assert "no `timestamp` key" in issue
    assert "time-stamp" in issue


def test_describe_timestamp_anomaly_flags_non_numeric_value():
    issue = _describe_timestamp_anomaly({"photoTakenTime": {"timestamp": "soon"}})
    assert "isn't a number" in issue


def test_describe_geo_anomaly_none_when_absent():
    # No location block at all is normal, not an anomaly.
    assert _describe_geo_anomaly({"photoTakenTime": {"timestamp": "100"}}) is None


def test_describe_geo_anomaly_none_for_expected_shape():
    data = {"geoDataExif": {"latitude": 1.0, "longitude": 2.0}}
    assert _describe_geo_anomaly(data) is None


def test_describe_geo_anomaly_flags_renamed_sub_key():
    data = {"geoDataExif": {"lat": 1.0, "lon": 2.0}}
    issue = _describe_geo_anomaly(data)
    assert "geoDataExif" in issue
    assert "latitude/longitude" in issue


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
            json_path=f"{input_dir}/a.jpg.json",
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
            json_path=f"{input_dir}/b.jpg.json",
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


def test_dryrun_summary_expectation_checks_clean_when_shapes_match(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(
            f"{input_dir}/a.jpg",
            Outcome.UPDATED,
            "mtime, EXIF timestamp",
            json_path=f"{input_dir}/a.jpg.json",
            sidecar_data={"photoTakenTime": {"timestamp": "100"}},
        ),
    ]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "## Sidecar expectation checks" in text
    assert "No shape anomalies found" in text


def test_dryrun_summary_expectation_checks_flag_schema_drift(tmp_path):
    input_dir = str(tmp_path)
    results = [
        FileResult(
            f"{input_dir}/a.jpg",
            Outcome.UPDATED,
            "mtime",
            json_path=f"{input_dir}/AlbumA/a.jpg.json",
            sidecar_data={"photoTakenTime": {"time-stamp": "100"}},
        ),
        FileResult(
            f"{input_dir}/b.jpg",
            Outcome.UPDATED,
            "mtime",
            json_path=f"{input_dir}/AlbumB/b.jpg.json",
            sidecar_data={
                "photoTakenTime": {"timestamp": "200"},
                "geoDataExif": {"lat": 1.0, "lon": 2.0},
            },
        ),
    ]
    report_path = f"{input_dir}/report.md"

    write_dryrun_summary(results, report_path, input_dir)
    text = open(report_path).read()

    assert "**Timestamp shape issues:**" in text
    assert "no `timestamp` key" in text
    assert "time-stamp" in text
    assert "AlbumA/a.jpg.json" in text

    assert "**GPS shape issues:**" in text
    assert "geoDataExif" in text
    assert "AlbumB/b.jpg.json" in text


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
