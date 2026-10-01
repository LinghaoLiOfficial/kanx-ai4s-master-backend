from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.audit import record_audit
from ..auth.dependencies import AuthPrincipal, require_verified_user
from ..database.session import get_session
from ..email.service import EmailService
from ..email.settings import get_email_settings
from ..rbac.models import MembershipRole, Organization, OrganizationMembership, Role, RolePermission
from ..rbac.service import RoleService, slugify_organization
from ..users.models import User, UserStatus
from ..workspace_files.models import WorkspaceFile
from .models import GroupInvitation, Notification, utcnow

router = APIRouter(tags=["collaboration"])
INVITE_TTL = timedelta(days=7)


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class GroupPatch(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class RolePatch(BaseModel):
    role: str = Field(pattern="^(admin|member)$")


class InvitationCreate(BaseModel):
    user_id: str | None = None
    email: str | None = None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _metadata(group: Organization, role: str, member_count: int) -> dict[str, Any]:
    return {
        "id": group.id,
        "name": group.name,
        "slug": group.slug,
        "kind": group.kind,
        "role": role,
        "member_count": member_count,
        "created_at": group.created_at,
    }


async def _group(session: AsyncSession, group_id: str) -> Organization:
    group = await session.scalar(
        select(Organization).where(
            Organization.id == group_id,
            Organization.kind == "group",
            Organization.deleted_at.is_(None),
        )
    )
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


async def _role(session: AsyncSession, user_id: str, group_id: str) -> str:
    names = await RoleService().role_names(session, user_id, group_id)
    if not names:
        raise HTTPException(status_code=403, detail="Not a group member")
    return "owner" if "owner" in names else "admin" if "admin" in names else "member"


async def _require(
    session: AsyncSession, user: AuthPrincipal, group_id: str, allowed: set[str]
) -> tuple[Organization, str]:
    group = await _group(session, group_id)
    role = await _role(session, user.id, group_id)
    if role not in allowed:
        raise HTTPException(status_code=403, detail="Permission denied")
    return group, role


async def _role_id(session: AsyncSession, group_id: str, name: str) -> str:
    role = await session.scalar(
        select(Role).where(Role.organization_id == group_id, Role.name == name)
    )
    if role is None:
        raise HTTPException(status_code=500, detail="Group roles are not configured")
    return role.id


async def _notify(
    session: AsyncSession,
    recipient_id: str,
    *,
    type_: str,
    title: str,
    body: str,
    group_id: str | None = None,
    invitation_id: str | None = None,
    actor_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    session.add(
        Notification(
            recipient_id=recipient_id,
            actor_id=actor_id,
            organization_id=group_id,
            invitation_id=invitation_id,
            type=type_,
            title=title,
            body=body,
            payload=payload or {},
        )
    )


@router.get("/users/search")
async def search_users(
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str = Query(min_length=1, max_length=80),
) -> dict[str, Any]:
    del user
    query = q.strip()
    items = (
        await session.scalars(
            select(User)
            .where(
                User.status == UserStatus.ACTIVE,
                User.email_verified_at.is_not(None),
                or_(User.display_name.ilike(f"%{query}%"), User.email.ilike(f"%{query}%")),
            )
            .order_by(User.display_name)
            .limit(10)
        )
    ).all()
    return {
        "items": [
            {"id": item.id, "display_name": item.display_name, "email": item.email}
            for item in items
        ]
    }


@router.get("/groups")
async def list_groups(
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    groups = (
        await session.scalars(
            select(Organization)
            .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
            .where(
                OrganizationMembership.user_id == user.id,
                Organization.kind == "group",
                Organization.deleted_at.is_(None),
            )
            .order_by(Organization.name)
        )
    ).all()
    roles = RoleService()
    result = []
    for group in groups:
        count = int(
            await session.scalar(
                select(func.count())
                .select_from(OrganizationMembership)
                .where(OrganizationMembership.organization_id == group.id)
            )
            or 0
        )
        names = await roles.role_names(session, user.id, group.id)
        result.append(
            _metadata(
                group,
                "owner" if "owner" in names else "admin" if "admin" in names else "member",
                count,
            )
        )
    return {"items": result}


@router.post("/groups", status_code=201)
async def create_group(
    body: GroupCreate,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    name = body.name.strip()
    group = Organization(
        name=name, slug=f"{slugify_organization(name)}-{uuid4().hex[:8]}", kind="group"
    )
    session.add(group)
    await session.flush()
    membership = OrganizationMembership(organization_id=group.id, user_id=user.id)
    session.add(membership)
    roles: dict[str, Role] = {}
    for name in ("owner", "admin", "member"):
        role = Role(organization_id=group.id, name=name, builtin=True)
        session.add(role)
        roles[name] = role
    await session.flush()
    session.add_all(
        [
            RolePermission(role_id=roles["owner"].id, permission="*"),
            RolePermission(role_id=roles["admin"].id, permission="organization:read"),
            RolePermission(role_id=roles["admin"].id, permission="workspace_files:read"),
            RolePermission(role_id=roles["admin"].id, permission="workspace_files:write"),
            RolePermission(role_id=roles["admin"].id, permission="workspace_files:delete"),
            RolePermission(role_id=roles["member"].id, permission="organization:read"),
            RolePermission(role_id=roles["member"].id, permission="workspace_files:read"),
            RolePermission(role_id=roles["member"].id, permission="workspace_files:write"),
            RolePermission(role_id=roles["member"].id, permission="workspace_files:delete"),
            MembershipRole(membership_id=membership.id, role_id=roles["owner"].id),
        ]
    )
    await record_audit(session, "group.create", resource_type="group", resource_id=group.id)
    await session.commit()
    await session.refresh(group)
    return _metadata(group, "owner", 1)


@router.get("/groups/{group_id}")
async def get_group(
    group_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    group, role = await _require(session, user, group_id, {"owner", "admin", "member"})
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(OrganizationMembership)
            .where(OrganizationMembership.organization_id == group.id)
        )
        or 0
    )
    return _metadata(group, role, count)


@router.patch("/groups/{group_id}")
async def update_group(
    group_id: str,
    body: GroupPatch,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    group, _ = await _require(session, user, group_id, {"owner"})
    group.name = body.name.strip()
    group.slug = f"{slugify_organization(group.name)}-{group.id[:8]}"
    await session.commit()
    return _metadata(
        group,
        "owner",
        int(
            await session.scalar(
                select(func.count())
                .select_from(OrganizationMembership)
                .where(OrganizationMembership.organization_id == group.id)
            )
            or 0
        ),
    )


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    group, _ = await _require(session, user, group_id, {"owner"})
    member_ids = list(
        await session.scalars(
            select(OrganizationMembership.user_id).where(
                OrganizationMembership.organization_id == group_id
            )
        )
    )
    for member_id in member_ids:
        if member_id != user.id:
            await _notify(
                session,
                member_id,
                type_="group_disbanded",
                title=f"{group.name} 已解散",
                body="群组及其文件空间已关闭。",
                group_id=group_id,
                actor_id=user.id,
            )
    group.deleted_at = utcnow()
    await session.execute(
        update(WorkspaceFile)
        .where(WorkspaceFile.organization_id == group_id, WorkspaceFile.deleted_at.is_(None))
        .values(deleted_at=utcnow())
    )
    await session.execute(
        update(GroupInvitation)
        .where(GroupInvitation.organization_id == group_id, GroupInvitation.status == "pending")
        .values(status="revoked", responded_at=utcnow())
    )
    await session.execute(
        delete(OrganizationMembership).where(OrganizationMembership.organization_id == group_id)
    )
    await session.commit()


@router.get("/groups/{group_id}/members")
async def list_members(
    group_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _require(session, user, group_id, {"owner", "admin", "member"})
    rows = (
        await session.execute(
            select(OrganizationMembership, User, Role.name)
            .join(User, User.id == OrganizationMembership.user_id)
            .join(MembershipRole, MembershipRole.membership_id == OrganizationMembership.id)
            .join(Role, Role.id == MembershipRole.role_id)
            .where(OrganizationMembership.organization_id == group_id)
        )
    ).all()
    return {
        "items": [
            {
                "id": membership.user_id,
                "display_name": account.display_name,
                "email": account.email,
                "role": role,
            }
            for membership, account, role in rows
        ]
    }


@router.patch("/groups/{group_id}/members/{member_id}/role")
async def update_member_role(
    group_id: str,
    member_id: str,
    body: RolePatch,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    await _require(session, user, group_id, {"owner"})
    if member_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    membership = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == group_id,
            OrganizationMembership.user_id == member_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    await session.execute(
        delete(MembershipRole).where(MembershipRole.membership_id == membership.id)
    )
    session.add(
        MembershipRole(
            membership_id=membership.id, role_id=await _role_id(session, group_id, body.role)
        )
    )
    await session.commit()
    return {"role": body.role}


@router.delete("/groups/{group_id}/members/{member_id}", status_code=204)
async def remove_member(
    group_id: str,
    member_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    _, role = await _require(session, user, group_id, {"owner", "admin"})
    membership = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == group_id,
            OrganizationMembership.user_id == member_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    target_role = await _role(session, member_id, group_id)
    if (
        member_id == user.id
        or target_role == "owner"
        or (role == "admin" and target_role != "member")
    ):
        raise HTTPException(status_code=403, detail="Permission denied")
    await session.delete(membership)
    await session.commit()


@router.post("/groups/{group_id}/leave", status_code=204)
async def leave_group(
    group_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    _, role = await _require(session, user, group_id, {"owner", "admin", "member"})
    if role == "owner":
        raise HTTPException(status_code=400, detail="Owner must disband the group")
    membership = await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == group_id,
            OrganizationMembership.user_id == user.id,
        )
    )
    await session.delete(membership)
    await session.commit()


@router.post("/groups/{group_id}/invitations", status_code=201)
async def invite_user(
    group_id: str,
    body: InvitationCreate,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    group, _ = await _require(session, user, group_id, {"owner", "admin"})
    target = None
    if body.user_id:
        target = await session.get(User, body.user_id)
    elif body.email:
        target = await session.scalar(
            select(User).where(User.normalized_email == body.email.strip().casefold())
        )
    if target is None or target.email_verified_at is None or target.status != UserStatus.ACTIVE:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id or await session.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == group_id,
            OrganizationMembership.user_id == target.id,
        )
    ):
        raise HTTPException(status_code=409, detail="already_member")
    pending = await session.scalar(
        select(GroupInvitation).where(
            GroupInvitation.organization_id == group_id,
            GroupInvitation.invitee_id == target.id,
            GroupInvitation.status == "pending",
            GroupInvitation.expires_at > utcnow(),
        )
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="invitation_pending")
    raw = secrets.token_urlsafe(32)
    invitation = GroupInvitation(
        organization_id=group_id,
        inviter_id=user.id,
        invitee_id=target.id,
        kind="direct",
        token_digest=_digest(raw),
        expires_at=utcnow() + INVITE_TTL,
    )
    session.add(invitation)
    await session.flush()
    await _notify(
        session,
        target.id,
        type_="group_invite",
        title=f"邀请加入 {group.name}",
        body=f"{user.display_name} 邀请你加入群组。",
        group_id=group_id,
        invitation_id=invitation.id,
        actor_id=user.id,
        payload={"group_name": group.name},
    )
    base_url = get_email_settings().public_base_url.rstrip("/")
    await EmailService().enqueue(
        session,
        to=target.email,
        template="group_invite",
        context={
            "url": f"{base_url}/workspace/invitations/{raw}",
            "group_name": group.name,
            "inviter": user.display_name,
        },
        organization_id=group_id,
        idempotency_key=f"group-invite:{invitation.id}",
    )
    await session.commit()
    return {"id": invitation.id, "status": invitation.status, "expires_at": invitation.expires_at}


@router.post("/groups/{group_id}/invite-links", status_code=201)
async def create_invite_link(
    group_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    group, _ = await _require(session, user, group_id, {"owner", "admin"})
    raw = secrets.token_urlsafe(32)
    invitation = GroupInvitation(
        organization_id=group_id,
        inviter_id=user.id,
        kind="share",
        token_digest=_digest(raw),
        expires_at=utcnow() + INVITE_TTL,
    )
    session.add(invitation)
    await session.commit()
    return {"url": f"/workspace/invitations/{raw}", "expires_at": invitation.expires_at}


async def _invitation(
    session: AsyncSession, token: str, user_id: str | None = None
) -> tuple[GroupInvitation, Organization]:
    invitation = await session.scalar(
        select(GroupInvitation).where(GroupInvitation.token_digest == _digest(token))
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    group = await _group(session, invitation.organization_id)
    if invitation.status == "pending" and invitation.expires_at <= utcnow():
        invitation.status = "expired"
        await session.commit()
    if invitation.invitee_id and user_id and invitation.invitee_id != user_id:
        raise HTTPException(status_code=403, detail="Invitation is for another user")
    return invitation, group


@router.get("/invitations/{token}")
async def get_invitation(
    token: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    invitation, group = await _invitation(session, token, user.id)
    inviter = await session.get(User, invitation.inviter_id)
    return {
        "id": invitation.id,
        "status": invitation.status,
        "kind": invitation.kind,
        "group_id": group.id,
        "group_name": group.name,
        "inviter_name": inviter.display_name if inviter else "",
        "expires_at": invitation.expires_at,
    }


@router.get("/invitations/by-id/{invitation_id}")
async def get_direct_invitation(
    invitation_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    invitation = await session.get(GroupInvitation, invitation_id)
    if invitation is None or invitation.invitee_id != user.id:
        raise HTTPException(status_code=404, detail="Invitation not found")
    group = await _group(session, invitation.organization_id)
    if invitation.status == "pending" and invitation.expires_at <= utcnow():
        invitation.status = "expired"
        await session.commit()
    inviter = await session.get(User, invitation.inviter_id)
    return {
        "id": invitation.id,
        "status": invitation.status,
        "kind": invitation.kind,
        "group_id": group.id,
        "group_name": group.name,
        "inviter_name": inviter.display_name if inviter else "",
        "expires_at": invitation.expires_at,
    }


@router.post("/invitations/{token}/{action}")
async def respond_invitation(
    token: str,
    action: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    if action not in {"accept", "decline"}:
        raise HTTPException(status_code=404, detail="Unknown invitation action")
    invitation, group = await _invitation(session, token, user.id)
    return await _respond(session, invitation, group, action, user)


async def _respond(
    session: AsyncSession,
    invitation: GroupInvitation,
    group: Organization,
    action: str,
    user: AuthPrincipal,
) -> dict[str, str]:
    if invitation.status != "pending":
        return {"status": invitation.status}
    if invitation.invitee_id is None:
        invitation.invitee_id = user.id
    if action == "decline":
        invitation.status = "declined"
    else:
        existing = await session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == group.id,
                OrganizationMembership.user_id == user.id,
            )
        )
        if existing is None:
            membership = OrganizationMembership(organization_id=group.id, user_id=user.id)
            session.add(membership)
            await session.flush()
            session.add(
                MembershipRole(
                    membership_id=membership.id, role_id=await _role_id(session, group.id, "member")
                )
            )
        invitation.status = "accepted"
        await _notify(
            session,
            invitation.inviter_id,
            type_="group_joined",
            title=f"{user.display_name} 已加入 {group.name}",
            body="群组邀请已接受。",
            group_id=group.id,
            invitation_id=invitation.id,
            actor_id=user.id,
        )
    invitation.responded_at = utcnow()
    await session.commit()
    return {"status": invitation.status}


@router.post("/invitations/by-id/{invitation_id}/{action}")
async def respond_direct_invitation(
    invitation_id: str,
    action: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    if action not in {"accept", "decline"}:
        raise HTTPException(status_code=404, detail="Unknown invitation action")
    invitation = await session.get(GroupInvitation, invitation_id)
    if invitation is None or invitation.invitee_id != user.id:
        raise HTTPException(status_code=404, detail="Invitation not found")
    group = await _group(session, invitation.organization_id)
    if invitation.status == "pending" and invitation.expires_at <= utcnow():
        invitation.status = "expired"
        await session.commit()
    return await _respond(session, invitation, group, action, user)


@router.get("/notifications")
async def list_notifications(
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    unread_only: bool = False,
) -> dict[str, Any]:
    conditions = [Notification.recipient_id == user.id, Notification.deleted_at.is_(None)]
    if unread_only:
        conditions.append(Notification.read_at.is_(None))
    total = int(
        await session.scalar(select(func.count()).select_from(Notification).where(*conditions)) or 0
    )
    unread = int(
        await session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.recipient_id == user.id,
                Notification.deleted_at.is_(None),
                Notification.read_at.is_(None),
            )
        )
        or 0
    )
    items = (
        await session.scalars(
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "type": item.type,
                "title": item.title,
                "body": item.body,
                "payload": item.payload,
                "created_at": item.created_at,
                "read_at": item.read_at,
                "invitation_id": item.invitation_id,
            }
            for item in items
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "unread_count": unread,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.patch("/notifications/{notification_id}/read", status_code=204)
async def read_notification(
    notification_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    item = await session.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_id == user.id,
            Notification.deleted_at.is_(None),
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    item.read_at = utcnow()
    await session.commit()


@router.post("/notifications/read-all", status_code=204)
async def read_all_notifications(
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await session.execute(
        update(Notification)
        .where(
            Notification.recipient_id == user.id,
            Notification.deleted_at.is_(None),
            Notification.read_at.is_(None),
        )
        .values(read_at=utcnow())
    )
    await session.commit()


@router.delete("/notifications/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: str,
    user: Annotated[AuthPrincipal, Depends(require_verified_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    item = await session.scalar(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.recipient_id == user.id,
            Notification.deleted_at.is_(None),
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    item.deleted_at = utcnow()
    await session.commit()
