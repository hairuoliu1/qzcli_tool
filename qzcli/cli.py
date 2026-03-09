#!/usr/bin/env python3
"""qzcli - 启智平台核心 CLI。"""

import argparse
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .api import QzAPIError, get_api
from .config import (
    clear_cookie,
    find_resource_by_name,
    find_workspace_by_name,
    get_cookie,
    get_workspace_resources,
    load_all_resources,
    save_cookie,
)
from .display import format_duration, get_display

try:
    from pypinyin import Style, lazy_pinyin
except Exception:  # pragma: no cover - optional dependency fallback
    Style = None
    lazy_pinyin = None


def _format_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "-"
    return f"{(numerator / denominator) * 100:.1f}%"


def _env_candidates() -> List[Path]:
    """返回可能的 .env 路径（优先项目根目录）。"""
    repo_env = Path(__file__).resolve().parents[1] / ".env"
    cwd_env = Path.cwd() / ".env"
    if cwd_env == repo_env:
        return [repo_env]
    return [repo_env, cwd_env]


def _write_env_credentials(username: str, password: str) -> Path:
    """写入 .env（u/p）。"""
    env_path = _env_candidates()[0]
    env_path.write_text(f"u={username}\np={password}\n", encoding="utf-8")
    return env_path


def _load_dotenv_vars() -> Dict[str, str]:
    """加载 .env 键值（仅简单 KEY=VALUE 形式）。"""
    env: Dict[str, str] = {}
    for path in _env_candidates():
        if not path.exists():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    env[key] = value
        except OSError:
            continue
        if env:
            break
    return env


def _get_env_credentials() -> Tuple[str, str]:
    """优先读环境变量，其次读 .env（兼容 u/p 简写）。"""
    env_file = _load_dotenv_vars()
    username = (
        os.environ.get("QZCLI_USERNAME")
        or env_file.get("QZCLI_USERNAME")
        or env_file.get("username")
        or env_file.get("u")
        or ""
    )
    password = (
        os.environ.get("QZCLI_PASSWORD")
        or env_file.get("QZCLI_PASSWORD")
        or env_file.get("password")
        or env_file.get("p")
        or ""
    )
    return username, password


def _is_cookie_invalid_error(err: Exception) -> bool:
    text = str(err)
    return ("401" in text) or ("过期" in text) or ("无效" in text)


def _auto_login_with_env(api, display, workspace_id: str = "") -> Optional[Dict[str, str]]:
    username, password = _get_env_credentials()
    if not username or not password:
        return None
    try:
        display.print("[dim]检测到 cookie 无效，正在使用 .env 自动登录刷新...[/dim]")
        cookie = api.login_with_cas(username, password)
        save_cookie(cookie, workspace_id=workspace_id)
        return {"cookie": cookie, "workspace_id": workspace_id}
    except QzAPIError as e:
        display.print_warning(f"自动登录失败: {e}")
        return None


def _get_valid_cookie(api, display, workspace_hint: str = "") -> Optional[Dict[str, str]]:
    """获取可用 cookie；失效时尝试用 .env 自动刷新。"""
    cookie_data = get_cookie() or {}
    cookie = cookie_data.get("cookie", "")
    workspace_id = workspace_hint or cookie_data.get("workspace_id", "")

    if cookie:
        try:
            api.list_workspaces(cookie)
            return {"cookie": cookie, "workspace_id": workspace_id}
        except QzAPIError as e:
            if not _is_cookie_invalid_error(e):
                display.print_error(f"cookie 校验失败: {e}")
                return None
            refreshed = _auto_login_with_env(api, display, workspace_id=workspace_id)
            if refreshed:
                return refreshed
            return None

    refreshed = _auto_login_with_env(api, display, workspace_id=workspace_id)
    if refreshed:
        return refreshed
    return None


def _fetch_all_tasks(
    api,
    workspace_id: str,
    cookie: str,
    project_id: Optional[str] = None,
    page_size: int = 200,
) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    page_num = 1
    while True:
        data = api.list_task_dimension(
            workspace_id,
            cookie,
            project_id=project_id,
            page_num=page_num,
            page_size=page_size,
        )
        page_tasks = data.get("task_dimensions", [])
        total = data.get("total", 0)
        tasks.extend(page_tasks)
        if len(tasks) >= total or not page_tasks:
            break
        page_num += 1
    return tasks


def _fetch_all_nodes(
    api,
    workspace_id: str,
    cookie: str,
    logic_compute_group_id: Optional[str] = None,
    page_size: int = 500,
) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    page_num = 1
    while True:
        data = api.list_node_dimension(
            workspace_id,
            cookie,
            logic_compute_group_id=logic_compute_group_id,
            page_num=page_num,
            page_size=page_size,
        )
        page_nodes = data.get("node_dimensions", [])
        total = data.get("total", 0)
        nodes.extend(page_nodes)
        if len(nodes) >= total or not page_nodes:
            break
        page_num += 1
    return nodes


