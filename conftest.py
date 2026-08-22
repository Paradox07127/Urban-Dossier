"""Separate the tests CI can run from the ones that need the local data tree.

`data/` is gitignored and exists only on the workstation, so a CI checkout can
run 427 of the 461 tests and no more. Before this file the choice was between
running nothing behaviour-shaped in CI and running a suite that fails for a
reason that is not a regression; the audit's remaining release blocker was the
former.

Two rules, and the second is the one that matters:

1. Tests listed below are marked `needs_data`. CI deselects them with
   `-m "not needs_data"`.
2. On a machine that HAS the data, they must not be skipped. `--require-data`
   turns any skip of a `needs_data` test into a failure, so the workstation
   gate cannot go green because a path was wrong. That is not hypothetical:
   READY_DATA_DIR does not follow URBAN_DOSSIER_DATA_ROOT, and pointing only
   the latter at the data made 16 tests fail with assertions that read exactly
   like real regressions (see pytest.ini).

The registry is explicit rather than detected. Detection would decide "no data
here, skip" on its own, which is the failure mode rule 2 exists to prevent.
"""
from __future__ import annotations

import pytest

# Whole modules that read the published Parquet layer.
_DATA_MODULES = {
    "test_overview_artifacts.py",
    "test_presentation_contract.py",
    "test_query_dataset_rows.py",
    "test_timeline_contract.py",
}

# test_search_address.py is mixed: its parse_location_query cases are pure
# string handling and run anywhere; these hit the location index.
_DATA_TESTS = {
    "test_street_name_returns_geocoded_hits",
    "test_place_name_substring_matches",
    "test_case_insensitive_and_limit_respected",
    "test_adding_a_borough_no_longer_empties_the_search",
    "test_borough_filters_rather_than_decorates",
    "test_an_indexed_numbered_avenue_still_resolves",
    "test_a_named_place_that_is_not_an_address_resolves",
    "test_landmark_ranking_prefers_the_place_over_a_business_named_after_it",
    "test_a_postal_state_suffix_no_longer_empties_the_search",
    "test_qualifier_stripping_is_segment_anchored_not_word_removal",
    "test_results_declare_how_they_were_matched",
    "test_search_does_not_poison_the_thread_connection",
}


def pytest_addoption(parser):
    parser.addoption(
        "--require-data",
        action="store_true",
        default=False,
        help=(
            "Fail instead of skip when a needs_data test cannot reach the "
            "published Parquet layer. Use on the workstation gate."
        ),
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "needs_data: reads data/ready, which is gitignored and absent in CI",
    )


def pytest_collection_modifyitems(config, items):
    for item in items:
        module = item.path.name if hasattr(item, "path") else ""
        if module in _DATA_MODULES or item.name in _DATA_TESTS:
            item.add_marker(pytest.mark.needs_data)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not item.config.getoption("--require-data"):
        return
    if report.when != "call" or not report.skipped:
        return
    if item.get_closest_marker("needs_data"):
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid} was skipped, but --require-data says the "
            "published data layer should be reachable. A skipped data test "
            "is how a broken path goes green."
        )
