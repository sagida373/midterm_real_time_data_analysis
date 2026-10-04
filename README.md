
## Installation and startup

Use Python 3.11–3.13. Open PowerShell in the directory containing main.py:

```powershell
python -m pip install -r requirements.txt
python main.py
```

The dependencies are installed into the Python environment selected by the python command. A virtual environment is optional:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

The default address is http://127.0.0.1:8871. Keep the terminal open. Press Ctrl+C to stop the server.

- Another port: `python main.py --port 8872`.
- Disable automatic browser opening: `python main.py --no-open`.
- Start paused for a step-by-step demonstration: `python main.py --paused`.

## JSON input and the result map

The home page includes an example with three employee requests and two vehicles. Edit the JSON and click **Calculate JSON**. The JSON response appears in the expandable response panel, and its route map opens automatically on the right.

Alternatively, open http://127.0.0.1:8871/docs and select **POST /api/optimize → Try it out → Execute**.

The complete example is saved in `data/example_request.json`. The input format is:

```json
{
  "mode": "batch",
  "employees": [
    {
      "employee_id": "E001",
      "workplace_address": "Астана, Тұран даңғылы, 24",
      "shift_end": "22:00",
      "request_time": "22:05",
      "home_address": "Астана, переулок Жауказын, 5",
      "passengers": 1
    }
  ],
  "vehicles": [
    {
      "vehicle_id": "CAR-01",
      "capacity": 4,
      "start_address": "Астана, Тұран даңғылы, 24",
      "color": "red"
    }
  ]
}
```

Street addresses remain in their original spelling so that geocoding and cached address matching continue to work. Interface labels and application messages are in English.

The response includes `routes`, ordered stops, pickup and dropoff times, road distances and geometry, `optimization_runs`, summary indicators, `map_url`, and `graph_url`. Each JSON calculation has its own directory under `results/plans/<plan_id>` and does not replace the CSV simulation.

In **batch** mode, the entire submitted list is known at once. Planning starts at the time of the latest request in that list; earlier passengers have already been waiting. In **stream** mode, requests become available at their scheduled arrival times. Use stream mode or the main CSV simulation for the midterm demonstration.

JSON calculations are synchronous. Finding new addresses and road routes can take time. Wait for the current calculation before submitting another one. This educational version supports up to 25 requests and 10 vehicles per scenario.

## Editing CSV data

`data/employees.csv`:

```csv
employee_id,workplace_address,shift_end,request_time,home_address,passengers
E001,"Астана, Тұран даңғылы, 24",22:00,22:05,"Астана, переулок Жауказын, 5",1
```

`data/vehicles.csv`:

```csv
vehicle_id,capacity,start_address,color
CAR-01,4,"Астана, Тұран даңғылы, 24",red
```

Times use HH:MM. The date and time zone settings are stored in config.json. This version supports a single calendar day; crossing midnight is not supported.

After editing the CSV files, restart the application or click **Reset**, then **Start**. Reset reads both files again. You do not need to enter coordinates manually. New addresses are resolved through Nominatim. Include the city, street, and building number. Missing or ambiguous addresses produce an error identifying the input row; the application does not guess coordinates.

Coordinates and some road data for the original 15 employees are already cached. These are service responses, not a completed dispatch plan. Stop order is calculated during each run. When an address changes, missing data is requested automatically.

## Route optimization

The optimizer uses OR-Tools CP-SAT. All requests in the current calculation are mandatory. Its priorities are:

1. Minimize the number of requests whose pickup waiting time exceeds 15 minutes.
2. Minimize the total excess waiting time.
3. Minimize total road distance, including empty vehicle travel.

Objective weights are calculated from upper bounds so that a lower priority cannot outweigh a higher one. Constraints enforce vehicle capacity, pickup before dropoff, the same vehicle for each request's pickup and dropoff, and exactly one delivery per request. A vehicle may drop off one group and then pick up another within a stop sequence. During streaming, a vehicle can be assigned again after completing its active trip.

