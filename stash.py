#!/usr/bin/env python3
"""Hide and restore Herdr workspaces (layout + cwds, optionally commands)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT_VERSION = 1
SHELL_NAMES = {
    "bash",
    "zsh",
    "fish",
    "sh",
    "dash",
    "ksh",
    "tmux",
    "nu",
    "pwsh",
    "powershell",
}


class StashError(Exception):
    """User-facing failure; workspace must stay open on hide errors."""


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def run_herdr(args: list[str], *, check: bool = True) -> dict[str, Any]:
    cmd = [herdr_bin(), *args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        detail = stderr or stdout or f"exit {proc.returncode}"
        if check:
            raise StashError(f"herdr {' '.join(args)} failed: {detail}")
        return {"_error": detail, "_status": proc.returncode}

    if not stdout:
        return {}

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise StashError(
            f"herdr {' '.join(args)} returned non-JSON: {stdout[:200]}"
        ) from exc

    if isinstance(payload, dict) and "error" in payload and payload["error"]:
        err = payload["error"]
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or str(err)
        else:
            msg = str(err)
        raise StashError(f"herdr {' '.join(args)} error: {msg}")

    return payload


def result_of(payload: dict[str, Any]) -> dict[str, Any]:
    if "result" in payload and isinstance(payload["result"], dict):
        return payload["result"]
    return payload


def config_dir() -> Path:
    raw = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if raw:
        path = Path(raw)
    else:
        # Local/dev fallback when not launched by Herdr.
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        path = Path(xdg) / "herdr" / "plugins" / "stash"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hidden_dir() -> Path:
    path = config_dir() / "hidden"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_config() -> dict[str, Any]:
    cfg_path = config_dir() / "config.toml"
    if not cfg_path.exists():
        cfg_path.write_text(
            "# herdr-stash plugin config\n"
            "# When true, restore re-runs captured foreground commands.\n"
            "rerun_commands = false\n",
            encoding="utf-8",
        )
    text = cfg_path.read_text(encoding="utf-8")
    rerun = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if stripped.lower().startswith("rerun_commands"):
            _, _, value = stripped.partition("=")
            rerun = value.strip().lower() in {"true", "1", "yes", "on"}
    return {"rerun_commands": rerun}


def rects_close(a: dict[str, Any], b: dict[str, Any], tol: int = 2) -> bool:
    for key in ("x", "y", "width", "height"):
        if abs(int(a.get(key, 0)) - int(b.get(key, 0))) > tol:
            return False
    return True


def split_rect(
    rect: dict[str, int], direction: str, ratio: float
) -> tuple[dict[str, int], dict[str, int]]:
    x, y = int(rect["x"]), int(rect["y"])
    w, h = int(rect["width"]), int(rect["height"])
    if direction == "right":
        w1 = max(1, int(round(w * ratio)))
        w1 = min(w1, w - 1) if w > 1 else w
        first = {"x": x, "y": y, "width": w1, "height": h}
        second = {"x": x + w1, "y": y, "width": w - w1, "height": h}
        return first, second
    if direction == "down":
        h1 = max(1, int(round(h * ratio)))
        h1 = min(h1, h - 1) if h > 1 else h
        first = {"x": x, "y": y, "width": w, "height": h1}
        second = {"x": x, "y": y + h1, "width": w, "height": h - h1}
        return first, second
    raise StashError(f"unsupported split direction: {direction!r}")


class _Leaf:
    __slots__ = ("rect",)

    def __init__(self, rect: dict[str, int]) -> None:
        self.rect = {
            "x": int(rect["x"]),
            "y": int(rect["y"]),
            "width": int(rect["width"]),
            "height": int(rect["height"]),
        }


def build_split_plan(
    layout: dict[str, Any], pane_details: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Turn a live pane.layout into a replayable create/split plan."""
    area = layout.get("area") or {}
    if not area:
        raise StashError("layout missing area")

    leaves: list[_Leaf] = [_Leaf(area)]
    creation: list[_Leaf] = [leaves[0]]
    ops: list[dict[str, Any]] = []

    for sp in layout.get("splits") or []:
        direction = sp.get("direction")
        ratio = float(sp.get("ratio", 0.5))
        if direction not in {"right", "down"}:
            raise StashError(f"unsupported split direction: {direction!r}")
        target_rect = sp.get("rect") or area
        target_idx = None
        for i, leaf in enumerate(leaves):
            if rects_close(leaf.rect, target_rect):
                target_idx = i
                break
        if target_idx is None:
            # Fallback: largest overlap / closest center.
            best_i, best_score = 0, -1.0
            tx = int(target_rect.get("x", 0)) + int(target_rect.get("width", 0)) / 2
            ty = int(target_rect.get("y", 0)) + int(target_rect.get("height", 0)) / 2
            for i, leaf in enumerate(leaves):
                cx = leaf.rect["x"] + leaf.rect["width"] / 2
                cy = leaf.rect["y"] + leaf.rect["height"] / 2
                score = -((cx - tx) ** 2 + (cy - ty) ** 2)
                if score > best_score:
                    best_i, best_score = i, score
            target_idx = best_i

        target = leaves[target_idx]
        first_rect, second_rect = split_rect(target.rect, direction, ratio)
        target.rect = first_rect
        new_leaf = _Leaf(second_rect)
        leaves.append(new_leaf)
        creation.append(new_leaf)
        ops.append(
            {
                "split_pane_index": creation.index(target),
                "direction": direction,
                "ratio": ratio,
                "new_pane_index": len(creation) - 1,
                "rect": {
                    "x": int(target_rect.get("x", 0)),
                    "y": int(target_rect.get("y", 0)),
                    "width": int(target_rect.get("width", 0)),
                    "height": int(target_rect.get("height", 0)),
                },
            }
        )

    layout_panes = list(layout.get("panes") or [])
    used: set[str] = set()
    pane_records: list[dict[str, Any]] = []

    for leaf in creation:
        match = None
        for lp in layout_panes:
            pid = lp.get("pane_id")
            if not pid or pid in used:
                continue
            if rects_close(leaf.rect, lp.get("rect") or {}):
                match = lp
                break
        if match is None:
            # Closest remaining pane by center distance.
            best, best_score = None, float("inf")
            cx = leaf.rect["x"] + leaf.rect["width"] / 2
            cy = leaf.rect["y"] + leaf.rect["height"] / 2
            for lp in layout_panes:
                pid = lp.get("pane_id")
                if not pid or pid in used:
                    continue
                r = lp.get("rect") or {}
                px = int(r.get("x", 0)) + int(r.get("width", 0)) / 2
                py = int(r.get("y", 0)) + int(r.get("height", 0)) / 2
                dist = (px - cx) ** 2 + (py - cy) ** 2
                if dist < best_score:
                    best, best_score = lp, dist
            match = best
        if match is None:
            raise StashError("could not match layout leaves to panes")
        pid = match["pane_id"]
        used.add(pid)
        detail = pane_details.get(pid, {})
        pane_records.append(
            {
                "cwd": detail.get("cwd") or os.path.expanduser("~"),
                "focused": bool(match.get("focused") or detail.get("focused")),
                "command": detail.get("command"),
                "rect": dict(leaf.rect),
                "source_pane_id": pid,
            }
        )

    focused_index = 0
    for i, rec in enumerate(pane_records):
        if rec.get("focused"):
            focused_index = i
            break

    return {
        "area": {
            "x": int(area.get("x", 0)),
            "y": int(area.get("y", 0)),
            "width": int(area.get("width", 0)),
            "height": int(area.get("height", 0)),
        },
        "zoomed": bool(layout.get("zoomed")),
        "focused_pane_index": focused_index,
        "panes": pane_records,
        "splits": ops,
    }


