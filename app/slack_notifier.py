from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any


LOGGER = logging.getLogger(__name__)
SLACK_API_URL = "https://slack.com/api/chat.postMessage"

_AGENT_IDENTITIES = {
    "planner": ("Planner Agent", ":clipboard:"),
    "coder": ("Developer Agent", ":hammer_and_wrench:"),
    "reviewer": ("Reviewer Agent", ":mag:"),
    "validator": ("Validator", ":white_check_mark:"),
    "orchestrator": ("Investory Orchestrator", ":robot_face:"),
}


def publish_task_activity(activity: dict[str, Any]) -> bool:
    """Best-effort delivery of persisted task activity to the configured Slack channel."""
    token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    channel = os.getenv("SLACK_CHANNEL_ID", "").strip()
    if not token or not channel:
        return False

    actor = str(activity.get("actor", "orchestrator"))
    username, icon = _AGENT_IDENTITIES.get(actor, (actor.title(), ":robot_face:"))
    text = str(activity.get("message", "")).strip()
    if not text:
        return True
    task_id = str(activity.get("task_id", ""))
    if task_id:
        text = f"*Task {task_id}* · {text}"

    payload = json.dumps({
        "channel": channel,
        "text": text,
        "username": username,
        "icon_emoji": icon,
        "unfurl_links": False,
        "unfurl_media": False,
    }).encode("utf-8")
    request = Request(
        SLACK_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            LOGGER.warning("Slack activity delivery failed (%s).", result.get("error", "unknown_error"))
            return False
        return True
    except HTTPError as error:
        LOGGER.warning("Slack activity delivery failed with HTTP %s.", error.code)
    except (URLError, TimeoutError, OSError, ValueError) as error:
        LOGGER.warning("Slack activity delivery failed (%s).", type(error).__name__)
    return False
