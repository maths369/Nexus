"""Spreadsheet Service — 受控 Excel/CSV 能力。"""

from __future__ import annotations

import importlib
from pathlib import Path

from nexus.services.workspace import WorkspaceService


class SpreadsheetService:
    """基于 pandas 的最小 Excel/CSV 能力。"""

    def __init__(self, workspace: WorkspaceService):
        self._workspace = workspace

    def list_sheets(self, excel_path: str | Path) -> list[str]:
        source = self._workspace.resolve(excel_path)
        pd = self._load_pandas()
        engine = self._engine_for(source)
        workbook = pd.ExcelFile(source, engine=engine)
        return [str(name) for name in workbook.sheet_names]

    def excel_to_csv(
        self,
        excel_path: str | Path,
        *,
        output_path: str | Path | None = None,
        sheet_name: str | None = None,
        include_index: bool = False,
    ) -> Path:
        source = self._workspace.resolve(excel_path)
        target = self._resolve_output_path(source, output_path, sheet_name)
        pd = self._load_pandas()
        engine = self._engine_for(source)
        dataframe = pd.read_excel(source, sheet_name=sheet_name or 0, engine=engine)
        csv_text = dataframe.to_csv(index=include_index)
        return self._workspace.write_text(target, csv_text)

    @staticmethod
    def _load_pandas():
        try:
            return importlib.import_module("pandas")
        except ImportError as exc:
            raise RuntimeError(
                "Excel capability is not enabled. Run capability_enable('excel_processing') first."
            ) from exc

    @staticmethod
    def _engine_for(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".xls":
            return "xlrd"
        return "openpyxl"

    def _resolve_output_path(
        self,
        source: Path,
        output_path: str | Path | None,
        sheet_name: str | None,
    ) -> Path:
        if output_path is not None:
            return self._workspace.resolve(output_path)
        suffix = f".{sheet_name}" if sheet_name else ""
        return self._workspace.resolve(source.with_suffix(f"{suffix}.csv"))