def capture_command(pane_id: str) -> list[str] | str | None:
    try:
        payload = run_herdr(["pane", "process-info", "--pane", pane_id])
    except StashError:
        return None
    info = result_of(payload).get("process_info") or {}
    shell_pid = info.get("shell_pid")
    processes = list(info.get("foreground_processes") or [])
    if not processes:
        return None

    # Prefer a non-shell process; fall back to first entry.
    chosen = None
    for proc in processes:
        name = str(proc.get("name") or "").lower()
        base = name.split()[0] if name else ""
        argv0 = ""
        argv = proc.get("argv") or []
        if argv:
            argv0 = Path(str(argv[0])).name.lower()
        if base in SHELL_NAMES or argv0 in SHELL_NAMES:
            continue
        if shell_pid is not None and proc.get("pid") == shell_pid:
            continue
        chosen = proc
        break
    if chosen is None:
        return None

    argv = chosen.get("argv")
    if isinstance(argv, list) and argv:
        # Saved for optional restore; only replayed when rerun_commands is true.
        return [str(a) for a in argv]
    cmdline = chosen.get("cmdline")
    if isinstance(cmdline, str) and cmdline.strip():
        return cmdline.strip()
    return None


def resolve_workspace_id() -> str:
    env_id = os.environ.get("HERDR_WORKSPACE_ID")
    if env_id:
        return env_id

    ctx_raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if ctx_raw:
        try:
            ctx = json.loads(ctx_raw)
        except json.JSONDecodeError:
            ctx = {}
        for key in ("workspace_id", "workspaceId"):
            if ctx.get(key):
                return str(ctx[key])
        workspace = ctx.get("workspace")
        if isinstance(workspace, dict) and workspace.get("workspace_id"):
            return str(workspace["workspace_id"])
        if isinstance(workspace, str) and workspace:
            return workspace

    payload = run_herdr(["api", "snapshot"])
    snap = result_of(payload).get("snapshot") or {}
    focused = snap.get("focused_workspace_id")
    if focused:
        return str(focused)
    raise StashError(
        "could not determine the current workspace "
        "(missing HERDR_WORKSPACE_ID / focus)"
    )


