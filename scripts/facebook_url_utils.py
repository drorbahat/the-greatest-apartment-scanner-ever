#!/usr/bin/env python3
"""Facebook URL normalization helpers for group posts.

Goal: never present a group/profile URL as a post link, and prefer a
permalink.php URL that behaves better across desktop and iPhone.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

POST_RE = re.compile(r"facebook\.com/groups/([^/?#]+)/posts/([0-9]+)")
USER_RE = re.compile(r"facebook\.com/groups/([^/?#]+)/user/([0-9]+)")
GROUP_RE = re.compile(r"facebook\.com/groups/([^/?#]+)/?$")
PCB_RE = re.compile(r"(?:set=pcb\.|/pcb\.|pcb\.)([0-9]+)")
BARE_ID_RE = re.compile(r"^[0-9]{8,}$")


def extract_group_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"facebook\.com/groups/([^/?#]+)", str(url))
    return match.group(1) if match else None


def extract_post_id_from_link(link: str | None) -> str | None:
    if not link:
        return None
    value = str(link).strip()
    if BARE_ID_RE.fullmatch(value):
        return value

    match = POST_RE.search(value)
    if match:
        return match.group(2)

    match = PCB_RE.search(value)
    if match:
        return match.group(1)

    qs = parse_qs(urlparse(value).query)
    for key in ("story_fbid", "fbid"):
        vals = qs.get(key)
        if vals and BARE_ID_RE.fullmatch(vals[0]):
            return vals[0]
    return None


def classify_facebook_url(url: str | None) -> str:
    if not url:
        return "missing"
    value = str(url)
    if POST_RE.search(value):
        return "valid_post"
    if USER_RE.search(value):
        return "profile_url"
    if GROUP_RE.search(value):
        return "group_url"
    return "other"


def build_url_bundle(group_id: str | None, post_id: str | None) -> dict[str, str | None]:
    if not group_id or not post_id:
        return {
            "desktop_post_url": None,
            "mobile_post_url": None,
            "permalink_url": None,
            "mobile_permalink_url": None,
            "universal_post_url": None,
            "url_status": "missing_post_id",
        }
    permalink = f"https://www.facebook.com/permalink.php?story_fbid={post_id}&id={group_id}"
    return {
        "desktop_post_url": f"https://www.facebook.com/groups/{group_id}/posts/{post_id}/",
        "mobile_post_url": f"https://m.facebook.com/groups/{group_id}/posts/{post_id}/",
        "permalink_url": permalink,
        "mobile_permalink_url": f"https://m.facebook.com/permalink.php?story_fbid={post_id}&id={group_id}",
        # Preferred for reports/messages: works on desktop and is usually better on iPhone.
        "universal_post_url": permalink,
        "url_status": "valid_post",
    }


def normalize_facebook_item_urls(item: dict[str, Any]) -> dict[str, str | None]:
    links = item.get("links") or []
    crossposts = item.get("crosspost_urls") or []
    original = item.get("post_url")
    group_id = item.get("group") or extract_group_id(original)

    post_id = None
    for link in [original, *links, *crossposts]:
        post_id = extract_post_id_from_link(link)
        if post_id:
            break

    bundle = build_url_bundle(group_id, post_id)
    original_status = classify_facebook_url(original)
    if bundle["url_status"] == "missing_post_id" and original_status != "missing":
        bundle["url_status"] = original_status
    bundle["original_url"] = original
    bundle["original_url_status"] = original_status
    return bundle
