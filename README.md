# table2gmns

Convert spatial tables into GMNS `node.csv` and `link.csv` with **explicit column mapping**.
Inputs: Shapefile, GeoPackage, GeoDataFrame, or a CSV/DataFrame containing WKT line geometry.
Optional point-node files/tables can supply existing node IDs and zone attributes.

The API follows the read / fill defaults / export pattern of `osm2gmns`.
Source attributes are preserved by default. A field named `SPEED`, `LANE`, or `DIRECTION`
is not automatically interpreted as a GMNS field: choose the mapping yourself.

## Install

```bash
pip install git+https://github.com/Kim-yongki/table2gmns.git
```

Or download the repository and run `pip install .` in its folder. Python 3.10+ is required.
If the repository is made private, Git installation requires an account with access and
configured Git authentication. Do not put access tokens in notebooks or repository URLs.

## Bicycle line data

```python
import table2gmns as tg

net = tg.getNetFromFile(
    "prepared_bike.gpkg",
    mode_types="bike",
    link_field_map={"geometry": "geometry", "directed": "ONEWAY"},
)
tg.fillLinkAttributesWithDefaultValues(net, default_speed=True, default_capacity=True)
tg.outputNetToCSV(net, output_folder="bike_network")
```

The mapped `directed` field uses **0 = two-way, 1 = one-way in geometry vertex order**.
Numeric/Boolean values and numeric CSV strings are accepted. Every output row is
directed (`directed=1`); a two-way input produces forward and reverse rows, with
both node IDs and geometry reversed for the second row.

Dataset-specific codes (B/F/T, compass directions, -1, etc.) must be normalized
**before conversion**. Reverse the geometry and swap mapped endpoint IDs when the
allowed travel direction opposes vertex order. Do not infer vertex order from
cardinal/non-cardinal labels without checking the source data.

For an input with no direction column and known two-way lines, explicitly use
`default_directed=0` instead. Use `default_directed=1` for already directed input.
Missing flags need an explicit default; unknown codes always raise an error.
Only geometry and directed need mapping (or the directed default). Speed, lanes,
capacity, IDs and other mappings are optional.

`LTS`, `LANE`, `SPEED` and other source attributes are retained. The example does not
use motor-traffic `SPEED` or `LANE` as bicycle speed or lane count. The bicycle example
defaults are 19.312128 km/h (12 mph) and capacity 1,500 per directed link.

## Match your own columns

Every map is **`{output/GMNS field: input column}`**. `link_field_map` is required,
including an explicit `geometry` entry. No columns are guessed from similar names.

```python
net = tg.getNetFromFile(
    "roads.shp",
    link_field_map={
        "geometry": "geometry",
        "link_id": "SEGMENT_ID",
        "directed": "ONEWAY",
        "lanes": "LANES_AB",
        "free_speed": "SPEED_AB",
        "capacity": "CAP_AB",
        "name": "STREET",
        "link_type_name": "ROAD_TYPE",
    },
    reverse_field_map={"lanes": "LANES_BA", "free_speed": "SPEED_BA", "capacity": "CAP_BA"},
    speed_unit="mph",
)
tg.outputNetToCSV(net, "road_network")
```

CSVs are read as strings to preserve leading zeros in identifiers.
Mapped numeric attributes and the 0/1 directed flag are converted explicitly after reading.

## CSV with WKT, or an in-memory table

```python
net = tg.getNetFromFile(
    "links.csv",
    link_field_map={"geometry": "WKT", "directed": "ONEWAY", "free_speed": "SPD"},
    source_crs="EPSG:4326",
    speed_unit="km/h",
)
```

Pass a DataFrame/GeoDataFrame in place of a filename to avoid intermediate files.
For CSV/plain tables, declare `source_crs`; WKT by itself carries no CRS.
For spatial files the stored CRS is used. `source_crs` may supply a missing CRS but
cannot silently overwrite a conflicting one.

## Existing nodes and topology

