"""Convert explicitly mapped spatial tables to directed GMNS networks."""

from dataclasses import dataclass
from pathlib import Path
import math
import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely.geometry import Point

__version__ = "0.1.1"
__all__ = ["Network", "getNetFromFile", "fillLinkAttributesWithDefaultValues", "outputNetToCSV"]
_LENGTH = {"m": 1.0, "km": 1000.0, "ft": 0.3048, "us-ft": 1200 / 3937, "mile": 1609.344}
_SPEED = {"km/h": 1.0, "mph": 1.609344, "m/s": 3.6}
_MODES = {"auto": "drive", "bike": "bike", "walk": "walk"}
_DEFAULTS = {
    "lanes": {"auto": 1, "bike": 1, "walk": 1},
    "free_speed": {"auto": 50.0, "bike": 19.312128, "walk": 4.828032},
    "capacity": {"auto": 1800.0, "bike": 1500.0, "walk": 1500.0},
}
_GENERATED = {"geometry", "x_coord", "y_coord", "dir_flag", "source_link_id", "source_part", "source_directed"}
_CORE = _GENERATED | {"link_id", "from_node_id", "to_node_id", "length", "lanes", "free_speed", "capacity", "link_type", "link_type_name", "allowed_uses", "directed"}


@dataclass
class Network:
    """Tables use metres, km/h and WGS84; each link row is one travel direction."""
    nodes: pd.DataFrame
    links: pd.DataFrame
    mode_type: str
    metric_crs: CRS


def _read(source, source_crs=None, geometry_column=None, point_map=None):
    if isinstance(source, pd.DataFrame):
        frame = source.copy()
    elif Path(source).suffix.lower() == ".csv":
        frame = pd.read_csv(source, dtype=str)
    else:
        frame = gpd.read_file(source)
    if frame.empty:
        raise ValueError("Input contains no features.")
    if isinstance(frame, gpd.GeoDataFrame):
        if geometry_column is not None:
            if geometry_column not in frame:
                raise ValueError(f"Missing geometry column: {geometry_column!r}")
            frame = frame.set_geometry(geometry_column)
    else:
        if geometry_column is not None:
            if geometry_column not in frame:
                raise ValueError(f"Missing geometry column: {geometry_column!r}")
            geometry = frame[geometry_column]
            if isinstance(geometry.iloc[0], str):
                geometry = gpd.GeoSeries.from_wkt(geometry)
            frame[geometry_column] = geometry
            frame = gpd.GeoDataFrame(frame, geometry=geometry_column, crs=source_crs)
        elif point_map and {"x_coord", "y_coord"} <= set(point_map):
            missing = set(point_map.values()) - set(frame)
            if missing:
                raise ValueError(f"Missing node columns: {sorted(missing)}")
            geometry = gpd.points_from_xy(pd.to_numeric(frame[point_map["x_coord"]]), pd.to_numeric(frame[point_map["y_coord"]]))
            frame = gpd.GeoDataFrame(frame, geometry=geometry, crs=source_crs)
        else:
            raise ValueError("A table needs a mapped WKT geometry column, or node x_coord/y_coord columns.")
    if frame.crs is None:
        if source_crs is None:
            raise ValueError("Input CRS is missing; set source_crs.")
        frame = frame.set_crs(source_crs)
    elif source_crs is not None and CRS(source_crs) != frame.crs:
        raise ValueError("source_crs disagrees with the input CRS; reproject rather than relabel it.")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise ValueError("Input contains null or empty geometry.")
    return frame.reset_index(drop=True)


def _mapping(frame, values, label):
    values = dict(values or {})
    missing = set(values.values()) - set(frame.columns)
    if missing:
        raise ValueError(f"{label} references missing columns: {sorted(missing)}")
    return values


def _number(value, label):
    if pd.isna(value):
        return float("nan")
    try:
        value = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{label} must be numeric, got {value!r}") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative.")
    return value


def _directed(value, default):
    if pd.isna(value):
        value = default
    try:
        flag = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("directed must be 0 (two-way) or 1 (one-way in geometry order).") from error
    if flag not in (0, 1):
        raise ValueError("directed must be 0 (two-way) or 1 (one-way in geometry order).")
    return int(flag)


