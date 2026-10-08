# Tyler AI Assistant

A Python personal assistant with a Telegram interface, specialist routing, project tools, and Docker packaging. Its core workflows are Linear-driven planning, approval-based job-search support, and calendar/task help. It is an evolving personal system, with external actions controlled by authorization and review gates.

## What is here

- Terminal and Telegram interfaces, persistent memory, and file tools.
- Project-scoped GitHub tools for proposing code changes and draft pull requests.
- Cost-aware model routing and bounded autonomous runs.
- A separate TylerOS worker that defaults to a deterministic, no-model briefing flow.

Integrations require your own authorized configuration. Optional autonomous and paid-model paths need explicit setup; they are not a claim of production readiness.

## Get started

Use the [setup guide](docs/REFERENCE.md#setup) for prerequisites, environment configuration, and installation, then follow the [terminal usage](docs/REFERENCE.md#usage), [Telegram bot](docs/REFERENCE.md#running-247-telegram-bot), or [multi-bot group](docs/REFERENCE.md#running-the-multi-bot-group-interface-group_botpy) instructions. Keep credentials in ignored environment files or your host's secret configuration.

## Tests

```bash
python -m unittest discover -s tests
```

See [test setup](docs/REFERENCE.md#running-the-tests) for dependency requirements. Tests use mocks for external services; a passing local suite does not verify live integrations.

## Project workflows

[PROJECT_WORKFLOWS.md](PROJECT_WORKFLOWS.md) explains project selection, Linear tasks, and proposed changes. The bundled registry links [Worthlane](https://github.com/tymedina100/worthlane) and [Card Tracker](https://github.com/tymedina100/card-tracker).

Worthlane retains the legacy `vantage` project key so saved selections, branch prefixes, and `LINEAR_PROJECT_ID_VANTAGE` continue to work. Its repository target is `tymedina100/worthlane`. If a deployment has a `DATA_DIR/projects.json` override, update that copy separately before rollout.

## Documentation

- [Full configuration and operating reference](docs/REFERENCE.md)
- [TylerOS worker and its opt-in boundaries](docs/REFERENCE.md#tyleros-runtime-worker)
- [Autonomous assistant design](docs/autonomous-assistant-design.md)
- [Current implementation assessment](docs/autonomous-assistant-assessment.md)
- [Revenue sprint workflow](docs/revenue-sprint.md)

The detailed reference retains the existing setup and operational documentation while keeping this front page focused on the project and how to evaluate it.
