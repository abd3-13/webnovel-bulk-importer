import json

import pytest

from webnovel_importer.config import Config, ConfigError, build_config, load_config, validate


def test_defaults_valid():
    validate(Config())


def test_load_missing_default_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "config.json").retries == 2


def test_missing_explicit_file_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "x.json", explicit=True)


def test_paths_resolve_relative_to_config(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"input_file": "my.txt"}))
    assert load_config(p).input_file == tmp_path / "my.txt"


@pytest.mark.parametrize("bad", [
    {"retries": -1}, {"retries": "2"}, {"min_delay_s": 10, "max_delay_s": 1},
    {"navigation_timeout_s": 0}, {"headed": "yes"}, {"unknown_key": 1},
    {"labels": {"add_to_library": "("}}, {"labels": {"bogus": "x"}}, {"allowed_domains": []},
    {"home_url": "ftp://x"},
])
def test_invalid_values_rejected(bad):
    with pytest.raises(ConfigError):
        build_config(bad)


def test_labels_merge_with_defaults():
    cfg = build_config({"labels": {"in_library": "saved"}})
    assert cfg.labels["in_library"] == "saved" and "add_to_library" in cfg.labels


def test_shipped_config_json_is_valid():
    from pathlib import Path
    load_config(Path(__file__).resolve().parent.parent / "config.json", explicit=True)
