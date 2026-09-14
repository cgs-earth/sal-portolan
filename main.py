"""SAL Module wrapping the `portolan extract` sub-command.

Implements the SAL Module CLI spec (https://cgs-earth.github.io/sal/):
`salmodule ontology` prints this module's vocabulary, `salmodule run`
executes the salmodule:Task instance passed via SALMODULE_TASK_INSTANCE.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

SALMODULE_NS = "https://w3id.org/sal/cgs-earth/sal-module-spec/salmodule#"
MODULE_NS = "salmodule://github.com/cgs-earth/sal-portolan/"
TASK_CLASS = "Extract"
PROVIDERS = ("arcgis", "wfs", "carto")
ONTOLOGY_COMMANDS = ("ontology", "vocab", "vocabulary")
STAC_SPEC = "https://stacspec.org/"
# Fixed rather than a random tempdir: SAL mirrors this container path under
# .sal/data/blobs/, so a stable path here (not /tmp/portolan-extract-<random>)
# is what keeps the blob path predictable (.sal/data/blobs/portolan/...).
# Each task instance runs in its own fresh container, so there is no
# cross-run collision risk in reusing a fixed path.
DEFAULT_OUTPUT_ROOT = Path("/portolan")


def build_ontology() -> dict:
    return {
        "@context": {
            "@vocab": MODULE_NS,
            "salmodule": SALMODULE_NS,
            "owl": "http://www.w3.org/2002/07/owl#",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            "sh": "http://www.w3.org/ns/shacl#",
            "xsd": "http://www.w3.org/2001/XMLSchema#",
            "dcterms": "http://purl.org/dc/terms/",
            # order is significant for CLI arguments, and sources run in the
            # order listed, so both round-trip through RDF as rdf:List rather
            # than an unordered set of triples.
            "sources": {"@id": "sources", "@container": "@list"},
            "options": {"@id": "options", "@container": "@list"},
        },
        "@graph": [
            {
                "@id": ".",
                "@type": "owl:Ontology",
                "dcterms:title": "Portolan Extract SAL Module",
                "dcterms:description": (
                    "Wraps the `portolan extract` sub-command (ArcGIS, WFS, Carto) "
                    "as a SAL Module Task."
                ),
                "owl:versionInfo": "1.0",
            },
            {
                "@id": TASK_CLASS,
                "@type": "owl:Class",
                "rdfs:label": "Portolan Extract",
                "rdfs:comment": (
                    "Runs `portolan extract` once per remote geospatial source "
                    "(ArcGIS, WFS, or Carto), all into the same output directory so "
                    "portolan accumulates them as collections in one shared STAC "
                    "catalog. That directory is emitted once, as a single "
                    "trailing-slash file:/// node, so the SAL project copies it out "
                    "of the container whole, preserving the catalog's internal "
                    "structure."
                ),
                "rdfs:subClassOf": {"@id": "salmodule:Task"},
                "salmodule:self": {
                    "@type": "sh:NodeShape",
                    "sh:property": [
                        {
                            "sh:path": {"@id": "sources"},
                            "sh:minCount": 1,
                            "sh:message": "At least one source must be provided.",
                        }
                    ],
                },
            },
            {
                "@id": "sources",
                "@type": "owl:ObjectProperty",
                "rdfs:comment": (
                    "Ordered list of Source objects; one `portolan extract` run "
                    "happens per entry, all sharing one output directory."
                ),
                "rdfs:domain": {"@id": TASK_CLASS},
                "rdfs:range": {"@id": "Source"},
            },
            {
                "@id": "Source",
                "@type": "owl:Class",
                "rdfs:label": "Extraction Source",
                "rdfs:comment": (
                    "One remote endpoint to extract, plus the provider-specific "
                    "flags portolan needs to do it."
                ),
            },
            {
                "@id": "provider",
                "@type": "owl:DatatypeProperty",
                "rdfs:comment": (
                    "Which `portolan extract` sub-command to run: "
                    "'arcgis', 'wfs', or 'carto'."
                ),
                "rdfs:domain": {"@id": "Source"},
                "rdfs:range": {"@id": "xsd:string"},
            },
            {
                "@id": "url",
                "@type": "owl:DatatypeProperty",
                "rdfs:comment": "The service endpoint URL passed to `portolan extract <provider>`.",
                "rdfs:domain": {"@id": "Source"},
                "rdfs:range": {"@id": "xsd:anyURI"},
            },
            {
                "@id": "options",
                "@type": "owl:DatatypeProperty",
                "rdfs:comment": (
                    "Optional array of raw CLI arguments appended verbatim to "
                    "`portolan extract <provider> <url> <output_dir>`, e.g. "
                    '["--layers", "Census*", "--dry-run"]. See '
                    "`portolan extract <provider> --help` for available flags. "
                    "Left unconstrained by design."
                ),
                "rdfs:domain": {"@id": "Source"},
                "rdfs:range": {"@id": "rdf:List"},
            },
        ],
    }


def emit(node: dict) -> None:
    print(json.dumps(node))


def emit_error(label: str, message: str) -> None:
    """Emit a salmodule:Error node conforming to salmodule:Task's output shape."""
    emit({"@type": "salmodule:Error", "rdfs:label": label, "rdfs:comment": message})


