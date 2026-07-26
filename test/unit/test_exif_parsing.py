from datetime import datetime, timedelta, timezone

from main import (
    RECENT_DAYS,
    get_geo_from_json,
    get_signed_gps,
    gps_looks_bad,
    parse_exif_datetime,
    timestamp_looks_bad,
)


def test_parse_exif_datetime_colon_format():
    assert parse_exif_datetime("2020:01:15 10:30:00") == datetime(2020, 1, 15, 10, 30)


def test_parse_exif_datetime_dash_format():
    assert parse_exif_datetime("2020-01-15 10:30:00") == datetime(2020, 1, 15, 10, 30)


def test_parse_exif_datetime_invalid_returns_none():
    assert parse_exif_datetime("not a date") is None


def test_parse_exif_datetime_none_returns_none():
    assert parse_exif_datetime(None) is None


def test_get_signed_gps_applies_south_and_west_refs():
    tags = {
        "GPSLatitude": 51.5,
        "GPSLatitudeRef": "S",
        "GPSLongitude": 0.1,
        "GPSLongitudeRef": "W",
    }
    lat, lon = get_signed_gps(tags)
    assert lat == -51.5
    assert lon == -0.1


def test_get_signed_gps_defaults_to_north_east_when_ref_missing():
    tags = {"GPSLatitude": 51.5, "GPSLongitude": 0.1}
    assert get_signed_gps(tags) == (51.5, 0.1)


def test_get_signed_gps_missing_coords_returns_none_none():
    assert get_signed_gps({}) == (None, None)


def test_timestamp_looks_bad_for_recent_timestamp():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert timestamp_looks_bad(now) is True


def test_timestamp_looks_bad_false_for_old_timestamp():
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=RECENT_DAYS + 10
    )
    assert timestamp_looks_bad(old) is False


def test_gps_looks_bad_for_null_island():
    assert gps_looks_bad(0.0, 0.0) is True


def test_gps_looks_bad_false_for_real_coords():
    assert gps_looks_bad(51.5, -0.1) is False


def test_get_geo_from_json_prefers_geo_data_exif():
    data = {
        "geoDataExif": {"latitude": 1.0, "longitude": 2.0, "altitude": 3.0},
        "geoData": {"latitude": 9.0, "longitude": 9.0, "altitude": 9.0},
    }
    assert get_geo_from_json(data) == (1.0, 2.0, 3.0)


def test_get_geo_from_json_falls_back_to_geo_data():
    data = {"geoData": {"latitude": 1.0, "longitude": 2.0, "altitude": None}}
    assert get_geo_from_json(data) == (1.0, 2.0, None)


def test_get_geo_from_json_rejects_null_island():
    data = {"geoDataExif": {"latitude": 0.0, "longitude": 0.0}}
    assert get_geo_from_json(data) == (None, None, None)


def test_get_geo_from_json_missing_returns_none_triple():
    assert get_geo_from_json({}) == (None, None, None)
