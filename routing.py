import json
import math
import hashlib
import numpy as np
from ortools.sat.python import cp_model
from geocoding import read_cache, write_cache, fetch_json, service_lock


def point_key(point):
    return f"{point['lon']:.7f},{point['lat']:.7f}"


class RoadNetwork:
    def __init__(self, root, locations, config):
        self.root = root
        self.locations = locations
        self.config = config
        self.used_edges = set()

    def edge_key(self, a, b):
        return point_key(self.locations[a]) + "|" + point_key(self.locations[b])

    def save_response(self, url, response):
        name = hashlib.sha256(url.encode()).hexdigest() + ".json"
        write_cache(
            self.root / "cache" / "responses" / name, {"url": url, "response": response}
        )

    def prepare(self, ids):
        ids = list(dict.fromkeys(ids))
        with service_lock:
            path = self.root / "cache" / "roads.json"
            edges = read_cache(path)
            missing = any(self.edge_key(a, b) not in edges for a in ids for b in ids)
            if missing:
                if self.config.get("offline"):
                    raise ValueError(
                        "Road data is not cached; an internet connection is required"
                    )
                coordinates = ";".join(point_key(self.locations[id]) for id in ids)
                url = (
                    self.config["osrm_url"].rstrip("/")
                    + "/table/v1/driving/"
                    + coordinates
                    + "?annotations=distance,duration"
                )
                response = fetch_json(url, self.config, "OSRM")
                if response.get("code") != "Ok":
                    raise ValueError(
                        f"OSRM could not build the road table: {response.get('message', response.get('code'))}"
                    )
                self.save_response(url, response)
                for i, a in enumerate(ids):
                    for j, b in enumerate(ids):
                        distance = response["distances"][i][j]
                        duration = response["durations"][i][j]
                        if (
                            distance is None
                            or duration is None
                            or not np.isfinite([distance, duration]).all()
                        ):
                            raise ValueError(
                                f"No driving route: {self.locations[a]['address']} → {self.locations[b]['address']}"
                            )
                        edges[self.edge_key(a, b)] = {
                            "distance_m": distance,
                            "duration_s": duration,
                        }
                write_cache(path, edges)
            self.edges = edges

    def distance(self, a, b):
        return self.edges[self.edge_key(a, b)]["distance_m"] / 1000

    def duration(self, a, b):
        raw = self.edges[self.edge_key(a, b)]["duration_s"]
        multiplier = self.config["travel_time_multiplier"]
        return math.ceil(raw * multiplier) / multiplier

    def coordinates(self, a, b):
        self.used_edges.add((a, b))
        if a == b or point_key(self.locations[a]) == point_key(self.locations[b]):
            p = self.locations[a]
            return [[p["lon"], p["lat"]], [p["lon"], p["lat"]]]
        with service_lock:
            path = self.root / "cache" / "geometry.json"
            cache = read_cache(path)
            key = self.edge_key(a, b)
            if key not in cache:
                if self.config.get("offline"):
                    raise ValueError(
                        "Road geometry is not cached; an internet connection is required"
                    )
                url = (
                    self.config["osrm_url"].rstrip("/")
                    + "/route/v1/driving/"
                    + key.replace("|", ";")
                    + "?overview=full&geometries=geojson&steps=false"
                )
                response = fetch_json(url, self.config, "OSRM")
                if response.get("code") != "Ok" or not response.get("routes"):
                    raise ValueError("OSRM could not find a road route for the map")
                self.save_response(url, response)
                route = response["routes"][0]
                cache[key] = {
                    "coordinates": route["geometry"]["coordinates"],
                    "distance_m": route["distance"],
                    "duration_s": route["duration"],
                }
                write_cache(path, cache)
            return cache[key]["coordinates"]

    def geometry_is_road(self, a, b):
        return True


def feasible(stops, capacity):
    load = 0
    picked = set()
    dropped = set()
    for stop in stops:
        employee = stop["employee_id"]
        if stop["kind"] == "pickup":
            if employee in picked:
                return False
            picked.add(employee)
            load += stop["passengers"]
        else:
            if employee not in picked or employee in dropped:
                return False
            dropped.add(employee)
            load -= stop["passengers"]
        if not 0 <= load <= capacity:
            return False
    return load == 0 and picked == dropped


