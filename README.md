# Registry Agency Autonomous Name Scraper

GitHub Actions version of the Registry Agency name-search scraper.

## Repository layout

```text
.
├── scraper_registry_agency.py
├── .gitignore
├── README.md
└── .github/
    └── workflows/
        └── registry_agency_scraper.yml
```

The scraper stores its persistent state in `registry_agency_outputs/`:

- `100_percent_valid_uics.txt` — unique extracted UICs
- `processed_queries.txt` — fully completed search combinations
- `savegame_registry_agency.json` — resumable query/page position
- `CONTINUE_FLAG_REGISTRY_AGENCY` — tells Actions to start another run

The output directory is created automatically on first execution.
