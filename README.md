# herdr-stash

Herdr has no built-in way to tuck a workspace aside without closing it and losing its layout. **herdr-stash** saves the current workspace (tabs, nested splits, cwds, labels) to JSON, then closes it; later it recreates every hidden workspace in the current session.

## Requirements

- Herdr **0.8.0+** (`min_herdr_version`)
- Python 3 (standard library only)

## Installation

```sh
herdr plugin install AndrijaSkontra/herdr-stash
```

Or while developing from a checkout:

```sh
herdr plugin link /path/to/herdr-stash
```

Confirm it loaded:

```sh
herdr plugin action list --plugin stash
```

## Keybinding setup

Plugins cannot ship default keybindings. Add this to `~/.config/herdr/config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+H"
type = "plugin_action"
command = "stash.hide"
description = "hide (stash) current workspace"

[[keys.command]]
key = "prefix+B"
type = "plugin_action"
command = "stash.restore"
description = "bring back all hidden workspaces"
```

### Default binding conflicts

Herdr’s defaults use the **lowercase** letters:

| Default | Binding |
| --- | --- |
| `focus_pane_left` | `prefix+h` |
| `toggle_sidebar` | `prefix+b` |

`prefix+H` / `prefix+B` (shifted) avoid those defaults. If you prefer lowercase `prefix+h` / `prefix+b`, clear or rebind `focus_pane_left` and `toggle_sidebar` first (and any custom uses such as `split_horizontal = "prefix+h"`).

## Usage

| Action | Key (after setup) | CLI |
| --- | --- | --- |
| Hide current workspace | `prefix+H` | `herdr plugin action invoke stash.hide` |
| Restore all hidden workspaces | `prefix+B` | `herdr plugin action invoke stash.restore` |

Hide refuses to run when the session has only one workspace left.

Stash files live under the plugin config directory:

```sh
herdr plugin config-dir stash
# …/hidden/<timestamp>_<label>_<id>.json
```

Optional config in that same directory (`config.toml`):

```toml
rerun_commands = false
```

When `rerun_commands = true`, restore will also `herdr pane run` any non-shell foreground command that was captured. Default is `false`.

## What is and isn’t restored

**Restored:** workspace label, tab labels/order, nested split tree (direction + ratio), each pane’s cwd, and (optionally) captured foreground commands.

**Not restored:** running processes. Closing a workspace kills its panes. Only the layout (and optional commands) come back.

If you need processes to stay alive, use a named session instead:

```sh
herdr session attach <name>
```

## Troubleshooting

```sh
herdr plugin action list --plugin stash
herdr plugin log list --plugin stash --limit 20
herdr plugin list --plugin stash
```

Invoke actions manually to see stdout/stderr without keybindings. Failed restores leave their JSON files in place so you can retry.

## License

MIT