def list_workspaces() -> list[dict[str, Any]]:
    payload = run_herdr(["workspace", "list"])
    return list(result_of(payload).get("workspaces") or [])


def capture_workspace(workspace_id: str) -> dict[str, Any]:
    ws_payload = run_herdr(["workspace", "get", workspace_id])
    workspace = result_of(ws_payload).get("workspace") or {}
    label = workspace.get("label") or workspace_id

    tabs_payload = run_herdr(["tab", "list", "--workspace", workspace_id])
    tabs = list(result_of(tabs_payload).get("tabs") or [])
    tabs.sort(key=lambda t: int(t.get("number") or 0))

    panes_payload = run_herdr(["pane", "list", "--workspace", workspace_id])
    panes = list(result_of(panes_payload).get("panes") or [])
    pane_details: dict[str, dict[str, Any]] = {}
    for pane in panes:
        pid = pane.get("pane_id")
        if not pid:
            continue
        pane_details[pid] = {
            "cwd": pane.get("foreground_cwd") or pane.get("cwd") or os.path.expanduser("~"),
            "focused": bool(pane.get("focused")),
            "command": capture_command(pid),
            "tab_id": pane.get("tab_id"),
        }

    tab_records: list[dict[str, Any]] = []
    for tab in tabs:
        tab_id = tab.get("tab_id")
        if not tab_id:
            continue
        tab_panes = [p for p in panes if p.get("tab_id") == tab_id]
        if not tab_panes:
            raise StashError(f"tab {tab_id} has no panes")
        # Any pane in the tab yields that tab's layout.
        layout_payload = run_herdr(
            ["pane", "layout", "--pane", tab_panes[0]["pane_id"]]
        )
        layout = result_of(layout_payload).get("layout") or {}
        if layout.get("tab_id") and layout.get("tab_id") != tab_id:
            raise StashError(
                f"layout tab mismatch for {tab_id}: got {layout.get('tab_id')}"
            )
        plan = build_split_plan(layout, pane_details)
        tab_records.append(
            {
                "label": tab.get("label") or str(tab.get("number") or ""),
                "order": int(tab.get("number") or 0),
                "source_tab_id": tab_id,
                "layout": plan,
            }
        )

    if not tab_records:
        raise StashError(f"workspace {workspace_id} has no tabs to save")

    root_cwd = tab_records[0]["layout"]["panes"][0]["cwd"]
    active_tab_id = workspace.get("active_tab_id")
    active_index = 0
    for i, tab in enumerate(tab_records):
        if tab.get("source_tab_id") == active_tab_id:
            active_index = i
            break

    stash_id = str(uuid.uuid4())
    return {
        "format_version": FORMAT_VERSION,
        "id": stash_id,
        "hidden_at": datetime.now(timezone.utc).isoformat(),
        "source_workspace_id": workspace_id,
        "workspace": {
            "label": label,
            "cwd": root_cwd,
            "active_tab_index": active_index,
        },
        "tabs": tab_records,
    }


