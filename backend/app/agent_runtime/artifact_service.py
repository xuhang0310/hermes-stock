from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    AgentArtifact,
    AgentArtifactVersion,
    AiChatSession,
    DigitalHumanProfile,
    WorkspaceDocument,
)
from backend.app.core.settings import settings


def _workspace_doc_key(user_id: int, path: str) -> str:
    return f"users/{user_id}/{path.lstrip('/')}"


def _artifact_dict(row: AgentArtifact) -> dict[str, Any]:
    return {
        "artifact_id": row.artifact_id,
        "path": row.workspace_path,
        "name": Path(row.workspace_path).name,
        "title": row.title,
        "mime_type": row.mime_type,
        "active_version": row.active_version,
        "status": row.status,
        "session_id": row.session_id,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class ArtifactService:
    """Versioned workspace artifacts with stable identity and lineage."""

    def __init__(self, session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _resolve(user_id: int, rel_path: str) -> tuple[Path, Path]:
        from backend.app.skills.agent_workspace import _ensure_root, _resolve_in_jail, workspace_user_scope

        with workspace_user_scope(user_id):
            root = _ensure_root()
            target = _resolve_in_jail(rel_path)
        return root, target

    @staticmethod
    def _validate(path: Path) -> tuple[str, dict[str, Any]]:
        suffix = path.suffix.lower()
        details: dict[str, Any] = {"extension": suffix, "size_bytes": path.stat().st_size}
        try:
            if suffix == ".pdf":
                from pypdf import PdfReader

                details["pages"] = len(PdfReader(str(path)).pages)
            elif suffix == ".docx":
                from docx import Document

                document = Document(str(path))
                details["paragraphs"] = len(document.paragraphs)
            elif suffix == ".xlsx":
                from openpyxl import load_workbook

                workbook = load_workbook(str(path), read_only=True, data_only=False)
                details["sheets"] = list(workbook.sheetnames)
                workbook.close()
            elif suffix == ".pptx":
                from pptx import Presentation

                details["slides"] = len(Presentation(str(path)).slides)
            elif suffix in {".docx", ".xlsx", ".pptx"}:
                with zipfile.ZipFile(path) as archive:
                    bad = archive.testzip()
                    if bad:
                        raise ValueError(f"corrupt archive member: {bad}")
            elif suffix in {".md", ".txt", ".csv", ".json", ".html", ".svg"}:
                path.read_text(encoding="utf-8")
            elif suffix in {".png", ".jpg", ".jpeg"}:
                header = path.read_bytes()[:16]
                if suffix == ".png" and not header.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("invalid PNG signature")
                if suffix in {".jpg", ".jpeg"} and not header.startswith(b"\xff\xd8"):
                    raise ValueError("invalid JPEG signature")
            return "valid", details
        except Exception as exc:  # noqa: BLE001
            details["error"] = str(exc)[:500]
            return "invalid", details

    def register_file(
        self,
        *,
        user_id: int,
        workspace_path: str,
        session_id: int | None,
        run_id: str | None,
        tool_execution_id: str | None = None,
        title: str | None = None,
        created_by: str | None = None,
        parent_version: int | None = None,
    ) -> dict[str, Any]:
        root, target = self._resolve(user_id, workspace_path)
        if not target.exists() or not target.is_file() or target.is_symlink():
            raise FileNotFoundError(f"Workspace artifact does not exist: {workspace_path}")
        rel_path = target.relative_to(root).as_posix()
        if rel_path == ".hermes" or rel_path.startswith(".hermes/"):
            raise ValueError("Internal artifact snapshots cannot be registered as user artifacts.")
        raw = target.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        validation_status, validation = self._validate(target)

        with self.session_factory() as db:
            artifact = db.scalar(
                select(AgentArtifact)
                .where(
                    AgentArtifact.user_id == user_id,
                    AgentArtifact.workspace_path == rel_path,
                )
                .with_for_update()
            )
            if artifact is None:
                digital_human_code = None
                if session_id:
                    chat_session = db.get(AiChatSession, session_id)
                    if chat_session and chat_session.user_id == user_id and chat_session.digital_human_id:
                        profile = db.get(DigitalHumanProfile, chat_session.digital_human_id)
                        digital_human_code = profile.code if profile else None
                artifact = AgentArtifact(
                    artifact_id=uuid.uuid4().hex,
                    user_id=user_id,
                    session_id=session_id,
                    digital_human_code=digital_human_code,
                    workspace_path=rel_path,
                    title=(title or target.name)[:500],
                    mime_type=mime_type,
                    status="active",
                    active_version=0,
                    created_by_run_id=run_id,
                )
                db.add(artifact)
                try:
                    db.flush()
                except IntegrityError:
                    db.rollback()
                    # A concurrent writer created the stable artifact identity.
                    # Retry once through the existing-row path, which locks it
                    # before allocating the next version.
                    return self.register_file(
                        user_id=user_id,
                        workspace_path=workspace_path,
                        session_id=session_id,
                        run_id=run_id,
                        tool_execution_id=tool_execution_id,
                        title=title,
                        created_by=created_by,
                        parent_version=parent_version,
                    )
            else:
                active = db.scalar(
                    select(AgentArtifactVersion).where(
                        AgentArtifactVersion.artifact_id == artifact.artifact_id,
                        AgentArtifactVersion.version == artifact.active_version,
                    )
                )
                if active is not None and active.sha256 == digest:
                    result = _artifact_dict(artifact)
                    result.update(
                        {
                            "version": active.version,
                            "sha256": active.sha256,
                            "bytes": active.size_bytes,
                            "validation_status": active.validation_status,
                            "unchanged": True,
                        }
                    )
                    return result

            version = int(artifact.active_version or 0) + 1
            version_rel = (
                Path(".hermes")
                / "versions"
                / artifact.artifact_id
                / str(version)
                / target.name
            ).as_posix()
            version_path = (root / version_rel).resolve()
            version_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, version_path)
            db.add(
                AgentArtifactVersion(
                    artifact_id=artifact.artifact_id,
                    version=version,
                    parent_version=parent_version if parent_version is not None else (version - 1 or None),
                    run_id=run_id,
                    tool_execution_id=tool_execution_id,
                    storage_path=version_rel,
                    sha256=digest,
                    size_bytes=len(raw),
                    mime_type=mime_type,
                    validation_status=validation_status,
                    validation_json=json.dumps(validation, ensure_ascii=False, default=str),
                )
            )
            artifact.active_version = version
            artifact.session_id = session_id or artifact.session_id
            artifact.title = (title or artifact.title or target.name)[:500]
            artifact.mime_type = mime_type
            artifact.status = "active"

            document_key = _workspace_doc_key(user_id, rel_path)
            document = db.scalar(
                select(WorkspaceDocument).where(
                    WorkspaceDocument.user_id == user_id,
                    WorkspaceDocument.path == document_key,
                )
            )
            if document is None:
                db.add(
                    WorkspaceDocument(
                        path=document_key,
                        user_id=user_id,
                        title=(title or target.name)[:500],
                        session_id=session_id,
                        digital_human_code=artifact.digital_human_code,
                        created_by=(created_by or "agent")[:80],
                    )
                )
            else:
                document.title = (title or document.title or target.name)[:500]
                document.session_id = session_id or document.session_id
                document.digital_human_code = artifact.digital_human_code or document.digital_human_code
            db.commit()
            db.refresh(artifact)
            result = _artifact_dict(artifact)
            result.update(
                {
                    "version": version,
                    "sha256": digest,
                    "bytes": len(raw),
                    "validation_status": validation_status,
                    "validation": validation,
                    "unchanged": False,
                }
            )
            return result

    def register_result(
        self,
        *,
        result: dict[str, Any],
        user_id: int,
        session_id: int | None,
        run_id: str | None,
        tool_execution_id: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        enriched: list[dict[str, Any]] = []
        for item in result.get("artifacts") or []:
            if isinstance(item, str):
                path = item
                source: dict[str, Any] = {"path": item}
            elif isinstance(item, dict):
                path = str(item.get("path") or "")
                source = dict(item)
            else:
                continue
            if not path:
                continue
            try:
                registered = self.register_file(
                    user_id=user_id,
                    workspace_path=path,
                    session_id=session_id,
                    run_id=run_id,
                    tool_execution_id=tool_execution_id,
                    title=str(source.get("name") or "") or None,
                    created_by=created_by,
                )
                enriched.append({**source, **registered})
            except (FileNotFoundError, ValueError):
                enriched.append(source)
        if enriched:
            result = dict(result)
            result["artifacts"] = enriched
        return result

    def list_versions(self, artifact_id: str, *, user_id: int) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            artifact = db.scalar(
                select(AgentArtifact).where(
                    AgentArtifact.artifact_id == artifact_id,
                    AgentArtifact.user_id == user_id,
                )
            )
            if artifact is None:
                return []
            rows = db.scalars(
                select(AgentArtifactVersion)
                .where(AgentArtifactVersion.artifact_id == artifact_id)
                .order_by(AgentArtifactVersion.version.desc())
                .limit(max(1, settings.artifact_version_limit))
            ).all()
            return [
                {
                    "artifact_id": artifact_id,
                    "version": row.version,
                    "parent_version": row.parent_version,
                    "sha256": row.sha256,
                    "bytes": row.size_bytes,
                    "mime_type": row.mime_type,
                    "validation_status": row.validation_status,
                    "active": row.version == artifact.active_version,
                    "run_id": row.run_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    def restore(self, artifact_id: str, version: int, *, user_id: int, run_id: str | None = None) -> dict[str, Any]:
        with self.session_factory() as db:
            artifact = db.scalar(
                select(AgentArtifact).where(
                    AgentArtifact.artifact_id == artifact_id,
                    AgentArtifact.user_id == user_id,
                )
            )
            snapshot = db.scalar(
                select(AgentArtifactVersion).where(
                    AgentArtifactVersion.artifact_id == artifact_id,
                    AgentArtifactVersion.version == version,
                )
            )
            if artifact is None or snapshot is None:
                raise FileNotFoundError("Artifact version does not exist.")
            workspace_path = artifact.workspace_path
            session_id = artifact.session_id
            storage_path = snapshot.storage_path
        root, target = self._resolve(user_id, workspace_path)
        snapshot_path = (root / storage_path).resolve()
        snapshot_path.relative_to(root)
        if not snapshot_path.is_file() or snapshot_path.is_symlink():
            raise FileNotFoundError("Artifact snapshot file is missing.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_path, target)
        return self.register_file(
            user_id=user_id,
            workspace_path=workspace_path,
            session_id=session_id,
            run_id=run_id,
            title=artifact.title,
            parent_version=version,
            created_by="artifact_restore",
        )

    def active_context(self, *, user_id: int, session_id: int | None, limit: int = 8) -> str:
        if session_id is None:
            return ""
        with self.session_factory() as db:
            rows = db.scalars(
                select(AgentArtifact)
                .where(
                    AgentArtifact.user_id == user_id,
                    AgentArtifact.session_id == session_id,
                    AgentArtifact.status == "active",
                )
                .order_by(AgentArtifact.updated_at.desc())
                .limit(max(1, min(limit, 20)))
            ).all()
        if not rows:
            return ""
        lines = [
            "Active workspace artifacts from this conversation. Reuse and edit these paths when the user refers to an existing file:",
        ]
        for row in rows:
            lines.append(
                f"- artifact_id={row.artifact_id}; path={row.workspace_path}; version={row.active_version}; title={row.title or Path(row.workspace_path).name}"
            )
        return "\n".join(lines)


artifact_service = ArtifactService()
