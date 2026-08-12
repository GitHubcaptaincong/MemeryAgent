from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from memory_agent.config import Settings
from memory_agent.models import AgentProfile, User


def ensure_local_identity(session: Session, settings: Settings) -> tuple[User, AgentProfile]:
    user = session.get(User, settings.local_user_id)
    if user is None:
        user = User(id=settings.local_user_id, email=settings.local_user_email)
        session.add(user)
        session.flush()

    profile = session.scalar(
        select(AgentProfile).where(
            AgentProfile.user_id == user.id,
            AgentProfile.name == "default",
        )
    )
    if profile is None:
        profile = AgentProfile(
            user_id=user.id,
            name="default",
            core_profile_summary="尚未形成已确认的长期用户画像。",
            config_json={"memory_write_requires_approval": True},
        )
        session.add(profile)
    session.commit()
    return user, profile
