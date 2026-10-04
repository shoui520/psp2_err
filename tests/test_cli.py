import json
import subprocess
from pathlib import Path

import pytest

from psp2_err.cli import main


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_source_launcher_runs_without_installation(tmp_path):
    result = subprocess.run(
        [str(PROJECT_ROOT / "psp2_err"), "C2-2000-2"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "SCE_APPUTIL_ERROR_PARAMETER" in result.stdout


def test_exact_lookup(capsys):
    assert main(["C2-2000-2"]) == 0
    output = capsys.readouterr().out
    assert "0x80100600" in output
    assert "SCE_APPUTIL_ERROR_PARAMETER" in output
    assert "Could not be saved." in output


@pytest.mark.parametrize("query", ["C2-12828-1", "0x80103909"])
def test_firmware_lookup_with_community_remark(query, capsys):
    assert main([query]) == 0
    output = capsys.readouterr().out
    assert "Hex:      0x80103909" in output
    assert "Display:  C2-12828-1" in output
    assert "An error occurred in the following applications." in output


def test_unknown_numeric_value_is_decoded(capsys):
    assert main(["0x80107e42"]) == 1
    output = capsys.readouterr().out
    assert "No exact match found" in output
    assert "SCE_ERROR_FACILITY_VSH" in output
    assert "0x80107EXX" in output


def test_json_lookup(capsys):
    assert main(["--json", "0x8041210b"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["matches"][0]["codes"] == ["NW-2029-3"]
    assert payload[0]["decoded"]["facility_name"] == "SCE_ERROR_FACILITY_NETWORK"


def test_search_and_miss(capsys):
    assert main(["--search", "CMA data verify", "--limit", "0"]) == 0
    assert "C2-12858-4" in capsys.readouterr().out

    assert main(["--search", "definitely-not-a-vita-error"]) == 1
    assert "No matches found" in capsys.readouterr().out


def test_incompatible_modes_are_rejected():
    with pytest.raises(SystemExit) as error:
        main(["--stats", "C2-2000-2"])
    assert error.value.code == 2
