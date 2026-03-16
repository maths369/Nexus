from __future__ import annotations

from nexus.services.spreadsheet import SpreadsheetService
from nexus.services.workspace import WorkspaceService


class _FakeFrame:
    def to_csv(self, index: bool = False):
        return "a,b\n1,2\n"


class _FakeWorkbook:
    def __init__(self, path, engine=None):
        self.sheet_names = ["Sheet1", "Sheet2"]


class _FakePandas:
    ExcelFile = _FakeWorkbook

    @staticmethod
    def read_excel(path, sheet_name=0, engine=None):
        return _FakeFrame()


def test_list_sheets(monkeypatch, tmp_path):
    workbook = tmp_path / "sample.xlsx"
    workbook.write_text("placeholder", encoding="utf-8")
    workspace = WorkspaceService([tmp_path])
    service = SpreadsheetService(workspace)
    monkeypatch.setattr(service, "_load_pandas", lambda: _FakePandas)

    sheets = service.list_sheets(workbook)
    assert sheets == ["Sheet1", "Sheet2"]


def test_excel_to_csv_writes_output(monkeypatch, tmp_path):
    workbook = tmp_path / "sample.xlsx"
    workbook.write_text("placeholder", encoding="utf-8")
    workspace = WorkspaceService([tmp_path])
    service = SpreadsheetService(workspace)
    monkeypatch.setattr(service, "_load_pandas", lambda: _FakePandas)

    output = service.excel_to_csv(workbook)
    assert output.name == "sample.csv"
    assert output.read_text(encoding="utf-8") == "a,b\n1,2\n"

