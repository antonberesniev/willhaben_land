# Standalone daily willhaben.at Grundstück agent

This standalone agent checks willhaben.at search pages every day and documents all proposals that match:

- listing appears to be a **Grundstück**
- **EUR/m² <= 2.00**

It does not depend on any existing repository code.

## 1) Setup

```bash
cd standalone_willhaben_agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json` and set one or more willhaben search URLs in `search_urls`.
(`config.json` is ignored by git to avoid committing personal configuration.)

## 2) Run once

```bash
python willhaben_land_agent.py --config config.json
```

## 3) Daily automation

A GitHub Actions workflow is included at:

- `.github/workflows/willhaben_land_agent.yml`

It runs daily at 06:00 UTC and can also be started manually.
If `config.json` is not in the repository, set repository secret
`WILLHABEN_AGENT_CONFIG_JSON` with the full JSON config.

## Output

The agent writes:

- SQLite DB: `standalone_willhaben_agent/data/proposals.db`
- Markdown report: `standalone_willhaben_agent/data/suitable_proposals.md`

The report contains all suitable proposals (deduplicated by URL) with first/last seen timestamps.

## Notes

- Website HTML can change, so selectors may need occasional updates.
- If a card has no explicit EUR/m² value, the agent computes it from total price and area when available.
