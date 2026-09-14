"""Tests for main.py.

Every test runs offline: subprocess.run is monkeypatched so nothing here
shells out to a real `portolan` binary or hits a network service.
"""

import json
import subprocess
from pathlib import Path

import pytest
from pyld import jsonld

import main


def test_build_ontology_declares_the_extract_task():
    ontology = main.build_ontology()
    ids = [node["@id"] for node in ontology["@graph"]]

    assert main.TASK_CLASS in ids
    assert ontology["@context"]["@vocab"] == "salmodule://github.com/cgs-earth/sal-portolan/"
    assert ontology["@context"]["sources"]["@container"] == "@list"
    assert ontology["@context"]["options"]["@container"] == "@list"


def test_build_ontology_is_valid_jsonld():
    """Regression test: a bad @context entry (e.g. a bare term with no
    @vocab set) makes SAL fail to parse the *entire* ontology, which then
    fails validation for every term, not just the broken one. jsonld.expand
    raises on exactly that class of error.
    """
    jsonld.expand(main.build_ontology())


def test_slugify_replaces_non_alphanumerics_and_lowercases():
    assert main.slugify("https://example.com/FeatureServer!!") == "https-example-com-featureserver"


def test_slugify_falls_back_when_nothing_alphanumeric_remains():
    assert main.slugify("///") == "source"


def test_emit_error_writes_a_conformant_salmodule_error_node(capsys):
    main.emit_error("SomeLabel", "some message")

    node = json.loads(capsys.readouterr().out)
    assert node == {
        "@type": "salmodule:Error",
        "rdfs:label": "SomeLabel",
        "rdfs:comment": "some message",
    }


def test_get_task_instance_requires_the_env_var(monkeypatch, capsys):
    monkeypatch.delenv("SALMODULE_TASK_INSTANCE", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main.get_task_instance()

    assert exc_info.value.code == 1
    error = json.loads(capsys.readouterr().out)
    assert error["rdfs:label"] == "MissingTaskInstance"


def test_get_task_instance_rejects_invalid_json(monkeypatch, capsys):
    monkeypatch.setenv("SALMODULE_TASK_INSTANCE", "{not valid json")

    with pytest.raises(SystemExit):
        main.get_task_instance()

    error = json.loads(capsys.readouterr().out)
    assert error["rdfs:label"] == "InvalidTaskInstance"


def test_get_task_instance_rejects_an_unknown_task_type(monkeypatch, capsys):
    monkeypatch.setenv("SALMODULE_TASK_INSTANCE", json.dumps({"@type": "SomethingElse"}))

    with pytest.raises(SystemExit):
        main.get_task_instance()

    error = json.loads(capsys.readouterr().out)
    assert error["rdfs:label"] == "UnknownTask"


def test_get_task_instance_accepts_a_well_formed_extract_instance(monkeypatch):
    monkeypatch.setenv(
        "SALMODULE_TASK_INSTANCE", json.dumps({"@type": "Extract", "sources": []})
    )

    assert main.get_task_instance()["@type"] == "Extract"


def test_run_source_rejects_an_unknown_provider(tmp_path):
    ok = main.run_source(0, {"provider": "ftp", "url": "https://example.com"}, tmp_path)
    assert ok is False


def test_run_source_rejects_a_missing_url(tmp_path):
    ok = main.run_source(0, {"provider": "wfs"}, tmp_path)
    assert ok is False


def test_run_source_rejects_non_string_options(tmp_path):
    ok = main.run_source(
        0, {"provider": "wfs", "url": "https://example.com", "options": [1, 2]}, tmp_path
    )
    assert ok is False


def test_run_source_emits_one_trailing_slash_node_for_the_whole_output_dir(
    tmp_path, monkeypatch, capsys
):
    def fake_run(command, capture_output, text):
        output_dir = Path(command[4])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "catalog.json").write_text("{}")
        collection_dir = output_dir / "collection"
        collection_dir.mkdir()
        (collection_dir / "item.json").write_text("{}")
        (collection_dir / "data.parquet").write_bytes(b"x")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    ok = main.run_source(
        0,
        {"provider": "arcgis", "url": "https://example.com/FeatureServer", "name": "demo"},
        tmp_path,
    )
    assert ok is True

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    node = json.loads(lines[0])
    assert node["@id"].startswith("file://")
    assert node["@id"].endswith("/")
    assert node["dcterms:conformsTo"] == {"@id": main.STAC_SPEC}


