from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ThingKind = Literal["submission", "comment"]


@dataclass(frozen=True)
class ReportedItem:
    fullname: str
    kind: ThingKind
    subreddit: str
    author: str
    permalink: str
    link_url: str | None
    media_url: str | None
    thumbnail_url: str | None
    title: str
    snippet: str
    num_reports: int
    created_utc: float
    num_comments: int | None
    locked: bool
    reports_ignored: bool
    removed: bool
    approved: bool
    user_reports: list[str] = field(default_factory=list)
    mod_reports: list[str] = field(default_factory=list)


@dataclass
class ReportViewPayload:
    fullname: str
    kind: ThingKind
    subreddit: str
    author: str
    permalink: str
    link_url: str | None
    media_url: str | None
    thumbnail_url: str | None
    title: str
    snippet: str
    num_reports: int
    created_utc: float
    num_comments: int | None
    locked: bool
    reports_ignored: bool
    removed: bool
    approved: bool
    user_reports: list[str]
    mod_reports: list[str]
    handled: bool = False
    action_log: list[str] = field(default_factory=list)
    view_version: int = 1
    setup_id: str | None = None

    @classmethod
    def from_reported_item(
        cls, item: ReportedItem, *, setup_id: str | None = None
    ) -> "ReportViewPayload":
        return cls(
            fullname=item.fullname,
            kind=item.kind,
            subreddit=item.subreddit,
            author=item.author,
            permalink=item.permalink,
            link_url=item.link_url,
            media_url=item.media_url,
            thumbnail_url=item.thumbnail_url,
            title=item.title,
            snippet=item.snippet,
            num_reports=item.num_reports,
            created_utc=item.created_utc,
            num_comments=item.num_comments,
            locked=item.locked,
            reports_ignored=item.reports_ignored,
            removed=item.removed,
            approved=item.approved,
            user_reports=list(item.user_reports),
            mod_reports=list(item.mod_reports),
            setup_id=setup_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_version": self.view_version,
            "fullname": self.fullname,
            "kind": self.kind,
            "subreddit": self.subreddit,
            "author": self.author,
            "permalink": self.permalink,
            "link_url": self.link_url,
            "media_url": self.media_url,
            "thumbnail_url": self.thumbnail_url,
            "title": self.title,
            "snippet": self.snippet,
            "num_reports": self.num_reports,
            "created_utc": self.created_utc,
            "num_comments": self.num_comments,
            "locked": self.locked,
            "reports_ignored": self.reports_ignored,
            "removed": self.removed,
            "approved": self.approved,
            "user_reports": self.user_reports,
            "mod_reports": self.mod_reports,
            "handled": self.handled,
            "action_log": self.action_log,
            "setup_id": self.setup_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReportViewPayload":
        kind = payload.get("kind")
        if kind not in {"submission", "comment"}:
            kind = "submission"
        user_reports = payload.get("user_reports")
        if not isinstance(user_reports, list):
            user_reports = []
        mod_reports = payload.get("mod_reports")
        if not isinstance(mod_reports, list):
            mod_reports = []
        action_log = payload.get("action_log")
        if not isinstance(action_log, list):
            action_log = []
        setup_id = payload.get("setup_id")
        if setup_id is not None and not isinstance(setup_id, str):
            setup_id = str(setup_id)

        link_url = payload.get("link_url")
        if not isinstance(link_url, str) or not link_url.strip():
            link_url = None
        media_url = payload.get("media_url")
        if not isinstance(media_url, str) or not media_url.strip():
            media_url = None
        thumbnail_url = payload.get("thumbnail_url")
        if not isinstance(thumbnail_url, str) or not thumbnail_url.strip():
            thumbnail_url = None

        return cls(
            view_version=int(payload.get("view_version", 1)),
            fullname=str(payload.get("fullname", "")),
            kind=kind,
            subreddit=str(payload.get("subreddit", "")),
            author=str(payload.get("author", "[deleted]")),
            permalink=str(payload.get("permalink", "")),
            link_url=link_url,
            media_url=media_url,
            thumbnail_url=thumbnail_url,
            title=str(payload.get("title", "")),
            snippet=str(payload.get("snippet", "")),
            num_reports=int(payload.get("num_reports", 0)),
            created_utc=float(payload.get("created_utc", 0.0)),
            num_comments=payload.get("num_comments") if payload.get("num_comments") is not None else None,
            locked=bool(payload.get("locked", False)),
            reports_ignored=bool(payload.get("reports_ignored", False)),
            removed=bool(payload.get("removed", False)),
            approved=bool(payload.get("approved", False)),
            user_reports=[str(value) for value in user_reports],
            mod_reports=[str(value) for value in mod_reports],
            handled=bool(payload.get("handled", False)),
            action_log=[str(value) for value in action_log],
            setup_id=setup_id,
        )
