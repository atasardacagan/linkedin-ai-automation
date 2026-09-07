import asyncio

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from linkedin_automation.db import SessionLocal
from linkedin_automation.models import Setting, Topic

TOPICS = [
    "Data Science",
    "Data Analytics",
    "Yapay Zeka",
    "Machine Learning",
    "Automotive",
    "Mobility",
    "SaaS",
    "Veri Mühendisliği",
    "Python",
    "SQL",
    "Kariyer",
    "Otomasyon",
    "Dijital Dönüşüm",
]

DEFAULT_SETTINGS = [
    {
        "key": "content_calendar",
        "value": {"weekdays": [0, 2, 4], "hour": 9, "minute": 0},
    },
    {"key": "system_paused", "value": {"enabled": False}},
    {"key": "style_profile", "value": {"preferences": [], "revision_signals": {}}},
]


async def main():
    async with SessionLocal() as db:
        await db.execute(
            insert(Topic)
            .values([{"name": name} for name in TOPICS])
            .on_conflict_do_nothing(index_elements=[func.lower(Topic.name)])
        )
        await db.execute(
            insert(Setting).values(DEFAULT_SETTINGS).on_conflict_do_nothing(index_elements=["key"])
        )
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
