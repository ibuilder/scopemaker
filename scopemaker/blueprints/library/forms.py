"""Clause and specification library forms."""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from ...data.masterformat import DIVISIONS
from ...models.library import CLAUSE_CATEGORIES

DIVISION_CHOICES = [("", "All trades (universal)")] + [
    (d.code, f"{d.code} - {d.title}") for d in DIVISIONS
]

CATEGORY_CHOICES = [(key, label) for key, label in CLAUSE_CATEGORIES.items()]


class ClauseForm(FlaskForm):
    category = SelectField("Category", choices=CATEGORY_CHOICES, validators=[DataRequired()])
    division_code = SelectField("Applies to", choices=DIVISION_CHOICES, validators=[Optional()])
    text = TextAreaField(
        "Clause text", validators=[DataRequired(), Length(max=4000)],
        description="Write it as it should read in the exhibit.",
    )
    is_default = BooleanField("Pre-select this clause on new scopes")
    is_active = BooleanField("Active", default=True)
    position = IntegerField(
        "Sort order", validators=[Optional(), NumberRange(min=0, max=100000)], default=0
    )
    notes = TextAreaField("Internal notes", validators=[Optional(), Length(max=1000)])
    submit = SubmitField("Save clause")


class SpecSectionForm(FlaskForm):
    code = StringField("Section number", validators=[DataRequired(), Length(max=20)])
    title = StringField("Section title", validators=[DataRequired(), Length(max=255)])
    division_code = SelectField(
        "Lives in division",
        choices=[(d.code, f"{d.code} - {d.title}") for d in DIVISIONS],
        validators=[DataRequired()],
    )
    related_divisions = StringField(
        "Also offer to divisions",
        validators=[Optional(), Length(max=120)],
        description="Comma-separated division numbers, e.g. 21, 22, 23.",
    )
    is_universal = BooleanField("Offer on every scope")
    is_default = BooleanField("Pre-select on new scopes")
    is_active = BooleanField("Active", default=True)
    position = IntegerField(
        "Sort order", validators=[Optional(), NumberRange(min=0, max=100000)], default=0
    )
    submit = SubmitField("Save section")


class TemplateForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=200)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=1000)])
    division_code = SelectField("Division", choices=DIVISION_CHOICES, validators=[Optional()])
    is_default = BooleanField("Use by default for this division")
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save template")
