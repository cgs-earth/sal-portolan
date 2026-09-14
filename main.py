"""SAL Module wrapping the `portolan extract` sub-command.

Implements the SAL Module CLI spec (https://cgs-earth.github.io/sal/):
`salmodule ontology` prints this module's vocabulary, `salmodule run`
executes the salmodule:Task instance passed via SALMODULE_TASK_INSTANCE.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

SALMODULE_NS = "https://w3id.org/sal/cgs-earth/sal-module-spec/salmodule#"
MODULE_NS = "salmodule://github.com/cgs-earth/sal-portolan/"
TASK_CLASS = "Extract"
PROVIDERS = ("arcgis", "wfs", "carto")
ONTOLOGY_COMMANDS = ("ontology", "vocab", "vocabulary")
STAC_SPEC = "https://stacspec.org/"


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
                    "Runs `portolan extract` against one or more remote geospatial "
                    "sources (ArcGIS, WFS, or Carto). Each source's output directory "
                    "is emitted as a single trailing-slash file:/// node, so the SAL "
                    "project copies it out of the container whole, preserving the "
                    "catalog's internal structure."
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
                    "happens per entry."
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
                "@id": "name",
                "@type": "owl:DatatypeProperty",
                "rdfs:comment": (
                    "Optional slug for this source's extraction subdirectory, so "
                    "multiple sources don't collide. Defaults to a sanitized form "
                    "of the URL."
                ),
                "rdfs:domain": {"@id": "Source"},
                "rdfs:range": {"@id": "xsd:string"},
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


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:60] or "source"


def run_source(index: int, source: dict, output_root: Path) -> bool:
    """Run one `portolan extract <provider>` call and emit its output directory.

    The whole output directory is emitted as a single trailing-slash file:///
    node, so SAL copies it verbatim instead of content-addressing each file
    individually. That preserves the relative hrefs between a STAC catalog,
    its collections, items, and assets, which a flat, per-file digest copy
    would break. Unless --raw was passed, the directory is also asserted to
    conform to the STAC spec. The directory is only emitted once the
    subprocess exits, so it is guaranteed fully written first, per the SAL
    file:/// contract.
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

    name = source.get("name") or slugify(url)
    output_dir = output_root / f"{index:02d}-{provider}-{slugify(name)}"
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

    if any(output_dir.iterdir()):
        node: dict = {"@id": f"file://{output_dir.resolve()}/"}
        if "--raw" not in options:
            # portolan writes a full STAC catalog by default; --raw skips it
            # in favor of bare extraction files.
            node["dcterms:conformsTo"] = {"@id": STAC_SPEC}
        emit(node)

    return True


def run_cmd(output_root: Path | None = None) -> int:
    """Execute the salmodule:Task subclass instance set in SALMODULE_TASK_INSTANCE."""
    instance = get_task_instance()

    sources = instance.get("sources")
    if not isinstance(sources, list) or not sources:
        fail("InvalidTaskInstance", "'sources' must be a non-empty array.")

    if output_root is None:
        # Deliberately not a TemporaryDirectory context manager: SAL copies
        # each source's output directory out of the container after reading
        # the file:// node naming it, which can happen after this process
        # has already exited. Cleaning up here raced that copy and left SAL
        # with nothing to read (the directory was gone by the time `docker
        # cp` ran). The container itself is discarded once SAL is done with
        # it, so there is nothing left to clean up on our end.
        output_root = Path(tempfile.mkdtemp(prefix="portolan-extract-"))

    ok = all([run_source(index, source, output_root) for index, source in enumerate(sources)])

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
