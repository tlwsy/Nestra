"""APScheduler lifecycle for the single-process Nestra pipeline."""

from __future__ import annotations

from typing import Any

from ..core.logging import get_logger
from .jobs import (
    JobDependencies,
    crawl_sites,
    dispatch_notifications,
    download_attachments,
    housekeeping,
    retry_deliveries,
    tag_articles,
)

log = get_logger(__name__)


class PipelineScheduler:
    def __init__(self, dependencies: JobDependencies) -> None:
        self.dependencies = dependencies
        self._scheduler: Any = None

    @property
    def running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        if self.running:
            return
        settings = self.dependencies.settings
        scheduler = AsyncIOScheduler(
            timezone=settings.app.timezone,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
        )
        interval = settings.schedule
        jobs = (
            # crawl_sites applies each site's interval; this only checks which sites are due.
            (crawl_sites, min(interval.crawl_default_interval_sec, 60), "crawl"),
            (download_attachments, interval.dispatch_interval_sec, "attachments"),
            (tag_articles, interval.tag_interval_sec, "tag"),
            (dispatch_notifications, interval.dispatch_interval_sec, "match"),
            (retry_deliveries, interval.retry_delivery_interval_sec, "delivery"),
        )
        for function, seconds, job_id in jobs:
            scheduler.add_job(
                function,
                "interval",
                seconds=seconds,
                args=(self.dependencies,),
                id=job_id,
                replace_existing=True,
            )
        scheduler.add_job(
            housekeeping,
            CronTrigger.from_crontab(interval.housekeeping_cron),
            args=(self.dependencies,),
            id="housekeeping",
            replace_existing=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        log.info("scheduler_started", jobs=len(scheduler.get_jobs()))

    async def aclose(self) -> None:
        if self._scheduler is not None and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            log.info("scheduler_stopped")
        self._scheduler = None
        await self.dependencies.aclose()


__all__ = ["PipelineScheduler"]
