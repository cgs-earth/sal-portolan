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


def test_run_source_emits_nothing_itself_on_success(tmp_path, monkeypatch, capsys):
    """run_source no longer emits a file:// node — run_cmd does, once, after
    every source has extracted into the shared directory."""

    def fake_run(command, capture_output, text):
        output_dir = Path(command[4])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "catalog.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    ok = main.run_source(0, {"provider": "arcgis", "url": "https://example.com/FeatureServer"}, tmp_path)

    assert ok is True
    assert capsys.readouterr().out == ""


def test_run_cmd_extracts_every_source_into_the_same_shared_directory(
    tmp_path, monkeypatch, capsys
):
    """Regression test: multiple sources must extract into one shared
    directory so portolan accumulates them as collections in a single STAC
    catalog, not a separate catalog per source."""
    output_dirs = []

    def fake_run(command, capture_output, text):
        output_dir = Path(command[4])
        output_dirs.append(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setenv(
        "SALMODULE_TASK_INSTANCE",
        json.dumps(
            {
                "@type": "Extract",
                "sources": [
                    {"provider": "arcgis", "url": "https://example.com/boston/FeatureServer"},
                    {"provider": "arcgis", "url": "https://example.com/philly/FeatureServer"},
                ],
            }
        ),
    )

    exit_code = main.run_cmd(output_root=tmp_path)

    assert exit_code == 0
    assert output_dirs == [tmp_path, tmp_path]


def test_run_cmd_emits_one_node_for_the_whole_shared_catalog(tmp_path, monkeypatch, capsys):
    def fake_run(command, capture_output, text):
        output_dir = Path(command[4])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "catalog.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setenv(
        "SALMODULE_TASK_INSTANCE",
        json.dumps(
            {
                "@type": "Extract",
                "sources": [
                    {"provider": "arcgis", "url": "https://example.com/a"},
                    {"provider": "arcgis", "url": "https://example.com/b"},
                ],
            }
        ),
    )

    exit_code = main.run_cmd(output_root=tmp_path)

    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    node = json.loads(lines[0])
    assert node["@id"] == f"file://{tmp_path.resolve()}/"
    assert node["dcterms:conformsTo"] == {"@id": main.STAC_SPEC}


def test_run_cmd_leaves_the_shared_directory_in_place_after_returning(
    tmp_path, monkeypatch, capsys
):
    """Regression test: SAL copies the directory named on stdout out of the
    container asynchronously, which can happen after this process has
    already moved on. Deleting the directory ourselves races that copy and
    fails it with 'file not found in container' — so nothing here may ever
    remove what it wrote.
    """

    def fake_run(command, capture_output, text):
        output_dir = Path(command[4])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "catalog.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setenv(
        "SALMODULE_TASK_INSTANCE",
        json.dumps(
            {
                "@type": "Extract",
                "sources": [{"provider": "arcgis", "url": "https://example.com/a"}],
            }
        ),
    )

    main.run_cmd(output_root=tmp_path)

    assert tmp_path.is_dir()
    assert (tmp_path / "catalog.json").exists()


def test_run_cmd_defaults_to_a_fixed_output_root(monkeypatch, capsys):
    monkeypatch.setattr(main, "DEFAULT_OUTPUT_ROOT", monkeypatch_dir := main.Path("/tmp/portolan-default-root-test"))

    def fake_run(command, capture_output, text):
        output_dir = Path(command[4])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "catalog.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setenv(
        "SALMODULE_TASK_INSTANCE",
        json.dumps(
            {
                "@type": "Extract",
                "sources": [{"provider": "arcgis", "url": "https://example.com/a"}],
            }
        ),
    )

    try:
        exit_code = main.run_cmd()
        assert exit_code == 0
        node = json.loads(capsys.readouterr().out)
        assert node["@id"] == f"file://{monkeypatch_dir.resolve()}/"
    finally:
        import shutil

        shutil.rmtree(monkeypatch_dir, ignore_errors=True)


def test_run_cmd_emits_no_conforms_to_when_raw_skips_the_catalog(tmp_path, monkeypatch, capsys):
    def fake_run(command, capture_output, text):
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
                    {"provider": "arcgis", "url": "https://example.com/a", "options": ["--raw"]}
                ],
            }
        ),
    )

    exit_code = main.run_cmd(output_root=tmp_path)

    assert exit_code == 0
    node = json.loads(capsys.readouterr().out)
    assert "dcterms:conformsTo" not in node


def test_run_cmd_emits_nothing_when_the_shared_dir_stays_empty(tmp_path, monkeypatch, capsys):
    def fake_run(command, capture_output, text):
        Path(command[4]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    monkeypatch.setenv(
        "SALMODULE_TASK_INSTANCE",
        json.dumps(
            {
                "@type": "Extract",
                "sources": [
                    {"provider": "wfs", "url": "https://example.com", "options": ["--dry-run"]}
                ],
            }
        ),
    )

    exit_code = main.run_cmd(output_root=tmp_path)

    assert exit_code == 0
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


def test_run_cmd_attempts_every_source_even_after_a_failure(tmp_path, monkeypatch, capsys):
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

    exit_code = main.run_cmd(output_root=tmp_path)

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
