from ruamel.yaml import YAML

from byoc_sync.yamledit.mask import mask

DEST = "files/galaxy/tpv/destinations.yml.j2"


def parse(text):
    return YAML(typ="safe").load(text)


def test_masked_eu_destinations_is_valid_yaml_with_same_line_count(eu_fixtures):
    text = (eu_fixtures / DEST).read_text()
    masked = mask(text)
    assert masked.count("\n") == text.count("\n")
    data = parse(masked)
    assert "pulsar_egi01_tpv" in data["destinations"]


def test_macro_definitions_are_commented_out_whole():
    text = "{% macro vols() -%}\n        a:rw,\n        b:ro\n{%- endmacro %}\n---\nx: 1\n"
    masked = mask(text)
    assert masked.splitlines()[:4] == ["#" + line for line in text.splitlines()[:4]]
    assert parse(masked) == {"x": 1}


def test_expression_becomes_a_stable_token_that_depends_on_the_expression():
    a = mask('k: "{{ foo }}/x"\n')
    b = mask('k: "{{ foo }}/x"\n')
    c = mask('k: "{{ bar }}/x"\n')
    assert a == b and a != c
    assert "{{" not in a and parse(a)["k"].endswith("/x")


def test_unquoted_expression_value_parses():
    assert parse(mask("container_resolvers:\n  {{ resolvers() | indent(8) }}\nnext: 1\n"))["next"] == 1


def test_jinja_comment_is_removed():
    assert parse(mask("a: 1 {# note #}\n")) == {"a": 1}


def test_standalone_tag_lines_are_commented_out():
    text = "items:\n{% for i in xs %}\n  - {{ i }}\n{% endfor %}\n"
    masked = mask(text)
    assert masked.splitlines()[1].startswith("#") and masked.splitlines()[3].startswith("#")
    assert len(parse(masked)["items"]) == 1


def test_crlf_line_endings_are_kept():
    text = "a: '{{ x }}'\r\nb: 2\r\n"
    masked = mask(text)
    assert masked.count("\r\n") == 2 and parse(masked)["b"] == 2


def test_plain_yaml_is_unchanged():
    text = "a: 1\nb:\n  - c # comment\n"
    assert mask(text) == text


def test_expression_spanning_lines_is_refused():
    import pytest

    from byoc_sync.yamledit.mask import MaskError

    with pytest.raises(MaskError, match="line 1"):
        mask("a: '{{ x\n  }}'\n")
