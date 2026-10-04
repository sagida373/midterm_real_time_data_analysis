import hashlib
import json
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

service_lock = threading.RLock()
last_requests = {}


def normalize_address(address):
    return " ".join(address.strip().casefold().split())


def read_cache(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_cache(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def fetch_json(url, config, service):
    with service_lock:
        gap = 1.1
        elapsed = time.monotonic() - last_requests.get(service, 0)
        if elapsed < gap:
            time.sleep(gap - elapsed)
        request = Request(
            url,
            headers={"User-Agent": config["user_agent"], "Accept": "application/json"},
        )
        try:
            with urlopen(
                request, timeout=config.get("http_timeout_seconds", 25)
            ) as response:
                return json.load(response)
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise ValueError(
                f"Service {service} is unavailable: {error}. Try again later or use cached data."
            ) from error
        finally:
            last_requests[service] = time.monotonic()


def geocode_address(address, root, config):
    key = normalize_address(address)
    path = root / "cache" / "geocoding.json"
    with service_lock:
        cache = read_cache(path)
        if key in cache:
            return dict(cache[key])
        if config.get("offline"):
            raise ValueError(
                f"No cached coordinates for address {address!r}; disable offline mode"
            )
        query = urlencode(
            {"q": address, "format": "jsonv2", "limit": 5, "addressdetails": 1}
        )
        candidates = fetch_json(
            config["geocoding_url"] + "?" + query, config, "Nominatim"
        )
        names = [
            item
            for item in candidates
            if item.get("name") and normalize_address(item["name"]) in key
        ]
        if names:
            candidates = names
        points = {}
        for item in candidates:
            details = item.get("address", {})
            if details.get("house_number") and details.get("road"):
                point = tuple(
                    normalize_address(str(details.get(field, "")))
                    for field in ["country_code", "city", "road", "house_number"]
                )
            else:
                point = (round(float(item["lat"]), 4), round(float(item["lon"]), 4))
            if point not in points or item.get("osm_type") == "way":
                points[point] = item
        if len(points) != 1:
            reason = "not found" if not points else "ambiguous"
            raise ValueError(
                f"Address {address!r} is {reason}. Specify the city, street and building number."
            )
        selected = next(iter(points.values()))
        result = {
            "lat": float(selected["lat"]),
            "lon": float(selected["lon"]),
            "display_name": selected["display_name"],
            "source": "Nominatim / OpenStreetMap",
            "osm_id": selected.get("osm_id"),
        }
        cache[key] = result
        write_cache(path, cache)
        return result


def prepare_locations(employees, vehicles, root, config):
    from validation import parse_time

    locations = {}

    def location(address, kind):
        id = "L" + hashlib.sha256(normalize_address(address).encode()).hexdigest()[:12]
        if id not in locations:
            point = geocode_address(address, root, config)
            locations[id] = {
                "id": id,
                "kind": kind,
                "name": address,
                "address": address,
                **point,
            }
        return id

    prepared = []
    for row_number, original in enumerate(employees, 2):
        row = dict(original)
        try:
            row["workplace_id"] = location(row["workplace_address"], "workplace")
            row["home_id"] = location(row["home_address"], "home")
        except ValueError as error:
            raise ValueError(f"employees.csv, row {row_number}: {error}") from error
        row["workplace_name"] = row["workplace_address"]
        row["shift_end"] = parse_time(row["shift_end"], config).isoformat()
        row["request_timestamp"] = parse_time(row["request_time"], config).isoformat()
        prepared.append(row)
    cars = []
    for row_number, original in enumerate(vehicles, 2):
        row = dict(original)
        try:
            row["initial_location_id"] = location(row["start_address"], "vehicle_start")
        except ValueError as error:
            raise ValueError(f"vehicles.csv, row {row_number}: {error}") from error
        cars.append(row)
    return prepared, cars, locations