def fail(label: str, message: str) -> NoReturn:
    emit_error(label, message)
    sys.exit(1)


def get_task_instance() -> dict:
    raw = os.environ.get("SALMODULE_TASK_INSTANCE")
    if not raw:
        fail("MissingTaskInstance", "SALMODULE_TASK_INSTANCE environment variable is not set.")

    try:
        instance = json.loads(raw)
    except json.JSONDecodeError as e:
        fail("InvalidTaskInstance", f"SALMODULE_TASK_INSTANCE is not valid JSON: {e}")

    if instance.get("@type") != TASK_CLASS:
        fail(
            "UnknownTask",
            f"Task subclass '{instance.get('@type')}' is not recognized; "
            f"expected '{TASK_CLASS}'.",
        )

    return instance


def run_source(index: int, source: dict, output_dir: Path) -> bool:
    """Run one `portolan extract <provider>` call into the shared output directory.

    Every source in a task instance targets the same output_dir, so portolan
    accumulates each one as its own collection under one shared STAC catalog
    (see portolan-cli issue #767) instead of a separate catalog per source.
    """
    provider = source.get("provider")
    url = source.get("url")
    options = source.get("options") or []

    if provider not in PROVIDERS:
        emit_error(
            "InvalidSource",
            f"sources[{index}]: 'provider' must be one of {PROVIDERS}, got {provider!r}.",
        )
        return False
    if not url or not isinstance(url, str):
        emit_error("InvalidSource", f"sources[{index}]: 'url' is required.")
        return False
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        emit_error("InvalidSource", f"sources[{index}]: 'options' must be an array of strings.")
        return False

    output_dir.mkdir(parents=True, exist_ok=True)
    command = ["portolan", "extract", provider, url, str(output_dir), "--auto", *options]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError:
        emit_error("PortolanNotFound", "The 'portolan' executable was not found on PATH.")
        return False

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        emit_error(
            "ExtractionFailed",
            f"sources[{index}] ({provider} {url}) failed: {detail}",
        )
        return False

    return True


def run_cmd(output_root: Path | None = None) -> int:
    """Execute the salmodule:Task subclass instance set in SALMODULE_TASK_INSTANCE."""
    instance = get_task_instance()

    sources = instance.get("sources")
    if not isinstance(sources, list) or not sources:
        fail("InvalidTaskInstance", "'sources' must be a non-empty array.")

    if output_root is None:
        # Not cleaned up afterward: SAL copies the output directory out of
        # the container after reading the file:// node naming it, which can
        # happen after this process has already exited. Deleting it here
        # raced that copy and left SAL with nothing to read (the directory
        # was gone by the time `docker cp` ran). The container itself is
        # discarded once SAL is done with it, so there is nothing left to
        # clean up on our end.
        output_root = DEFAULT_OUTPUT_ROOT

    ok = all([run_source(index, source, output_root) for index, source in enumerate(sources)])

    # One node for the whole shared catalog, not one per source: the whole
    # point of extracting every source into output_root is that portolan
    # accumulates them into a single STAC catalog there.
    if output_root.is_dir() and any(output_root.iterdir()):
        node: dict = {"@id": f"file://{output_root.resolve()}/"}
        if (output_root / "catalog.json").exists():
            node["dcterms:conformsTo"] = {"@id": STAC_SPEC}
        emit(node)

    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="main.py", description="Main application CLI.")
    top = parser.add_subparsers(dest="group")

    salmodule = top.add_parser("salmodule", help="Implements the salmodule (CLI) specification.")
    sub = salmodule.add_subparsers(dest="command")

    for name in ONTOLOGY_COMMANDS:
        sub.add_parser(name, help="Print this SAL Module's ontology.")

    sub.add_parser(
        "run", help="Execute the salmodule:Task instance set in SALMODULE_TASK_INSTANCE."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.group != "salmodule" or args.command is None:
        parser.print_help()
        return 0

    if args.command in ONTOLOGY_COMMANDS:
        print(json.dumps(build_ontology(), indent=2))
        return 0

    return run_cmd()


if __name__ == "__main__":
    sys.exit(main())
