"""
Appliance/machine configuration models (washing machine, dishwasher, etc.).
"""


from pydantic import BaseModel, ConfigDict, Field

from ..base import EntityId


class MachineProgram(BaseModel):
    """Single machine program with power profile."""

    name: str = Field(
        description="Program name (e.g., 'eco', 'quick wash', 'off')",
        json_schema_extra={
            "x-help": "Descriptive name for this program. Examples: 'eco', 'quick wash', 'intensive', 'off'. Must include an 'off' program with zero power.",
            "x-ui-section": "Machine Specifications",
        },
    )
    power: list[float] = Field(
        description="Power consumption in watts for each 15-minute interval",
        json_schema_extra={
            "x-help": "Power profile as list of watts, one value per 15 minutes. Length defines program duration. Example: [2000, 2000, 500, 500, 100] for a wash cycle of 1 hour 15 minutes.",
            "x-unit": "W",
            "x-ui-section": "Machine Specifications",
            "x-validation-hint": "List of power values, one per 15 minutes",
        },
    )

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "x-help": "Define power profile for a machine program. Each program has a name and power consumption pattern over time.",
            "x-ui-section": "Machine Specifications",
        },
    )


class MachineConfig(BaseModel):
    """Appliance/machine configuration (washing machine, dishwasher, etc.)."""

    name: str = Field(
        description="Machine name/identifier",
        json_schema_extra={
            "x-help": "Unique name for this appliance. Use descriptive names like 'Washing Machine', 'Dishwasher', or 'Dryer' for multiple machines.",
            "x-ui-section": "Machine Specifications",
        },
    )
    programs: list[MachineProgram] = Field(
        min_length=1,
        description="Available programs with power profiles",
        json_schema_extra={
            "x-help": "List of available programs with their power profiles. Must include at least one program. Always include an 'off' program with zero power consumption.",
            "x-ui-section": "Machine Specifications",
            "x-validation-hint": "At least 1 program required, include 'off' program",
        },
    )
    entity_start_window: EntityId = Field(
        alias="entity start window",
        description="HA entity for start window datetime",
        json_schema_extra={
            "x-help": "Home Assistant entity for the earliest allowed start. Machine can start any time after this. Either a time of day (input_datetime with time only, e.g. 18:00): the window then lies on the next day on which it has not yet passed. Or a date and time (input_datetime with date and time, or a datetime entity): the window is used as it is and can lie further ahead, up to the end of the planning horizon.",
            "x-ui-section": "Machine Specifications",
            "x-ui-widget-filter": "input_datetime,datetime",
        },
    )
    entity_end_window: EntityId = Field(
        alias="entity end window",
        description="HA entity for end window datetime",
        json_schema_extra={
            "x-help": "Home Assistant entity for the latest allowed completion. Machine must finish before this deadline. A time of day (e.g. 08:00) or a date and time, like the start window. A window with a date must end after its start.",
            "x-ui-section": "Machine Specifications",
            "x-ui-widget-filter": "input_datetime,datetime",
        },
    )
    entity_selected_program: EntityId = Field(
        alias="entity selected program",
        description="HA entity for selected program",
        json_schema_extra={
            "x-help": "Home Assistant entity to select which program to run. Must match program names defined in 'programs' list.",
            "x-ui-section": "Machine Specifications",
            "x-ui-widget-filter": "input_select,select",
        },
    )
    entity_calculated_start: EntityId = Field(
        alias="entity calculated start",
        description="HA entity for calculated optimal start time",
        json_schema_extra={
            "x-help": "Home Assistant entity where system writes the calculated optimal start time. User/automation can use this to trigger machine.",
            "x-ui-section": "Machine Specifications",
            "x-ui-widget-filter": "input_datetime,datetime",
        },
    )
    entity_calculated_end: EntityId = Field(
        alias="entity calculated end",
        description="HA entity for calculated end time",
        json_schema_extra={
            "x-help": "Home Assistant entity where system writes the calculated program end time. Useful for notifications and planning.",
            "x-ui-section": "Machine Specifications",
            "x-ui-widget-filter": "input_datetime,datetime",
        },
    )
    entity_calculated_program: EntityId | None = Field(
        default=None,
        alias="entity calculated program",
        description="HA entity for the program of the calculated plan",
        json_schema_extra={
            "x-help": "Optional: Home Assistant entity where the system writes the program the calculated start and end belong to. While that run is going on, its remaining consumption is taken from this program, so the selected program can change (for example back to 'off', or to the next run's program) without changing the planned consumption. Without it, the program selected at the time of each optimisation is used. An input_text, or an input_select/select with the program names as options.",
            "x-ui-section": "Machine Specifications",
            "x-ui-widget-filter": "input_text,input_select,select",
        },
    )
    entity_instant_start: EntityId | None = Field(
        default=None,
        alias="entity instant start",
        description="HA entity for instant start",
        json_schema_extra={
            "x-help": "Optional: Home Assistant entity to force immediate start, bypassing optimization. Useful for urgent wash cycles. When it is on, the next optimisation plans the run to start now and then turns the entity off.",
            "x-ui-section": "Machine Specifications",
            "x-ui-widget-filter": "input_boolean,switch",
        },
    )

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        loc_by_alias=False,
        json_schema_extra={
            "x-ui-group": "Devices",
            "x-icon": "washing-machine",
            "x-order": 6,
            "x-help": """# Appliance / Machine Configuration

Optimize operation of flexible load appliances (washing machines, dishwashers, dryers, etc.) based on electricity prices and constraints.

## How It Works

The system optimizes when to start appliances within time windows:
1. User sets start window (earliest start) and end window (must finish by)
2. User selects program (each program has a power profile)
3. System calculates optimal start time to minimize cost
4. System writes calculated start time to HA entity
5. User/automation triggers machine at optimal time

## Programs

Each program defines power consumption over time:
- **Power profile**: List of watts, one value per 15 minutes
- **Duration**: Length of power list (e.g., 5 values = 1 hour 15 minutes)
- **'off' program**: Required, with zero power consumption

Examples:
- Eco wash: `[1800, 1800, 400, 400, 100]` (1 hour 15 minutes)
- Quick wash: `[2200, 500, 100]` (45 minutes)
- Off: `[]` or `[0]`

## Time Windows

- **Start window**: Earliest allowed start (e.g., "20:00", or "2026-11-10 20:00" with a date)
- **End window**: Latest allowed finish (e.g., "08:00", which lies on the next day when it is before the start)
- With times only, the window repeats every day; with a date, it is used once
- System finds cheapest start time within window
- Machine must complete before end window

## Tips

- Define accurate power profiles for each program
- Set realistic time windows (at least program duration)
- Wider time windows = better optimization opportunities
- Combine with solar production forecasts
- Use instant start for urgent loads; DAO turns it off once the run is planned
- Multiple machines can be optimized together
""",
            "x-docs-url": "https://github.com/corneel27/day-ahead/wiki/Machine-Configuration",
        },
    )
