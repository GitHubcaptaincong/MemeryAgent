from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_agent.config import Settings
from memory_agent.models import AgentProfile, User


DEFAULT_PROFILE_SUMMARY = "尚未形成已确认的长期用户画像。"


def _new_default_profile(user_id) -> AgentProfile:
    return AgentProfile(
        user_id=user_id,
        name="default",
        core_profile_summary=DEFAULT_PROFILE_SUMMARY,
        config_json={"memory_write_requires_approval": True},
    )


def ensure_default_profile(session: Session, user: User) -> AgentProfile:
    profile = session.scalar(
        select(AgentProfile).where(
            AgentProfile.user_id == user.id,
            AgentProfile.name == "default",
        )
    )
    if profile is None:
        profile = _new_default_profile(user.id)
        session.add(profile)
        session.flush()
    return profile


def ensure_local_identity(session: Session, settings: Settings) -> tuple[User, AgentProfile]:
    user = session.get(User, settings.local_user_id)
    profile = (
        session.scalar(
            select(AgentProfile).where(
                AgentProfile.user_id == settings.local_user_id,
                AgentProfile.name == "default",
            )
        )
        if user is not None
        else None
    )
    if user is not None and profile is not None:
        # Hot path: identity already provisioned; stay read-only instead of
        # issuing an empty write transaction on every request.
        return user, profile
    if user is None:
        user = User(id=settings.local_user_id, email=settings.local_user_email)
        session.add(user)
        session.flush()
    if profile is None:
        profile = _new_default_profile(user.id)
        session.add(profile)
        session.flush()
    session.commit()
    return user, profile
