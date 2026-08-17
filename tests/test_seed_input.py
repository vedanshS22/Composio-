from pathlib import Path

import pytest

from scripts.seed_db import load_seed_input


def test_generic_three_column_input_assigns_stable_row_ids(tmp_path: Path) -> None:
    source = tmp_path / "apps.csv"
    source.write_text(
        "name,website,category\nExample,https://example.com,Testing\nSecond,https://second.example,Other\n",
        encoding="utf-8",
    )
    seeds = load_seed_input(source)
    assert [(seed.id, seed.name, str(seed.hint_url)) for seed in seeds] == [
        (1, "Example", "https://example.com/"),
        (2, "Second", "https://second.example/"),
    ]


def test_generic_input_accepts_legacy_hint_url_and_rejects_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "apps.csv"
    source.write_text(
        "id,name,hint_url,category\n7,Example,https://example.com/docs,Testing\n8,example,https://other.example,Other\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate company name"):
        load_seed_input(source)
