"""Accessibility properties that are easy to regress and cheap to assert.

These do not replace a screen-reader pass -- markup can satisfy every one of
them and still be unusable. They pin down the specific defects found in the
audit, so that fixing them stays fixed.

The important one is keyboard reordering. Drag and drop is a pointer gesture
with no keyboard equivalent, so while it was the only way to reorder clauses,
the core editing action of the application was impossible without a mouse
(WCAG 2.1.1). Deleting those buttons would break nothing else and no other test
would notice.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

PAGES_WITH_TABLES = [
    "/dashboard",
    "/scopes/",
    "/projects/",
    "/library/",
    "/admin/",
    "/auth/profile",
]


def html(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    return response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Keyboard reordering
# ---------------------------------------------------------------------------

def test_every_draggable_item_also_has_keyboard_move_buttons(auth_client, scope):
    """A pointer-only gesture cannot be the only way to do something."""
    body = html(auth_client, f"/scopes/{scope.id}")

    draggable = body.count('draggable="true"')
    assert draggable > 0, "expected a draggable outline to test"

    up = len(re.findall(r'data-move="up"', body))
    down = len(re.findall(r'data-move="down"', body))
    assert up == draggable, f"{draggable} draggable items but {up} move-up buttons"
    assert down == draggable, f"{draggable} draggable items but {down} move-down buttons"


def test_move_buttons_say_which_item_they_move(auth_client, scope):
    """"Move up" repeated 72 times tells a screen-reader user nothing."""
    body = html(auth_client, f"/scopes/{scope.id}")
    labels = re.findall(r'aria-label="(Move item [^"]+)"', body)

    assert labels, "move buttons have no accessible names"
    assert len(set(labels)) == len(labels), (
        "move buttons share accessible names; each should name its own item"
    )
    assert any(re.search(r"Move item \d", label) for label in labels)
    # Item numbers restart in every section, so the section has to be named too.
    assert all(" in " in label for label in labels)


def test_the_reorder_status_region_is_live(auth_client, scope):
    """Reordering changes nothing visible to a screen reader without this."""
    body = html(auth_client, f"/scopes/{scope.id}")
    assert 'id="reorder-status"' in body
    region = body[body.index('id="reorder-status"') - 200:]
    assert 'aria-live="polite"' in region[:300] or 'role="status"' in region[:300]


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES_WITH_TABLES)
def test_table_headers_declare_a_scope(auth_client, path):
    """A cell read on its own should still say which column it came from."""
    body = html(auth_client, path)
    headers = re.findall(r"<th\b[^>]*>", body)
    if not headers:
        pytest.skip(f"{path} has no table headers")
    unscoped = [h for h in headers if "scope=" not in h]
    assert not unscoped, f"{path} has header cells without scope: {unscoped[:3]}"


class ControlScanner(HTMLParser):
    """Find form controls that have no accessible name.

    Regex gets this wrong in both directions: a bare ``id="`` also matches
    inside ``aria-invalid="``, and a control wrapped in a ``<label>`` rather
    than referenced by ``for=`` looks unlabelled when it is not. This tracks
    label nesting and collects ``for`` targets properly.
    """

    CONTROLS = {"input", "select", "textarea"}
    SKIP_TYPES = {"hidden", "submit", "button", "image", "reset"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.label_depth = 0
        self.label_targets: set[str] = set()
        self.controls: list[tuple[dict[str, str], bool]] = []

    def handle_starttag(self, tag, attrs):
        attributes = {k: (v or "") for k, v in attrs}
        if tag == "label":
            self.label_depth += 1
            if attributes.get("for"):
                self.label_targets.add(attributes["for"])
        elif tag in self.CONTROLS:
            if attributes.get("type", "").lower() in self.SKIP_TYPES:
                return
            self.controls.append((attributes, self.label_depth > 0))

    def handle_endtag(self, tag):
        if tag == "label" and self.label_depth:
            self.label_depth -= 1

    def unnamed(self) -> list[str]:
        missing = []
        for attributes, wrapped_in_label in self.controls:
            if wrapped_in_label:
                continue
            if attributes.get("aria-label") or attributes.get("aria-labelledby"):
                continue
            if attributes.get("id") in self.label_targets:
                continue
            missing.append(
                f"<{attributes.get('name') or attributes.get('id') or '?'} "
                f"type={attributes.get('type', 'select/textarea')}>"
            )
        return missing


@pytest.mark.parametrize("path", [*PAGES_WITH_TABLES, "/scopes/new"])
def test_form_controls_are_labelled(auth_client, path):
    """Every control needs a name, and the admin role select had none."""
    scanner = ControlScanner()
    scanner.feed(html(auth_client, path))

    if not scanner.controls:
        pytest.skip(f"{path} has no form controls")
    assert not scanner.unnamed(), (
        f"{path} has unlabelled controls: {scanner.unnamed()[:3]}"
    )


def test_every_page_has_one_h1_and_a_skip_link(auth_client):
    for path in PAGES_WITH_TABLES:
        body = html(auth_client, path)
        assert body.count("<h1") == 1, f"{path} should have exactly one h1"
        assert 'class="skip-link"' in body, f"{path} is missing the skip link"


def test_the_document_language_is_declared(auth_client):
    """Screen readers pick a voice from this."""
    assert re.search(r'<html[^>]+lang="[a-z]{2}', html(auth_client, "/dashboard"))
