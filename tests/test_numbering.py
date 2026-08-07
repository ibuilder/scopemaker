"""Outline numbering.

The labels these produce are the identifiers people argue over in a contract
dispute, so they get tested as carefully as any calculation.
"""

from __future__ import annotations

import pytest

from scopemaker.services.numbering import (
    LEGAL,
    OUTLINE,
    Numberer,
    format_counter,
)


class Node:
    def __init__(self, text, children=None, position=0):
        self.text = text
        self.children = children or []
        self.position = position


@pytest.fixture()
def tree():
    return [
        Node("A", [Node("A1"), Node("A2", [Node("A2a"), Node("A2b")])]),
        Node("B"),
        Node("C", [Node("C1")]),
    ]


def labels(numberer, tree):
    return [(node.label, node.item.text) for node in numberer.flatten(tree)]


def test_legal_scheme_concatenates_ancestors(tree):
    assert labels(Numberer(scheme=LEGAL), tree) == [
        ("1.", "A"),
        ("1.1", "A1"),
        ("1.2", "A2"),
        ("1.2.1", "A2a"),
        ("1.2.2", "A2b"),
        ("2.", "B"),
        ("3.", "C"),
        ("3.1", "C1"),
    ]


def test_outline_scheme_uses_per_level_styles(tree):
    assert labels(Numberer(scheme=OUTLINE), tree) == [
        ("1.", "A"),
        ("A.", "A1"),
        ("B.", "A2"),
        ("1)", "A2a"),
        ("2)", "A2b"),
        ("2.", "B"),
        ("3.", "C"),
        ("A.", "C1"),
    ]


def test_path_is_style_independent(tree):
    legal = {n.item.text: n.path for n in Numberer(scheme=LEGAL).flatten(tree)}
    outline = {n.item.text: n.path for n in Numberer(scheme=OUTLINE).flatten(tree)}
    # The dotted counter path identifies a clause regardless of how it is
    # rendered, which is what revision diffing relies on.
    assert legal == outline
    assert legal["A2b"] == "1.2.2"


@pytest.mark.parametrize(
    ("value", "style", "expected"),
    [
        (1, "decimal", "1"),
        (1, "lower-alpha", "a"),
        (26, "lower-alpha", "z"),
        (27, "lower-alpha", "aa"),
        (52, "lower-alpha", "az"),
        (53, "lower-alpha", "ba"),
        (27, "upper-alpha", "AA"),
        (4, "lower-roman", "iv"),
        (9, "lower-roman", "ix"),
        (14, "lower-roman", "xiv"),
        (1987, "lower-roman", "mcmlxxxvii"),
        (4, "upper-roman", "IV"),
    ],
)
def test_counter_formatting(value, style, expected):
    assert format_counter(value, style) == expected


def test_children_are_ordered_by_position():
    tree = [
        Node(
            "root",
            [Node("third", position=30), Node("first", position=10), Node("second", position=20)],
        )
    ]
    assert [n.item.text for n in Numberer().flatten(tree)] == [
        "root", "first", "second", "third",
    ]


def test_depth_beyond_configured_styles_reuses_the_last_one():
    numberer = Numberer(["decimal", "upper-alpha"], scheme=OUTLINE)
    # Four levels deep with only two styles configured: it must not raise.
    assert numberer.label((1, 2, 3, 4)) == "D."


def test_unknown_styles_fall_back_to_decimal():
    numberer = Numberer(["not-a-style"], scheme=LEGAL)
    assert numberer.label((3,)) == "3."


def test_empty_tree_produces_no_labels():
    assert list(Numberer().flatten([])) == []
