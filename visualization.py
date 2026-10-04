import html
from pathlib import Path
import folium


def create_map(state, locations):
    if not locations:
        return "<p>The scenario has not been loaded yet.</p>"
    first = next(iter(locations.values()))
    map = folium.Map(
        location=[first["lat"], first["lon"]], zoom_start=12, tiles="OpenStreetMap"
    )
    for point in locations.values():
        color = "blue" if point["kind"] == "workplace" else "gray"
        folium.CircleMarker(
            [point["lat"], point["lon"]],
            radius=5,
            color=color,
            fill=True,
            tooltip=html.escape(point["address"]),
        ).add_to(map)
    for trip in state.get("trips", []):
        line = [[lat, lon] for lon, lat in trip["geometry"]]
        title = f"{trip['trip_id']} · {trip['vehicle_id']} · {trip['distance_km']:.2f} km · {trip['planned_duration_min']:.1f} min"
        group = folium.FeatureGroup(name=title)
        popup = (
            html.escape(title)
            + "<br>Passengers: "
            + html.escape(", ".join(trip["employee_ids"]))
        )
        folium.PolyLine(
            line, color=trip["color"], weight=5, opacity=0.8, tooltip=title, popup=popup
        ).add_to(group)
        for number, stop in enumerate(trip["stops"], 1):
            point = locations[stop["location_id"]]
            label = f"{number}. {stop['employee_id']} · {'Pickup' if stop['kind']=='pickup' else 'Dropoff'} · {stop['arrival_at'][11:19]}"
            folium.CircleMarker(
                [point["lat"], point["lon"]],
                radius=7,
                color=trip["color"],
                fill=True,
                popup=html.escape(label),
                tooltip=html.escape(label),
            ).add_to(group)
        group.add_to(map)
    folium.LayerControl().add_to(map)
    map.fit_bounds([[p["lat"], p["lon"]] for p in locations.values()])
    return map.get_root().render()


def save_visualizations(state, locations, directory):
    import os

    os.environ.setdefault("MPLCONFIGDIR", str(directory / ".plot_cache"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    (directory / "map.html").write_text(create_map(state, locations), encoding="utf-8")
    history = pd.DataFrame(state["history"])
    figure, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    minutes = range(len(history))
    axes[0].plot(minutes, history["waiting"], label="Waiting passengers")
    axes[0].plot(minutes, history["delivered"], label="Delivered passengers")
    axes[0].set_ylabel("People")
    axes[0].legend()
    axes[1].plot(minutes, history["mean_waiting_min"], label="Mean current wait")
    axes[1].plot(minutes, history["max_waiting_min"], label="Maximum current wait")
    axes[1].axhline(15, color="red", linestyle="--", label="15 minute target")
    axes[1].set_xlabel("Recorded simulation step")
    axes[1].set_ylabel("Minutes")
    axes[1].legend()
    figure.tight_layout()
    figure.savefig(directory / "indicators.png", dpi=160)
    plt.close(figure)
