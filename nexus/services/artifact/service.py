"""Unified artifact capture and materialization pipeline."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - optional dependency
    PdfReader = None  # type: ignore[assignment]

from nexus.knowledge import VaultContentStore
from nexus.services.audio import AudioService
from nexus.services.document import DocumentEditorService, DocumentService

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".amr", ".webm", ".opus"}
_TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml"}


@dataclass
class ArtifactRecord:
    artifact_id: str
    artifact_type: str
    source: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    relative_path: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def absolute_path(self) -> Path:
        raw = self.metadata.get("absolute_path")
        return Path(str(raw)).resolve()


@dataclass
class ArtifactMaterializationResult:
    artifact: ArtifactRecord
    status: str
    note: str
    page_relative_path: str | None = None
    transcript_relative_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def attachment_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact.artifact_id,
            "artifact_type": self.artifact.artifact_type,
            "filename": self.artifact.filename,
            "mime_type": self.artifact.mime_type,
            "size_bytes": self.artifact.size_bytes,
            "sha256": self.artifact.sha256,
            "relative_path": self.artifact.relative_path,
            "status": self.status,
            "page_relative_path": self.page_relative_path,
            "transcript_relative_path": self.transcript_relative_path,
            "metadata": dict(self.metadata),
        }

    def summary_line(self) -> str:
        parts = [
            f"{self.artifact.artifact_type} `{self.artifact.filename}` 已保存到 `{self.artifact.relative_path}`",
        ]
        if self.transcript_relative_path:
            parts.append(f"转录 `{self.transcript_relative_path}`")
        if self.page_relative_path:
            parts.append(f"知识页 `{self.page_relative_path}`")
        if self.note:
            parts.append(self.note)
        return "，".join(parts)


@dataclass
class ArtifactBatchResult:
    status: str
    note: str
    page_relative_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ArtifactService:
    """Capture raw inbound artifacts and materialize them into Vault knowledge pages."""

    def __init__(
        self,
        content_store: VaultContentStore,
        document_service: DocumentService,
        *,
        document_editor: DocumentEditorService | None = None,
        audio_service: AudioService | None = None,
        image_text_extractor: Any | None = None,
    ) -> None:
        self._content = content_store
        self._documents = document_service
        self._editor = document_editor
        self._audio = audio_service
        self._image_text_extractor = image_text_extractor

    async def ingest_bytes(
        self,
        *,
        artifact_type: str,
        source: str,
        data: bytes,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactMaterializationResult:
        record = self.store_bytes(
            artifact_type=artifact_type,
            source=source,
            data=data,
            filename=filename,
            mime_type=mime_type,
            metadata=metadata,
        )
        return await self.materialize(record)

    def store_bytes(
        self,
        *,
        artifact_type: str,
        source: str,
        data: bytes,
        filename: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        kind = self._normalize_artifact_type(
            artifact_type,
            filename=filename,
            mime_type=mime_type,
        )
        normalized_name = self._normalize_filename(filename, kind=kind)
        resolved_mime = mime_type or mimetypes.guess_type(normalized_name)[0] or self._default_mime(kind)
        ext = Path(normalized_name).suffix or self._extension_for_mime(resolved_mime, kind=kind)
        stem = Path(normalized_name).stem or kind
        safe_name = self._sanitize_filename(stem)
        sha = hashlib.sha256(data).hexdigest()
        now = datetime.now(timezone.utc)
        relative_dir = self._storage_prefix(kind) / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        relative_path = (relative_dir / f"{sha[:12]}-{safe_name}{ext}").as_posix()
        absolute_path = self._content.resolve_path(relative_path)
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        if not absolute_path.exists():
            absolute_path.write_bytes(data)

        record = ArtifactRecord(
            artifact_id=f"art_{sha[:16]}",
            artifact_type=kind,
            source=source,
            filename=f"{safe_name}{ext}",
            mime_type=resolved_mime,
            size_bytes=len(data),
            sha256=sha,
            relative_path=relative_path,
            metadata={
                **(metadata or {}),
                "absolute_path": str(absolute_path),
                "captured_at": now.isoformat(),
                "source": source,
            },
        )
        return record

    async def materialize(self, artifact: ArtifactRecord) -> ArtifactMaterializationResult:
        try:
            if artifact.artifact_type == "audio":
                return await self._materialize_audio(artifact)
            if artifact.artifact_type == "image":
                return await self._materialize_image(artifact)
            return await self._materialize_file(artifact)
        except Exception as exc:  # noqa: BLE001
            return ArtifactMaterializationResult(
                artifact=artifact,
                status="stored_with_warning",
                note=f"已保留原始资产，但自动物化失败：{exc}",
            )

    async def create_batch_manifest(
        self,
        artifacts: list[ArtifactMaterializationResult],
        *,
        source: str,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactBatchResult:
        valid_items = [item for item in artifacts if item.artifact.relative_path]
        if not valid_items:
            return ArtifactBatchResult(status="skipped", note="没有可用于生成批量导入清单的资产。")

        now = datetime.now(timezone.utc)
        manifest_title = title or f"批量导入清单 {now.strftime('%Y-%m-%d %H:%M')}"
        counts: dict[str, int] = {}
        for item in valid_items:
            counts[item.artifact.artifact_type] = counts.get(item.artifact.artifact_type, 0) + 1

        body_lines = [
            f"# {manifest_title}",
            "",
            "## 导入概览",
            "",
            f"- 来源：`{source}`",
            f"- 文件数：{len(valid_items)}",
            f"- 类型统计：{', '.join(f'{kind}={count}' for kind, count in sorted(counts.items()))}",
            "",
            "## 明细",
            "",
        ]
        for idx, item in enumerate(valid_items, start=1):
            body_lines.extend(
                [
                    f"### {idx}. {item.artifact.filename}",
                    "",
                    f"- 类型：`{item.artifact.artifact_type}`",
                    f"- 原始文件：`{item.artifact.relative_path}`",
                    f"- SHA256：`{item.artifact.sha256[:16]}`",
                    f"- 物化状态：`{item.status}`",
                ]
            )
            if item.page_relative_path:
                body_lines.append(f"- 物化页面：`{item.page_relative_path}`")
            if item.transcript_relative_path:
                body_lines.append(f"- 转录文件：`{item.transcript_relative_path}`")
            if item.note:
                body_lines.append(f"- 说明：{item.note}")
            body_lines.extend(["", "---", ""])

        page = await self._documents.create_page(
            title=manifest_title,
            body="\n".join(body_lines).rstrip(),
            section="inbox/imports/feishu/manifests",
            page_type="artifact_manifest",
            metadata={
                "source": source,
                "artifact_count": len(valid_items),
                "artifact_types": ",".join(sorted(counts.keys())),
                **(metadata or {}),
            },
        )
        return ArtifactBatchResult(
            status="materialized",
            note="已生成批量导入清单。",
            page_relative_path=page.relative_path,
            metadata={
                "page_title": page.title,
                "artifact_count": len(valid_items),
                "type_counts": counts,
            },
        )

    async def _materialize_audio(self, artifact: ArtifactRecord) -> ArtifactMaterializationResult:
        if self._audio is None:
            return ArtifactMaterializationResult(
                artifact=artifact,
                status="stored",
                note="音频服务未配置，仅保存了原始文件。",
            )
        result = await self._audio.transcribe_and_materialize(
            audio_path=artifact.absolute_path,
            target_section="meetings",
            title=self._title_from_filename(artifact.filename, prefix="会议录音"),
            metadata={
                "artifact_path": artifact.relative_path,
                "artifact_type": artifact.artifact_type,
                "source": artifact.source,
            },
        )
        return ArtifactMaterializationResult(
            artifact=artifact,
            status="materialized",
            note="已自动转录并生成会议记录页面。",
            page_relative_path=result.page.relative_path,
            transcript_relative_path=result.transcript_path,
            metadata={
                "page_title": result.page.title,
            },
        )

    async def _materialize_image(self, artifact: ArtifactRecord) -> ArtifactMaterializationResult:
        title = self._title_from_filename(artifact.filename, prefix="图片归档")
        ocr_text, ocr_engine = self._extract_image_text(artifact.absolute_path)
        body_lines = [
            f"# {title}",
            "",
            "## 资产信息",
            "",
            f"- 原始文件：`{artifact.relative_path}`",
            f"- MIME：`{artifact.mime_type}`",
            f"- 大小：`{artifact.size_bytes}` bytes",
            "",
        ]
        if self._editor is not None:
            body_lines.append(self._editor.render_image_block(artifact.relative_path))
        else:
            body_lines.extend(
                [
                    "## 图片引用",
                    "",
                    f"`{artifact.relative_path}`",
                ]
            )
        if ocr_text:
            body_lines.extend(
                [
                    "",
                    "## OCR 文本",
                    "",
                    ocr_text,
                ]
            )
            if self._editor is not None:
                body_lines.extend(["", "## 内容摘要", "", self._editor.render_summary_block(self._summarize_text(ocr_text))])
        page = await self._documents.create_page(
            title=title,
            body="\n".join(body_lines),
            section="pages/captures",
            page_type="image_capture",
            metadata={
                "artifact_path": artifact.relative_path,
                "artifact_type": artifact.artifact_type,
                "source": artifact.source,
                "ocr_engine": ocr_engine,
            },
        )
        return ArtifactMaterializationResult(
            artifact=artifact,
            status="materialized",
            note="已生成图片归档页面。" if not ocr_text else f"已生成图片归档页面，并提取 OCR 文本（{ocr_engine}）。",
            page_relative_path=page.relative_path,
            metadata={"page_title": page.title, "ocr_engine": ocr_engine, "ocr_text_present": bool(ocr_text)},
        )

    async def _materialize_file(self, artifact: ArtifactRecord) -> ArtifactMaterializationResult:
        suffix = Path(artifact.filename).suffix.lower()
        if suffix in _TEXT_EXTENSIONS:
            return await self._materialize_text_file(artifact, suffix=suffix)
        if suffix == ".pdf":
            return await self._materialize_pdf(artifact)
        return await self._materialize_binary_receipt(artifact)

    async def _materialize_text_file(self, artifact: ArtifactRecord, *, suffix: str) -> ArtifactMaterializationResult:
        text = artifact.absolute_path.read_text(encoding="utf-8", errors="replace")
        title = self._title_from_filename(artifact.filename, prefix="导入文件")
        if suffix == ".md":
            body = text
        else:
            lang = suffix.replace(".", "") or "text"
            body = "\n".join(
                [
                    f"# {title}",
                    "",
                    "## 原始资产",
                    "",
                    f"- 文件：`{artifact.relative_path}`",
                    "",
                    "## 内容",
                    "",
                    f"```{lang}",
                    text.strip(),
                    "```",
                ]
            )
        page = await self._documents.create_page(
            title=title,
            body=body,
            section="inbox/imports/feishu",
            page_type="imported_file",
            metadata={
                "artifact_path": artifact.relative_path,
                "artifact_type": artifact.artifact_type,
                "source": artifact.source,
                "original_filename": artifact.filename,
            },
        )
        return ArtifactMaterializationResult(
            artifact=artifact,
            status="materialized",
            note="已生成可检索的导入页面。",
            page_relative_path=page.relative_path,
            metadata={"page_title": page.title},
        )

    async def _materialize_pdf(self, artifact: ArtifactRecord) -> ArtifactMaterializationResult:
        extracted = ""
        if PdfReader is not None:
            reader = PdfReader(str(artifact.absolute_path))
            extracted = "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
        title = self._title_from_filename(artifact.filename, prefix="导入 PDF")
        body_lines = [
            f"# {title}",
            "",
            "## 原始资产",
            "",
            f"- 文件：`{artifact.relative_path}`",
            "",
        ]
        if extracted:
            body_lines.extend(
                [
                    "## 提取文本",
                    "",
                    extracted,
                ]
            )
            note = "已提取 PDF 文本并生成导入页面。"
        else:
            body_lines.extend(
                [
                    "## 说明",
                    "",
                    "当前环境未能提取 PDF 文本，但原始文件已经保存在 Vault 中。",
                ]
            )
            note = "已保存 PDF 原件，并生成导入记录页。"
        page = await self._documents.create_page(
            title=title,
            body="\n".join(body_lines),
            section="inbox/imports/feishu",
            page_type="imported_pdf",
            metadata={
                "artifact_path": artifact.relative_path,
                "artifact_type": artifact.artifact_type,
                "source": artifact.source,
                "original_filename": artifact.filename,
            },
        )
        return ArtifactMaterializationResult(
            artifact=artifact,
            status="materialized",
            note=note,
            page_relative_path=page.relative_path,
            metadata={"page_title": page.title},
        )

    async def _materialize_binary_receipt(self, artifact: ArtifactRecord) -> ArtifactMaterializationResult:
        title = self._title_from_filename(artifact.filename, prefix="导入附件")
        body = "\n".join(
            [
                f"# {title}",
                "",
                "## 原始资产",
                "",
                f"- 文件：`{artifact.relative_path}`",
                f"- MIME：`{artifact.mime_type}`",
                f"- 大小：`{artifact.size_bytes}` bytes",
                "",
                "## 说明",
                "",
                "当前类型尚未自动解析，但原始文件已纳入 Vault，可在后续任务中继续处理。",
            ]
        )
        page = await self._documents.create_page(
            title=title,
            body=body,
            section="inbox/imports/feishu",
            page_type="imported_attachment",
            metadata={
                "artifact_path": artifact.relative_path,
                "artifact_type": artifact.artifact_type,
                "source": artifact.source,
                "original_filename": artifact.filename,
            },
        )
        return ArtifactMaterializationResult(
            artifact=artifact,
            status="materialized",
            note="已保存原始文件，并生成导入记录页。",
            page_relative_path=page.relative_path,
            metadata={"page_title": page.title},
        )

    @staticmethod
    def _normalize_artifact_type(
        value: str,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        lowered = str(value or "file").strip().lower()
        if lowered in {"audio", "voice"}:
            return "audio"
        if lowered in {"image", "screenshot"}:
            return "image"
        mime = str(mime_type or "").strip().lower()
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("image/"):
            return "image"
        suffix = Path(str(filename or "")).suffix.lower()
        if suffix in _AUDIO_EXTENSIONS:
            return "audio"
        if suffix in _IMAGE_EXTENSIONS:
            return "image"
        if lowered in {"file", "media", "document"}:
            return "file"
        return "file"

    @staticmethod
    def _sanitize_filename(value: str) -> str:
        sanitized = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", value.strip(), flags=re.UNICODE)
        sanitized = re.sub(r"_+", "_", sanitized).strip("._")
        return sanitized[:80] or "artifact"

    def _normalize_filename(self, filename: str | None, *, kind: str) -> str:
        if filename:
            return filename
        extension = self._extension_for_mime(self._default_mime(kind), kind=kind)
        return f"{kind}{extension}"

    @staticmethod
    def _storage_prefix(kind: str) -> Path:
        if kind == "audio":
            return Path("_system/audio")
        if kind == "image":
            return Path("_system/artifacts/images")
        return Path("_system/artifacts/files")

    @staticmethod
    def _default_mime(kind: str) -> str:
        if kind == "audio":
            return "audio/mpeg"
        if kind == "image":
            return "image/png"
        return "application/octet-stream"

    def _extension_for_mime(self, mime_type: str, *, kind: str) -> str:
        if mime_type:
            guessed = mimetypes.guess_extension(mime_type, strict=False)
            if guessed:
                return guessed
        return {
            "audio": ".bin",
            "image": ".png",
        }.get(kind, ".bin")

    @staticmethod
    def _title_from_filename(filename: str, *, prefix: str) -> str:
        stem = Path(filename).stem.replace("_", " ").replace("-", " ").strip()
        return f"{prefix} {stem}".strip()

    def _extract_image_text(self, image_path: Path) -> tuple[str, str | None]:
        if self._image_text_extractor is not None:
            extracted = self._image_text_extractor(image_path)
            if isinstance(extracted, tuple):
                text, engine = extracted
                return self._clean_extracted_text(str(text or "")), str(engine or "custom")
            return self._clean_extracted_text(str(extracted or "")), "custom"

        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore

            text = pytesseract.image_to_string(Image.open(image_path), lang="chi_sim+eng")
            cleaned = self._clean_extracted_text(text)
            if cleaned:
                return cleaned, "pytesseract"
        except Exception:
            pass

        for lang in ("chi_sim+eng", "eng"):
            try:
                completed = subprocess.run(
                    ["tesseract", str(image_path), "stdout", "-l", lang],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            except Exception:
                continue
            if completed.returncode != 0:
                continue
            cleaned = self._clean_extracted_text(completed.stdout)
            if cleaned:
                return cleaned, f"tesseract:{lang}"

        return "", None

    @staticmethod
    def _clean_extracted_text(text: str) -> str:
        cleaned_lines = [" ".join(line.split()) for line in text.splitlines()]
        cleaned = "\n".join(line for line in cleaned_lines if line).strip()
        return cleaned[:12000]

    @staticmethod
    def _summarize_text(text: str, *, limit: int = 400) -> str:
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"
