import pandas as pd
import numpy as np


def calculate_metrics(
    now, requests, vehicles, arrival_times, previous_metrics, rejected
):
    frame = pd.DataFrame(list(requests.values()))
    pending = (
        frame[frame["status"].isin(["waiting", "assigned"])].copy()
        if len(frame)
        else pd.DataFrame()
    )
    waiting_minutes = np.array(
        [
            (now - r["requested_at"]).total_seconds() / 60
            for r in pending.to_dict("records")
        ],
        dtype=float,
    )
    actual_waits = [
        (r["picked_up_at"] - r["requested_at"]).total_seconds() / 60
        for r in requests.values()
        if r["picked_up_at"] is not None
    ]
    cutoff = now - pd.Timedelta(minutes=5)
    recent = sum((cutoff < t <= now for t in arrival_times))
    current_tick = sum(
        (now - pd.Timedelta(minutes=1) < t <= now for t in arrival_times)
    )
    rolling_counts = [m["arrivals_this_minute"] for m in previous_metrics[-4:]] + [
        current_tick
    ]
    capacities = np.array([v["capacity"] for v in vehicles.values()], dtype=float)
    onboard = np.array([v["onboard"] for v in vehicles.values()], dtype=float)
    return {
        "timestamp": now.isoformat(),
        "received": len(requests),
        "waiting": int(pending["passengers"].sum()) if len(pending) else 0,
        "unassigned": sum((r["status"] == "waiting" for r in requests.values())),
        "onboard": int(onboard.sum()),
        "delivered": sum(
            r["passengers"] for r in requests.values() if r["status"] == "delivered"
        ),
        "mean_waiting_min": (
            round(float(waiting_minutes.mean()), 3) if len(waiting_minutes) else 0.0
        ),
        "max_waiting_min": (
            round(float(waiting_minutes.max()), 3) if len(waiting_minutes) else 0.0
        ),
        "mean_pickup_wait_min": (
            round(float(np.mean(actual_waits)), 3) if actual_waits else 0.0
        ),
        "fleet_occupancy_pct": round(float(onboard.sum() / capacities.sum() * 100), 3),
        "busy_vehicles": sum((v["active_trip"] is not None for v in vehicles.values())),
        "requests_last_5min": recent,
        "request_rate_per_min": round(recent / 5, 3),
        "arrivals_this_minute": current_tick,
        "rolling_arrival_std_5min": round(float(np.std(rolling_counts, ddof=0)), 3),
        "distance_travelled_km": round(
            sum((v["travelled_km"] for v in vehicles.values())), 3
        ),
        "rejected_records": rejected,
    }


def workplace_summary(requests):
    if not requests:
        return []
    frame = pd.DataFrame(list(requests.values()))
    frame["is_waiting"] = frame["status"].isin(["waiting", "assigned"]).astype(int)
    frame["is_delivered"] = frame["status"].eq("delivered").astype(int)
    return (
        frame.groupby(["workplace_id", "workplace_name"], sort=False)
        .agg(
            received=("employee_id", "count"),
            waiting=("is_waiting", "sum"),
            delivered=("is_delivered", "sum"),
        )
        .reset_index()
        .to_dict("records")
    )
