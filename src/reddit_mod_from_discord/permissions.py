from __future__ import annotations

import discord


def is_allowed_moderator(member: discord.Member | None, allowed_role_ids: set[int]) -> bool:
    if member is None:
        return False
    if member.guild_permissions.administrator:
        return True
    if any(role.id in allowed_role_ids for role in member.roles):
        return True
    if not allowed_role_ids and (
        member.guild_permissions.manage_messages
        or member.guild_permissions.moderate_members
        or member.guild_permissions.manage_guild
    ):
        return True
    return False
