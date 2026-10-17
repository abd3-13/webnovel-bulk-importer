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


def test_selector_in_account_marker_is_rejected_with_hint():
    with pytest.raises(ConfigError, match="account_marker_selector"):
        build_config({"labels": {"account_marker": "a.j_header_user_trigger"}})


def test_selector_key_accepts_css_and_satisfies_requirement():
    cfg = build_config({"labels": {"account_marker": None, "account_marker_selector": "a.j_header_user_trigger"}})
    assert cfg.labels["account_marker_selector"] == "a.j_header_user_trigger"


def test_marker_required_but_none_configured():
    with pytest.raises(ConfigError):
        build_config({"require_account_marker": True, "labels": {"account_marker": None}})


def test_defaults_do_not_assume_a_marker():
    cfg = Config()
    assert not cfg.require_account_marker and not cfg.labels["account_marker"] and not cfg.labels["account_marker_selector"]


@pytest.mark.parametrize("bad", [{"block_resource_types": ["script"]}, {"block_resource_types": "image"},
                                 {"login_confirm_window_s": 99}])
def test_new_option_validation(bad):
    with pytest.raises(ConfigError):
        build_config(bad)


def test_blocking_can_be_disabled():
    assert build_config({"block_resource_types": []}).block_resource_types == []


def test_shipped_config_json_is_valid():
    from pathlib import Path
    load_config(Path(__file__).resolve().parent.parent / "config.json", explicit=True)
