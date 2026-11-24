# tau-rex-status

A Tau extension that reports the agent's state to [Rex](https://www.superlogical.com/rex) using the
[Program Status Protocol](https://www.superlogical.com/rex/docs/build/program-status) (OSC 7501).

Rex shows the status in tab headers, the session picker, and Lua
`terminal.program_status_changed` events. The extension leaves presentation to the terminal.

## Install

```bash
tau install git:github.com/alejandro-ao/tau-rex-status
```

Or load it without installing:

```bash
git clone https://github.com/alejandro-ao/tau-rex-status.git
tau -e ./tau-rex-status
```

Restart Tau or run `/reload` after installing.

## States

| Tau lifecycle | Reported state |
| --- | --- |
| Session started / cancelled run | `idle` |
| Model thinking, tool running, compaction, retry | `working` |
| `ask_user_question` tool waiting for a selection | `blocked` with `kind=question` |
| Agent run finished successfully | `done`, with the response preview as `msg` |
| Provider, retry, or compaction failure | `error` |
| Session reload, resume, new session, or quit | `clear` |

Reports use `app=tau`, replace Tau's root record, and include a session-name/cwd
`title`. Free-text values are sanitized to one line and capped to the protocol's
byte limits. Duplicate reports are not repeated.

## Disable

Set `TAU_PROGRAM_STATUS=0` (or `false`, `no`, `off`) to stop the extension from
writing escape sequences. Reports are also disabled automatically when stdout is
not a TTY or `TERM=dumb`.

## Requirements

- Tau with extension support
- A terminal that implements OSC 7501, such as Rex

The protocol is terminal-generic. Any program that understands OSC 7501 can use
these reports, not only Rex.
