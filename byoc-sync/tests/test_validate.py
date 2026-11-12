import pytest

from byoc_sync.models import ByocRequest, UserRecord, ValidationFailure
from byoc_sync.validate import validate

PW = "MyPulS4rP4ssw0rd"


def rec(**prefs):
    base = {
        "byoc_pulsar|username": "alice01",
        "byoc_pulsar|password": PW,
        "byoc_pulsar|max_accepted_cores": "8",
        "byoc_pulsar|max_accepted_mem": "15.5",
        "byoc_pulsar|min_accepted_gpus": "0",
        "byoc_pulsar|max_accepted_gpus": "0",
    }
    for k, v in prefs.items():
        key = f"byoc_pulsar|{k}"
        if v is ...:
            base.pop(key, None)
        else:
            base[key] = v
    return UserRecord(
        galaxy_id="1",
        email="alice@example.org",
        username="alice",
        deleted=False,
        purged=False,
        active=True,
        prefs=base,
        source="test",
    )


def test_valid_record_gives_typed_request():
    req = validate(rec())
    assert req == ByocRequest(
        name="alice01", password=PW, max_cores=8, max_mem=15.5, min_gpus=0, max_gpus=0, nfs_path=None
    )
    assert isinstance(req.max_cores, int) and isinstance(req.min_gpus, int)


def test_integral_float_strings_become_ints():
    req = validate(rec(max_accepted_cores="8.0", max_accepted_mem="16"))
    assert req.max_cores == 8 and isinstance(req.max_cores, int)
    assert req.max_mem == 16 and isinstance(req.max_mem, int)


def test_numbers_from_api_json_accepted():
    assert validate(rec(max_accepted_cores=8, max_accepted_mem=15.5, max_accepted_gpus=1)).max_gpus == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_accepted_cores", "8.5"),
        ("max_accepted_cores", "0"),
        ("max_accepted_cores", "1025"),
        ("max_accepted_cores", "eight"),
        ("max_accepted_mem", "0"),
        ("max_accepted_mem", "-1"),
        ("max_accepted_mem", "16385"),
        ("max_accepted_mem", "nan"),
        ("max_accepted_mem", "inf"),
        ("min_accepted_gpus", "-1"),
        ("max_accepted_gpus", "65"),
        ("max_accepted_cores", True),
    ],
)
def test_bad_numbers_rejected(field, value):
    out = validate(rec(**{field: value}))
    assert isinstance(out, ValidationFailure)
    assert field in out.errors


def test_min_gpus_above_max_rejected():
    out = validate(rec(min_accepted_gpus="2", max_accepted_gpus="1"))
    assert isinstance(out, ValidationFailure) and "min_accepted_gpus" in out.errors


@pytest.mark.parametrize(
    "name",
    [
        "a{{b}}",
        "x'y",
        'x"y',
        "x\ny",
        "Alice",
        "ab",
        "x_byoc",
        "x__y",
        "x_",
        "1abc",
        "a b",
        "a-b",
        "a" * 33,
        "",
    ],
)
def test_bad_usernames_rejected(name):
    out = validate(rec(username=name))
    assert isinstance(out, ValidationFailure) and "username" in out.errors


@pytest.mark.parametrize("pw", ["short9chr", "has space1234", "has{{jinja}}12", "ünicode12345", "a" * 129])
def test_bad_passwords_rejected_without_echoing_the_value(pw):
    out = validate(rec(password=pw))
    assert isinstance(out, ValidationFailure) and "password" in out.errors
    assert pw not in str(out) and pw not in repr(out) and all(pw not in e for e in out.errors.values())


@pytest.mark.parametrize("path", ["/a/../b", "rel/path", "/a//b", "/a b", "/a{{x}}", "/" + "a" * 256, "/", "~/x"])
def test_bad_nfs_paths_rejected(path):
    out = validate(rec(nfs_path=path))
    assert isinstance(out, ValidationFailure) and "nfs_path" in out.errors


SHAPE = "must be an absolute path of letters, digits, '.', '_', '-' and '/' (max 255)"
SEGMENTS = "must not be '/', contain '//' or '.'/'..' segments"