def test_run_source_omits_conforms_to_when_raw_is_passed(tmp_path, monkeypatch, capsys):
    def fake_run(command, capture_output, text):
        output_dir = Path(command[4])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "data.parquet").write_bytes(b"x")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    ok = main.run_source(
        0, {"provider": "arcgis", "url": "https://example.com/FeatureServer", "options": ["--raw"]}, tmp_path
    )
    assert ok is True

    node = json.loads(capsys.readouterr().out)
    assert "dcterms:conformsTo" not in node


def test_run_source_emits_nothing_when_the_output_dir_is_empty(tmp_path, monkeypatch, capsys):
    def fake_run(command, capture_output, text):
        Path(command[4]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    ok = main.run_source(0, {"provider": "wfs", "url": "https://example.com", "options": ["--dry-run"]}, tmp_path)
    assert ok is True
    assert capsys.readouterr().out == ""


def test_run_source_reports_a_nonzero_exit_as_an_error(tmp_path, monkeypatch, capsys):
    def fake_run(command, capture_output, text):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    ok = main.run_source(0, {"provider": "carto", "url": "https://example.com"}, tmp_path)
    assert ok is False

    error = json.loads(capsys.readouterr().out)
    assert error["rdfs:label"] == "ExtractionFailed"
    assert "boom" in error["rdfs:comment"]


def test_run_source_reports_a_missing_portolan_binary(tmp_path, monkeypatch, capsys):
    def fake_run(command, capture_output, text):
        raise FileNotFoundError()

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    ok = main.run_source(0, {"provider": "wfs", "url": "https://example.com"}, tmp_path)
    assert ok is False

    error = json.loads(capsys.readouterr().out)
    assert error["rdfs:label"] == "PortolanNotFound"


def test_run_cmd_attempts_every_source_even_after_a_failure(monkeypatch, capsys):
    calls = []

    def fake_run(command, capture_output, text):
        calls.append(command)
        if command[2] == "wfs":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="nope")
        output_dir = Path(command[4])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "data.parquet").write_bytes(b"x")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setenv(
        "SALMODULE_TASK_INSTANCE",
        json.dumps(
            {
                "@type": "Extract",
                "sources": [
                    {"provider": "arcgis", "url": "https://example.com/a"},
                    {"provider": "wfs", "url": "https://example.com/b"},
                ],
            }
        ),
    )

    exit_code = main.run_cmd()

    assert exit_code == 1
    assert len(calls) == 2

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    file_nodes = [n for n in lines if "@id" in n]
    errors = [n for n in lines if n.get("@type") == "salmodule:Error"]
    assert len(file_nodes) == 1
    assert len(errors) == 1


def test_run_cmd_requires_a_non_empty_sources_array(monkeypatch):
    monkeypatch.setenv("SALMODULE_TASK_INSTANCE", json.dumps({"@type": "Extract", "sources": []}))

    with pytest.raises(SystemExit):
        main.run_cmd()


def test_cli_help_exits_zero():
    with pytest.raises(SystemExit) as exc_info:
        main.main(["--help"])
    assert exc_info.value.code == 0


def test_cli_ontology_prints_the_ontology_as_json(capsys):
    exit_code = main.main(["salmodule", "ontology"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["@graph"]


def test_cli_with_no_subcommand_prints_help_and_exits_zero(capsys):
    exit_code = main.main([])

    assert exit_code == 0
    assert "usage" in capsys.readouterr().out
