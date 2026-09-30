from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.audit import record_audit
from ..auth.dependencies import AuthPrincipal, require_verified_user
from ..database.session import get_session
from ..rbac.service import AuthorizationError, RoleService
from .models import WorkspaceFile

router = APIRouter(prefix="/organizations/{organization_id}/workspace-files", tags=["workspace-files"])


class FileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(pattern="^(mindmap|markdown)$")


class FilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    content: dict[str, Any] | None = None


def _default_mindmap(name: str) -> dict[str, Any]:
    return {"version": 2, "tree": {"rootId": "root", "nodes": {"root": {"id": "root", "parentId": None, "children": [], "kind": "text", "text": name, "collapsed": False}}}}


async def _permission(session: AsyncSession, user: AuthPrincipal, organization_id: str, permission: str) -> None:
    try:
        await RoleService().require(session, user.id, organization_id, permission)
    except AuthorizationError as error:
        raise HTTPException(status_code=403, detail="Permission denied") from error


async def _file(session: AsyncSession, user: AuthPrincipal, organization_id: str, file_id: str, permission: str) -> WorkspaceFile:
    await _permission(session, user, organization_id, permission)
    item = await session.scalar(select(WorkspaceFile).where(WorkspaceFile.id == file_id, WorkspaceFile.organization_id == organization_id, WorkspaceFile.deleted_at.is_(None)))
    if item is None:
        raise HTTPException(status_code=404, detail="File not found")
    return item


def _metadata(item: WorkspaceFile, include_content: bool = False) -> dict[str, Any]:
    result = {"id": item.id, "name": item.name, "type": item.file_type, "created_at": item.created_at, "updated_at": item.updated_at}
    if include_content:
        result["content"] = item.content
    return result


@router.get("")
async def list_files(organization_id: str, user: Annotated[AuthPrincipal, Depends(require_verified_user)], session: Annotated[AsyncSession, Depends(get_session)], page: int = Query(1, ge=1), page_size: int = Query(12, ge=1, le=100), query: str = Query("", max_length=255), type: str | None = Query(None, pattern="^(mindmap|markdown)$")) -> dict[str, Any]:
    await _permission(session, user, organization_id, "workspace_files:read")
    conditions = [WorkspaceFile.organization_id == organization_id, WorkspaceFile.deleted_at.is_(None)]
    if query:
        conditions.append(WorkspaceFile.name.ilike(f"%{query}%"))
    if type:
        conditions.append(WorkspaceFile.file_type == type)
    total = int(await session.scalar(select(func.count()).select_from(WorkspaceFile).where(*conditions)) or 0)
    items = (await session.scalars(select(WorkspaceFile).where(*conditions).order_by(WorkspaceFile.updated_at.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [_metadata(item) for item in items], "page": page, "page_size": page_size, "total": total, "total_pages": max(1, (total + page_size - 1) // page_size)}


@router.post("", status_code=201)
async def create_file(organization_id: str, body: FileCreate, user: Annotated[AuthPrincipal, Depends(require_verified_user)], session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    await _permission(session, user, organization_id, "workspace_files:write")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="File name is required")
    item = WorkspaceFile(organization_id=organization_id, created_by=user.id, name=name, file_type=body.type, content=_default_mindmap(name) if body.type == "mindmap" else None)
    session.add(item)
    await record_audit(session, "workspace_file.create", resource_type="workspace_file", resource_id=item.id, details={"name": item.name, "type": item.file_type})
    await session.commit()
    await session.refresh(item)
    return _metadata(item, True)


@router.get("/{file_id}")
async def get_file(organization_id: str, file_id: str, user: Annotated[AuthPrincipal, Depends(require_verified_user)], session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    return _metadata(await _file(session, user, organization_id, file_id, "workspace_files:read"), True)


@router.patch("/{file_id}")
async def update_file(organization_id: str, file_id: str, body: FilePatch, user: Annotated[AuthPrincipal, Depends(require_verified_user)], session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    item = await _file(session, user, organization_id, file_id, "workspace_files:write")
    if body.name is not None:
        item.name = body.name.strip()
        if not item.name:
            raise HTTPException(status_code=422, detail="File name is required")
    if body.content is not None:
        if item.file_type != "mindmap" or body.content.get("version") != 2 or not isinstance(body.content.get("tree"), dict):
            raise HTTPException(status_code=422, detail="Invalid mindmap document")
        item.content = body.content
    item.updated_at = datetime.now(UTC)
    await record_audit(session, "workspace_file.update", resource_type="workspace_file", resource_id=item.id)
    await session.commit()
    await session.refresh(item)
    return _metadata(item, True)


@router.delete("/{file_id}", status_code=204)
async def delete_file(organization_id: str, file_id: str, user: Annotated[AuthPrincipal, Depends(require_verified_user)], session: Annotated[AsyncSession, Depends(get_session)]) -> None:
    item = await _file(session, user, organization_id, file_id, "workspace_files:delete")
    item.deleted_at = datetime.now(UTC)
    await record_audit(session, "workspace_file.delete", resource_type="workspace_file", resource_id=item.id)
    await session.commit()
