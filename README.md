# Agent Zero CLI

Terminal chat interface for Agent Zero.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Editable installs update immediately when files change.

## Run

```bash
agentzero
```

Or:

```bash
python -m agent_zero_cli
```

No install option:

```bash
PYTHONPATH=cli/src python -m agent_zero_cli
```

## Configuration

Create a `.cli-config.json` in the current directory or in `~/.agentzero/`:

```json
{
  "instance_url": "http://localhost:5080",
  "theme": "dark"
}
```
