# Daily Brief

A small personal news digest that runs on GitHub Actions and emails one summary each morning.

- `brief.py`: checks news feeds and official web pages for new items, flags keywords, and emails the digest.
- `dispatch.py`: polls a public dispatch dashboard feed every 15 minutes and saves matching entries for the digest.
- `dashboard.py`: builds a password-protected dashboard page (encrypted; unlocked in the browser) published with GitHub Pages.
- `common.py`: loads the private configuration and encrypts saved state.

All configuration (places, keywords, sources) is supplied privately through repository secrets. Saved state is stored only in encrypted `.enc` files. This repo contains no personal or location data.

## Secrets used
| Secret | Purpose |
|---|---|
| `BRIEF_CONFIG` | JSON configuration (see `config.example.json`) |
| `STATE_KEY` | Encryption key for saved state |
| `GMAIL_USER` / `GMAIL_APP_PASSWORD` | Sending account |
| `BRIEF_TO` | Recipient(s), optional |
| `DASH_PASSWORD` | Password that unlocks the dashboard |