def _resolve_workspace_list(api, cookie: str, workspace_input: Optional[str]) -> List[Tuple[str, str]]:
    """返回 [(workspace_id, workspace_name)]。"""
    if workspace_input:
        if workspace_input.startswith("ws-"):
            ws_data = get_workspace_resources(workspace_input) or {}
            return [(workspace_input, ws_data.get("name", ""))]

        ws_id = find_workspace_by_name(workspace_input)
        if ws_id:
            ws_data = get_workspace_resources(ws_id) or {}
            return [(ws_id, ws_data.get("name", workspace_input))]

        # 缓存里找不到时，再查线上列表做名称匹配
        try:
            workspaces = api.list_workspaces(cookie)
        except QzAPIError:
            workspaces = []

        for ws in workspaces:
            if ws.get("name", "") == workspace_input:
                return [(ws.get("id", ""), ws.get("name", ""))]
        for ws in workspaces:
            if workspace_input.lower() in ws.get("name", "").lower():
                return [(ws.get("id", ""), ws.get("name", ""))]

        raise QzAPIError(f"未找到工作空间: {workspace_input}")

    # 不指定 -w 时：默认全量 workspace
    workspaces: List[Tuple[str, str]] = []
    try:
        ws_list = api.list_workspaces(cookie)
        for ws in ws_list:
            ws_id = ws.get("id", "")
            ws_name = ws.get("name", "")
            if ws_id:
                workspaces.append((ws_id, ws_name))
    except QzAPIError:
        pass

    if not workspaces:
        # 兜底：本地缓存
        all_resources = load_all_resources()
        for ws_id, ws_data in all_resources.items():
            workspaces.append((ws_id, ws_data.get("name", "")))

    # 去重
    seen = set()
    unique: List[Tuple[str, str]] = []
    for ws_id, ws_name in workspaces:
        if ws_id and ws_id not in seen:
            seen.add(ws_id)
            unique.append((ws_id, ws_name))
    return unique


