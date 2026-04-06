from cmb.judge_io import format_pass, parse_judgement


def test_format_pass_happy():
    assert format_pass("feat(api): add v2 endpoint")
    assert format_pass("fix: handle null token")


def test_format_pass_rejects_long_subject():
    assert not format_pass("feat: " + "x" * 80)


def test_format_pass_rejects_trailing_period():
    assert not format_pass("feat: add thing.")


def test_format_pass_rejects_non_cc():
    assert not format_pass("Added a new feature")


def test_parse_judgement_fenced_block():
    text = """thinking out loud...

    ```json
    {
      "accuracy": 5,
      "completeness": 4,
      "conciseness": 5,
      "format": 5,
      "body_quality": 4,
      "rationale": "great"
    }
    ```
    """
    out = parse_judgement(text)
    assert out is not None
    assert out["accuracy"] == 5
    assert out["rationale"] == "great"


def test_parse_judgement_bare_object():
    text = '{"accuracy": 3, "completeness": 3, "conciseness": 3, "format": 3, "body_quality": 3}'
    out = parse_judgement(text)
    assert out is not None
    assert out["accuracy"] == 3
    assert out["rationale"] == ""


def test_parse_judgement_clamps_to_range():
    text = '{"accuracy": 9, "completeness": 0, "conciseness": 3, "format": 3, "body_quality": 3}'
    out = parse_judgement(text)
    assert out["accuracy"] == 5
    assert out["completeness"] == 1


def test_parse_judgement_rejects_missing_fields():
    text = '{"accuracy": 5}'
    assert parse_judgement(text) is None


def test_parse_judgement_rejects_garbage():
    assert parse_judgement("hello world") is None
    assert parse_judgement("") is None