def write_stash_file(data: dict[str, Any]) -> Path:
    hidden = hidden_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = "".join(
        ch if ch.isalnum() or ch in "-_" else "_"
        for ch in str(data.get("workspace", {}).get("label") or "workspace")
    )[:40]
    path = hidden / f"{stamp}_{label}_{data['id'][:8]}.json"

    raw = json.dumps(data, indent=2, sort_keys=True) + "\n"
    # Atomic write then validate round-trip.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".stash-", suffix=".json", dir=str(hidden)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if loaded.get("id") != data["id"]:
            raise StashError("stash file validation failed: id mismatch")
        if not loaded.get("tabs"):
            raise StashError("stash file validation failed: missing tabs")
        for tab in loaded["tabs"]:
            if not tab.get("layout", {}).get("panes"):
                raise StashError("stash file validation failed: empty tab panes")
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return path


def apply_tab_layout(
    root_pane_id: str,
    plan: dict[str, Any],
    *,
    rerun_commands: bool,
) -> list[str]:
    panes = list(plan.get("panes") or [])
    if not panes:
        raise StashError("tab layout has no panes")

    pane_ids = [root_pane_id]
    for op in plan.get("splits") or []:
        parent_index = int(op["split_pane_index"])
        new_index = int(op["new_pane_index"])
        if parent_index >= len(pane_ids):
            raise StashError("split plan references unknown parent pane")
        parent_id = pane_ids[parent_index]
        new_cwd = panes[new_index]["cwd"]
        direction = op["direction"]
        ratio = float(op["ratio"])
        payload = run_herdr(
            [
                "pane",
                "split",
                parent_id,
                "--direction",
                direction,
                "--ratio",
                str(ratio),
                "--cwd",
                new_cwd,
                "--no-focus",
            ]
        )
        new_pane = result_of(payload).get("pane") or {}
        new_id = new_pane.get("pane_id")
        if not new_id:
            raise StashError("pane split did not return pane_id")
        if new_index != len(pane_ids):
            raise StashError(
                f"split plan out of order: expected new index {len(pane_ids)}, "
                f"got {new_index}"
            )
        pane_ids.append(new_id)

    if len(pane_ids) != len(panes):
        raise StashError(
            f"restored {len(pane_ids)} panes but stash has {len(panes)}"
        )

    if rerun_commands:
        for pane_id, rec in zip(pane_ids, panes):
            command = rec.get("command")
            if not command:
                continue
            if isinstance(command, list):
                args = [str(a) for a in command]
            else:
                args = [str(command)]
            if args:
                run_herdr(["pane", "run", pane_id, *args], check=False)

    focused_index = int(plan.get("focused_pane_index") or 0)
    if 0 <= focused_index < len(pane_ids):
        # Best-effort: focus via tab focus is handled by caller; there is no
        # direct "focus this pane id" CLI, so we leave the last created focus
        # alone unless we can use pane APIs. Snapshot focus is restored at
        # workspace/tab level.
        pass

    return pane_ids


