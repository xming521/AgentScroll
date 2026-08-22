"""Command-line interface for AgentScroll."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

_Result = TypeVar("_Result")
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
        raise click.ClickException(str(exc)) from exc


def _print_json(value: Any) -> None:
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


def _comma_separated(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _config_path(context: click.Context) -> Path | None:
    return context.find_root().obj["config_path"]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config-path",
    type=_INPUT_FILE,
    help="推理配置文件；默认读取 settings.jsonc 或 AGENTSCROLL_INFERENCE_CONFIG。",
)
@click.pass_context
def cli(context: click.Context, config_path: Path | None) -> None:
    """AgentScroll：搜索互联网并学习最新热点。"""
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


@hotlist.command("select")
@click.argument("snapshot", type=_INPUT_FILE)
@click.pass_context
def hotlist_select(context: click.Context, snapshot: Path) -> None:
    """用模型完成话题级粗筛，不访问详情页。"""
    from agentscroll.workflows import select_hotlist_first_pass

    result = _run(
        lambda: select_hotlist_first_pass(
            snapshot,
            config_path=_config_path(context),
        )
    )
    _print_json(result)


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
)
@click.option("--output-dir", type=_PATH, help="知识卡目录。")
@click.option("--share-output-dir", type=_PATH, help="分享队列目录。")
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
    """粗筛热榜、采集证据并生成知识卡和分享队列。"""
    from agentscroll.workflows import (
        generate_selected_hotlist_knowledge_cards,
        select_hotlist_first_pass,
    )

    config_path = _config_path(context)
    selection = _run(
        lambda: select_hotlist_first_pass(snapshot, config_path=config_path)
    )
    result = _run(
        lambda: generate_selected_hotlist_knowledge_cards(
            snapshot,
            selection,
            config_path=config_path,
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


if __name__ == "__main__":
    cli()
