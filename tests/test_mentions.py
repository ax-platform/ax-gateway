from ax_cli.mentions import extract_explicit_mentions, merge_explicit_mentions_metadata


def test_extract_explicit_mentions_dedupes_and_excludes_self():
    assert extract_explicit_mentions(
        "@nemotron please ask @Hermes and @nemotron again, not email@example.com",
        exclude=["hermes"],
    ) == ["nemotron"]


def test_merge_explicit_mentions_metadata_preserves_existing_values():
    metadata = {"routing": {"mode": "reply_target"}, "mentions": ["existing"]}

    merged = merge_explicit_mentions_metadata(metadata, "@nemotron ping @existing")

    assert merged == {
        "routing": {"mode": "reply_target"},
        "mentions": ["existing", "nemotron"],
    }
    assert metadata["mentions"] == ["existing"]
