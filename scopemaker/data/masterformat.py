"""Canonical CSI MasterFormat divisions.

MasterFormat numbers run 00-49 but the list is *not* contiguous: 15-20, 24,
29, 30, 36-39, 47 and 49 are reserved for future expansion and must not be
offered as if they were real divisions.  The prototype this project replaces
hand-typed sixteen arbitrary entries, which is how scopes end up filed under a
division that does not exist.

Division titles follow MasterFormat 2020.  Section *numbers* within a division
are copyrighted by CSI and are not reproduced here; the seed library references
only the handful of section numbers that are in common public use as
cross-references, with titles kept descriptive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Subgroup labels, in MasterFormat order.
PROCUREMENT = "Procurement and Contracting Requirements"
GENERAL = "General Requirements"
FACILITY_CONSTRUCTION = "Facility Construction"
FACILITY_SERVICES = "Facility Services"
SITE_INFRASTRUCTURE = "Site and Infrastructure"
PROCESS_EQUIPMENT = "Process Equipment"


@dataclass(frozen=True)
class Division:
    code: str
    title: str
    subgroup: str
    # Trade names commonly used on bid packages for this division. The first is
    # used as the default when a scope is generated.
    trades: tuple[str, ...] = field(default_factory=tuple)
    reserved: bool = False

    @property
    def label(self) -> str:
        return f"Division {self.code} - {self.title}"

    @property
    def default_trade(self) -> str:
        return self.trades[0] if self.trades else self.title


_ALL: tuple[Division, ...] = (
    Division("00", "Procurement and Contracting Requirements", PROCUREMENT,
             ("General Conditions",)),
    Division("01", "General Requirements", GENERAL,
             ("General Requirements", "General Conditions")),
    Division("02", "Existing Conditions", FACILITY_CONSTRUCTION,
             ("Demolition", "Selective Demolition", "Hazardous Material Abatement")),
    Division("03", "Concrete", FACILITY_CONSTRUCTION,
             ("Concrete", "Cast-in-Place Concrete", "Precast Concrete")),
    Division("04", "Masonry", FACILITY_CONSTRUCTION,
             ("Masonry", "Unit Masonry", "Stone Assemblies")),
    Division("05", "Metals", FACILITY_CONSTRUCTION,
             ("Structural Steel", "Miscellaneous Metals", "Metal Fabrications")),
    Division("06", "Wood, Plastics, and Composites", FACILITY_CONSTRUCTION,
             ("Rough Carpentry", "Finish Carpentry", "Architectural Millwork")),
    Division("07", "Thermal and Moisture Protection", FACILITY_CONSTRUCTION,
             ("Roofing", "Waterproofing", "Firestopping", "Building Insulation")),
    Division("08", "Openings", FACILITY_CONSTRUCTION,
             ("Doors, Frames and Hardware", "Glass and Glazing", "Curtain Wall",
              "Overhead Doors")),
    Division("09", "Finishes", FACILITY_CONSTRUCTION,
             ("Drywall and Framing", "Painting", "Flooring", "Acoustical Ceilings",
              "Tile")),
    Division("10", "Specialties", FACILITY_CONSTRUCTION,
             ("Specialties", "Toilet Partitions and Accessories", "Signage")),
    Division("11", "Equipment", FACILITY_CONSTRUCTION,
             ("Equipment", "Food Service Equipment", "Loading Dock Equipment")),
    Division("12", "Furnishings", FACILITY_CONSTRUCTION,
             ("Furnishings", "Casework", "Window Treatments")),
    Division("13", "Special Construction", FACILITY_CONSTRUCTION,
             ("Special Construction", "Pre-Engineered Structures", "Pools")),
    Division("14", "Conveying Equipment", FACILITY_CONSTRUCTION,
             ("Elevators", "Escalators", "Conveying Equipment")),
    Division("15", "Reserved for Future Expansion", FACILITY_CONSTRUCTION, reserved=True),
    Division("16", "Reserved for Future Expansion", FACILITY_CONSTRUCTION, reserved=True),
    Division("17", "Reserved for Future Expansion", FACILITY_CONSTRUCTION, reserved=True),
    Division("18", "Reserved for Future Expansion", FACILITY_CONSTRUCTION, reserved=True),
    Division("19", "Reserved for Future Expansion", FACILITY_CONSTRUCTION, reserved=True),
    Division("20", "Reserved for Future Expansion", FACILITY_SERVICES, reserved=True),
    Division("21", "Fire Suppression", FACILITY_SERVICES,
             ("Fire Protection", "Fire Suppression", "Sprinkler Systems")),
    Division("22", "Plumbing", FACILITY_SERVICES,
             ("Plumbing", "Plumbing and Medical Gas")),
    Division("23", "Heating, Ventilating, and Air Conditioning (HVAC)", FACILITY_SERVICES,
             ("HVAC", "Mechanical", "Sheet Metal", "Test and Balance")),
    Division("24", "Reserved for Future Expansion", FACILITY_SERVICES, reserved=True),
    Division("25", "Integrated Automation", FACILITY_SERVICES,
             ("Integrated Automation", "Building Automation")),
    Division("26", "Electrical", FACILITY_SERVICES,
             ("Electrical", "Electrical Systems")),
    Division("27", "Communications", FACILITY_SERVICES,
             ("Communications", "Structured Cabling", "Audio-Visual")),
    Division("28", "Electronic Safety and Security", FACILITY_SERVICES,
             ("Electronic Safety and Security", "Access Control", "Fire Alarm")),
    Division("29", "Reserved for Future Expansion", FACILITY_SERVICES, reserved=True),
    Division("30", "Reserved for Future Expansion", SITE_INFRASTRUCTURE, reserved=True),
    Division("31", "Earthwork", SITE_INFRASTRUCTURE,
             ("Earthwork", "Sitework", "Excavation and Backfill", "Deep Foundations")),
    Division("32", "Exterior Improvements", SITE_INFRASTRUCTURE,
             ("Site Improvements", "Paving", "Landscaping", "Fencing")),
    Division("33", "Utilities", SITE_INFRASTRUCTURE,
             ("Site Utilities", "Underground Utilities", "Storm Drainage")),
    Division("34", "Transportation", SITE_INFRASTRUCTURE,
             ("Transportation", "Rail", "Bridges")),
    Division("35", "Waterway and Marine Construction", SITE_INFRASTRUCTURE,
             ("Marine Construction", "Waterway Construction")),
    Division("36", "Reserved for Future Expansion", SITE_INFRASTRUCTURE, reserved=True),
    Division("37", "Reserved for Future Expansion", SITE_INFRASTRUCTURE, reserved=True),
    Division("38", "Reserved for Future Expansion", SITE_INFRASTRUCTURE, reserved=True),
    Division("39", "Reserved for Future Expansion", SITE_INFRASTRUCTURE, reserved=True),
    Division("40", "Process Interconnections", PROCESS_EQUIPMENT,
             ("Process Piping", "Process Interconnections")),
    Division("41", "Material Processing and Handling Equipment", PROCESS_EQUIPMENT,
             ("Material Handling",)),
    Division("42", "Process Heating, Cooling, and Drying Equipment", PROCESS_EQUIPMENT,
             ("Process Heating and Cooling",)),
    Division("43", "Process Gas and Liquid Handling, Purification, and Storage Equipment",
             PROCESS_EQUIPMENT, ("Process Gas and Liquid Handling",)),
    Division("44", "Pollution and Waste Control Equipment", PROCESS_EQUIPMENT,
             ("Pollution Control",)),
    Division("45", "Industry-Specific Manufacturing Equipment", PROCESS_EQUIPMENT,
             ("Manufacturing Equipment",)),
    Division("46", "Water and Wastewater Equipment", PROCESS_EQUIPMENT,
             ("Water and Wastewater Equipment",)),
    Division("47", "Reserved for Future Expansion", PROCESS_EQUIPMENT, reserved=True),
    Division("48", "Electrical Power Generation", PROCESS_EQUIPMENT,
             ("Power Generation", "Solar Photovoltaic")),
    Division("49", "Reserved for Future Expansion", PROCESS_EQUIPMENT, reserved=True),
)

#: Every division that can actually be specified against.
DIVISIONS: tuple[Division, ...] = tuple(d for d in _ALL if not d.reserved)

#: Including the reserved placeholders, for validation and documentation.
ALL_DIVISIONS: tuple[Division, ...] = _ALL

DIVISION_BY_CODE: dict[str, Division] = {d.code: d for d in _ALL}

RESERVED_CODES: frozenset[str] = frozenset(d.code for d in _ALL if d.reserved)

SUBGROUP_ORDER: tuple[str, ...] = (
    PROCUREMENT,
    GENERAL,
    FACILITY_CONSTRUCTION,
    FACILITY_SERVICES,
    SITE_INFRASTRUCTURE,
    PROCESS_EQUIPMENT,
)


def normalize_code(code: str | int | None) -> str | None:
    """Coerce ``3``/``'3'``/``'03'`` to the canonical two-digit form."""
    if code is None:
        return None
    text = str(code).strip()
    if not text:
        return None
    if text.isdigit():
        text = text.zfill(2)
    return text if text in DIVISION_BY_CODE else None


def get_division(code: str | int | None) -> Division | None:
    normalized = normalize_code(code)
    return DIVISION_BY_CODE.get(normalized) if normalized else None


def is_specifiable(code: str | int | None) -> bool:
    """True when the code is a real division and not a reserved placeholder."""
    division = get_division(code)
    return bool(division and not division.reserved)


def divisions_by_subgroup() -> list[tuple[str, list[Division]]]:
    """Selectable divisions grouped for rendering an optgroup dropdown."""
    grouped: dict[str, list[Division]] = {name: [] for name in SUBGROUP_ORDER}
    for division in DIVISIONS:
        grouped[division.subgroup].append(division)
    return [(name, grouped[name]) for name in SUBGROUP_ORDER if grouped[name]]


def trade_choices(code: str | int | None) -> tuple[str, ...]:
    division = get_division(code)
    return division.trades if division else ()