Without mapped node IDs, shared rounded endpoints become nodes. To preserve an
existing topology, map **both** `from_node_id` and `to_node_id`. Existing IDs are kept;
one ID occurring at different locations is an error. This also allows distinct node
IDs at the same location, such as grade-separated facilities.

An optional node file supplies point geometry and attributes:

```python
net = tg.getNetFromFile(
    "links.csv",
    link_field_map={"geometry": "WKT", "from_node_id": "A", "to_node_id": "B"},
    default_directed=1,
    source_crs="EPSG:26917",
    node_file="nodes.csv",
    node_field_map={"node_id": "N", "x_coord": "X", "y_coord": "Y", "zone_id": "TAZ"},
    node_source_crs="EPSG:26917",
)
```

Nodes can instead be a point Shapefile/GeoPackage with its stored CRS; map `node_id`
and other desired fields. Link IDs are regenerated because one feature may create
multiple directed links; mapped input IDs remain in `source_link_id`. Multi-part
line input is split into parts, recorded in `source_part`. Multi-part features with
a single mapped from/to ID pair must be split and assigned IDs before conversion.

## Units and defaults

- Output: WGS84 longitude/latitude and WKT geometry, lengths in metres, speeds in km/h.
- `metric_crs`: native metre-based projected CRS by default; otherwise estimated local
  UTM. Specify one for cross-zone or large-area data. `node_precision=2` rounds endpoint
  coordinates to two decimal places in metres; it is not a distance-based snap search.
- `length_unit`: `m`, `km`, `ft`, `us-ft`, `mile`, for a mapped length field. Without
  a mapping, length is measured from geometry. A mapped multi-part total is apportioned
  according to each part's geometric length.
- `speed_unit`: `km/h`, `mph`, `m/s`, for mapped speeds in either travel direction.
- `lanes_are_total=True`: split even two-way totals equally. Odd totals require explicit
  directional fields; the converter will not guess an allocation.
- `capacity_per_lane=True`: multiply a mapped capacity by the directional lane count.
  Otherwise mapped capacity is per directed link.
- `keep_source_columns=True`: preserve source attributes. Name conflicts with output
  fields receive a `source_` prefix. Set `False` only if you intentionally want to omit
  unmapped attributes. Input tables/files are never modified.

`fillLinkAttributesWithDefaultValues()` does nothing unless a flag is set. It fills
missing values only; it does not replace source values or treat zero as missing.
Built-in **example assumptions**, not measurements or universal standards:

| Mode | Lanes | Speed (km/h) | Capacity per directed link |
| --- | ---: | ---: | ---: |
| auto | 1 | 50 | 1800 |
| bike | 1 | 19.312128 | 1500 |
| walk | 1 | 4.828032 | 1500 |

Override by `link_type_name`, for example:

```python
tg.fillLinkAttributesWithDefaultValues(
    net,
    default_speed=True,
    default_speed_dict={"bike": 16, "shared_path": 12},
)
```

Unknown link types fall back to the selected mode's example default. Output directories
are created by `outputNetToCSV()`. Existing `node.csv` and `link.csv` there are overwritten.

## Limits

This is a converter, not a routing engine or automatic network cleaner. It does not
split interior intersections, resolve bridges/tunnels, remove duplicate source features,
infer access restrictions, interpret turn restrictions, or apply a study-area buffer.
`mode_types` labels the output and chooses optional defaults; it does not discover
mode permissions from arbitrary source data. Filter your study area and permitted
facilities before conversion. Current support is one mode per conversion.

Downstream tools may require numeric node IDs, positive capacities, a particular
directional lane convention, or additional columns. Supply those explicitly for the
target model. The generated `zone_id` field is blank unless mapped from a node table.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use synthetic data only. No source GIS data, credentials, or analysis outputs
are distributed with this package. The public API naming is inspired by
[osm2gmns](https://github.com/jiawlu/OSM2GMNS); this package is an independent converter.
