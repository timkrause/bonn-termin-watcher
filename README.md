# Bonn Termin Watcher

Checks [termine.bonn.de](https://termine.bonn.de/m/dlz/extern/calendar/?uid=b91bb67b-15cf-44df-ab0b-96ebe25c1ae3&lang=de)
about every 5 minutes (every minute from 07:05 to 07:20, when the city frees up slots)
for a slot for the services and windows in `config.json`, books it, and pushes an alert via ntfy.

- `termin_watch.py`: one check (also runs locally on macOS with notifications)
- `run_loop.sh` + `.github/workflows/watch.yml`: keeps it running on GitHub Actions
- `data/`: state and check history, committed by the workflow
- `docs/index.html`: status page, served at https://timkrause.github.io/bonn-termin-watcher/

Contact details and the ntfy topic live in the repository secrets, never in the code.
Stop it with `gh workflow disable watch.yml -R timkrause/bonn-termin-watcher`.
