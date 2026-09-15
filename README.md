# sal-portolan

A [SAL Module](https://cgs-earth.github.io/sal/) wrapping the
[`portolan extract`](https://github.com/portolan-sdi/portolan-cli) sub-command.
It runs `portolan extract arcgis|wfs|carto` once per configured source, all
into the same output directory, so portolan accumulates every source as a
collection in one shared STAC catalog. That directory is emitted as a
single trailing-slash `file:///` node, so a SAL project copies the whole
Portolan catalog (GeoParquet/COG + STAC metadata) out at once, hrefs and
all.

## The `Extract` task

The module declares a single `salmodule:Task` subclass, `Extract`,
configured with a `sources` array. Each entry describes one
`portolan extract` invocation:

| property   | required | description                                                                                                                     |
| ---------- | -------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `provider` | yes      | `arcgis`, `wfs`, or `carto` — selects the `portolan extract` sub-command.                                                        |
| `url`      | yes      | The service endpoint URL.                                                                                                        |
| `options`  | no       | Array of raw extra CLI args passed through verbatim, e.g. `["--layers", "Census*"]`. See `portolan extract <provider> --help`.   |

The output directory itself isn't configurable — every source in a task
instance extracts into the same fixed `/portolan/` inside the container
(not a randomly-named temp directory, since SAL mirrors the container path
under `.sal/data/blobs/`, so a fixed path here is what keeps the blob path
predictable: `.sal/data/blobs/portolan/`). Pointing every source at the same
directory is deliberate, not incidental — portolan's own auto-init logic
adds each source as another collection in the catalog already at that path
instead of creating a new one (see
[portolan-cli#767](https://github.com/portolan-sdi/portolan-cli/issues/767)),
so a task with three sources produces one catalog with three collections,
not three catalogs. `--auto` is always passed so the extraction never blocks
on an interactive confirmation prompt.

Once every source has run, the module reports that one shared directory as
a single `file:///.../` node (trailing slash). SAL copies a trailing-slash
path verbatim, preserving the relative hrefs between the catalog, its
collections, items, and assets, rather than content-addressing each file
individually and scattering them across a flat blob store. If the resulting
directory contains a `catalog.json` (i.e. no source's `options` disabled it
with `--raw`), the node also carries `dcterms:conformsTo
<https://stacspec.org/>`, so the RDF graph records that its contents are a
STAC catalog rather than a bag of unstructured files.

## Usage

### Building the SAL Module

```sh
docker build . -t sal-portolan:latest
```

### Fetching the SAL Module's ontology

```sh
docker run sal-portolan:latest salmodule ontology
```

### Running the `Extract` task

```sh
docker run -e SALMODULE_TASK_INSTANCE="$(cat tests/SALMODULE_TASK_INSTANCE.json)" sal-portolan:latest salmodule run
```

See [`tests/SALMODULE_TASK_INSTANCE.json`](tests/SALMODULE_TASK_INSTANCE.json)
for an example task instance.

## Configuring a task in Turtle

`SALMODULE_TASK_INSTANCE` JSON is what the container reads at runtime, but
inside a SAL project the canonical way to configure a task is as RDF —
typically Turtle — committed alongside the project's other source data.
SAL resolves bare terms against the module's `salmodule://` namespace, so a
project only needs to alias that namespace to a prefix and describe an
instance of `portolan:Extract`:

```turtle
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix portolan: <salmodule://github.com/cgs-earth/sal-portolan/> .

<https://example.org/my-project/tasks/geospatial-extract>
    a portolan:Extract ;
    portolan:sources (
        [
            portolan:provider "arcgis" ;
            portolan:url "https://services.arcgis.com/C34zQ7veRS0V1t04/ArcGIS/rest/services/Irrigation_District_2024/FeatureServer/0" ;
            portolan:options ( "--output-crs" "EPSG:4326" )
        ]
        [
            portolan:provider "arcgis" ;
            portolan:url "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer" ;
            portolan:options ( "--bbox" "-112.2,36.0,-112.0,36.2" )
        ]
    ) .
```

The first source is a `FeatureServer` layer, extracted to GeoParquet. The
second is USGS's [3DEP elevation `ImageServer`](https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer),
a raster source — `portolan extract arcgis` routes an `ImageServer` URL to
its COG extraction path instead of GeoParquet. `--bbox` keeps the example to
a small area around the Grand Canyon rather than the whole CONUS-wide
elevation dataset. Both land as collections in the one catalog under
`/portolan/`, not two separate catalogs.

`portolan:sources` and `portolan:options` are declared as `@container: @list`
in this module's ontology (see `salmodule ontology`), which is why they're
written as RDF collections (`( ... )`) rather than repeated predicates —
extraction order and CLI argument order both matter, and Turtle's list
syntax is what preserves that order through RDF. SAL frames this graph back
into the `tests/SALMODULE_TASK_INSTANCE.json` shape shown above before
invoking `salmodule run`.

## Development

```sh
uv sync --group dev      # installs pytest, pyright, pre-commit
uv run pre-commit install  # wires the hooks into `git commit`
```

`uv run pytest` runs the test suite (`tests/test_main.py`) — it mocks
`subprocess.run`, so it never shells out to a real `portolan` binary or hits
a live service. `uv run pyright` type-checks `main.py` and the tests. Both
run as pre-commit hooks (see [`.pre-commit-config.yaml`](.pre-commit-config.yaml))
and again in CI on every push (see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