The model uses integer seconds and meters. **OPTIMAL** means that optimality was proven for the current model, known requests, and available vehicles. **FEASIBLE** means a feasible solution was found without an optimality proof. **FALLBACK_FEASIBLE** means the solver did not return a solution within its budget and the validated initial feasible plan is used instead. Responses report these statuses. The application does not claim a globally optimal plan for an evening with unknown future requests.

Active trips remain fixed. Waiting may exceed 15 minutes, in which case the application emits an alert. OSRM travel estimates are static and multiplied by travel_time_multiplier; live traffic is not included. OSRM provides road paths based on its fastest-route model. The dispatch optimizer minimizes the distance of those paths; it does not search all alternative street paths itself.

## Project files

| File | Responsibility |
|---|---|
| main.py | terminal startup, API server, automatic browser opening |
| validation.py | CSV/JSON and event validation |
| streaming.py | generator, timed processing, controls |
| processing.py | vehicle state, requests, pickups, dropoffs |
| geocoding.py | address lookup and coordinate cache |
| routing.py | OSRM requests and OR-Tools optimization |
| analysis.py | Pandas/NumPy indicators and aggregations |
| visualization.py | HTML map and PNG chart |
| storage.py | SQLite, CSV, JSONL, JSON, GeoJSON |
| api.py | FastAPI, Swagger, JSON calculations, map endpoints |
| client.py | API controls from a second terminal |
| browser.html | browser API interface with a map |
| test.py | automated checks |

## Commands and endpoints

Start the application:

```powershell
python main.py
```

From a second terminal in the same directory:

```powershell
python client.py metrics
python client.py requests
python client.py vehicles
python client.py trips
python client.py events
python client.py pause
python client.py step
python client.py start
python client.py finish
python client.py reset
python client.py speed 1
```

Complete a simulation without an HTTP server or browser:

```powershell
python main.py --headless --speed 0 --output results/my_run
```

Start FastAPI directly, as in Assignment 1:

```powershell
python -m uvicorn api:app --port 8871
```

With direct Uvicorn startup, open the browser manually; automatic browser opening is implemented in main.py.

- GET `/api/health`, `/api/state`, `/api/metrics`, `/api/requests`, `/api/vehicles`, `/api/trips`, `/api/events`, `/api/example`.
- POST `/api/optimize`: employee and vehicle JSON.
- POST `/api/control/{action}`: start, pause, step, finish, reset, or speed. Speed accepts `{"value":1}`.
- Legacy command paths such as `/api/start` and `/api/step` remain supported.
- Pages: `/`, `/docs`, `/map`, `/maps/<plan_id>`, `/graphs/<plan_id>`.

## Saved results and reproducibility

The CSV simulation writes to results/live. JSON calculations write to results/plans. A complete sample run is included in results/example; this sample directory is optional and can be deleted.

Saved artifacts include JSONL events, SQLite, current state JSON, CSV requests/trips/stops/indicators, summary.json, routes.geojson, map.html, indicators.png, input JSON, configuration, and a snapshot of cached service responses.

To repeat a calculation, use the same input list, settings, and saved cache from the inputs directory. One solver worker, a fixed seed, and a deterministic work limit reduce variation. If the hard wall-clock limit is reached, feasible solutions may differ between computers. Fetching new road service responses can also change results.

## Internet access and sources

New addresses and road data require internet access. OpenStreetMap tiles, Leaflet/Folium browser assets, and Swagger UI assets also load from the internet. config.json contains the Nominatim and OSRM URLs, application identifier, and timeout. Requests are serialized, rate-limited, and cached. Public services are not intended for bulk production use.

CSV/JSON addresses are sent to the geocoding service; coordinates are sent to the road service. Employees and requests in the sample are synthetic. Real building addresses are illustrative, and building centers have not been verified as pickup entrances.

© [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).
[Nominatim policy](https://operations.osmfoundation.org/policies/nominatim/), [OSRM API](https://project-osrm.org/docs/v5.24.0/api/), [OR-Tools CP-SAT statuses](https://developers.google.com/optimization/cp/cp_solver).
