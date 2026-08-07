"""Forms for creating and editing scopes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DecimalField,
    HiddenField,
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, Optional, ValidationError

from ...data.masterformat import DIVISIONS, divisions_by_subgroup
from ...models.scope import DEFAULT_SECTIONS, SCOPE_STATUSES, STATUS_LABELS
from ...services.numbering import STYLE_LABELS, SUPPORTED_STYLES

DIVISION_CHOICES = [("", "-- Select a division --")] + [
    (d.code, f"{d.code} - {d.title}") for d in DIVISIONS
]

NUMBERING_SCHEME_CHOICES = [
    ("legal", "Legal - 1., 1.1, 1.1.1 (recommended)"),
    ("outline", "Outline - 1., A., 1), a)"),
]

STYLE_CHOICES = [(style, STYLE_LABELS[style]) for style in SUPPORTED_STYLES]


class ScopeStartForm(FlaskForm):
    """Step 1 of the wizard: what is this scope for?"""

    project_id = SelectField("Project", validators=[Optional()], choices=[])
    bid_package_id = SelectField("Bid package", validators=[Optional()], choices=[])
    division_code = SelectField(
        "CSI division", validators=[DataRequired(message="Choose a division.")],
        choices=DIVISION_CHOICES,
    )
    trade_name = StringField(
        "Trade name", validators=[Optional(), Length(max=160)],
        description="Defaults to the standard trade name for the division.",
    )
    exhibit_label = StringField(
        "Exhibit label", validators=[DataRequired(), Length(max=60)], default="EXHIBIT B"
    )
    title = StringField(
        "Document title", validators=[DataRequired(), Length(max=255)],
        default="Scope of Work",
    )
    numbering_scheme = SelectField(
        "Numbering", choices=NUMBERING_SCHEME_CHOICES, default="legal"
    )
    template_id = SelectField("Start from template", validators=[Optional()], choices=[])
    submit = SubmitField("Choose clauses")

    def __init__(self, *args, projects=None, packages=None, templates=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project_id.choices = [("", "-- No project --")] + [
            (p.id, p.display_title) for p in (projects or [])
        ]
        self.bid_package_id.choices = [("", "-- No bid package --")] + [
            (b.id, b.display_title) for b in (packages or [])
        ]
        self.template_id.choices = [("", "-- Standard structure --")] + [
            (t.id, t.name) for t in (templates or [])
        ]
        self.division_groups = divisions_by_subgroup()


class ScopeGenerateForm(FlaskForm):
    """Step 2: which clauses and specification sections go on it."""

    project_id = HiddenField()
    bid_package_id = HiddenField()
    division_code = HiddenField(validators=[DataRequired()])
    trade_name = HiddenField()
    exhibit_label = HiddenField()
    title = HiddenField()
    numbering_scheme = HiddenField()
    template_id = HiddenField()

    clause_ids = SelectMultipleField("Clauses", choices=[], validate_choice=False)
    spec_section_ids = SelectMultipleField(
        "Specification sections", choices=[], validate_choice=False
    )
    enabled_sections = SelectMultipleField(
        "Sections", choices=[(s["key"], s["heading"]) for s in DEFAULT_SECTIONS],
        validate_choice=False,
    )
    base_bid_amount = DecimalField("Base bid amount", validators=[Optional()], places=2)
    submit = SubmitField("Generate scope")


class ScopeSettingsForm(FlaskForm):
    """Editing a scope's identity, presentation and financials."""

    title = StringField("Document title", validators=[DataRequired(), Length(max=255)])
    exhibit_label = StringField("Exhibit label", validators=[DataRequired(), Length(max=60)])
    trade_name = StringField("Trade name", validators=[Optional(), Length(max=160)])
    division_code = SelectField("CSI division", choices=DIVISION_CHOICES, validators=[Optional()])
    status = SelectField(
        "Status",
        choices=[(s, STATUS_LABELS[s]) for s in SCOPE_STATUSES if s != "archived"],
    )
    project_id = SelectField("Project", validators=[Optional()], choices=[])
    bid_package_id = SelectField("Bid package", validators=[Optional()], choices=[])

    numbering_scheme = SelectField("Numbering scheme", choices=NUMBERING_SCHEME_CHOICES)
    level1_style = SelectField("Level 1", choices=STYLE_CHOICES, default="decimal")
    level2_style = SelectField("Level 2", choices=STYLE_CHOICES, default="decimal")
    level3_style = SelectField("Level 3", choices=STYLE_CHOICES, default="decimal")

    currency = StringField("Currency", validators=[Optional(), Length(min=3, max=3)],
                           default="USD")
    base_bid_amount = DecimalField("Base bid", validators=[Optional()], places=2)
    alternates_amount = DecimalField("Accepted alternates", validators=[Optional()], places=2)
    adjustments_amount = DecimalField(
        "Other additions / deletions", validators=[Optional()], places=2
    )

    submit = SubmitField("Save")

    def __init__(self, *args, projects=None, packages=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.project_id.choices = [("", "-- No project --")] + [
            (p.id, p.display_title) for p in (projects or [])
        ]
        self.bid_package_id.choices = [("", "-- No bid package --")] + [
            (b.id, b.display_title) for b in (packages or [])
        ]

    def validate_currency(self, field) -> None:
        if field.data and not field.data.isalpha():
            raise ValidationError("Use a three-letter currency code such as USD.")


class SectionForm(FlaskForm):
    heading = StringField("Heading", validators=[DataRequired(), Length(max=255)])
    body_html = TextAreaField("Introductory text", validators=[Optional()])
    is_enabled = BooleanField("Include in document", default=True)
    submit = SubmitField("Save section")


class ItemForm(FlaskForm):
    text_html = TextAreaField("Text", validators=[DataRequired()])
    parent_id = HiddenField()
    submit = SubmitField("Save")


class AddClausesForm(FlaskForm):
    section_key = HiddenField(validators=[DataRequired()])
    clause_ids = SelectMultipleField("Clauses", choices=[], validate_choice=False)
    submit = SubmitField("Add selected clauses")


class SaveTemplateForm(FlaskForm):
    name = StringField("Template name", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=1000)])
    submit = SubmitField("Save as template")


class RevisionNoteForm(FlaskForm):
    note = StringField("Note", validators=[Optional(), Length(max=500)])
    submit = SubmitField("Issue scope")


def parse_money(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


__all__ = [
    "DIVISION_CHOICES",
    "AddClausesForm",
    "ItemForm",
    "RevisionNoteForm",
    "SaveTemplateForm",
    "ScopeGenerateForm",
    "ScopeSettingsForm",
    "ScopeStartForm",
    "SectionForm",
    "parse_money",
]