def restore_one(path: Path, *, rerun_commands: bool) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("format_version") or 0) != FORMAT_VERSION:
        raise StashError(
            f"{path.name}: unsupported format_version "
            f"{data.get('format_version')!r}"
        )

    ws_meta = data.get("workspace") or {}
    label = ws_meta.get("label") or "restored"
    tabs = list(data.get("tabs") or [])
    if not tabs:
        raise StashError(f"{path.name}: no tabs to restore")

    first = tabs[0]
    first_plan = first.get("layout") or {}
    first_panes = list(first_plan.get("panes") or [])
    if not first_panes:
        raise StashError(f"{path.name}: first tab has no panes")
    root_cwd = first_panes[0].get("cwd") or ws_meta.get("cwd") or os.path.expanduser("~")

    created = run_herdr(
        [
            "workspace",
            "create",
            "--label",
            str(label),
            "--cwd",
            str(root_cwd),
            "--no-focus",
        ]
    )
    result = result_of(created)
    workspace = result.get("workspace") or {}
    workspace_id = workspace.get("workspace_id")
    root_pane = result.get("root_pane") or {}
    root_pane_id = root_pane.get("pane_id")
    first_tab = result.get("tab") or {}
    first_tab_id = first_tab.get("tab_id")
    if not workspace_id or not root_pane_id or not first_tab_id:
        raise StashError("workspace create returned incomplete data")

    desired_label = first.get("label")
    if desired_label and desired_label != first_tab.get("label"):
        run_herdr(["tab", "rename", first_tab_id, str(desired_label)])

    apply_tab_layout(root_pane_id, first_plan, rerun_commands=rerun_commands)
    restored_tab_ids = [first_tab_id]

    for tab in tabs[1:]:
        plan = tab.get("layout") or {}
        panes = list(plan.get("panes") or [])
        if not panes:
            raise StashError(f"{path.name}: tab {tab.get('label')!r} has no panes")
        tab_cwd = panes[0]["cwd"]
        tab_label = str(tab.get("label") or "tab")
        tab_created = run_herdr(
            [
                "tab",
                "create",
                "--workspace",
                workspace_id,
                "--label",
                tab_label,
                "--cwd",
                str(tab_cwd),
                "--no-focus",
            ]
        )
        tab_result = result_of(tab_created)
        new_tab = tab_result.get("tab") or {}
        new_tab_id = new_tab.get("tab_id")
        new_root = tab_result.get("root_pane") or {}
        new_root_id = new_root.get("pane_id")
        if not new_tab_id or not new_root_id:
            raise StashError("tab create returned incomplete data")
        apply_tab_layout(new_root_id, plan, rerun_commands=rerun_commands)
        restored_tab_ids.append(new_tab_id)

    active_index = int(ws_meta.get("active_tab_index") or 0)
    if 0 <= active_index < len(restored_tab_ids):
        run_herdr(["tab", "focus", restored_tab_ids[active_index]], check=False)
    run_herdr(["workspace", "focus", workspace_id], check=False)
    return str(label)


def cmd_hide() -> int:
    workspace_id = resolve_workspace_id()
    workspaces = list_workspaces()
    if len(workspaces) <= 1:
        raise StashError(
            "refusing to hide the last remaining workspace in this session"
        )
    if not any(w.get("workspace_id") == workspace_id for w in workspaces):
        raise StashError(f"workspace {workspace_id} not found in session")

    data = capture_workspace(workspace_id)
    path = write_stash_file(data)
    # Close only after durable, validated save.
    run_herdr(["workspace", "close", workspace_id])
    label = data["workspace"]["label"]
    print(f"hid workspace {label!r} -> {path}")
    return 0


def cmd_restore() -> int:
    cfg = ensure_config()
    files = sorted(hidden_dir().glob("*.json"))
    if not files:
        print("nothing hidden — no stashed workspaces to restore")
        return 0

    restored = 0
    failed = 0
    for path in files:
        try:
            label = restore_one(path, rerun_commands=bool(cfg["rerun_commands"]))
            path.unlink()
            print(f"restored workspace {label!r} (removed {path.name})")
            restored += 1
        except Exception as exc:  # noqa: BLE001 - keep going across files
            failed += 1
            print(f"failed to restore {path.name}: {exc}", file=sys.stderr)

    if restored == 0 and failed:
        return 1
    if failed:
        print(f"restored {restored}, failed {failed}", file=sys.stderr)
        return 1
    print(f"restored {restored} workspace(s)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"hide", "restore"}:
        print(f"usage: {argv[0]} hide|restore", file=sys.stderr)
        return 2
    ensure_config()
    try:
        if argv[1] == "hide":
            return cmd_hide()
        return cmd_restore()
    except StashError as exc:
        print(f"herdr-stash: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"herdr-stash: unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
