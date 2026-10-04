import re
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np


def parse_time(value, config):
    if not re.fullmatch(r"\d{2}:\d{2}", str(value)):
        raise ValueError(f"Time {value!r}: expected HH:MM format")
    offset = config.get("utc_offset_hours", 5)
    return datetime.strptime(config["date"] + " " + value, "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone(timedelta(hours=offset))
    )


def validate_inputs(employees, vehicles, config):
    if not employees or not vehicles:
        raise ValueError("At least one passenger request and one vehicle are required")
    if len(employees) > config.get("max_employees", 25) or len(vehicles) > 10:
        raise ValueError(
            "The educational project supports up to 25 requests and 10 vehicles"
        )
    result = []
    for records, fields, id_field in [
        (
            employees,
            [
                "employee_id",
                "workplace_address",
                "shift_end",
                "request_time",
                "home_address",
                "passengers",
            ],
            "employee_id",
        ),
        (vehicles, ["vehicle_id", "capacity", "start_address", "color"], "vehicle_id"),
    ]:
        frame = pd.DataFrame(records).copy()
        missing = set(fields) - set(frame.columns)
        if missing:
            raise ValueError(f"Missing fields: {', '.join(sorted(missing))}")
        for field in fields:
            if field not in ("passengers", "capacity"):
                if frame[field].isna().any():
                    raise ValueError(f"Empty field {field}")
                frame[field] = frame[field].astype(str).str.strip()
                if frame[field].eq("").any():
                    raise ValueError(f"Empty field {field}")
        if frame[id_field].duplicated().any():
            raise ValueError(f"Duplicate {id_field}")
        number = "passengers" if id_field == "employee_id" else "capacity"
        values = pd.to_numeric(frame[number], errors="coerce")
        if (
            any(isinstance(v, bool) for v in frame[number])
            or not np.isfinite(values).all()
            or (values <= 0).any()
            or (values % 1 != 0).any()
        ):
            raise ValueError(f"{number}: a positive integer is required")
        frame[number] = values.astype(int)
        result.append(frame[fields].to_dict("records"))
    employees, vehicles = result
    start = datetime.fromisoformat(config["start_timestamp"])
    if start.date().isoformat() != config["date"]:
        raise ValueError("The date in start_timestamp must match date in config.json")
    limit = start + timedelta(minutes=config["max_simulation_minutes"])
    for row in employees:
        shift = parse_time(row["shift_end"], config)
        request = parse_time(row["request_time"], config)
        if request < shift or request < start or request >= limit:
            raise ValueError(
                f"{row['employee_id']}: the request must be after the shift ends and within the simulation horizon"
            )
        if row["passengers"] > max(v["capacity"] for v in vehicles):
            raise ValueError(
                f"{row['employee_id']}: the passenger group exceeds every vehicle capacity"
            )
    if any(v["capacity"] > 20 for v in vehicles):
        raise ValueError(
            "The maximum vehicle capacity for the educational example is 20"
        )
    for vehicle in vehicles:
        if not re.fullmatch(r"[a-zA-Z]+|#[0-9a-fA-F]{6}", vehicle["color"]):
            raise ValueError("Color must be a name or a HEX code")
    return employees, vehicles


def validate_event(raw, employees, locations, seen_ids=None, requested_ids=None):
    errors = []
    notes = []
    record = dict(raw)
    seen_ids = seen_ids or set()
    requested_ids = requested_ids or set()
    try:
        for field in ["event_id", "employee_id", "workplace_id", "home_id"]:
            value = str(record[field]).strip()
            if value != record[field]:
                notes.append(f"trimmed {field}")
            record[field] = value
        count = pd.to_numeric(pd.Series([record["passengers"]]), errors="coerce").iloc[
            0
        ]
        if (
            isinstance(record["passengers"], bool)
            or not np.isfinite(count)
            or count <= 0
            or count % 1
        ):
            errors.append("Invalid passenger count")
        if record["event_id"] in seen_ids or record["employee_id"] in requested_ids:
            errors.append("Duplicate request")
        employee = employees[record["employee_id"]]
        if (
            record["workplace_id"] != employee["workplace_id"]
            or record["home_id"] != employee["home_id"]
        ):
            errors.append("The address does not match the employee record")
        event_time = datetime.fromisoformat(record["event_timestamp"])
        arrival = datetime.fromisoformat(record["arrival_timestamp"])
        if (
            event_time.tzinfo is None
            or arrival.tzinfo is None
            or event_time > arrival
            or event_time < datetime.fromisoformat(employee["shift_end"])
        ):
            errors.append("Invalid timestamp")
        record["passengers"] = int(count) if not errors else 0
        record.update(
            workplace_name=employee["workplace_name"],
            home_address=employee["home_address"],
        )
    except (KeyError, ValueError, TypeError, OverflowError):
        errors.append("Missing or invalid field")
    return {
        "valid": not errors,
        "errors": errors,
        "record": record if not errors else None,
        "raw": raw,
        "cleaning_notes": notes,
    }
