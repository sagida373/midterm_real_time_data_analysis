import json, sqlite3
from pathlib import Path
import pandas as pd


class ResultStore:

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            directory / "dispatch.sqlite", check_same_thread=False
        )
        self.connection.executescript(
            "\n        CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, timestamp TEXT, type TEXT, payload TEXT);\n        CREATE TABLE IF NOT EXISTS metrics (timestamp TEXT PRIMARY KEY, payload TEXT);\n        CREATE TABLE IF NOT EXISTS requests (employee_id TEXT PRIMARY KEY, status TEXT, payload TEXT);\n        CREATE TABLE IF NOT EXISTS trips (trip_id TEXT PRIMARY KEY, vehicle_id TEXT, payload TEXT);\n        DELETE FROM events; DELETE FROM metrics; DELETE FROM requests; DELETE FROM trips;\n        "
        )
        self.logfile = (directory / "events_processed.jsonl").open(
            "w", encoding="utf-8"
        )

    def event(self, event):
        serialized = json.dumps(event, ensure_ascii=False, allow_nan=False)
        self.connection.execute(
            "INSERT INTO events(timestamp,type,payload) VALUES (?,?,?)",
            (event["timestamp"], event["type"], serialized),
        )
        self.logfile.write(serialized + "\n")
        self.logfile.flush()

    def snapshot(self, state):
        metric = state["metrics"]
        self.connection.execute(
            "INSERT OR REPLACE INTO metrics VALUES (?,?)",
            (metric["timestamp"], json.dumps(metric, ensure_ascii=False)),
        )
        for request in state["requests"]:
            self.connection.execute(
                "INSERT OR REPLACE INTO requests VALUES (?,?,?)",
                (
                    request["employee_id"],
                    request["status"],
                    json.dumps(request, ensure_ascii=False),
                ),
            )
        for trip in state["trips"]:
            self.connection.execute(
                "INSERT OR REPLACE INTO trips VALUES (?,?,?)",
                (
                    trip["trip_id"],
                    trip["vehicle_id"],
                    json.dumps(trip, ensure_ascii=False),
                ),
            )
        self.connection.commit()
        (self.directory / "latest_state.json").write_text(
            json.dumps(state, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )

    def export(self, state, metrics, source_manifest):
        for filename, rows in [
            ("metrics.csv", metrics),
            ("requests.csv", state["requests"]),
        ]:
            pd.DataFrame(rows).to_csv(
                self.directory / filename, index=False, encoding="utf-8-sig"
            )
        trip_rows = []
        stops = []
        features = []
        for trip in state["trips"]:
            trip_rows.append(
                {
                    k: v
                    for k, v in trip.items()
                    if k not in ["stops", "geometry", "legs", "employee_ids"]
                }
            )
            trip_rows[-1]["employee_ids"] = ";".join(trip["employee_ids"])
            for i, stop in enumerate(trip["stops"], 1):
                stops.append(
                    {
                        "trip_id": trip["trip_id"],
                        "vehicle_id": trip["vehicle_id"],
                        "stop_number": i,
                        **stop,
                    }
                )
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "trip_id": trip["trip_id"],
                        "vehicle_id": trip["vehicle_id"],
                        "distance_km": trip["distance_km"],
                        "geometry_source": trip["geometry_source"],
                    },
                    "geometry": {"type": "LineString", "coordinates": trip["geometry"]},
                }
            )
        pd.DataFrame(trip_rows).to_csv(
            self.directory / "trips.csv", index=False, encoding="utf-8-sig"
        )
        pd.DataFrame(stops).to_csv(
            self.directory / "stops.csv", index=False, encoding="utf-8-sig"
        )
        (self.directory / "routes.geojson").write_text(
            json.dumps(
                {"type": "FeatureCollection", "features": features}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        (self.directory / "run_manifest.json").write_text(
            json.dumps(source_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (self.directory / "summary.json").write_text(
            json.dumps(state["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def close(self):
        self.logfile.close()
        self.connection.close()
