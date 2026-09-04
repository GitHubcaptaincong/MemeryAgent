"""Build (or safely send) a reminder payload without claiming a database job.

Examples:
  python scripts/verify_wechat_reminder.py
  python scripts/verify_wechat_reminder.py --send --openid OPENID --template-id TEMPLATE_ID
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

sys.path.insert(0, "backend/src")

from memory_agent.config import Settings  # noqa: E402
from memory_agent.reminder_worker import map_template_data  # noqa: E402
from memory_agent.wechat import WeChatClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="send directly to WeChat")
    parser.add_argument("--openid")
    parser.add_argument("--template-id")
    args = parser.parse_args()
    settings = Settings()
    logical_data = {
        "plan_name": "今日知识复习",
        "reminder_time": "2026-08-13 12:00",
        "summary": "今天有 13 个知识点建议复习",
    }
    payload = map_template_data(logical_data, settings.wechat_template_field_map)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not args.send:
        return
    if not args.openid or not args.template_id:
        parser.error("--send requires --openid and --template-id")
    if not settings.wechat_app_id or not settings.wechat_app_secret_value:
        parser.error("--send requires WECHAT app credentials in the environment")
    client = WeChatClient(
        app_id=settings.wechat_app_id,
        app_secret=settings.wechat_app_secret_value,
        timeout_seconds=settings.wechat_send_timeout_seconds,
        miniprogram_state=settings.wechat_miniprogram_state,
        lang=settings.wechat_subscribe_lang,
    )

    async def send() -> None:
        try:
            result = await client.send_subscribe_message(
                args.openid, args.template_id, settings.wechat_subscribe_page, payload
            )
            print(json.dumps({"errcode": result.errcode, "errmsg": result.errmsg}, ensure_ascii=False))
        finally:
            await client.aclose()

    asyncio.run(send())


if __name__ == "__main__":
    main()