def optimize_routes(network, vehicles, requests, now, started_at, config):
    if not vehicles or not requests:
        return {}, {"status": "NO_ASSIGNMENT"}
    stops = []
    for request in requests:
        for kind, field in [("pickup", "workplace_id"), ("dropoff", "home_id")]:
            stops.append(
                {
                    "kind": kind,
                    "employee_id": request["employee_id"],
                    "location_id": request[field],
                    "passengers": request["passengers"],
                }
            )
    count = len(stops)
    network.prepare(
        [v["location_id"] for v in vehicles] + [s["location_id"] for s in stops]
    )
    model = cp_model.CpModel()
    horizon = config["max_simulation_minutes"] * 60
    current = int((now - started_at).total_seconds())
    service = config["stop_service_seconds"]

    def travel(a, b):
        return round(network.duration(a, b) * config["travel_time_multiplier"])

    times = [model.new_int_var(current, horizon, f"time_{i}") for i in range(count)]
    loads = [
        model.new_int_var(0, max(v["capacity"] for v in vehicles), f"load_{i}")
        for i in range(count)
    ]
    assigned = {}
    arcs = {}
    distances = []
    for k, vehicle in enumerate(vehicles):
        start_id = vehicle["location_id"]
        used = model.new_bool_var(f"used_{k}")
        starts = []
        ends = []
        for i, stop in enumerate(stops):
            assigned[k, i] = model.new_bool_var(f"assigned_{k}_{i}")
            model.add(loads[i] <= vehicle["capacity"]).only_enforce_if(assigned[k, i])
            arcs[k, -1, i] = model.new_bool_var(f"start_{k}_{i}")
            arcs[k, i, count] = model.new_bool_var(f"end_{k}_{i}")
            starts.append(arcs[k, -1, i])
            ends.append(arcs[k, i, count])
            delta = (
                stop["passengers"] if stop["kind"] == "pickup" else -stop["passengers"]
            )
            model.add(
                times[i] == current + travel(start_id, stop["location_id"])
            ).only_enforce_if(arcs[k, -1, i])
            model.add(loads[i] == delta).only_enforce_if(arcs[k, -1, i])
            model.add(loads[i] == 0).only_enforce_if(arcs[k, i, count])
            distances.append(
                (
                    arcs[k, -1, i],
                    round(network.distance(start_id, stop["location_id"]) * 1000),
                )
            )
        model.add(sum(starts) == used)
        model.add(sum(ends) == used)
        for i in range(count):
            for j in range(count):
                if i == j:
                    continue
                arc = model.new_bool_var(f"arc_{k}_{i}_{j}")
                arcs[k, i, j] = arc
                delta = (
                    stops[j]["passengers"]
                    if stops[j]["kind"] == "pickup"
                    else -stops[j]["passengers"]
                )
                model.add(
                    times[j]
                    == times[i]
                    + service
                    + travel(stops[i]["location_id"], stops[j]["location_id"])
                ).only_enforce_if(arc)
                model.add(loads[j] == loads[i] + delta).only_enforce_if(arc)
                distances.append(
                    (
                        arc,
                        round(
                            network.distance(
                                stops[i]["location_id"], stops[j]["location_id"]
                            )
                            * 1000
                        ),
                    )
                )
        for i in range(count):
            incoming = [arcs[k, -1, i]] + [
                arcs[k, j, i] for j in range(count) if j != i
            ]
            outgoing = [arcs[k, i, count]] + [
                arcs[k, i, j] for j in range(count) if j != i
            ]
            model.add(sum(incoming) == assigned[k, i])
            model.add(sum(outgoing) == assigned[k, i])
        for r in range(len(requests)):
            model.add(assigned[k, 2 * r] == assigned[k, 2 * r + 1])
    for i in range(count):
        model.add(sum(assigned[k, i] for k in range(len(vehicles))) == 1)
    late_flags = []
    excesses = []
    for r, request in enumerate(requests):
        pickup = 2 * r
        dropoff = pickup + 1
        requested = int((request["requested_at"] - started_at).total_seconds())
        threshold = requested + config["max_wait_minutes"] * 60
        model.add(times[pickup] >= requested)
        model.add(times[dropoff] >= times[pickup] + service)
        late = model.new_bool_var(f"late_{r}")
        model.add(times[pickup] > threshold).only_enforce_if(late)
        model.add(times[pickup] <= threshold).only_enforce_if(late.Not())
        excess = model.new_int_var(0, horizon, f"excess_{r}")
        model.add_max_equality(excess, [times[pickup] - threshold, 0])
        late_flags.append(late)
        excesses.append(excess)
    max_distance = count * max([d for _, d in distances] + [1])
    excess_weight = max_distance + 1
    late_weight = len(requests) * horizon * excess_weight + max_distance + 1
    if len(requests) * late_weight > 2**62:
        raise ValueError(
            "Distances are too large for this model; use addresses within one city"
        )
    distance_cost = sum(arc * distance for arc, distance in distances)
    model.minimize(
        sum(late_flags) * late_weight + sum(excesses) * excess_weight + distance_cost
    )
    hint_paths = {k: [] for k in range(len(vehicles))}
    hint_clock = [current] * len(vehicles)
    hint_location = [v["location_id"] for v in vehicles]
    hint_times = {}
    for r, request in enumerate(requests):
        options = []
        for k, vehicle in enumerate(vehicles):
            if request["passengers"] <= vehicle["capacity"]:
                pickup_at = hint_clock[k] + travel(
                    hint_location[k], request["workplace_id"]
                )
                options.append((pickup_at, k))
        pickup_at, k = min(options)
        drop_at = (
            pickup_at + service + travel(request["workplace_id"], request["home_id"])
        )
        hint_paths[k].extend([2 * r, 2 * r + 1])
        hint_times[2 * r] = pickup_at
        hint_times[2 * r + 1] = drop_at
        hint_clock[k] = drop_at + service
        hint_location[k] = request["home_id"]
    if max(hint_clock) <= horizon:
        chosen = set()
        for k, path in hint_paths.items():
            if path:
                chosen.add((k, -1, path[0]))
                chosen.update((k, a, b) for a, b in zip(path, path[1:]))
                chosen.add((k, path[-1], count))
            for i in range(count):
                model.add_hint(assigned[k, i], int(i in path))
        for key, arc in arcs.items():
            model.add_hint(arc, int(key in chosen))
        for i in range(count):
            model.add_hint(times[i], hint_times[i])
            model.add_hint(
                loads[i], stops[i]["passengers"] if stops[i]["kind"] == "pickup" else 0
            )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.get("solver_seconds", 2)
    solver.parameters.max_deterministic_time = config.get("solver_work_limit", 0.1)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = config.get("seed", 240068)
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if status == cp_model.UNKNOWN and max(hint_clock) <= horizon:
            plans = {
                vehicles[k]["vehicle_id"]: [stops[i] for i in path]
                for k, path in hint_paths.items()
                if path
            }
            return plans, {
                "status": "FALLBACK_FEASIBLE",
                "optimality_proven": False,
                "message": "Solver limit reached: using the validated initial feasible plan",
            }
        raise ValueError(
            f"Solver: {solver.status_name(status)}. Increase the simulation horizon or solver limit."
        )
    plans = {}
    for k, vehicle in enumerate(vehicles):
        first = next((i for i in range(count) if solver.value(arcs[k, -1, i])), None)
        if first is None:
            continue
        path = []
        node = first
        while node != count:
            path.append(stops[node])
            node = next(
                j
                for j in range(count + 1)
                if j != node and (k, node, j) in arcs and solver.value(arcs[k, node, j])
            )
        if not feasible(path, vehicle["capacity"]):
            raise RuntimeError("The solver returned an invalid route")
        plans[vehicle["vehicle_id"]] = path
    return plans, {
        "status": solver.status_name(status),
        "optimality_proven": status == cp_model.OPTIMAL,
        "late_requests": sum(solver.value(x) for x in late_flags),
        "excess_wait_seconds": sum(solver.value(x) for x in excesses),
        "distance_m": solver.value(distance_cost),
        "objective": solver.objective_value,
        "best_bound": solver.best_objective_bound,
        "wall_time_seconds": solver.wall_time,
    }


def position_on_line(coordinates, fraction):
    coords = np.asarray(coordinates, dtype=float)
    lengths = np.sqrt(
        np.sum(
            (np.diff(coords, axis=0) * [np.cos(np.deg2rad(coords[:, 1].mean())), 1])
            ** 2,
            axis=1,
        )
    )
    if not lengths.sum():
        return coords[-1].tolist()
    cumulative = np.concatenate([[0], np.cumsum(lengths)])
    target = float(np.clip(fraction, 0, 1)) * lengths.sum()
    index = min(
        int(np.searchsorted(cumulative, target, side="right") - 1), len(lengths) - 1
    )
    ratio = (target - cumulative[index]) / lengths[index] if lengths[index] else 0
    return (coords[index] + (coords[index + 1] - coords[index]) * ratio).tolist()