def _source_attributes(row, geometry_name, reserved):
    """Keep source attributes; prefix collisions rather than overwriting them."""
    attributes = {}
    source_names = set(row.index)
    for column, value in row.items():
        if column == geometry_name:
            continue
        name = column
        if name in reserved:
            name = "source_" + name
            while name in source_names or name in reserved or name in attributes:
                name = "source_" + name
        attributes[name] = value
    return attributes


def getNetFromFile(
    filepath, mode_types="auto", *, link_field_map,
    default_directed=None, reverse_field_map=None,
    node_file=None, node_field_map=None, source_crs=None, node_source_crs=None, metric_crs=None,
    length_unit="m", speed_unit="km/h", node_precision=2,
    lanes_are_total=False, capacity_per_lane=False, keep_source_columns=True,
):
    """Read SHP/GPKG, WKT CSV or a spatial table using explicit field maps.

    link_field_map is required and must include {'geometry': source_column}.
    Maps are {output field: source column}. CSV/plain tables need source_crs.

    Map directed (0 = two-way, 1 = one-way), or set default_directed.
    One-way links follow geometry vertex order. Normalize source codes and
    reverse geometry when needed before calling this converter.
    Map both from_node_id/to_node_id to retain existing topology, optionally
    providing a point node_file and node_field_map. Otherwise endpoint rounding
    in a metric CRS generates node IDs. Interior crossings are NOT split.

    All source attributes are retained by default. Name collisions use a
    source_ prefix. Only explicitly mapped columns populate GMNS attributes.
    Source link IDs are stored as source_link_id; directed link_id is generated.
    Mapped lengths/speeds are converted from length_unit/speed_unit to m/km/h.
    Unmapped lengths are calculated in metric_crs (native metres or local UTM).
    Missing speed/capacity/lanes remain missing until explicitly filled.
    """
    if not isinstance(mode_types, str) or mode_types not in _MODES:
        raise ValueError("mode_types must be 'auto', 'bike' or 'walk' (one mode per conversion).")
    if length_unit not in _LENGTH or speed_unit not in _SPEED:
        raise ValueError(f"length_unit: {list(_LENGTH)}; speed_unit: {list(_SPEED)}")
    if not isinstance(node_precision, int) or isinstance(node_precision, bool) or node_precision < 0:
        raise ValueError("node_precision must be a nonnegative integer.")
    if default_directed is not None:
        _directed(default_directed, None)
    if not isinstance(link_field_map, dict) or "geometry" not in link_field_map:
        raise ValueError("link_field_map must explicitly map geometry to its source column.")
    source = _read(filepath, source_crs, link_field_map["geometry"])
    fields = _mapping(source, link_field_map, "link_field_map")
    reverse_fields = _mapping(source, reverse_field_map, "reverse_field_map")
    if set(fields) & (_GENERATED - {"geometry"}):
        raise ValueError(f"Cannot map generated fields: {sorted(set(fields) & (_GENERATED - {'geometry'}))}")
    if set(reverse_fields) & (_GENERATED | {"directed", "link_id", "from_node_id", "to_node_id", "length"}):
        raise ValueError("reverse_field_map may override attributes, not IDs, geometry, length or directed.")
    if "directed" not in fields and default_directed is None:
        raise ValueError("Map a directed column or explicitly set default_directed.")
    has_ids = {"from_node_id", "to_node_id"} <= set(fields)
    if bool({"from_node_id", "to_node_id"} & set(fields)) != has_ids:
        raise ValueError("Map both from_node_id and to_node_id, or neither.")
    if node_file is not None and not has_ids:
        raise ValueError("node_file requires mapped from_node_id and to_node_id.")
    if node_field_map is not None and node_file is None:
        raise ValueError("node_field_map requires node_file.")
    if lanes_are_total and ("lanes" not in fields or "lanes" in reverse_fields):
        raise ValueError("lanes_are_total requires mapped lanes and no reverse lanes override.")
    if capacity_per_lane and not {"lanes", "capacity"} <= set(fields):
        raise ValueError("capacity_per_lane requires lanes and capacity mappings.")
    native = CRS(source.crs)
    if metric_crs is None:
        in_metres = native.is_projected and all(abs(a.unit_conversion_factor - 1) < 1e-10 for a in native.axis_info[:2])
        metric_crs = native if in_metres else source.estimate_utm_crs()
    if metric_crs is None:
        raise ValueError("Cannot estimate a local metric CRS; set metric_crs.")
    metric_crs = CRS(metric_crs)
    if not metric_crs.is_projected or any(abs(a.unit_conversion_factor - 1) > 1e-10 for a in metric_crs.axis_info[:2]):
        raise ValueError("metric_crs must be projected with metre units.")
    source = source.to_crs(metric_crs)

    parts = []
    for source_row, row in source.iterrows():
        shape = row[source.geometry.name]
        if shape.geom_type not in ("LineString", "MultiLineString") or not shape.is_valid:
            raise ValueError(f"Feature {source_row} must be a valid line geometry.")
        segments = [shape] if shape.geom_type == "LineString" else list(shape.geoms)
        if len(segments) > 1 and has_ids:
            raise ValueError("Multipart links need separate from/to IDs; split them first.")
        directed = _directed(row[fields["directed"]] if "directed" in fields else None, default_directed)
        for part_number, segment in enumerate(segments):
            if segment.length <= 0 or not all(math.isfinite(v) for xy in segment.coords for v in xy[:2]):
                raise ValueError(f"Feature {source_row} has zero length or invalid coordinates.")
            parts.append((source_row, part_number, row, segment, segment.length / shape.length, directed))

    def endpoint_key(point):
        return tuple(round(float(v), node_precision) for v in point[:2])

    starts = [endpoint_key(p[3].coords[0]) for p in parts]
    ends = [endpoint_key(p[3].coords[-1]) for p in parts]
    if any(a == b and p[3].length <= math.sqrt(2) * 10 ** (-node_precision) for a, b, p in zip(starts, ends, parts)):
        raise ValueError("Endpoint rounding collapses a link; increase node_precision.")
    positions = {}
    if has_ids:
        start_ids = [p[2][fields["from_node_id"]] for p in parts]
        end_ids = [p[2][fields["to_node_id"]] for p in parts]
        for identifier, xy in zip(start_ids + end_ids, starts + ends):
            if pd.isna(identifier):
                raise ValueError("Mapped node IDs cannot be null.")
            if identifier in positions and positions[identifier] != xy:
                raise ValueError(f"Node ID {identifier!r} occurs at different endpoints.")
            positions[identifier] = xy
    else:
        point_ids = {xy: i for i, xy in enumerate(dict.fromkeys(starts + ends), start=1)}
        start_ids, end_ids = [point_ids[p] for p in starts], [point_ids[p] for p in ends]
        positions = {i: xy for xy, i in point_ids.items()}
    points = gpd.GeoSeries([Point(xy) for xy in positions.values()], crs=metric_crs).to_crs(4326)
    nodes = pd.DataFrame({"node_id": list(positions), "x_coord": points.x, "y_coord": points.y})
    nodes["zone_id"] = float("nan")
    if node_file is not None:
        node_source = _read(node_file, node_source_crs, (node_field_map or {}).get("geometry"), node_field_map)
        node_fields = _mapping(node_source, node_field_map, "node_field_map")
        if "node_id" not in node_fields:
            raise ValueError("node_field_map must map node_id.")
        ids = node_source[node_fields["node_id"]]
        if ids.isna().any() or not ids.is_unique or not (node_source.geom_type == "Point").all():
            raise ValueError("node_file requires unique non-null IDs and Point geometries.")
        node_source.index = ids
        missing = set(positions) - set(ids)
        if missing:
            raise ValueError(f"node_file is missing referenced IDs: {list(missing)[:5]}")
        selected = node_source.loc[list(positions)].to_crs(metric_crs)
        tolerance = math.sqrt(2) * 10 ** (-node_precision)
        for identifier, geometry in zip(selected.index, selected.geometry):
            if geometry.distance(Point(positions[identifier])) > tolerance:
                raise ValueError(f"Node geometry disagrees with link endpoint: {identifier!r}")
        selected = selected.to_crs(4326)
        nodes["x_coord"], nodes["y_coord"] = selected.geometry.x.to_numpy(), selected.geometry.y.to_numpy()
        if keep_source_columns:
            raw = pd.DataFrame([_source_attributes(row, selected.geometry.name, set(nodes) | set(node_fields)) for _, row in selected.iterrows()])
            nodes = pd.concat([nodes, raw], axis=1)
        for target, column in node_fields.items():
            if target not in {"node_id", "x_coord", "y_coord", "geometry"}:
                nodes[target] = selected[column].to_numpy()

    lonlat = gpd.GeoSeries([p[3] for p in parts], crs=metric_crs).to_crs(4326)
    forward_rows, reverse_rows = [], []
    for i, (source_row, part_number, row, geometry, fraction, directed) in enumerate(parts):
        raw = _source_attributes(row, source.geometry.name, _CORE | set(fields) | set(reverse_fields)) if keep_source_columns else {}
        attributes = {target: row[column] for target, column in fields.items() if target not in {"geometry", "directed", "link_id", "from_node_id", "to_node_id", "length"}}
        length = _number(row[fields["length"]], "length") * _LENGTH[length_unit] * fraction if "length" in fields else geometry.length
        if not math.isfinite(length) or length <= 0:
            raise ValueError(f"Feature {source_row} requires positive length.")
        base = {**raw, "from_node_id": start_ids[i], "to_node_id": end_ids[i],
                "length": length, "geometry": lonlat.iloc[i].wkt,
                "link_type": 1, "link_type_name": mode_types, "allowed_uses": _MODES[mode_types],
                "lanes": float("nan"), "free_speed": float("nan"), "capacity": float("nan"), **attributes,
                "source_link_id": row[fields["link_id"]] if "link_id" in fields else source_row,
                "source_part": part_number,
                "source_directed": directed,
                "directed": 1, "dir_flag": 1}
        for reverse in ([False, True] if directed == 0 else [False]):
            link = base.copy()
            if reverse:
                link.update({target: row[column] for target, column in reverse_fields.items()})
                link["from_node_id"], link["to_node_id"] = base["to_node_id"], base["from_node_id"]
                link["geometry"] = lonlat.iloc[i].reverse().wkt
            for column in ("lanes", "free_speed", "capacity"):
                link[column] = _number(link[column], column)
            link["free_speed"] *= _SPEED[speed_unit]
            if lanes_are_total and directed == 0 and not pd.isna(link["lanes"]):
                if link["lanes"] % 2:
                    raise ValueError("Odd total lane counts need explicit directional lane fields.")
                link["lanes"] /= 2
            if capacity_per_lane:
                if pd.isna(link["lanes"]) or link["lanes"] <= 0:
                    raise ValueError("Per-lane capacity needs a positive lane count in each direction.")
                link["capacity"] *= link["lanes"]
            (reverse_rows if reverse else forward_rows).append(link)
    links = pd.DataFrame(forward_rows + reverse_rows)
    links.insert(0, "link_id", range(1, len(links) + 1))
    return Network(nodes, links, mode_types, metric_crs)


