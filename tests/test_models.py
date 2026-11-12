from byoc_sync.models import UserRecord, section_prefs


def test_section_prefs_keeps_only_the_section():
    raw = {
        "byoc_pulsar|username": "alice01",
        "byoc_pulsar|password": "secret12345",
        "omero_account|password": "omero-secret",
        "localization|locale": "en",
    }
    assert section_prefs(raw, "byoc_pulsar") == {
        "byoc_pulsar|username": "alice01",
        "byoc_pulsar|password": "secret12345",
    }


def test_section_prefs_of_nothing_is_none():
    assert section_prefs(None, "byoc_pulsar") is None
    assert section_prefs({"other|x": "1"}, "byoc_pulsar") == {}


def test_section_prefs_rejects_non_mapping_json():
    assert section_prefs(["not", "a", "dict"], "byoc_pulsar") is None


def test_record_repr_masks_every_password_value():
    r = UserRecord("1", "a@b.c", "a", False, False, True, {"byoc_pulsar|password": "secret12345"}, "test")
    assert "secret12345" not in repr(r) and "secret12345" not in str(r)
