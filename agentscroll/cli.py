"""Command-line interface for AgentScroll."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

from agentscroll.runtime_logging import configure_runtime_logging

_Result = TypeVar("_Result")
_INTERVAL_PATTERN = re.compile(r"^([1-9]\d*)([mhd])$", re.IGNORECASE)
_INTERVAL_MULTIPLIERS = {"m": 60, "h": 60 * 60, "d": 24 * 60 * 60}
_PATH = click.Path(file_okay=False, path_type=Path)
_INPUT_FILE = click.Path(
    exists=True,
    dir_okay=False,
    readable=True,
    path_type=Path,
)


def _run(function: Callable[[], _Result]) -> _Result:
    try:
        return function()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        logging.getLogger(__name__).exception("AgentScroll command failed")
        raise click.ClickException(str(exc)) from exc


def _print_json(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _comma_separated(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _parse_interval_seconds(value: str) -> int:
    match = _INTERVAL_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("使用正整数加 m、h 或 d，例如 30m、4h、1d")
    amount, unit = match.groups()
    return int(amount) * _INTERVAL_MULTIPLIERS[unit.lower()]


def _config_path(context: click.Context) -> Path | None:
    return context.find_root().obj["config_path"]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config-path",
    type=_INPUT_FILE,
    help="AgentScroll 配置文件；默认读取 settings.jsonc 或 AGENTSCROLL_CONFIG。",
)
@click.pass_context
def cli(context: click.Context, config_path: Path | None) -> None:
    """AgentScroll：搜索互联网并学习最新热点。"""
    _run(configure_runtime_logging)
    context.ensure_object(dict)
    context.obj["config_path"] = config_path


@cli.command("search")
@click.argument("topic")
@click.option("--sources", help="逗号分隔的平台；设置后覆盖场景路由。")
@click.option(
    "--scene",
    type=click.Choice(("auto", "formal", "informal")),
    default="auto",
    show_default=True,
    help="搜索场景。",
)
@click.option("--days", type=click.IntRange(min=1), default=30, show_default=True)
@click.option("--as-of", help="查询终点日期，格式为 YYYY-MM-DD。")
@click.option(
    "--depth",
    type=click.Choice(("quick", "default", "deep")),
    default="default",
    show_default=True,
    help="候选搜索规模：quick 最多 5 条，default 最多 10 条，deep 最多 20 条。",
)
@click.option("--output-dir", type=_PATH, help="知识文件目录。")
def search(
    topic: str,
    sources: str | None,
    scene: str,
    days: int,
    as_of: str | None,
    depth: str,
    output_dir: Path | None,
) -> None:
    """按主题搜索正文和评论，并保存知识文件。"""
    from agentscroll.collector import collect

    result = _run(
        lambda: collect(
            topic,
            sources=_comma_separated(sources),
            scene=scene,
            days=days,
            as_of=as_of,
            depth=depth,
            output_dir=output_dir,
        )
    )
    _print_json(result)


@cli.group("hotlist")
def hotlist() -> None:
    """拉取、筛选和学习 NewsNow 热榜。"""


@hotlist.command("groups")
def hotlist_groups() -> None:
    """列出全部热榜类别及其 Source ID。"""
    from agentscroll.collector import list_newsnow_groups

    _print_json(list_newsnow_groups())


@hotlist.command("fetch")
@click.option("--groups", default="综合", show_default=True, help="逗号分隔的类别。")
@click.option("--base-url", help="NewsNow 部署地址。")
@click.option("--latest", is_flag=True, help="请求 NewsNow 刷新数据。")
@click.option("--per-source-limit", type=click.IntRange(min=1))
@click.option("--timeout", type=click.IntRange(min=1), default=15, show_default=True)
@click.option("--output-dir", type=_PATH, help="热榜快照目录。")
@click.option("--no-save", is_flag=True, help="不保存本地快照。")
def hotlist_fetch(
    groups: str,
    base_url: str | None,
    latest: bool,
    per_source_limit: int | None,
    timeout: int,
    output_dir: Path | None,
    no_save: bool,
) -> None:
    """按类别拉取热榜索引。"""
    from agentscroll.collector import fetch_newsnow_hotlists

    result = _run(
        lambda: fetch_newsnow_hotlists(
            _comma_separated(groups) or (),
            base_url=base_url,
            latest=latest,
            per_source_limit=per_source_limit,
            timeout=timeout,
            output_dir=output_dir,
            save=not no_save,
        )
    )
    _print_json(result)
    if not result["total_items"]:
        raise click.exceptions.Exit(1)


@hotlist.command("learn")
@click.argument("snapshot", type=_INPUT_FILE)
@click.option(
    "--posts-per-entry",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
)
@click.option(
    "--max-entries-per-topic",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="每个话题期望保留的可读采集入口数。",
)
@click.option("--output-dir", type=_PATH, help="知识卡目录。")
@click.option("--share-output-dir", type=_PATH, help="分享 review 产物目录。")
@click.option("--no-supplement", is_flag=True, help="关闭失败话题的自动补搜。")
@click.option("--generation-effort", default="xhigh", show_default=True)
@click.option("--supplement-effort", default="xhigh", show_default=True)
@click.pass_context
def hotlist_learn(
    context: click.Context,
    snapshot: Path,
    posts_per_entry: int,
    max_entries_per_topic: int,
    output_dir: Path | None,
    share_output_dir: Path | None,
    no_supplement: bool,
    generation_effort: str,
    supplement_effort: str,
) -> None:
    """粗筛热榜、采集证据并生成知识卡和分享 review 产物。"""
    from agentscroll.workflows import learn_hotlist_snapshot

    result = _run(
        lambda: learn_hotlist_snapshot(
            snapshot,
            config_path=_config_path(context),
            output_dir=output_dir,
            share_output_dir=share_output_dir,
            posts_per_entry=posts_per_entry,
            max_entries_per_topic=max_entries_per_topic,
            supplement_failed=not no_supplement,
            generation_effort=generation_effort,
            supplement_effort=supplement_effort,
        )
    )
    _print_json(result)


@hotlist.command("run")
@click.option("--groups", default="综合", show_default=True, help="逗号分隔的类别。")
@click.option("--base-url", help="NewsNow 部署地址。")
@click.option("--latest", is_flag=True, help="请求 NewsNow 刷新数据。")
@click.option("--per-source-limit", type=click.IntRange(min=1))
@click.option("--timeout", type=click.IntRange(min=1), default=15, show_default=True)
@click.option("--snapshot-output-dir", type=_PATH, help="热榜快照目录。")
@click.option(
    "--posts-per-entry",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
)
@click.option(
    "--max-entries-per-topic",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="每个话题期望保留的可读采集入口数。",
)
@click.option("--output-dir", type=_PATH, help="知识卡目录。")
@click.option("--share-output-dir", type=_PATH, help="分享 review 产物目录。")
@click.option("--no-supplement", is_flag=True, help="关闭失败话题的自动补搜。")
@click.option("--generation-effort", default="xhigh", show_default=True)
@click.option("--supplement-effort", default="xhigh", show_default=True)
@click.option(
    "--scheduled",
    is_flag=True,
    help="按 settings.jsonc 的 schedule 时间范围和间隔持续运行。",
)
@click.pass_context
def hotlist_run(
    context: click.Context,
    groups: str,
    base_url: str | None,
    latest: bool,
    per_source_limit: int | None,
    timeout: int,
    snapshot_output_dir: Path | None,
    posts_per_entry: int,
    max_entries_per_topic: int,
    output_dir: Path | None,
    share_output_dir: Path | None,
    no_supplement: bool,
    generation_effort: str,
    supplement_effort: str,
    scheduled: bool,
) -> None:
    """拉取最新热榜并完成知识卡与分享生成，可按间隔持续运行。"""
    from agentscroll.workflows import fetch_and_learn_hotlists

    def run_once() -> dict[str, Any]:
        return fetch_and_learn_hotlists(
            _comma_separated(groups) or (),
            base_url=base_url,
            latest=latest,
            per_source_limit=per_source_limit,
            timeout=timeout,
            snapshot_output_dir=snapshot_output_dir,
            config_path=_config_path(context),
            output_dir=output_dir,
            share_output_dir=share_output_dir,
            posts_per_entry=posts_per_entry,
            max_entries_per_topic=max_entries_per_topic,
            supplement_failed=not no_supplement,
            generation_effort=generation_effort,
            supplement_effort=supplement_effort,
        )

    if not scheduled:
        _print_json(_run(run_once))
        return

    from agentscroll.scheduler import run_at_interval

    from agentscroll.config import load_settings

    settings = _run(lambda: load_settings(_config_path(context)))
    share_dispatcher = None
    if settings.sharing.enabled:
        from agentscroll.integrations import build_share_transports
        from agentscroll.sharing import ShareDispatcher

        transport_names = {
            destination.transport
            for destination in settings.sharing.destinations
        }
        transports = _run(
            lambda: build_share_transports(transport_names, settings.integrations)
        )
        share_dispatcher = _run(
            lambda: ShareDispatcher(
                settings.sharing,
                transports=transports,
                database_path=settings.storage.database_path,
            )
        )
        policy = settings.sharing.policy
        if policy.mode == "score_only":
            policy_summary = (
                f"仅发送评分不低于 {policy.score_only.min_score:g} 的消息"
            )
        else:
            policy_summary = (
                f"仅发送评分不低于 {policy.window.min_score:g} 的消息，"
                f"普通消息每 {policy.window.window_minutes} 分钟最多 "
                f"{policy.window.max_messages_per_window} 条"
            )
        click.echo(
            "即时分享已启用："
            f"{len(settings.sharing.destinations)} 个目标，{policy_summary}",
            err=True,
        )

    def scheduled_run() -> None:
        result = run_once()
        if share_dispatcher is not None:
            learned = result.get("learn", {})
            share_group_id = learned.get("share_group_id")
            generated_at = learned.get("share_generated_at")
            shares = learned.get("shares")
            if (
                share_group_id
                and generated_at
                and isinstance(shares, list)
                and shares
            ):
                try:
                    result["sharing"] = share_dispatcher.submit_shares(
                        str(share_group_id), str(generated_at), shares
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    result["sharing"] = {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
        _print_json(result)

    configure_scheduler = (
        share_dispatcher.attach_scheduler
        if share_dispatcher is not None
        else None
    )

    schedule = settings.schedule
    interval_seconds = _run(lambda: _parse_interval_seconds(schedule.every))
    click.echo(
        f"定时运行：本地时间 {schedule.start_time} 至 {schedule.end_time}，"
        f"从启动时间起每隔 {schedule.every} 检查并执行",
        err=True,
    )
    _run(
        lambda: run_at_interval(
            scheduled_run,
            interval_seconds=interval_seconds,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            configure_scheduler=configure_scheduler,
        )
    )


if __name__ == "__main__":
    cli()
