# Standalone daily willhaben.at Grundstück agent

This standalone agent checks willhaben.at search pages every day and documents all proposals that match:

- listing appears to be a **Grundstück**
- **EUR/m² <= 2.00**

It does not depend on any existing repository code.

## 1) Setup

```bash
cd /home/runner/work/willhaben_land/willhaben_land/standalone_willhaben_agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Edit `config.json` and set one or more willhaben search URLs in `search_urls`.

## 2) Run once

```bash
python /home/runner/work/willhaben_land/willhaben_land/standalone_willhaben_agent/willhaben_land_agent.py --config /home/runner/work/willhaben_land/willhaben_land/standalone_willhaben_agent/config.json
```

## 3) Daily automation

A GitHub Actions workflow is included at:

- `/home/runner/work/willhaben_land/willhaben_land/.github/workflows/willhaben_land_agent.yml`

It runs daily at 06:00 UTC and can also be started manually.

## Output

The agent writes:

- SQLite DB: `standalone_willhaben_agent/data/proposals.db`
- Markdown report: `standalone_willhaben_agent/data/suitable_proposals.md`

The report contains all suitable proposals (deduplicated by URL) with first/last seen timestamps.

## Notes

- Website HTML can change, so selectors may need occasional updates.
- If a card has no explicit EUR/m² value, the agent computes it from total price and area when available.
