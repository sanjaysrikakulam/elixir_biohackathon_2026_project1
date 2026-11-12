import copy

import pytest

from byoc_sync.yamledit.verify import VerificationError, verify


def add_b(data):
    data = copy.deepcopy(data)
    data["b"] = 2
    return data


def test_expected_change_passes():
    verify("a: 1\n", "a: 1\nb: 2\n", add_b)


def test_unexpected_extra_change_fails():
    with pytest.raises(VerificationError, match="unexpected"):
        verify("a: 1\n", "a: 9\nb: 2\n", add_b)


def test_changed_jinja_expression_is_detected():
    with pytest.raises(VerificationError):
        verify('a: "{{ x }}"\n', 'a: "{{ y }}"\nb: 2\n', add_b)


def test_after_text_that_does_not_parse_fails():
    with pytest.raises(VerificationError, match="parse"):
        verify("a: 1\n", "a: [\n", add_b)


def test_list_length_change_is_reported_with_its_path():
    with pytest.raises(VerificationError, match=r"~items"):
        verify("items: [1]\n", "items: [1, 2]\nb: 2\n", add_b)


def test_nested_value_change_is_reported_with_its_path():
    with pytest.raises(VerificationError, match=r"~d\.x\[0\]"):
        verify("d: {x: [1]}\n", "d: {x: [5]}\nb: 2\n", add_b)