@pytest.mark.parametrize(
    "path,message",
    [
        ("/", SEGMENTS),
        ("///", SEGMENTS),
        ("/a//b", SEGMENTS),
        ("/a/../b", SEGMENTS),
        ("/a/./b", SEGMENTS),
        ("rel/path", SHAPE),
        ("/a b", SHAPE),
        ("/" + "a" * 256, SHAPE),
    ],
)
def test_bad_nfs_paths_get_the_message_that_fits(path, message):
    out = validate(rec(nfs_path=path))
    assert isinstance(out, ValidationFailure) and out.errors["nfs_path"] == message


def test_nfs_path_trailing_slash_stripped():
    assert validate(rec(nfs_path="/data/share/")).nfs_path == "/data/share"


def test_empty_nfs_path_means_none():
    assert validate(rec(nfs_path="")).nfs_path is None


def test_whitespace_around_values_is_stripped():
    assert validate(rec(username=" alice01 ", max_accepted_cores=" 8 ")).name == "alice01"


@pytest.mark.parametrize("empty", [None, ""])
def test_all_fields_empty_means_no_request(empty):
    keys = [
        "username",
        "password",
        "max_accepted_cores",
        "max_accepted_mem",
        "min_accepted_gpus",
        "max_accepted_gpus",
        "nfs_path",
    ]
    assert validate(rec(**{k: empty for k in keys})) is None


def test_no_section_keys_means_no_request():
    r = rec()
    r = UserRecord(**{**r.__dict__, "prefs": {"other|x": "1"}})
    assert validate(r) is None


def test_none_prefs_means_no_request():
    r = rec()
    assert validate(UserRecord(**{**r.__dict__, "prefs": None})) is None


def test_partial_form_is_a_failure_listing_missing_fields():
    out = validate(rec(password=..., max_accepted_mem=""))
    assert isinstance(out, ValidationFailure)
    assert {"password", "max_accepted_mem"} <= set(out.errors)


def test_missing_gpu_fields_default_to_zero():
    req = validate(rec(min_accepted_gpus=..., max_accepted_gpus=""))
    assert req.min_gpus == 0 and req.max_gpus == 0


def test_request_repr_masks_password():
    req = validate(rec())
    assert PW not in repr(req) and PW not in str(req)
    assert "********" in repr(req)


def test_user_record_repr_masks_password():
    r = rec()
    assert PW not in repr(r) and PW not in str(r)


@pytest.mark.parametrize("field", ["username", "password", "max_accepted_cores", "max_accepted_mem", "nfs_path"])
@pytest.mark.parametrize("value", [["x"], {"a": 1}, True])
def test_non_scalar_values_fail_the_field_instead_of_crashing(field, value):
    out = validate(rec(**{field: value}))
    assert isinstance(out, ValidationFailure) and field in out.errors


@pytest.mark.parametrize("field,value", [("username", "alice\nbob"), ("nfs_path", "/data\n/share")])
def test_embedded_newlines_rejected(field, value):
    out = validate(rec(**{field: value}))
    assert isinstance(out, ValidationFailure) and field in out.errors


def test_regexes_are_applied_to_the_whole_value():
    from byoc_sync import validate as module

    # re.match with "$" accepts a trailing newline; the module must use fullmatch semantics.
    assert module.NAME_RE.fullmatch("alice\n") is None
    assert not module.is_valid_name("alice\n")


@pytest.mark.parametrize("field", ["max_accepted_cores", "max_accepted_mem"])
def test_huge_numbers_fail_the_field_instead_of_crashing(field):
    out = validate(rec(**{field: 10**400}))
    assert isinstance(out, ValidationFailure) and field in out.errors


@pytest.mark.parametrize("value", ["1e1", "1_0", "１０", "0x10", "+8", " 8 8 ", "8.", ".5"])
def test_only_plain_decimal_strings_are_numbers(value):
    out = validate(rec(max_accepted_cores=value))
    assert isinstance(out, ValidationFailure) and "max_accepted_cores" in out.errors


def test_memory_below_a_tenth_of_a_gigabyte_is_rejected():
    out = validate(rec(max_accepted_mem="0.00001"))
    assert isinstance(out, ValidationFailure) and "max_accepted_mem" in out.errors
