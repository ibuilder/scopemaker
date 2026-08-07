"""CSI MasterFormat data integrity.

The prototype this replaces offered a hand-typed list of sixteen divisions,
some of which do not exist. These tests exist so that cannot come back.
"""

from __future__ import annotations

import pytest

from scopemaker.data.masterformat import (
    ALL_DIVISIONS,
    DIVISIONS,
    RESERVED_CODES,
    divisions_by_subgroup,
    get_division,
    is_specifiable,
    normalize_code,
)

# The numbers CSI reserves for future expansion in MasterFormat 2020.
EXPECTED_RESERVED = {
    "15", "16", "17", "18", "19", "20", "24", "29",
    "30", "36", "37", "38", "39", "47", "49",
}


def test_covers_every_number_from_00_to_49():
    assert [d.code for d in ALL_DIVISIONS] == [f"{n:02d}" for n in range(50)]


def test_reserved_numbers_match_the_standard():
    assert RESERVED_CODES == EXPECTED_RESERVED


def test_reserved_divisions_are_not_offered_for_selection():
    offered = {d.code for d in DIVISIONS}
    assert offered.isdisjoint(EXPECTED_RESERVED)
    assert len(offered) == 50 - len(EXPECTED_RESERVED) == 35


@pytest.mark.parametrize("code", sorted(EXPECTED_RESERVED))
def test_reserved_codes_are_not_specifiable(code):
    assert is_specifiable(code) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (21, "21"),
        ("21", "21"),
        ("3", "03"),
        (3, "03"),
        (" 07 ", "07"),
        ("", None),
        (None, None),
        ("99", None),
        ("abc", None),
    ],
)
def test_code_normalisation(raw, expected):
    assert normalize_code(raw) == expected


def test_known_division_titles():
    assert get_division("21").title == "Fire Suppression"
    assert get_division("23").title.startswith("Heating, Ventilating")
    assert get_division("40").title == "Process Interconnections"
    assert get_division("48").title == "Electrical Power Generation"


def test_every_selectable_division_has_a_default_trade():
    for division in DIVISIONS:
        assert division.default_trade, f"Division {division.code} has no trade name"


def test_subgroups_partition_the_selectable_divisions():
    grouped = divisions_by_subgroup()
    flattened = [d.code for _, items in grouped for d in items]
    assert sorted(flattened) == sorted(d.code for d in DIVISIONS)
    assert len(flattened) == len(set(flattened)), "a division appears in two subgroups"
