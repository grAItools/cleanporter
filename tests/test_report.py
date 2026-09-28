"""``--format json|sarif|github``: the machine-readable reports, end to end and unit."""

from __future__ import annotations

import base64
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

import cleanporter
from cleanporter import _report, cli, engine, model, resolver

CONSUMER = (
    "from demo.helpers import THING\n"
    "from definitely_missing_pkg_xyz import other\n"
    "total = THING + other\n"
)


@pytest.fixture
def project(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A project holding one `CP001` and one `CP002`, with the cwd at its root."""
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "demo" / "helpers.py").write_text("THING = 42\n", encoding="utf-8")
    (tmp_path / "src" / "demo" / "consumer.py").write_text(CONSUMER, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _json_run(capsys: pytest.CaptureFixture[str], *argv: str):
    rc, out, _ = _run(capsys, "--format", "json", *argv)
    document = json.loads(out)  # the whole of stdout is one document
    assert isinstance(document, dict)
    return rc, document


# -- json -------------------------------------------------------------------


def test_json_is_one_document_with_every_field(project, capsys):
    rc, document = _json_run(capsys, "src")
    assert rc == 1
    assert list(document) == [
        "tool",
        "format_version",
        "mode",
        "strict",
        "exit_code",
        "counts",
        "findings",
        "errors",
        "warnings",
        "notes",
        "patches",
    ]
    assert document["tool"] == {"name": "cleanporter", "version": cleanporter.__version__}
    assert document["format_version"] == 1
    assert document["mode"] == "check"
    assert document["exit_code"] == rc
    assert document["counts"] == {
        "files_checked": 3,
        "changed": 0,
        "violations": 1,
        "not_rewritten": 0,
        "unresolved": 1,
        "skipped_by_config": 0,
        "errors": 0,
    }
    assert document["findings"] == [
        {
            "code": "CP001",
            "status": "violation",
            "level": "error",
            "path": "src/demo/consumer.py",
            "line": 1,
            "column": 0,
            "parent": "demo.helpers",
            "name": "THING",
            "message": "imports object 'THING' from module 'demo.helpers'; "
            "import the module and use 'helpers.THING'",
            "detail": "",
        },
        {
            "code": "CP002",
            "status": "unresolved",
            "level": "warning",
            "path": "src/demo/consumer.py",
            "line": 2,
            "column": 0,
            "parent": "definitely_missing_pkg_xyz",
            "name": "other",
            "message": "could not determine whether 'definitely_missing_pkg_xyz.other' is a "
            "module: 'definitely_missing_pkg_xyz' is not importable in the target interpreter",
            "detail": "'definitely_missing_pkg_xyz' is not importable in the target interpreter",
        },
    ]
    assert document["errors"] == document["patches"] == []


def test_json_messages_are_the_text_report_lines(project, capsys):
    """Same findings, same words: ``path:line:column: code message`` rebuilt from JSON."""
    _, text, _ = _run(capsys, "src")
    _, document = _json_run(capsys, "src")
    findings = document["findings"]
    assert isinstance(findings, list)
    rebuilt = [
        f"{f['path']}:{f['line']}:{f['column']}: {f['code']} {f['message']}" for f in findings
    ]
    assert rebuilt == text.splitlines()[: len(rebuilt)]


def test_strict_makes_cp002_an_error_and_changes_the_exit_code(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from definitely_missing_pkg_xyz import other\n", encoding="utf-8"
    )
    rc, document = _json_run(capsys, "src")
    assert rc == document["exit_code"] == 0
    rc, document = _json_run(capsys, "--strict", "src")
    assert rc == document["exit_code"] == 1
    assert document["strict"] is True
    findings = document["findings"]
    assert isinstance(findings, list)
    assert [f["level"] for f in findings] == ["error"]


def test_cp004_is_counted_always_and_listed_only_under_show_skipped(project, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        "[tool.cleanporter]\nskip = [{ file = '.*consumer[.]py', reason = 'legacy' }]\n",
        encoding="utf-8",
    )
    rc, document = _json_run(capsys, "src")
    assert rc == 0
    counts = document["counts"]
    assert isinstance(counts, dict)
    assert counts["skipped_by_config"] == 2
    assert document["findings"] == []
    _, document = _json_run(capsys, "--show-skipped", "src")
    findings = document["findings"]
    assert isinstance(findings, list)
    assert {(f["code"], f["level"]) for f in findings} == {("CP004", "note")}


def test_json_lists_a_file_it_could_not_process_as_an_error(project, capsys):
    (project / "src" / "demo" / "broken.py").write_text("def (:\n", encoding="utf-8")
    rc, out, err = _run(capsys, "--format", "json", "src")
    document = json.loads(out)
    assert rc == document["exit_code"] == 2
    [error] = document["errors"]
    assert list(error) == ["code", "path", "line", "column", "message"]
    assert error["path"] == "src/demo/broken.py"
    assert error["message"].startswith("file not processed: ")
    assert all(f["path"] != "src/demo/broken.py" for f in document["findings"])
    # In the document, and echoed to stderr for whoever reads the log.
    assert "src/demo/broken.py:1:0: CP002 file not processed: " in err


def test_json_diff_carries_the_patch_in_the_document(project, capsys):
    before = (project / "src" / "demo" / "consumer.py").read_bytes()
    rc, out, err = _run(capsys, "--format", "json", "--diff", "src")
    document = json.loads(out)  # no raw patch ahead of the document
    assert rc == document["exit_code"] == 1  # the CP001 is still there: nothing was written
    assert document["mode"] == "diff"
    [patch] = document["patches"]
    assert patch["path"] == "src/demo/consumer.py"
    assert patch["written"] is False
    assert patch["write_error"] is None
    assert patch["diff_encoding"] == "utf-8"
    assert "-from demo.helpers import THING\n+from demo import helpers\n" in patch["diff"]
    assert (project / "src" / "demo" / "consumer.py").read_bytes() == before
    assert "checked 3 file(s)" in err  # the summary is on stderr


def test_json_fix_writes_and_reports_what_remains(project, capsys):
    rc, out, err = _run(capsys, "--format", "json", "--fix", "src")
    document = json.loads(out)
    assert rc == document["exit_code"] == 0
    [patch] = document["patches"]
    assert patch["written"] is True
    assert document["counts"]["changed"] == 1
    assert [f["code"] for f in document["findings"]] == ["CP002"]
    assert "fixed: src/demo/consumer.py" in err
    assert "re-run your tests" in err


def test_a_patch_that_is_not_utf8_is_carried_as_base64():
    diff = "-x = 'caf\xe9'\n".encode("latin-1")
    patch = engine.FilePatch(pathlib.Path("m.py"), diff, written=False, before=b"", after=b"")
    encoded = _report._json_patch(patch)
    assert isinstance(encoded, dict)
    assert encoded["diff_encoding"] == "base64"
    assert base64.b64decode(str(encoded["diff"])) == diff


# -- sarif ------------------------------------------------------------------


def _sarif_run(capsys: pytest.CaptureFixture[str], *argv: str):
    rc, out, err = _run(capsys, "--format", "sarif", *argv)
    document = json.loads(out)
    assert isinstance(document, dict)
    return rc, document, err


def test_sarif_has_the_required_structure(project, capsys):
    rc, document, _ = _sarif_run(capsys, "src")
    assert rc == 1
    assert document["version"] == "2.1.0"
    assert document["$schema"] == _report.SARIF_SCHEMA
    [run] = document["runs"]
    driver = run["tool"]["driver"]
    assert driver["name"] == "cleanporter"
    assert driver["version"] == cleanporter.__version__
    assert driver["informationUri"] == "https://graitools.github.io/cleanporter/"
    rules = driver["rules"]
    assert [r["id"] for r in rules] == ["CP001", "CP002", "CP003", "CP004"]
    for rule in rules:
        assert rule["shortDescription"]["text"]
        assert rule["fullDescription"]["text"]
        assert rule["help"]["text"] and rule["help"]["markdown"]
        assert rule["helpUri"] == "https://graitools.github.io/cleanporter/usage/#finding-codes"
        assert rule["helpUri"] in rule["help"]["markdown"]
    assert run["originalUriBaseIds"]["SRCROOT"]["uri"] == project.as_uri() + "/"
    assert run["columnKind"] == "unicodeCodePoints"
    [invocation] = run["invocations"]
    assert invocation["executionSuccessful"] is True
    assert invocation["exitCode"] == rc
    results = run["results"]
    assert [(r["ruleId"], r["level"]) for r in results] == [
        ("CP001", "error"),
        ("CP002", "warning"),
    ]
    for result in results:
        assert rules[result["ruleIndex"]]["id"] == result["ruleId"]
        assert result["message"]["text"]
        [location] = result["locations"]
        physical = location["physicalLocation"]
        assert physical["artifactLocation"] == {
            "uri": "src/demo/consumer.py",
            "uriBaseId": "SRCROOT",
        }
        assert physical["region"]["startColumn"] == 1  # 1-based; the text report's 0
    assert results[0]["properties"] == {"parent": "demo.helpers", "name": "THING"}


def test_sarif_strict_makes_cp002_an_error(project, capsys):
    _, document, _ = _sarif_run(capsys, "--strict", "src")
    [run] = document["runs"]
    assert [r["level"] for r in run["results"]] == ["error", "error"]


def test_sarif_reports_a_file_error_as_a_failed_execution(project, capsys):
    (project / "src" / "demo" / "broken.py").write_text("def (:\n", encoding="utf-8")
    rc, document, _ = _sarif_run(capsys, "src")
    assert rc == 2
    [run] = document["runs"]
    [invocation] = run["invocations"]
    assert invocation["executionSuccessful"] is False
    assert invocation["exitCode"] == 2
    [notification] = invocation["toolExecutionNotifications"]
    assert notification["level"] == "error"
    uri = notification["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "src/demo/broken.py"
    assert all(
        "broken" not in r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for r in run["results"]
    )


def test_sarif_uri_is_absolute_outside_the_working_directory(project, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "odd name.py"
    finding = model.Finding(elsewhere, 3, 4, "p", "n", model.Status.SKIPPED, "why")
    result = engine.RunResult(engine.Mode.CHECK, 1, (finding,), (), (), ())
    document = json.loads(
        _report.render("sarif", result, strict=False, show_skipped=False, cwd=project)
    )
    location = document["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert location["artifactLocation"] == {"uri": elsewhere.as_uri()}
    assert location["region"] == {"startLine": 3, "startColumn": 5}


def test_sarif_srcroot_of_the_filesystem_root_is_file_slash_slash_slash():
    result = engine.RunResult(engine.Mode.CHECK, 0, (), (), (), ())
    document = json.loads(
        _report.render("sarif", result, strict=False, show_skipped=False, cwd=pathlib.Path("/"))
    )
    assert document["runs"][0]["originalUriBaseIds"]["SRCROOT"]["uri"] == "file:///"


#: A filename that is not valid UTF-8, as Python hands it over: a lone surrogate.
_UNDECODABLE = pathlib.Path(os.fsdecode(b"bad\xff.py"))


def test_every_format_survives_a_surrogate_escaped_path(tmp_path):
    finding = model.Finding(_UNDECODABLE, 1, 0, "p", "n", model.Status.SKIPPED, "why")
    outside = model.Finding(
        pathlib.Path(os.fsdecode(b"/elsewhere/\xff.py")), 1, 0, "p", "n", model.Status.SKIPPED
    )
    result = engine.RunResult(engine.Mode.CHECK, 1, (finding, outside), (), (), ())
    outputs = {
        fmt: _report.render(fmt, result, strict=False, show_skipped=False, cwd=tmp_path)
        for fmt in ("json", "sarif", "github")
    }
    for out in outputs.values():
        out.encode("utf-8")  # writable to any UTF-8 stdout: no lone surrogate left
    sarif = json.loads(outputs["sarif"])
    uris = [
        r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for r in sarif["runs"][0]["results"]
    ]
    assert uris == ["bad%FF.py", "file:///elsewhere/%FF.py"]
    assert json.loads(outputs["json"])["findings"][0]["path"] == "bad\udcff.py"
    assert outputs["github"].startswith("::error file=bad\\xff.py,")


def test_sarif_on_a_real_undecodable_filename(project, capsys):
    try:
        (project / "src" / "demo" / _UNDECODABLE).write_text("x = (\n", encoding="utf-8")
    except (OSError, UnicodeEncodeError):
        pytest.skip("this filesystem does not take a non-UTF-8 filename")
    rc, document, _ = _sarif_run(capsys, "src")
    assert rc == 2  # the file does not parse: a file-level error, not a crash
    [run] = document["runs"]
    [notification] = run["invocations"][0]["toolExecutionNotifications"]
    uri = notification["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "src/demo/bad%FF.py"


def test_sarif_relative_uri_is_percent_encoded(tmp_path):
    finding = model.Finding(pathlib.Path("my dir/m%.py"), 1, 0, "p", "n", model.Status.VIOLATION)
    result = engine.RunResult(engine.Mode.CHECK, 1, (finding,), (), (), ())
    document = json.loads(
        _report.render("sarif", result, strict=False, show_skipped=False, cwd=tmp_path)
    )
    artifact = document["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert artifact["artifactLocation"]["uri"] == "my%20dir/m%25.py"


def test_sarif_carries_warnings_and_notes_as_notifications(tmp_path):
    result = engine.RunResult(engine.Mode.CHECK, 0, (), (), (), ("w1",), ("n1",))
    document = json.loads(
        _report.render("sarif", result, strict=False, show_skipped=False, cwd=tmp_path)
    )
    notifications = document["runs"][0]["invocations"][0]["toolExecutionNotifications"]
    assert notifications == [
        {"level": "warning", "message": {"text": "w1"}},
        {"level": "note", "message": {"text": "n1"}},
    ]


# -- github -----------------------------------------------------------------


def test_github_prints_one_workflow_command_per_finding(project, capsys):
    rc, out, err = _run(capsys, "--format", "github", "src")
    assert rc == 1
    assert out.splitlines() == [
        (
            "::error file=src/demo/consumer.py,line=1,col=1,title=CP001::imports object "
            "'THING' from module 'demo.helpers'; import the module and use 'helpers.THING'"
        ),
        (
            "::warning file=src/demo/consumer.py,line=2,col=1,title=CP002::could not "
            "determine whether 'definitely_missing_pkg_xyz.other' is a module: "
            "'definitely_missing_pkg_xyz' is not importable in the target interpreter"
        ),
    ]
    assert "checked 3 file(s)" in err


def test_github_levels_follow_strict_and_show_skipped(tmp_path):
    findings = (
        model.Finding(pathlib.Path("a.py"), 1, 0, "p", "n", model.Status.UNRESOLVED, "x"),
        model.Finding(pathlib.Path("a.py"), 2, 0, "p", "n", model.Status.SKIPPED, "x"),
        model.Finding(pathlib.Path("a.py"), 3, 0, "p", "n", model.Status.SKIPPED_BY_CONFIG, "x"),
    )
    result = engine.RunResult(engine.Mode.CHECK, 1, findings, (), (), ())

    def commands(*, strict: bool, show_skipped: bool) -> list[str]:
        out = _report.render(
            "github", result, strict=strict, show_skipped=show_skipped, cwd=tmp_path
        )
        return [line.split(" ", 1)[0] for line in out.splitlines()]

    assert commands(strict=False, show_skipped=False) == ["::warning", "::error"]
    assert commands(strict=True, show_skipped=True) == ["::error", "::error", "::notice"]


def test_github_file_error_is_an_error_annotation(project, capsys):
    (project / "src" / "demo" / "broken.py").write_text("def (:\n", encoding="utf-8")
    rc, out, _ = _run(capsys, "--format", "github", "src")
    assert rc == 2
    assert out.splitlines()[0].startswith(
        "::error file=src/demo/broken.py,line=1,col=1,title=CP002::file not processed: "
    )


@pytest.mark.parametrize(
    ("raw", "data", "prop"),
    [
        ("plain", "plain", "plain"),
        ("100%", "100%25", "100%25"),
        ("a\r\nb", "a%0D%0Ab", "a%0D%0Ab"),
        ("a:b,c", "a:b,c", "a%3Ab%2Cc"),
        ("%0A", "%250A", "%250A"),  # an escape already in the text is not decoded later
    ],
)
def test_github_escaping(raw, data, prop):
    assert _report.escape_data(raw) == data
    assert _report.escape_property(raw) == prop


def test_github_escapes_a_path_and_a_message(tmp_path):
    finding = model.Finding(
        pathlib.Path("we,ird:dir/m.py"), 1, 0, "p", "n", model.Status.SKIPPED, "50%\nmore"
    )
    result = engine.RunResult(engine.Mode.CHECK, 1, (finding,), (), (), ())
    out = _report.render("github", result, strict=False, show_skipped=False, cwd=tmp_path)
    assert out == (
        "::error file=we%2Cird%3Adir/m.py,line=1,col=1,title=CP003::"
        "'n' from 'p' not rewritten: 50%25%0Amore\n"
    )


# -- --diff and exit codes across formats -----------------------------------


@pytest.mark.parametrize("fmt", ["sarif", "github"])
@pytest.mark.parametrize("extra", [[], ["--fix"]])
def test_diff_is_a_usage_error_for_a_format_without_patches(project, capsys, fmt, extra):
    """Refused even beside `--fix`, which otherwise overrides `--diff`: no exception."""
    before = (project / "src" / "demo" / "consumer.py").read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli.main(["--format", fmt, "--diff", *extra, "src"])
    assert exc.value.code == 2
    assert "--diff cannot be combined with --format" in capsys.readouterr().err
    assert (project / "src" / "demo" / "consumer.py").read_bytes() == before


@pytest.mark.parametrize("fmt", ["sarif", "github"])
def test_fix_writes_without_printing_a_patch_for_a_format_without_patches(project, capsys, fmt):
    rc, out, err = _run(capsys, "--format", fmt, "--fix", "src")
    assert rc == 0
    assert "+from demo import helpers" not in out + err
    assert "fixed: src/demo/consumer.py" in err
    assert (
        (project / "src" / "demo" / "consumer.py")
        .read_text(encoding="utf-8")
        .startswith("from demo import helpers\n")
    )


@pytest.mark.parametrize("argv", [[], ["--strict"], ["--diff"], ["--fix"]])
def test_every_format_exits_as_the_text_report_does(tmp_path, monkeypatch, capsys, argv):
    codes = {}
    for fmt in ("text", "json", "sarif", "github"):
        if fmt in {"sarif", "github"} and argv == ["--diff"]:
            continue
        work = tmp_path / fmt
        (work / "src" / "demo").mkdir(parents=True)
        (work / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8"
        )
        (work / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
        (work / "src" / "demo" / "helpers.py").write_text("THING = 42\n", encoding="utf-8")
        (work / "src" / "demo" / "consumer.py").write_text(CONSUMER, encoding="utf-8")
        monkeypatch.chdir(work)
        codes[fmt] = cli.main(["--format", fmt, *argv, "src"])
        capsys.readouterr()
    assert len(set(codes.values())) == 1, codes


def test_text_is_the_default_and_unchanged(project, capsys):
    _, default, _ = _run(capsys, "src")
    _, text, _ = _run(capsys, "--format", "text", "src")
    assert default == text
    assert default.splitlines()[0].startswith("src/demo/consumer.py:1:0: CP001 ")


def test_a_package_writing_to_fd_1_on_import_cannot_corrupt_the_document(
    project, capfd, monkeypatch
):
    """The in-process probe imports third-party parents; one may `os.write(1, ...)`.

    `contextlib.redirect_stdout` alone does not catch that -- it bypasses
    `sys.stdout` -- so the probe also points descriptor 1 at stderr.
    """
    site = project / "site"
    (site / "loudpkg").mkdir(parents=True)
    (site / "loudpkg" / "__init__.py").write_text(
        "import os\nos.write(1, b'LOUD BANNER\\n')\nprint('PRINTED BANNER')\nthing = 1\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(site))
    (project / "src" / "demo" / "consumer.py").write_text(
        "from loudpkg import thing\n", encoding="utf-8"
    )
    rc = cli.main(["--format", "json", "--python", "self", "src"])
    captured = capfd.readouterr()
    document = json.loads(captured.out)  # nothing but the document
    assert rc == document["exit_code"] == 1
    assert [f["code"] for f in document["findings"]] == ["CP001"]
    assert "LOUD BANNER" in captured.err
    assert "PRINTED BANNER" in captured.err


class _BoomError(Exception):
    pass


def _raise_inside_the_redirection() -> None:
    with resolver._stdout_to_stderr():
        os.write(1, b"during\n")
        raise _BoomError


def test_descriptor_1_is_restored_when_the_block_raises(capfd):
    with pytest.raises(_BoomError):
        _raise_inside_the_redirection()
    os.write(1, b"after\n")
    captured = capfd.readouterr()
    assert captured.out == "after\n"
    assert captured.err == "during\n"


class _UnflushableStream(io.StringIO):
    def flush(self) -> None:
        raise OSError(9, "Bad file descriptor")


def test_descriptor_1_is_restored_when_a_flush_fails(capfd, monkeypatch):
    """A flush on the way out must not skip putting descriptor 1 back."""
    monkeypatch.setattr(sys, "stderr", _UnflushableStream())
    with resolver._stdout_to_stderr():
        os.write(1, b"during\n")
    os.write(1, b"after\n")
    monkeypatch.undo()
    captured = capfd.readouterr()
    assert captured.out == "after\n"
    assert captured.err == "during\n"


def test_a_run_with_no_sys_stdout_exits_cleanly(project, monkeypatch):
    """``>&-``, ``pythonw``, a GUI host: `sys.stdout` is None, and that is not a failure."""
    (project / "src" / "demo" / "consumer.py").write_text(
        "from definitely_missing_pkg_xyz import other\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys, "stdout", None)
    assert cli.main(["--python", "self", "src"]) == 0  # the probe ran in process


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell to close fd 2")
def test_a_run_with_descriptor_2_closed_exits_cleanly(project):
    """``2>&-``: descriptor 2 cannot be the target of the probe's redirection."""
    (project / "src" / "demo" / "consumer.py").write_text(
        "from definitely_missing_pkg_xyz import other\n", encoding="utf-8"
    )
    code = (
        "import sys; from cleanporter import cli; sys.exit(cli.main(['--python', 'self', 'src']))"
    )
    done = subprocess.run(
        ["sh", "-c", 'exec "$0" -c "$1" 2>&-', sys.executable, code],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout
    assert "checked 3 file(s)" in done.stdout


def test_whole_project_json_reports_only_the_listed_files(project, capsys):
    (project / "src" / "demo" / "clean.py").write_text(
        "from demo import helpers\n\nhelpers.THING\n", encoding="utf-8"
    )
    rc, document = _json_run(capsys, "--whole-project", "src/demo/clean.py")
    assert rc == document["exit_code"] == 0
    assert document["findings"] == []
    assert document["counts"]["files_checked"] == 1
    rc, document = _json_run(capsys, "--whole-project", "src/demo/consumer.py")
    assert rc == document["exit_code"] == 1
    assert {f["path"] for f in document["findings"]} == {"src/demo/consumer.py"}
