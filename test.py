import itertools
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from fastapi.testclient import TestClient
from api import create_app
from processing import (
    create_state,
    process_minute,
    get_state,
    close_processing,
    process_event,
)
from validation import validate_inputs
from geocoding import geocode_address
from routing import RoadNetwork, optimize_routes, feasible
from streaming import event_generator

root = Path(__file__).resolve().parent


class RoutingTests(unittest.TestCase):
    def test_optimizer_matches_exhaustive_search(self):
        class Network:
            def prepare(self, ids):
                pass

            def duration(self, a, b):
                return abs(int(a) - int(b)) * 100

            def distance(self, a, b):
                return abs(int(a) - int(b))

        network = Network()
        start = datetime.fromisoformat("2026-10-04T22:00:00+05:00")
        requests = [
            {
                "employee_id": "A",
                "workplace_id": "2",
                "home_id": "9",
                "passengers": 1,
                "requested_at": start,
            },
            {
                "employee_id": "B",
                "workplace_id": "3",
                "home_id": "5",
                "passengers": 1,
                "requested_at": start,
            },
        ]
        vehicles = [{"vehicle_id": "V", "capacity": 2, "location_id": "0"}]
        config = {
            "max_simulation_minutes": 240,
            "stop_service_seconds": 30,
            "travel_time_multiplier": 1,
            "max_wait_minutes": 5,
            "solver_seconds": 5,
            "solver_work_limit": 2,
        }
        plans, status = optimize_routes(
            network, vehicles, requests, start, start, config
        )

        def score(stops):
            now = 0
            previous = "0"
            distance = 0
            late = 0
            excess = 0
            for stop in stops:
                now += network.duration(previous, stop["location_id"])
                distance += network.distance(previous, stop["location_id"])
                if stop["kind"] == "pickup":
                    over = max(0, now - 300)
                    late += int(over > 0)
                    excess += over
                now += 30
                previous = stop["location_id"]
            return late, excess, distance

        candidates = [p for p in itertools.permutations(plans["V"]) if feasible(p, 2)]
        self.assertEqual(score(plans["V"]), min(map(score, candidates)))
        self.assertTrue(status["optimality_proven"])

    def test_capacity_with_sequential_groups(self):
        stops = [
            {"employee_id": e, "kind": kind, "passengers": 2}
            for e in ["A", "B"]
            for kind in ["pickup", "dropoff"]
        ]
        self.assertTrue(feasible(stops, 2))
        self.assertFalse(feasible([stops[0], stops[2], stops[1], stops[3]], 2))
        self.assertFalse(feasible([stops[1], stops[0]], 2))


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=root / "results")
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        self.employees = pd.read_csv(root / "data/employees.csv").to_dict("records")
        self.vehicles = pd.read_csv(root / "data/vehicles.csv").to_dict("records")

    def test_csv_validation(self):
        employees, vehicles = validate_inputs(
            self.employees, self.vehicles, self.config
        )
        self.assertEqual(len(employees), 15)
        for changes in [
            {"passengers": -1},
            {"passengers": True},
            {"request_time": "21:59"},
            {"home_address": ""},
        ]:
            with self.assertRaises(ValueError):
                validate_inputs([{**employees[0], **changes}], vehicles, self.config)
        with self.assertRaises(ValueError):
            validate_inputs([employees[0], employees[0]], vehicles, self.config)

    def test_new_address_is_geocoded_and_cached(self):
        response = [
            {
                "lat": "51.12",
                "lon": "71.42",
                "display_name": "New address",
                "osm_id": 123,
            }
        ]
        with patch("geocoding.fetch_json", return_value=response) as fetch:
            first = geocode_address(
                "Astana, New Street, 1", self.directory, self.config
            )
            second = geocode_address(
                "Astana, New Street, 1", self.directory, self.config
            )
            self.assertEqual(first, second)
            self.assertEqual(fetch.call_count, 1)

    def test_ambiguous_and_missing_address(self):
        for response in [[], [{"lat": "51", "lon": "71"}, {"lat": "52", "lon": "72"}]]:
            with patch("geocoding.fetch_json", return_value=response):
                with self.assertRaisesRegex(ValueError, "Specify"):
                    geocode_address("Unknown address", self.directory, self.config)

    def test_multiple_objects_at_same_address_are_deduplicated(self):
        response = [
            {
                "lat": "51.12",
                "lon": "71.42",
                "display_name": "Building",
                "osm_type": "way",
                "address": {"house_number": "1", "road": "Street", "city": "Astana"},
            },
            {
                "lat": "51.121",
                "lon": "71.421",
                "display_name": "Shop",
                "osm_type": "node",
                "address": {"house_number": "1", "road": "Street", "city": "Astana"},
            },
        ]
        with patch("geocoding.fetch_json", return_value=response):
            result = geocode_address("Astana, Street, 1", self.directory, self.config)
            self.assertEqual(result["lat"], 51.12)

    def test_offline_unknown_address(self):
        with self.assertRaisesRegex(ValueError, "No cached coordinates"):
            geocode_address(
                "Unknown address", self.directory, {**self.config, "offline": True}
            )

    def test_roads_are_fetched_and_failures_not_hidden(self):
        locations = {
            "A": {"lat": 51, "lon": 71, "address": "A"},
            "B": {"lat": 51.1, "lon": 71.1, "address": "B"},
        }
        network = RoadNetwork(self.directory, locations, self.config)
        response = {
            "code": "Ok",
            "distances": [[0, 1234], [1500, 0]],
            "durations": [[0, 100], [120, 0]],
        }
        with patch("routing.fetch_json", return_value=response) as fetch:
            network.prepare(["A", "B"])
            network.prepare(["A", "B"])
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(network.distance("A", "B"), 1.234)
        network.config = {**self.config, "offline": True}
        with self.assertRaisesRegex(ValueError, "geometry"):
            network.coordinates("A", "B")

    def test_unreachable_road_rejected(self):
        locations = {
            "A": {"lat": 51, "lon": 71, "address": "A"},
            "B": {"lat": 51.1, "lon": 71.1, "address": "B"},
        }
        response = {
            "code": "Ok",
            "distances": [[0, None], [None, 0]],
            "durations": [[0, None], [None, 0]],
        }
        with patch("routing.fetch_json", return_value=response):
            with self.assertRaisesRegex(ValueError, "No driving route"):
                RoadNetwork(self.directory, locations, self.config).prepare(["A", "B"])

    def test_generator_uses_updated_csv(self):
        scenario = self.directory / "scenario"
        shutil.copytree(root / "data", scenario / "data")
        shutil.copytree(root / "cache", scenario / "cache")
        shutil.copy2(root / "config.json", scenario / "config.json")
        frame = pd.DataFrame(self.employees[:2])
        frame.loc[0, "request_time"] = "22:12"
        frame.to_csv(scenario / "data/employees.csv", index=False)
        context = create_state(
            scenario, scenario / "results", config_override={"offline": True}
        )
        try:
            source = list(event_generator(list(context["employees"].values())))
            self.assertEqual(source[1]["arrival_timestamp"][11:16], "22:12")
            for minute in range(4):
                self.assertEqual(process_minute(context)["metrics"]["received"], 0)
        finally:
            close_processing(context)


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=root / "results")
        cls.directory = Path(cls.temp.name)
        cls.context = create_state(
            root, cls.directory, config_override={"offline": True}
        )
        cls.snapshots = [get_state(cls.context)]
        while not cls.context["finished"]:
            cls.snapshots.append(process_minute(cls.context))
        cls.final = cls.snapshots[-1]

    @classmethod
    def tearDownClass(cls):
        close_processing(cls.context)
        cls.temp.cleanup()

    def test_all_delivered_capacity_precedence_and_reuse(self):
        self.assertEqual(self.final["metrics"]["delivered"], 15)
        drops = []
        for trip in self.final["trips"]:
            self.assertTrue(feasible(trip["stops"], trip["capacity"]))
            drops.extend(
                s["employee_id"] for s in trip["stops"] if s["kind"] == "dropoff"
            )
        self.assertEqual(len(drops), len(set(drops)))
        self.assertGreater(len(self.final["trips"]), 4)
        for state in self.snapshots:
            for request in state["requests"]:
                self.assertLessEqual(request["arrived_at"], state["timestamp"])
            for vehicle in state["vehicles"]:
                self.assertLessEqual(vehicle["onboard"], vehicle["capacity"])

    def test_window_and_wait_alerts(self):
        states = {s["timestamp"][11:16]: s for s in self.snapshots}
        self.assertEqual(states["22:06"]["metrics"]["request_rate_per_min"], 0.4)
        self.assertEqual(states["22:11"]["metrics"]["requests_last_5min"], 0)
        long_waits = {
            r["employee_id"] for r in self.final["requests"] if r["waiting_min"] > 15
        }
        self.assertEqual(self.context["warned"], long_waits)

    def test_quality_detector(self):
        source = next(event_generator(list(self.context["employees"].values())))
        before = self.context["rejected"]
        process_event(self.context, source)
        process_event(self.context, {**source, "event_id": "BAD", "passengers": -1})
        self.assertEqual(self.context["rejected"], before + 2)

    def test_saved_artifacts(self):
        for name in [
            "summary.json",
            "requests.csv",
            "trips.csv",
            "stops.csv",
            "metrics.csv",
            "dispatch.sqlite",
            "routes.geojson",
            "map.html",
            "indicators.png",
            "inputs/input.json",
            "inputs/cache/roads.json",
        ]:
            self.assertTrue((self.directory / name).is_file(), name)
        self.assertTrue(
            all(t["geometry_source"] == "OSRM roads" for t in self.final["trips"])
        )

    def test_api_docs_json_map_and_controls(self):
        with patch("streaming.print_results"):
            with TestClient(
                create_app(self.directory / "api", running=False)
            ) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.get("/docs").status_code, 200)
                schema = client.get("/openapi.json").json()
                self.assertIn("/api/optimize", schema["paths"])
                self.assertEqual(client.get("/map").status_code, 200)
                self.assertEqual(
                    client.post("/api/control/step").json()["timestamp"][11:16], "22:01"
                )
                self.assertEqual(
                    client.post("/api/control/speed", json={"value": -1}).status_code,
                    400,
                )
                self.assertEqual(
                    client.post(
                        "/api/optimize", json={"employees": [], "vehicles": []}
                    ).status_code,
                    422,
                )
                sample = client.get("/api/example").json()
                result = client.post("/api/optimize", json=sample)
                self.assertEqual(result.status_code, 200, result.text)
                response = result.json()
                self.assertEqual(response["summary"]["delivered"], 3)
                self.assertTrue(response["routes"])
                self.assertEqual(client.get(response["map_url"]).status_code, 200)
                self.assertEqual(client.get(response["graph_url"]).status_code, 200)
                self.assertEqual(client.get("/maps/not-a-plan").status_code, 404)
                self.assertEqual(
                    client.post("/api/control/reset").json()["metrics"]["received"], 0
                )


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
