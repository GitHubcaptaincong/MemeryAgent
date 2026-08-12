from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from memory_agent.bootstrap import ensure_default_profile, ensure_local_identity
from memory_agent.config import Settings, get_settings
from memory_agent.database import get_session
from memory_agent.models import AgentProfile, User, WechatIdentity, utc_now


@dataclass(frozen=True)
class IdentityContext:
    user: User
    profile: AgentProfile
    settings: Settings


def _required_header(headers: Mapping[str, str], name: str, max_length: int) -> str:
    value = (headers.get(name) or "").strip()
    if not value or len(value) > max_length:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing or invalid {name} header",
        )
    return value


def _identity_for_openid(
    session: Session,
    *,
    app_id: str,
    openid: str,
) -> WechatIdentity | None:
    return session.scalar(
        select(WechatIdentity).where(
            WechatIdentity.app_id == app_id,
            WechatIdentity.openid == openid,
        )
    )


def _should_claim_local_user(
    session: Session,
    *,
    settings: Settings,
    openid: str,
) -> bool:
    configured_owner = settings.wechat_legacy_owner_openid_value
    if configured_owner is not None:
        return secrets.compare_digest(configured_owner, openid)
    if not settings.wechat_claim_local_user:
        return False
    existing_identity = session.scalar(
        select(WechatIdentity.id).where(WechatIdentity.user_id == settings.local_user_id)
    )
    return existing_identity is None


def _new_wechat_user(session: Session) -> User:
    user_id = uuid.uuid4()
    user = User(
        id=user_id,
        email=f"wechat+{user_id.hex}@memory-agent.invalid",
    )
    session.add(user)
    session.flush()
    return user


def resolve_identity(
    session: Session,
    settings: Settings,
    headers: Mapping[str, str],
) -> IdentityContext:
    if settings.auth_mode == "local":
        user, profile = ensure_local_identity(session, settings)
        return IdentityContext(user=user, profile=profile, settings=settings)

    openid = _required_header(headers, "x-wx-openid", 128)
    app_id = _required_header(headers, "x-wx-appid", 64)
    if settings.wechat_app_id and not secrets.compare_digest(settings.wechat_app_id, app_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="WeChat AppID is not allowed",
        )
    unionid = (headers.get("x-wx-unionid") or "").strip() or None
    if unionid is not None and len(unionid) > 128:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid x-wx-unionid header",
        )

    identity = _identity_for_openid(session, app_id=app_id, openid=openid)
    if identity is not None:
        user = identity.user
        if user.status != "active":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user is inactive")
        identity.unionid = unionid or identity.unionid
        identity.last_seen_at = utc_now()
        profile = ensure_default_profile(session, user)
        session.commit()
        return IdentityContext(user=user, profile=profile, settings=settings)

    if _should_claim_local_user(session, settings=settings, openid=openid):
        user = session.scalar(
            select(User)
            .where(User.id == settings.local_user_id)
            .with_for_update()
        )
        if user is None:
            user, _profile = ensure_local_identity(session, settings)
        bound_identity = session.scalar(
            select(WechatIdentity).where(WechatIdentity.user_id == settings.local_user_id)
        )
        if bound_identity is not None:
            if settings.wechat_legacy_owner_openid_value is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="legacy user has already been claimed",
                )
            user = _new_wechat_user(session)
    else:
        user = _new_wechat_user(session)

    identity = WechatIdentity(
        user_id=user.id,
        app_id=app_id,
        openid=openid,
        unionid=unionid,
        last_seen_at=utc_now(),
    )
    session.add(identity)
    profile = ensure_default_profile(session, user)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        identity = _identity_for_openid(session, app_id=app_id, openid=openid)
        if identity is None:
            raise
        user = identity.user
        profile = ensure_default_profile(session, user)
        session.commit()
    return IdentityContext(user=user, profile=profile, settings=settings)


def get_current_identity(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> IdentityContext:
    return resolve_identity(session, settings, request.headers)
