import threading
import time


def event_generator(employees):
    ordered = sorted(
        employees, key=lambda row: (row["request_timestamp"], row["employee_id"])
    )
    for row in ordered:
        yield {
            "event_id": "REQ-" + row["employee_id"],
            "event_type": "ride_request",
            "employee_id": row["employee_id"],
            "workplace_id": row["workplace_id"],
            "home_id": row["home_id"],
            "event_timestamp": row["request_timestamp"],
            "arrival_timestamp": row["request_timestamp"],
            "passengers": row["passengers"],
        }


def print_results(state, new_events):
    metrics = state["metrics"]
    current_time = state["timestamp"][11:16]

    event_names = {
        "request": "New request",
        "assigned": "Car assigned",
        "dispatch": "Trip started",
        "optimization": "Route calculation",
        "pickup": "Passenger picked up",
        "dropoff": "Passenger delivered",
        "trip_completed": "Trip completed",
    }

    if new_events:
        for event in new_events:
            event_time = event["timestamp"][11:19]
            event_name = event_names.get(
                event["type"],
                event["type"].replace("_", " ").capitalize(),
            )
            print(
                f"  {event_time} | {event_name}: {event['message']}",
                flush=True,
            )
    else:
        print("  No new events.", flush=True)

    print("\nReal-time indicators:", flush=True)

    print(
        f"  1. Requests received: {metrics['received']}"
        "\n     Total requests received since the simulation started.",
        flush=True,
    )

    print(
        f"\n  2. Passengers waiting: {metrics['waiting']}"
        "\n     Passengers who have requested a ride but are not yet in a car.",
        flush=True,
    )

    print(
        f"\n  3. Passengers delivered: {metrics['delivered']}"
        "\n     Passengers who have reached their home.",
        flush=True,
    )

    print(
        f"\n  4. Average current waiting time: "
        f"{metrics['mean_waiting_min']:.1f} minutes"
        "\n     Average waiting time of requests still awaiting pickup.",
        flush=True,
    )

    print(
        f"\n  5. Maximum current waiting time: "
        f"{metrics['max_waiting_min']:.1f} minutes"
        "\n     Longest waiting time among requests still awaiting pickup.",
        flush=True,
    )

    if metrics["waiting"] == 0:
        print(
            "\n  Nobody is currently waiting. Current waiting times are zero.",
            flush=True,
        )


def simulate_stream(context, delay=0.8):
    from processing import process_minute

    while not context["finished"]:
        before = len(context["events"])
        state = process_minute(context)
        print_results(state, context["events"][before:])
        yield state
        if delay and not context["finished"]:
            time.sleep(delay)


def create_session(root, output, delay=0.8, running=True):
    from processing import create_state

    session = {
        "root": root,
        "output": output,
        "delay": delay,
        "running": running,
        "error": None,
        "lock": threading.RLock(),
        "stop": threading.Event(),
        "context": None,
    }
    try:
        session["context"] = create_state(root, output)
    except ValueError as error:
        session["running"] = False
        session["error"] = str(error)
        print(f"ERROR: {error}", flush=True)
    return session


def process_step(session):
    from processing import process_minute

    context = session["context"]
    if context is None:
        raise ValueError(session["error"] or "Scenario not loaded")
    if context["finished"]:
        session["running"] = False
        return
    before = len(context["events"])
    state = process_minute(context)
    print_results(state, context["events"][before:])
    if state["finished"]:
        session["running"] = False
        print(f"Finished. Results: {session['output']}", flush=True)


def run_background_stream(session):
    while not session["stop"].wait(session["delay"]):
        with session["lock"]:
            if session["running"]:
                try:
                    process_step(session)
                except Exception as error:
                    session["running"] = False
                    session["error"] = str(error)
                    print(f"ERROR: {error}", flush=True)


def get_session_state(session):
    from processing import get_state

    with session["lock"]:
        if session["context"] is None:
            return {
                "running": False,
                "error": session["error"],
                "finished": False,
                "metrics": {},
                "requests": [],
                "trips": [],
                "vehicles": [],
                "events": [],
            }
        return {
            **get_state(session["context"]),
            "running": session["running"],
            "error": session["error"],
            "speed": session["delay"],
        }


def control_stream(session, action, payload):
    from processing import create_state, close_processing

    with session["lock"]:
        if action == "reset":
            session["running"] = False
            if session["context"] is not None:
                close_processing(session["context"])
                session["context"] = None
            try:
                session["context"] = create_state(session["root"], session["output"])
                session["error"] = None
            except ValueError as error:
                session["error"] = str(error)
                raise
        elif action == "pause":
            session["running"] = False
        elif action == "speed":
            value = float(payload["value"])
            if not 0.05 <= value <= 60:
                raise ValueError("Speed must be 0.05–60 seconds per simulated minute")
            session["delay"] = value
        else:
            if session["context"] is None:
                raise ValueError(session["error"] or "Load a scenario first")
            if action == "start":
                session["running"] = not session["context"]["finished"]
            elif action == "step":
                session["running"] = False
                process_step(session)
            elif action == "finish":
                session["running"] = False
                while not session["context"]["finished"]:
                    process_step(session)
            else:
                raise ValueError("Unknown command")
        return get_session_state(session)


def calculate_plan(root, output, payload):
    from processing import (
        create_state,
        process_minute,
        get_state,
        close_processing,
        acquire_events,
        assign_routes,
        advance_vehicles,
        update_statistics,
    )

    context = create_state(root, output, payload)
    try:
        mode = payload.get("mode", "batch")
        if mode == "batch":
            from datetime import datetime

            context["now"] = max(
                datetime.fromisoformat(e["request_timestamp"])
                for e in context["employees"].values()
            )
            acquire_events(context)
            assign_routes(context)
            advance_vehicles(context)
            update_statistics(context)
        while not context["finished"]:
            process_minute(context)
        result = get_state(context)
        result["mode"] = mode
        return result
    finally:
        close_processing(context)
