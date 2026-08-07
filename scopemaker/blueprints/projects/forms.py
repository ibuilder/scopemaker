"""Project and bid package forms."""

from __future__ import annotations

from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    DecimalField,
    SelectField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Length, Optional

from ...data.masterformat import DIVISIONS

DIVISION_CHOICES = [("", "-- Not division specific --")] + [
    (d.code, f"{d.code} - {d.title}") for d in DIVISIONS
]

DELIVERY_METHODS = [
    ("", "-- Not specified --"),
    ("CMAR", "Construction Manager at Risk"),
    ("GMP", "Guaranteed Maximum Price"),
    ("Lump Sum", "Lump Sum / Hard Bid"),
    ("Design-Build", "Design-Build"),
    ("IPD", "Integrated Project Delivery"),
    ("Cost Plus", "Cost Plus"),
]


class ProjectForm(FlaskForm):
    name = StringField("Project name", validators=[DataRequired(), Length(max=255)])
    number = StringField("Project number", validators=[Optional(), Length(max=80)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=2000)])

    address = StringField("Address", validators=[Optional(), Length(max=255)])
    city = StringField("City", validators=[Optional(), Length(max=120)])
    state = StringField("State / Province", validators=[Optional(), Length(max=60)])
    postal_code = StringField("Postal code", validators=[Optional(), Length(max=20)])

    owner_name = StringField("Owner", validators=[Optional(), Length(max=255)])
    architect_name = StringField("Architect", validators=[Optional(), Length(max=255)])
    engineer_name = StringField("Engineer", validators=[Optional(), Length(max=255)])
    contractor_name = StringField("Contractor", validators=[Optional(), Length(max=255)])
    delivery_method = SelectField("Delivery method", choices=DELIVERY_METHODS,
                                  validators=[Optional()])

    start_date = DateField("Start date", validators=[Optional()])
    completion_date = DateField("Completion date", validators=[Optional()])

    submit = SubmitField("Save project")


class BidPackageForm(FlaskForm):
    number = StringField(
        "Package number", validators=[DataRequired(), Length(max=60)],
        description="For example BP-21A.",
    )
    name = StringField("Package name", validators=[DataRequired(), Length(max=255)])
    division_code = SelectField("CSI division", choices=DIVISION_CHOICES,
                                validators=[Optional()])
    trade_name = StringField("Trade name", validators=[Optional(), Length(max=160)])
    subcontractor_name = StringField("Subcontractor", validators=[Optional(), Length(max=255)])
    base_bid_amount = DecimalField("Base bid amount", validators=[Optional()], places=2)
    bid_due_date = DateField("Bid due date", validators=[Optional()])
    submit = SubmitField("Save bid package")
