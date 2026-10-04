from pathlib import Path
from datetime import datetime, timedelta
import json, hashlib
import pandas as pd
import numpy as np
from streaming import event_generator as stream_events
from validation import validate_event
from routing import RoadNetwork, optimize_routes, position_on_line, feasible
from analysis import calculate_metrics, workplace_summary
from storage import ResultStore


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def create_state(root, result_directory, payload=None, config_override=None):
    import shutil
    from validation import validate_inputs
    from geocoding import prepare_locations

    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    config.update(config_override or {})
    if payload is None:
        employees = pd.read_csv(
            root / "data/employees.csv", keep_default_na=False
        ).to_dict("records")
        vehicles = pd.read_csv(
            root / "data/vehicles.csv", keep_default_na=False
        ).to_dict("records")
    else:
        employees = payload["employees"]
        vehicles = payload["vehicles"]
    employees, vehicles = validate_inputs(employees, vehicles, config)
    original = {"employees": employees, "vehicles": vehicles}
    employees, vehicles, locations = prepare_locations(
        employees, vehicles, root, config
    )
    context = {
        "root": root,
        "output": result_directory,
        "config": config,
        "now": datetime.fromisoformat(config["start_timestamp"]),
        "finished": False,
        "locations": locations,
        "employees": {e["employee_id"]: e for e in employees},
        "vehicles": {},
        "requests": {},
        "trips": [],
        "events": [],
        "metrics": [],
        "arrival_times": [],
        "seen_ids": set(),
        "warned": set(),
        "rejected": 0,
        "raw_received": 0,
        "cleaning_count": 0,
        "optimization_runs": [],
        "input": original,
    }
    context["started_at"] = context["now"]
    for row in vehicles:
        row.update(
            location_id=row["initial_location_id"],
            active_trip=None,
            onboard=0,
            completed_km=0.0,
            travelled_km=0.0,
        )
        context["vehicles"][row["vehicle_id"]] = row
    context["network"] = RoadNetwork(root, locations, config)
    context["generator"] = stream_events(employees)
    context["next_event"] = next(context["generator"], None)
    context["store"] = ResultStore(result_directory)
    inputs = result_directory / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "input.json").write_text(
        json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (inputs / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (inputs / "locations.json").write_text(
        json.dumps(locations, ensure_ascii=False), encoding="utf-8"
    )
    update_statistics(context)
    return context


def log_event(context, kind, message, **payload):
    event = {
        "timestamp": iso(payload.pop("at", context["now"])),
        "type": kind,
        "message": message,
        **payload,
    }
    context["events"].append(event)
    context["store"].event(event)


def process_event(context, raw):
    context["raw_received"] += 1
    validation = validate_event(
        raw,
        context["employees"],
        context["locations"],
        context["seen_ids"],
        set(context["requests"]),
    )
    cleaned = validation["record"]
    errors = validation["errors"]
    notes = validation["cleaning_notes"]
    if errors:
        context["rejected"] += 1
        log_event(
            context,
            "rejected",
            "Record rejected: " + ", ".join(errors),
            event_id=raw.get("event_id"),
            errors=errors,
        )
    else:
        context["seen_ids"].add(cleaned["event_id"])
        employee_id = cleaned["employee_id"]
        requested = datetime.fromisoformat(cleaned["event_timestamp"])
        arrival = datetime.fromisoformat(cleaned["arrival_timestamp"])
        context["arrival_times"].append(arrival)
        cleaned.update(
            requested_at=requested,
            arrived_at=arrival,
            status="waiting",
            vehicle_id=None,
            trip_id=None,
            picked_up_at=None,
            delivered_at=None,
            cleaning_notes=notes,
        )
        context["requests"][employee_id] = cleaned
        log_event(
            context,
            "request",
            f"New request {employee_id}: {cleaned['workplace_name']}",
            employee_id=employee_id,
            workplace_id=cleaned["workplace_id"],
        )
        if notes:
            context["cleaning_count"] += len(notes)
            log_event(
                context,
                "cleaned",
                f"Record cleaned {employee_id}: " + "; ".join(notes),
                employee_id=employee_id,
                notes=notes,
            )


def acquire_events(context):
    while (
        context["next_event"] is not None
        and datetime.fromisoformat(context["next_event"]["arrival_timestamp"])
        <= context["now"]
    ):
        process_event(context, context["next_event"])
        context["next_event"] = next(context["generator"], None)


def assign_routes(context):
    idle = [v for v in context["vehicles"].values() if v["active_trip"] is None]
    waiting = sorted(
        (r for r in context["requests"].values() if r["status"] == "waiting"),
        key=lambda r: (r["requested_at"], r["employee_id"]),
    )
    if not idle or not waiting:
        return
    plans, result = optimize_routes(
        context["network"],
        idle,
        waiting,
        context["now"],
        context["started_at"],
        context["config"],
    )
    context["optimization_runs"].append({"timestamp": iso(context["now"]), **result})
    prepared = []
    for vehicle in idle:
        stops = plans.get(vehicle["vehicle_id"])
        if not stops:
            continue
        previous = vehicle["location_id"]
        for stop in stops:
            context["network"].coordinates(previous, stop["location_id"])
            previous = stop["location_id"]
        ids = {s["employee_id"] for s in stops}
        members = [r for r in waiting if r["employee_id"] in ids]
        prepared.append((vehicle, stops, members))
    for vehicle, stops, members in prepared:
        start_trip(context, vehicle, stops, members)
    log_event(
        context,
        "optimization",
        f"Route optimization: {result['status']}",
        result=result,
    )


def start_trip(context, vehicle, stops, members):
    assert feasible(stops, vehicle["capacity"])
    clock = context["now"]
    previous = vehicle["location_id"]
    legs = []
    geometry = []
    timed_stops = []
    trip_id = f"TRIP-{len(context['trips']) + 1:03d}"
    for stop in stops:
        seconds = (
            context["network"].duration(previous, stop["location_id"])
            * context["config"]["travel_time_multiplier"]
        )
        arrival = clock + timedelta(seconds=seconds)
        coords = context["network"].coordinates(previous, stop["location_id"])
        legs.append(
            {
                "from": previous,
                "to": stop["location_id"],
                "departure": clock,
                "arrival": arrival,
                "distance_km": context["network"].distance(
                    previous, stop["location_id"]
                ),
                "coordinates": coords,
                "road_geometry": context["network"].geometry_is_road(
                    previous, stop["location_id"]
                ),
            }
        )
        geometry.extend(coords if not geometry else coords[1:])
        timed_stops.append({**stop, "arrival_at": arrival, "visited": False})
        clock = arrival + timedelta(seconds=context["config"]["stop_service_seconds"])
        previous = stop["location_id"]
    trip = {
        "trip_id": trip_id,
        "vehicle_id": vehicle["vehicle_id"],
        "color": vehicle["color"],
        "capacity": int(vehicle["capacity"]),
        "employee_ids": [r["employee_id"] for r in members],
        "passenger_count": sum((r["passengers"] for r in members)),
        "start_location_id": vehicle["location_id"],
        "departed_at": context["now"],
        "finished_at": clock,
        "distance_km": round(sum((x["distance_km"] for x in legs)), 6),
        "planned_duration_min": round((clock - context["now"]).total_seconds() / 60, 3),
        "status": "active",
        "stops": timed_stops,
        "legs": legs,
        "geometry": geometry,
        "geometry_source": (
            "OSRM roads"
            if all((x["road_geometry"] for x in legs))
            else "some uncached legs shown as straight lines"
        ),
    }
    context["trips"].append(trip)
    vehicle["active_trip"] = trip
    for request in members:
        request.update(
            status="assigned", vehicle_id=vehicle["vehicle_id"], trip_id=trip_id
        )
        log_event(
            context,
            "assigned",
            f"{request['employee_id']} → {vehicle['vehicle_id']}",
            employee_id=request["employee_id"],
            vehicle_id=vehicle["vehicle_id"],
            trip_id=trip_id,
        )
    log_event(
        context,
        "dispatch",
        f"{vehicle['vehicle_id']} dispatched: {len(members)} passengers, {trip['distance_km']:.1f} km",
        vehicle_id=vehicle["vehicle_id"],
        trip_id=trip_id,
    )


def advance_vehicles(context):
    for vehicle in context["vehicles"].values():
        trip = vehicle["active_trip"]
        if trip is None:
            continue
        for stop, leg in zip(trip["stops"], trip["legs"]):
            if not stop["visited"] and stop["arrival_at"] <= context["now"]:
                request = context["requests"][stop["employee_id"]]
                if stop["kind"] == "pickup":
                    check_wait(context, request, stop["arrival_at"])
                    request.update(status="onboard", picked_up_at=stop["arrival_at"])
                    vehicle["onboard"] += stop["passengers"]
                    log_event(
                        context,
                        "pickup",
                        f"{stop['employee_id']} boarded {vehicle['vehicle_id']}",
                        at=stop["arrival_at"],
                        employee_id=stop["employee_id"],
                        vehicle_id=vehicle["vehicle_id"],
                    )
                else:
                    request.update(status="delivered", delivered_at=stop["arrival_at"])
                    vehicle["onboard"] -= stop["passengers"]
                    log_event(
                        context,
                        "dropoff",
                        f"{stop['employee_id']} delivered",
                        at=stop["arrival_at"],
                        employee_id=stop["employee_id"],
                        vehicle_id=vehicle["vehicle_id"],
                    )
                assert 0 <= vehicle["onboard"] <= vehicle["capacity"]
                vehicle["location_id"] = stop["location_id"]
                vehicle["completed_km"] += leg["distance_km"]
                stop["visited"] = True
        partial = 0.0
        for stop, leg in zip(trip["stops"], trip["legs"]):
            if stop["visited"]:
                continue
            total = (leg["arrival"] - leg["departure"]).total_seconds()
            fraction = (
                np.clip(
                    (context["now"] - leg["departure"]).total_seconds() / total, 0, 1
                )
                if total > 0
                else 0
            )
            partial = float(fraction) * leg["distance_km"]
            break
        vehicle["travelled_km"] = vehicle["completed_km"] + partial
        if context["now"] >= trip["finished_at"]:
            trip["status"] = "completed"
            vehicle["active_trip"] = None
            log_event(
                context,
                "trip_completed",
                f"{vehicle['vehicle_id']} completed its trip and is available",
                at=trip["finished_at"],
                trip_id=trip["trip_id"],
                vehicle_id=vehicle["vehicle_id"],
            )


def check_wait(context, request, until):
    wait = (until - request["requested_at"]).total_seconds() / 60
    if (
        wait > context["config"]["max_wait_minutes"]
        and request["employee_id"] not in context["warned"]
    ):
        context["warned"].add(request["employee_id"])
        log_event(
            context,
            "wait_alert",
            f"{request['employee_id']}: waiting time {wait:.1f} min exceeds the {context['config']['max_wait_minutes']} min target",
            at=until,
            employee_id=request["employee_id"],
            waiting_min=wait,
        )


def detect_events(context):
    for request in context["requests"].values():
        if request["status"] in ["waiting", "assigned"]:
            check_wait(context, request, context["now"])


def process_minute(context):
    if context["finished"]:
        return get_state(context)
    context["now"] += timedelta(seconds=context["config"]["tick_seconds"])
    advance_vehicles(context)
    acquire_events(context)
    elapsed = int((context["now"] - context["started_at"]).total_seconds() / 60)
    if elapsed % context["config"]["dispatch_interval_minutes"] == 0:
        assign_routes(context)
        advance_vehicles(context)
    detect_events(context)
    if (
        context["next_event"] is None
        and context["requests"]
        and all((r["status"] == "delivered" for r in context["requests"].values()))
        and all((v["active_trip"] is None for v in context["vehicles"].values()))
    ):
        context["finished"] = True
        log_event(
            context,
            "finished",
            "All received requests have been processed; all trips are complete",
        )
    if elapsed >= context["config"]["max_simulation_minutes"] and (
        not context["finished"]
    ):
        raise RuntimeError("Simulation time limit reached with unfinished requests")
    update_statistics(context)
    state = get_state(context)
    if context["finished"]:
        context["store"].export(state, context["metrics"], manifest(context))
        from visualization import save_visualizations

        save_visualizations(state, context["locations"], context["output"])
    return state


def update_statistics(context):
    metric = calculate_metrics(
        context["now"],
        context["requests"],
        context["vehicles"],
        context["arrival_times"],
        context["metrics"],
        context["rejected"],
    )
    context["metrics"].append(metric)
    context["store"].snapshot(get_state(context))


def vehicle_position(context, vehicle):
    trip = vehicle["active_trip"]
    if trip:
        for stop, leg in zip(trip["stops"], trip["legs"]):
            if stop["visited"]:
                continue
            seconds = (leg["arrival"] - leg["departure"]).total_seconds()
            fraction = (
                np.clip(
                    (context["now"] - leg["departure"]).total_seconds() / seconds, 0, 1
                )
                if seconds > 0
                else 0
            )
            lon, lat = position_on_line(leg["coordinates"], float(fraction))
            return [lat, lon]
    p = context["locations"][vehicle["location_id"]]
    return [p["lat"], p["lon"]]


def get_state(context):
    requests = []
    for request in context["requests"].values():
        item = {k: iso(v) for k, v in request.items() if k not in ["cleaning_notes"]}
        until = request["picked_up_at"] or context["now"]
        item["waiting_min"] = round(
            (until - request["requested_at"]).total_seconds() / 60, 2
        )
        item["ride_min"] = (
            round(
                (
                    (request["delivered_at"] or context["now"])
                    - request["picked_up_at"]
                ).total_seconds()
                / 60,
                2,
            )
            if request["picked_up_at"]
            else None
        )
        item["home_coordinates"] = [
            context["locations"][request["home_id"]]["lat"],
            context["locations"][request["home_id"]]["lon"],
        ]
        item["cleaning_notes"] = request["cleaning_notes"]
        requests.append(item)
    vehicles = []
    for vehicle in context["vehicles"].values():
        trip = vehicle["active_trip"]
        vehicles.append(
            {
                "vehicle_id": vehicle["vehicle_id"],
                "capacity": int(vehicle["capacity"]),
                "color": vehicle["color"],
                "onboard": vehicle["onboard"],
                "status": "busy" if trip else "available",
                "trip_id": trip["trip_id"] if trip else None,
                "position": vehicle_position(context, vehicle),
                "travelled_km": round(vehicle["travelled_km"], 3),
                "initial_location_id": vehicle["initial_location_id"],
            }
        )
    trips = []
    for trip in context["trips"]:
        item = {k: iso(v) for k, v in trip.items() if k not in ["stops", "legs"]}
        item["stops"] = [{k: iso(v) for k, v in stop.items()} for stop in trip["stops"]]
        item["legs"] = [{k: iso(v) for k, v in leg.items()} for leg in trip["legs"]]
        trips.append(item)
    metric = context["metrics"][-1] if context["metrics"] else {}
    active_alerts = sum(
        (
            r["status"] in ["waiting", "assigned"]
            and (context["now"] - r["requested_at"]).total_seconds() / 60
            > context["config"]["max_wait_minutes"]
            for r in context["requests"].values()
        )
    )
    summary = {
        **metric,
        "raw_records": context["raw_received"],
        "valid_requests": len(requests),
        "trips": len(trips),
        "cleaning_operations": context["cleaning_count"],
        "wait_alerts_total": len(context["warned"]),
        "finished": context["finished"],
    }
    return {
        "timestamp": iso(context["now"]),
        "finished": context["finished"],
        "config": context["config"],
        "metrics": metric,
        "history": context["metrics"],
        "requests": requests,
        "vehicles": vehicles,
        "trips": trips,
        "events": context["events"][-60:],
        "workplaces": [
            p for p in context["locations"].values() if p["kind"] == "workplace"
        ],
        "workplace_summary": workplace_summary(context["requests"]),
        "active_alerts": active_alerts,
        "summary": summary,
        "optimization_runs": context["optimization_runs"],
    }


def manifest(context):
    import shutil

    cache = context["root"] / "cache"
    destination = context["output"] / "inputs" / "cache"
    shutil.copytree(cache, destination, dirs_exist_ok=True)
    return {
        "config": context["config"],
        "optimization_runs": context["optimization_runs"],
        "input_sha256": {
            str(p.relative_to(context["output"] / "inputs")): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in (context["output"] / "inputs").rglob("*")
            if p.is_file()
        },
        "reproducibility": "Use inputs/input.json, inputs/config.json and inputs/cache; fixed seed and one solver worker.",
        "limitations": [
            "Synthetic employees",
            "Static OSRM fastest-road travel estimates, no live traffic",
            "Known requests only; active routes frozen",
            "OPTIMAL refers to the current integer model only",
            "Finite solver budget can return FEASIBLE or a feasible fallback",
            "Waiting is a soft target; unreachable addresses are reported",
            "Cached OSM building centres are not surveyed entrances",
        ],
    }


def close_processing(context):
    context["store"].close()