def fillLinkAttributesWithDefaultValues(
    network, default_lanes=False, default_lanes_dict=None,
    default_speed=False, default_speed_dict=None,
    default_capacity=False, default_capacity_dict=None,
):
    """Fill only missing, explicitly requested attributes in place.

    Dict keys are link_type_name values; unknown types fall back to the mode's
    documented example default. Existing values are never overwritten.
    Capacity defaults are per directed link, not per lane.
    """
    options = [("lanes", default_lanes, default_lanes_dict),
               ("free_speed", default_speed, default_speed_dict),
               ("capacity", default_capacity, default_capacity_dict)]
    for column, enabled, custom in options:
        if not enabled:
            continue
        values = dict(_DEFAULTS[column])
        for key, value in (custom or {}).items():
            value = _number(value, f"default {column}")
            if pd.isna(value) or value <= 0:
                raise ValueError(f"Default {column} must be positive.")
            values[key] = value
        defaults = network.links["link_type_name"].map(values).fillna(values[network.mode_type])
        network.links[column] = network.links[column].fillna(defaults)


def outputNetToCSV(network, output_folder=""):
    """Write node.csv/link.csv, creating the output directory if needed."""
    output = Path(output_folder)
    output.mkdir(parents=True, exist_ok=True)
    network.nodes.to_csv(output / "node.csv", index=False)
    network.links.to_csv(output / "link.csv", index=False)
