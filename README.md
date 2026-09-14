# sal-portolan

A [SAL Module](https://cgs-earth.github.io/sal/) wrapping the
[`portolan extract`](https://github.com/portolan-sdi/portolan-cli) sub-command.
It runs `portolan extract arcgis|wfs|carto` against one or more remote
sources and emits every file the extraction produces as a `file:///` node,
so a SAL project can copy the resulting Portolan catalog (GeoParquet/COG +
STAC metadata) into itself.

## The `Extract` task

The module declares a single `salmodule:Task` subclass, `Extract`,
configured with a `sources` array. Each entry describes one
`portolan extract` invocation:

| property   | required | description                                                                                                                     |
| ---------- | -------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `provider` | yes      | `arcgis`, `wfs`, or `carto` — selects the `portolan extract` sub-command.                                                        |
| `url`      | yes      | The service endpoint URL.                                                                                                        |
| `name`     | no       | Slug for this source's output subdirectory (defaults to a sanitized form of the URL).                                            |
| `options`  | no       | Array of raw extra CLI args passed through verbatim, e.g. `["--layers", "Census*"]`. See `portolan extract <provider> --help`.   |

The output directory itself isn't configurable — each source extracts into
its own directory inside the container, and the module reports every file
written there so SAL can copy it out. `--auto` is always passed so the
extraction never blocks on an interactive confirmation prompt.

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
for an example task instance with two sources.

## Configuring a task in Turtle

`SALMODULE_TASK_INSTANCE` JSON is what the container reads at runtime, but
inside a SAL project the canonical way to configure a task is as RDF —
typically Turtle — committed alongside the project's other source data.
SAL resolves bare terms against the module's `salmodule://` namespace, so a
project only needs to alias that namespace to a prefix and describe an
instance of `portolan:Extract`:

```turtle
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix portolan: <salmodule://github.com/cgs-earth/sal-portolan#> .

<https://example.org/my-project/tasks/census-extract>
    a portolan:Extract ;
    portolan:sources (
        [
            portolan:provider "arcgis" ;
            portolan:url "https://services.arcgis.com/example/arcgis/rest/services/Census/FeatureServer"^^xsd:anyURI ;
            portolan:name "census" ;
            portolan:options ( "--layers" "Census*" "--output-crs" "EPSG:4326" "--raw" )
        ]
        [
            portolan:provider "wfs" ;
            portolan:url "https://example.com/geoserver/wfs"^^xsd:anyURI ;
            portolan:options ( "--dry-run" )
        ]
    ) .
```

`portolan:sources` and `portolan:options` are declared as `@container: @list` in this
module's ontology (see `salmodule ontology`), which is why they're written
as RDF collections (`( ... )`) rather than repeated predicates — extraction
order and CLI argument order both matter, and Turtle's list syntax is what
preserves that order through RDF. SAL frames this graph back into the
`tests/SALMODULE_TASK_INSTANCE.json` shape shown above before invoking
`salmodule run`.