def _resolve_project_id(workspace_id: str, project_input: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if project_input.startswith("project-"):
        return project_input, None
    proj = find_resource_by_name(workspace_id, "projects", project_input)
    if not proj:
        return None, None
    return proj.get("id", ""), proj


def cmd_login(args):
    display = get_display()
    api = get_api()

    username = args.username
    if not username:
        try:
            username = input("学工号: ").strip()
        except (EOFError, KeyboardInterrupt):
            display.print("\n[dim]已取消[/dim]")
            return 1

    if not username:
        display.print_error("用户名不能为空")
        return 1

    password = args.password
    if not password:
        import getpass

        try:
            password = getpass.getpass("密码: ")
        except (EOFError, KeyboardInterrupt):
            display.print("\n[dim]已取消[/dim]")
            return 1

    if not password:
        display.print_error("密码不能为空")
        return 1

    display.print("[dim]正在登录...[/dim]")
    try:
        cookie = api.login_with_cas(username, password)
        save_cookie(cookie, workspace_id=args.workspace)
        env_path = _write_env_credentials(username, password)
        display.print_success("登录成功！")
        display.print(f"[dim]已写入凭据: {env_path}[/dim]")
        if args.workspace:
            display.print(f"[dim]默认工作空间: {args.workspace}[/dim]")
        return 0
    except QzAPIError as e:
        display.print_error(f"登录失败: {e}")
        return 1


def cmd_logout(args):
    display = get_display()
    clear_cookie()
    deleted = 0
    for env_path in _env_candidates():
        if env_path.exists():
            env_path.unlink()
            deleted += 1
    if deleted > 0:
        display.print_success("已退出登录（cookie 和 .env 已清除）")
    else:
        display.print_success("已退出登录（cookie 已清除，未找到 .env）")
    return 0


def cmd_workspace_usage(args):
    display = get_display()
    api = get_api()

    cookie_data = _get_valid_cookie(api, display, workspace_hint=(args.workspace or ""))
    if not cookie_data or not cookie_data.get("cookie"):
        display.print_error("未设置有效 cookie，请运行 qzcli login 或在 .env 配置 u/p")
        return 1

    cookie = cookie_data["cookie"]

    try:
        workspace_list = _resolve_workspace_list(api, cookie, args.workspace)
    except QzAPIError as e:
        display.print_error(str(e))
        return 1

    if not workspace_list:
        display.print_error("未找到可用工作空间")
        return 1

    min_priority = args.min_priority
    all_ok = 0

    for workspace_id, ws_name in workspace_list:
        try:
            tasks = _fetch_all_tasks(api, workspace_id, cookie, page_size=200)
            nodes = _fetch_all_nodes(api, workspace_id, cookie, page_size=500)
        except QzAPIError as e:
            display.print_warning(f"[{ws_name or workspace_id}] 查询失败: {e}")
            continue

        total_tasks = len(tasks)
        high_tasks = [t for t in tasks if (t.get("priority", 0) or 0) >= min_priority]
        total_gpu_used = sum((t.get("gpu") or {}).get("total", 0) for t in tasks)
        high_gpu_used = sum((t.get("gpu") or {}).get("total", 0) for t in high_tasks)

        schedulable_nodes = 0
        busy_nodes = 0
        total_gpu_capacity = 0

        for node in nodes:
            status = node.get("status", "")
            cordon_type = node.get("cordon_type", "")
            gpu = node.get("gpu") or {}
            gpu_total = gpu.get("total", 0)
            gpu_used = gpu.get("used", 0)
            if gpu_total <= 0:
                continue
            if status == "Ready" and not cordon_type:
                schedulable_nodes += 1
                total_gpu_capacity += gpu_total
                if gpu_used > 0:
                    busy_nodes += 1

        display.print(f"\n[bold]{ws_name or workspace_id}[/bold]")
        display.print(f"[dim]{workspace_id}[/dim]")
        display.print(f"运行任务: {total_tasks}（高优 P>={min_priority}: {len(high_tasks)}）")
        display.print(
            f"GPU 占用: {total_gpu_used}/{total_gpu_capacity} "
            f"(利用率 {_format_percent(total_gpu_used, total_gpu_capacity)})"
        )
        display.print(
            f"节点占用: {busy_nodes}/{schedulable_nodes} "
            f"(占用率 {_format_percent(busy_nodes, schedulable_nodes)})"
        )
        display.print(f"高优 GPU: {high_gpu_used}/{total_gpu_capacity}")
        all_ok += 1

    return 0 if all_ok > 0 else 1


def cmd_workspace_list(args):
    display = get_display()
    api = get_api()

    cookie_data = _get_valid_cookie(api, display)
    if not cookie_data or not cookie_data.get("cookie"):
        display.print_error("未设置有效 cookie，请运行 qzcli login 或在 .env 配置 u/p")
        return 1
    cookie = cookie_data["cookie"]

    rows: List[Tuple[str, str]] = []
    try:
        ws_list = api.list_workspaces(cookie)
        for ws in ws_list:
            ws_id = ws.get("id", "")
            ws_name = ws.get("name", "")
            if ws_id:
                rows.append((ws_id, ws_name))
    except QzAPIError:
        pass

    if not rows:
        all_resources = load_all_resources()
        for ws_id, ws_data in all_resources.items():
            rows.append((ws_id, ws_data.get("name", "")))

    if not rows:
        display.print_error("未找到可用工作空间")
        return 1

    # 去重并排序
    uniq: Dict[str, str] = {}
    for ws_id, ws_name in rows:
        uniq[ws_id] = ws_name or uniq.get(ws_id, "")
    sorted_rows = sorted(uniq.items(), key=lambda x: x[1] or x[0])

    display.print(f"\n[bold]Workspace 列表 ({len(sorted_rows)} 个)[/bold]\n")
    for idx, (ws_id, ws_name) in enumerate(sorted_rows, 1):
        display.print(f"[{idx:2d}] {ws_name or '[未命名]'}")
        display.print(f"     [dim]{ws_id}[/dim]")
    return 0


def cmd_project_usage(args):
    display = get_display()
    api = get_api()

    cookie_data = _get_valid_cookie(api, display, workspace_hint=(args.workspace or ""))
    if not cookie_data or not cookie_data.get("cookie"):
        display.print_error("未设置有效 cookie，请运行 qzcli login 或在 .env 配置 u/p")
        return 1

    cookie = cookie_data["cookie"]
    try:
        workspace_list = _resolve_workspace_list(api, cookie, args.workspace)
    except QzAPIError as e:
        display.print_error(str(e))
        return 1

    if not workspace_list:
        display.print_error("未找到可用工作空间")
        return 1

    min_priority = args.min_priority
    project_input = args.project
    ok_count = 0

    for workspace_id, ws_name in workspace_list:
        try:
            ws_tasks_all = _fetch_all_tasks(api, workspace_id, cookie, page_size=200)
        except QzAPIError as e:
            display.print_warning(f"[{ws_name or workspace_id}] 查询失败: {e}")
            continue

        ws_tasks = [t for t in ws_tasks_all if (t.get("priority", 0) or 0) >= min_priority]
        ws_gpu = sum((t.get("gpu") or {}).get("total", 0) for t in ws_tasks)

        # 不传 -p：默认展示该 workspace 的全部项目利用率
        if not project_input:
            stats: Dict[str, Dict[str, Any]] = {}
            for task in ws_tasks:
                proj = task.get("project") or {}
                proj_id = proj.get("id", "") or "unknown"
                proj_name = proj.get("name", "") or "未知项目"
                gpu_total = (task.get("gpu") or {}).get("total", 0)
                if proj_id not in stats:
                    stats[proj_id] = {"name": proj_name, "tasks": 0, "gpu": 0}
                stats[proj_id]["tasks"] += 1
                stats[proj_id]["gpu"] += gpu_total

            display.print(f"\n[bold]{ws_name or workspace_id}[/bold]")
            display.print(f"[dim]{workspace_id}[/dim]")
            display.print(f"[dim]工作空间总 GPU(P>={min_priority}): {ws_gpu}[/dim]")

            if not stats:
                display.print("[dim]无符合条件的项目任务[/dim]")
                ok_count += 1
                continue

            rows = sorted(stats.items(), key=lambda x: x[1]["gpu"], reverse=True)
            for idx, (proj_id, info) in enumerate(rows[: args.limit], 1):
                share = _format_percent(info["gpu"], ws_gpu)
                display.print(f"[{idx:2d}] {info['gpu']:>4} GPU | {share:>6} | {info['tasks']:>3} 任务 | {info['name']}")
                display.print(f"     [dim]{proj_id}[/dim]")
            ok_count += 1
            continue

        # 传了 -p：展示单项目详情
        project_id, project_data = _resolve_project_id(workspace_id, project_input)
        if not project_id:
            # 未命中该 workspace 的项目则跳过
            continue

        project_tasks_all = [t for t in ws_tasks_all if ((t.get("project") or {}).get("id", "") == project_id)]
        project_tasks = [t for t in project_tasks_all if (t.get("priority", 0) or 0) >= min_priority]
        project_gpu = sum((t.get("gpu") or {}).get("total", 0) for t in project_tasks)
        share = _format_percent(project_gpu, ws_gpu)

        project_name = ""
        if project_tasks:
            project_name = (project_tasks[0].get("project") or {}).get("name", "")
        elif project_data:
            project_name = project_data.get("name", "")

        display.print(f"\n[bold]Project 利用率[/bold]")
        display.print(f"[dim]workspace: {workspace_id}[/dim]")
        display.print(f"[dim]project_id: {project_id}[/dim]")
        if project_name:
            display.print(f"[dim]project_name: {project_name}[/dim]")
        display.print(f"任务数(P>={min_priority}): {len(project_tasks)}")
        display.print(f"项目 GPU: {project_gpu}")
        display.print(f"工作空间 GPU: {ws_gpu}")
        display.print(f"项目占比: {share}\n")

        if not project_tasks:
            display.print("[dim]当前无符合优先级条件的运行任务[/dim]")
            ok_count += 1
            continue

        project_tasks.sort(key=lambda x: (x.get("gpu") or {}).get("total", 0), reverse=True)
        for idx, task in enumerate(project_tasks[: args.limit], 1):
            job_id = task.get("id", "")
            name = task.get("name", "")
            priority = task.get("priority", 0)
            user_name = (task.get("user") or {}).get("name", "")
            gpu_total = (task.get("gpu") or {}).get("total", 0)
            gpu_usage = ((task.get("gpu") or {}).get("usage_rate", 0) or 0) * 100
            cpu_usage = ((task.get("cpu") or {}).get("usage_rate", 0) or 0) * 100
            mem_usage = ((task.get("memory") or {}).get("usage_rate", 0) or 0) * 100
            status = task.get("status", "UNKNOWN")
            running_time = format_duration(task.get("running_time_ms", ""))
            display.print(
                f"[{idx:2d}] {gpu_total:>3} GPU | P{priority} | {status} | {running_time} | {user_name}"
            )
            display.print(f"     利用率 GPU {gpu_usage:.0f}% | CPU {cpu_usage:.0f}% | MEM {mem_usage:.0f}%")
            display.print(f"     {name}")
            display.print(f"     [dim]{job_id}[/dim]")
        ok_count += 1

    if ok_count == 0 and project_input:
        display.print_error(f"未在目标工作空间中找到项目: {project_input}")
        return 1
    return 0 if ok_count > 0 else 1


def cmd_project_list(args):
    display = get_display()
    api = get_api()

    cookie_data = _get_valid_cookie(api, display, workspace_hint=(args.workspace or ""))
    if not cookie_data or not cookie_data.get("cookie"):
        display.print_error("未设置有效 cookie，请运行 qzcli login 或在 .env 配置 u/p")
        return 1
    cookie = cookie_data["cookie"]

    try:
        workspace_list = _resolve_workspace_list(api, cookie, args.workspace)
    except QzAPIError as e:
        display.print_error(str(e))
        return 1

    if not workspace_list:
        display.print_error("未找到可用工作空间")
        return 1

    shown = 0
    for workspace_id, ws_name in workspace_list:
        project_stats: Dict[str, Dict[str, Any]] = {}

        # 优先从运行任务统计，包含任务数和GPU
        try:
            tasks = _fetch_all_tasks(api, workspace_id, cookie, page_size=200)
            for task in tasks:
                proj = task.get("project") or {}
                proj_id = proj.get("id", "")
                proj_name = proj.get("name", "") or "未知项目"
                if not proj_id:
                    continue
                gpu_total = (task.get("gpu") or {}).get("total", 0)
                if proj_id not in project_stats:
                    project_stats[proj_id] = {"name": proj_name, "tasks": 0, "gpu": 0}
                project_stats[proj_id]["tasks"] += 1
                project_stats[proj_id]["gpu"] += gpu_total
        except QzAPIError:
            pass

        # 兜底补齐缓存中的项目
        ws_cache = get_workspace_resources(workspace_id) or {}
        cache_projects = ws_cache.get("projects", {})
        for proj_id, proj in cache_projects.items():
            if proj_id not in project_stats:
                project_stats[proj_id] = {
                    "name": proj.get("name", "") or "未知项目",
                    "tasks": 0,
                    "gpu": 0,
                }

        if not project_stats:
            continue

        display.print(f"\n[bold]{ws_name or workspace_id}[/bold]")
        display.print(f"[dim]{workspace_id}[/dim]")
        rows = sorted(project_stats.items(), key=lambda x: (x[1]["gpu"], x[1]["tasks"]), reverse=True)
        for idx, (proj_id, info) in enumerate(rows[: args.limit], 1):
            display.print(f"[{idx:2d}] {info['name']} | {info['tasks']} 任务 | {info['gpu']} GPU")
            display.print(f"     [dim]{proj_id}[/dim]")
        shown += 1

    if shown == 0:
        display.print_error("未找到项目")
        return 1
    return 0


def _task_user_values(task: Dict[str, Any]) -> List[str]:
    user = task.get("user") or {}
    values: List[str] = []
    for key in ("name", "username"):
        v = user.get(key)
        if v is None:
            continue
        sv = str(v).strip()
        if sv:
            values.append(sv)
    # de-duplicate while preserving order
    return list(dict.fromkeys(values))


def _normalize_user_query(value: str) -> str:
    return "".join(ch for ch in value.strip().lower() if ch not in " \t_-")


def _is_ascii_query(value: str) -> bool:
    return bool(value) and all(ord(ch) < 128 for ch in value)


@lru_cache(maxsize=4096)
def _username_pinyin_key(username: str) -> str:
    """Convert a Chinese username to compact pinyin key (e.g. 张三 -> zhangsan)."""
    if not username or not lazy_pinyin:
        return ""
    try:
        py = "".join(lazy_pinyin(username, style=Style.NORMAL, errors="ignore"))
    except Exception:
        return ""
    return _normalize_user_query(py)


def _task_match_user(task: Dict[str, Any], user_query: str) -> bool:
    query = _normalize_user_query(user_query)
    if not query:
        return False
    values = _task_user_values(task)
    for v in values:
        username_key = _normalize_user_query(v)
        if not username_key:
            continue
        if query == username_key:
            return True
        if query == _username_pinyin_key(v):
            return True
    return False


def cmd_user_jobs(args):
    display = get_display()
    api = get_api()

    cookie_data = _get_valid_cookie(api, display, workspace_hint=(args.workspace or ""))
    if not cookie_data or not cookie_data.get("cookie"):
        display.print_error("未设置有效 cookie，请运行 qzcli login 或在 .env 配置 u/p")
        return 1
    cookie = cookie_data["cookie"]

    user_query = args.user or _get_env_credentials()[0]
    if not user_query:
        display.print_error("未指定用户，且 .env 中没有 u/QZCLI_USERNAME")
        return 1
    normalized_query = _normalize_user_query(user_query)
    if _is_ascii_query(normalized_query) and not lazy_pinyin:
        display.print_error("英文查询需安装 pypinyin：pip install pypinyin")
        return 1

    try:
        workspace_list = _resolve_workspace_list(api, cookie, args.workspace)
    except QzAPIError as e:
        display.print_error(str(e))
        return 1
    if not workspace_list:
        display.print_error("未找到可用工作空间")
        return 1

    min_priority = args.min_priority
    total_found = 0
    display.print(f"\n[bold]用户任务[/bold] [dim]{user_query}[/dim]\n")

    for workspace_id, ws_name in workspace_list:
        try:
            tasks = _fetch_all_tasks(api, workspace_id, cookie, page_size=200)
        except QzAPIError as e:
            display.print_warning(f"[{ws_name or workspace_id}] 查询失败: {e}")
            continue

        matched = [
            t
            for t in tasks
            if _task_match_user(t, user_query) and (t.get("priority", 0) or 0) >= min_priority
        ]
        if not matched:
            continue

        total_found += len(matched)
        gpu_sum = sum((t.get("gpu") or {}).get("total", 0) for t in matched)

        display.print(f"[bold]{ws_name or workspace_id}[/bold]")
        display.print(f"[dim]{workspace_id}[/dim]")
        display.print(f"{len(matched)} 个任务 | {gpu_sum} GPU\n")

        matched.sort(key=lambda x: (x.get("gpu") or {}).get("total", 0), reverse=True)
        limit = args.limit if args.limit and args.limit > 0 else len(matched)
        for idx, task in enumerate(matched[:limit], 1):
            job_id = task.get("id", "")
            name = task.get("name", "")
            priority = task.get("priority", 0)
            status = task.get("status", "UNKNOWN")
            gpu_total = (task.get("gpu") or {}).get("total", 0)
            gpu_usage = ((task.get("gpu") or {}).get("usage_rate", 0) or 0) * 100
            cpu_usage = ((task.get("cpu") or {}).get("usage_rate", 0) or 0) * 100
            mem_usage = ((task.get("memory") or {}).get("usage_rate", 0) or 0) * 100
            running_time = format_duration(task.get("running_time_ms", ""))
            project_name = (task.get("project") or {}).get("name", "")
            display.print(f"[{idx:2d}] {gpu_total:>3} GPU | P{priority} | {status} | {running_time}")
            display.print(f"     利用率 GPU {gpu_usage:.0f}% | CPU {cpu_usage:.0f}% | MEM {mem_usage:.0f}%")
            display.print(f"     {name}")
            display.print(f"     [dim]{project_name} | {job_id}[/dim]")
        display.print("")

    if total_found == 0:
        display.print_warning("未找到该用户的任务")
        return 1
    return 0


def cmd_project_user_usage(args):
    display = get_display()
    api = get_api()

    cookie_data = _get_valid_cookie(api, display, workspace_hint=(args.workspace or ""))
    if not cookie_data or not cookie_data.get("cookie"):
        display.print_error("未设置有效 cookie，请运行 qzcli login 或在 .env 配置 u/p")
        return 1
    cookie = cookie_data["cookie"]

    try:
        workspace_list = _resolve_workspace_list(api, cookie, args.workspace)
    except QzAPIError as e:
        display.print_error(str(e))
        return 1
    if not workspace_list:
        display.print_error("未找到可用工作空间")
        return 1

    min_priority = args.min_priority
    found_any = 0

    for workspace_id, ws_name in workspace_list:
        project_id, project_data = _resolve_project_id(workspace_id, args.project)
        if not project_id:
            if args.project.startswith("project-"):
                project_id = args.project
            else:
                continue

        try:
            tasks = _fetch_all_tasks(api, workspace_id, cookie, project_id=project_id, page_size=200)
        except QzAPIError as e:
            display.print_warning(f"[{ws_name or workspace_id}] 查询失败: {e}")
            continue

        tasks = [t for t in tasks if (t.get("priority", 0) or 0) >= min_priority]
        if not tasks:
            continue

        found_any += 1
        project_name = (tasks[0].get("project") or {}).get("name", "") or (project_data or {}).get("name", "")

        # 按用户汇总占用
        user_stats: Dict[str, Dict[str, Any]] = {}
        for task in tasks:
            user = task.get("user") or {}
            user_name = str(user.get("name") or user.get("id") or user.get("username") or "未知用户")
            gpu_total = (task.get("gpu") or {}).get("total", 0)
            if user_name not in user_stats:
                user_stats[user_name] = {"tasks": 0, "gpu": 0}
            user_stats[user_name]["tasks"] += 1
            user_stats[user_name]["gpu"] += gpu_total

        total_gpu = sum(v["gpu"] for v in user_stats.values())
        display.print(f"\n[bold]{ws_name or workspace_id}[/bold]")
        display.print(f"[dim]{workspace_id}[/dim]")
        display.print(f"[dim]project: {project_name or project_id} ({project_id})[/dim]")
        display.print(f"[dim]任务总数: {len(tasks)} | GPU 总占用: {total_gpu}[/dim]\n")

        display.print("[bold]用户占用[/bold]")
        ranked_users = sorted(user_stats.items(), key=lambda x: x[1]["gpu"], reverse=True)
        for idx, (user_name, info) in enumerate(ranked_users, 1):
            share = _format_percent(info["gpu"], total_gpu)
            display.print(f"[{idx:2d}] {user_name} | {info['gpu']} GPU | {share} | {info['tasks']} 任务")

        display.print("\n[bold]全部 Job[/bold]")
        tasks.sort(key=lambda x: (x.get("gpu") or {}).get("total", 0), reverse=True)
        for idx, task in enumerate(tasks, 1):
            user = task.get("user") or {}
            user_name = str(user.get("name") or user.get("id") or user.get("username") or "未知用户")
            job_id = task.get("id", "")
            name = task.get("name", "")
            priority = task.get("priority", 0)
            status = task.get("status", "UNKNOWN")
            gpu_total = (task.get("gpu") or {}).get("total", 0)
            gpu_usage = ((task.get("gpu") or {}).get("usage_rate", 0) or 0) * 100
            cpu_usage = ((task.get("cpu") or {}).get("usage_rate", 0) or 0) * 100
            mem_usage = ((task.get("memory") or {}).get("usage_rate", 0) or 0) * 100
            running_time = format_duration(task.get("running_time_ms", ""))
            display.print(f"[{idx:2d}] {gpu_total:>3} GPU | P{priority} | {status} | {running_time} | {user_name}")
            display.print(f"     利用率 GPU {gpu_usage:.0f}% | CPU {cpu_usage:.0f}% | MEM {mem_usage:.0f}%")
            display.print(f"     {name}")
            display.print(f"     [dim]{job_id}[/dim]")
        display.print("")

    if found_any == 0:
        display.print_error(f"未找到项目任务: {args.project}")
        return 1
    return 0


def _extract_compute_groups(api, workspace_id: str, cookie: str) -> List[Dict[str, str]]:
    groups: List[Dict[str, str]] = []
    try:
        cluster_info = api.get_cluster_basic_info(workspace_id, cookie)
        for cg in cluster_info.get("compute_groups", []):
            for lcg in cg.get("logic_compute_groups", []):
                lcg_id = lcg.get("logic_compute_group_id", "")
                if not lcg_id:
                    continue
                resource_types = lcg.get("resource_types", [])
                groups.append(
                    {
                        "id": lcg_id,
                        "name": lcg.get("logic_compute_group_name", "") or lcg_id,
                        "gpu_type": (lcg.get("brand", "") or (resource_types[0] if resource_types else "")),
                    }
                )
    except QzAPIError:
        pass

    if groups:
        return groups

    # 兜底：本地缓存
    ws_cache = get_workspace_resources(workspace_id) or {}
    cg_map = ws_cache.get("compute_groups", {})
    for cg in cg_map.values():
        groups.append(
            {
                "id": cg.get("id", ""),
                "name": cg.get("name", ""),
                "gpu_type": cg.get("gpu_type", ""),
            }
        )
    return [g for g in groups if g.get("id")]


def cmd_train_suggest(args):
    display = get_display()
    api = get_api()

    cookie_data = _get_valid_cookie(api, display, workspace_hint=(args.workspace or ""))
    if not cookie_data or not cookie_data.get("cookie"):
        display.print_error("未设置有效 cookie，请运行 qzcli login 或在 .env 配置 u/p")
        return 1

    cookie = cookie_data["cookie"]

    try:
        workspace_list = _resolve_workspace_list(api, cookie, args.workspace)
    except QzAPIError as e:
        display.print_error(str(e))
        return 1

    if not workspace_list:
        display.print_error("未找到可用工作空间")
        return 1

    required_nodes = args.nodes
    include_low_priority = not args.no_low_priority
    high_priority_threshold = args.min_priority
    low_priority_threshold = args.low_priority_threshold

    queue_statuses = {"QUEUING", "PENDING", "job_queuing", "job_pending", "queuing", "pending"}
    suggestions: List[Dict[str, Any]] = []

    for workspace_id, ws_name in workspace_list:
        try:
            tasks = _fetch_all_tasks(api, workspace_id, cookie, page_size=200)
        except QzAPIError as e:
            display.print_warning(f"[{ws_name or workspace_id}] 拉取任务失败: {e}")
            continue

        queue_high = sum(
            1
            for t in tasks
            if ((t.get("priority", 0) or 0) >= high_priority_threshold and (t.get("status", "") in queue_statuses))
        )

        node_low_priority_gpu: Dict[str, int] = {}
        if include_low_priority:
            for task in tasks:
                priority = task.get("priority", 10)
                if priority > low_priority_threshold:
                    continue
                gpu_total = (task.get("gpu") or {}).get("total", 0)
                nodes_occupied = ((task.get("nodes_occupied") or {}).get("nodes") or [])
                if not nodes_occupied:
                    continue
                gpu_per_node = gpu_total // len(nodes_occupied) if len(nodes_occupied) > 1 else gpu_total
                for node_name in nodes_occupied:
                    node_low_priority_gpu[node_name] = node_low_priority_gpu.get(node_name, 0) + gpu_per_node

        compute_groups = _extract_compute_groups(api, workspace_id, cookie)
        for group in compute_groups:
            lcg_id = group["id"]
            try:
                nodes = _fetch_all_nodes(api, workspace_id, cookie, logic_compute_group_id=lcg_id, page_size=500)
            except QzAPIError:
                continue

            free_nodes = 0
            reclaimable_nodes = 0
            for node in nodes:
                status = node.get("status", "")
                cordon_type = node.get("cordon_type", "")
                gpu = node.get("gpu") or {}
                gpu_total = gpu.get("total", 0)
                gpu_used = gpu.get("used", 0)
                if gpu_total <= 0 or status != "Ready" or cordon_type:
                    continue
                if gpu_used == 0:
                    free_nodes += 1
                elif include_low_priority:
                    node_name = node.get("name", "")
                    if node_low_priority_gpu.get(node_name, 0) >= gpu_total:
                        reclaimable_nodes += 1

            available_nodes = free_nodes + reclaimable_nodes
            suggestions.append(
                {
                    "workspace_id": workspace_id,
                    "workspace_name": ws_name or workspace_id,
                    "compute_group_id": lcg_id,
                    "compute_group_name": group.get("name", lcg_id),
                    "gpu_type": group.get("gpu_type", ""),
                    "free_nodes": free_nodes,
                    "reclaimable_nodes": reclaimable_nodes,
                    "available_nodes": available_nodes,
                    "queue_high": queue_high,
                    "fit_now": free_nodes >= required_nodes,
                    "fit_total": available_nodes >= required_nodes,
                }
            )

    if not suggestions:
        display.print_error("没有获取到可用计算组信息")
        return 1

    suggestions.sort(
        key=lambda x: (
            0 if x["fit_now"] else 1,
            0 if x["fit_total"] else 1,
            -x["available_nodes"],
            x["queue_high"],
            -x["free_nodes"],
        )
    )

    display.print(
        f"\n[bold]多节点训练建议[/bold] "
        f"(需求: {required_nodes} 节点, 高优阈值 P>={high_priority_threshold})\n"
    )

    top = suggestions[: args.top]
    for idx, s in enumerate(top, 1):
        verdict = "推荐" if s["fit_total"] else "资源不足"
        lp_text = f"+{s['reclaimable_nodes']} 低优可回收" if include_low_priority else "未计低优回收"
        display.print(
            f"[{idx}] {verdict} | [{s['workspace_name']}] {s['compute_group_name']} "
            f"| 空节点 {s['free_nodes']} | {lp_text} | 可用 {s['available_nodes']} "
            f"| 高优排队 {s['queue_high']} | {s['gpu_type']}"
        )
        display.print(f"     WS={s['workspace_id']}  LCG={s['compute_group_id']}")

    best = top[0]
    display.print("\n[bold]建议首选[/bold]")
    display.print(
        f"[{best['workspace_name']}] {best['compute_group_name']} "
        f"(可用 {best['available_nodes']} 节点, 高优排队 {best['queue_high']})"
    )
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="qzcli",
        description="启智平台核心 CLI（登录、占用率、训练建议）",
    )
    parser.add_argument("--version", "-V", action="version", version=f"qzcli {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    login_parser = subparsers.add_parser("login", help="通过 CAS 统一认证登录获取 cookie")
    login_parser.add_argument("--username", "-u", help="学工号")
    login_parser.add_argument("--password", "-p", help="密码")
    login_parser.add_argument("--workspace", "-w", help="默认工作空间 ID")

    subparsers.add_parser("logout", help="退出登录并清除本地 cookie")

    subparsers.add_parser("workspace-list", help="列出可访问的工作空间（名称 + ID）")

    ws_usage_parser = subparsers.add_parser(
        "workspace-usage",
        help="查看工作空间占用率（不传 -w 时默认统计全部 workspace）",
    )
    ws_usage_parser.add_argument("--workspace", "-w", help="工作空间 ID 或名称")
    ws_usage_parser.add_argument("--min-priority", type=int, default=6, help="高优任务阈值（默认 6）")

    pj_usage_parser = subparsers.add_parser(
        "project-usage",
        help="查看项目利用率（项目GPU/工作空间GPU）",
    )
    pj_usage_parser.add_argument("--workspace", "-w", help="工作空间 ID（默认读取 cookie 中的 workspace_id）")
    pj_usage_parser.add_argument("--project", "-p", help="项目 ID 或项目别名（不传则显示全部项目）")
    pj_usage_parser.add_argument("--min-priority", type=int, default=6, help="高优任务阈值（默认 6）")
    pj_usage_parser.add_argument("--limit", "-n", type=int, default=20, help="最多显示多少个任务")

    pj_list_parser = subparsers.add_parser(
        "project-list",
        help="列出项目（不传 -w 时按全部 workspace 展示）",
    )
    pj_list_parser.add_argument("--workspace", "-w", help="工作空间 ID 或名称")
    pj_list_parser.add_argument("--limit", "-n", type=int, default=50, help="每个 workspace 最多显示多少项目")

    user_jobs_parser = subparsers.add_parser(
        "user-jobs",
        help="查看某个用户的任务（默认 .env 中的当前用户）",
    )
    user_jobs_parser.add_argument("--workspace", "-w", help="工作空间 ID 或名称（不填则遍历全部）")
    user_jobs_parser.add_argument("--user", "-u", help="用户名（匹配 name/username，支持拼音；不填默认自己）")
    user_jobs_parser.add_argument("--min-priority", type=int, default=1, help="最小优先级过滤（默认 1）")
    user_jobs_parser.add_argument("--limit", "-n", type=int, default=50, help="每个 workspace 最多显示多少任务（<=0 表示全部）")

    pj_user_usage_parser = subparsers.add_parser(
        "project-user-usage",
        help="查看某项目的用户占用，并显示该项目全部 job",
    )
    pj_user_usage_parser.add_argument("--workspace", "-w", help="工作空间 ID 或名称（不填则遍历全部）")
    pj_user_usage_parser.add_argument("--project", "-p", required=True, help="项目 ID 或项目名称/别名")
    pj_user_usage_parser.add_argument("--min-priority", type=int, default=1, help="最小优先级过滤（默认 1）")

    suggest_parser = subparsers.add_parser(
        "train-suggest",
        help="给出多节点训练建议（按可用节点和排队压力排序）",
    )
    suggest_parser.add_argument("--workspace", "-w", help="工作空间 ID 或名称（不填则遍历所有 workspace）")
    suggest_parser.add_argument("--nodes", "-n", type=int, default=8, help="目标节点数（默认 8）")
    suggest_parser.add_argument("--top", type=int, default=5, help="输出前 N 个候选（默认 5）")
    suggest_parser.add_argument("--min-priority", type=int, default=6, help="高优任务阈值（默认 6，按 6-10 为高优）")
    suggest_parser.add_argument("--low-priority-threshold", type=int, default=4, help="低优阈值（默认 4，按 1-4 为低优）")
    suggest_parser.add_argument("--no-low-priority", action="store_true", help="不计入低优可回收节点")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    commands = {
        "login": cmd_login,
        "logout": cmd_logout,
        "workspace-list": cmd_workspace_list,
        "workspace-usage": cmd_workspace_usage,
        "project-list": cmd_project_list,
        "project-usage": cmd_project_usage,
        "user-jobs": cmd_user_jobs,
        "project-user-usage": cmd_project_user_usage,
        "train-suggest": cmd_train_suggest,
    }

    cmd_func = commands.get(args.command)
    if not cmd_func:
        parser.print_help()
        return 1

    try:
        return cmd_func(args)
    except KeyboardInterrupt:
        print("\n操作已取消")
        return 130
    except Exception as e:  # pragma: no cover
        display = get_display()
        display.print_error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
