# Rapid Inbox

Rapid Inbox is a local-first inbound mailbox service. It receives mail over SMTP, stores raw messages and metadata on disk, and exposes public and admin HTTP surfaces for browsing and operations.

## Local Development

1. `python3.12 -m venv .venv`
2. `.venv/bin/pip install -c constraints-dev.txt -e .[dev]`
3. `.venv/bin/rapid-inbox-http`
4. Open `http://127.0.0.1:8000/admin/login`

The default HTTP launcher starts the FastAPI app and an embedded SMTP listener in one process, using the current working directory as the storage root. Running it from the repository root creates `./storage/` and `./storage/app.db`.
If you need a standalone SMTP listener for a custom setup, you can still run `.venv/bin/rapid-inbox-smtp` in a separate terminal.

## Defaults

The startup defaults live in `app/config.py`, and the launchers now automatically load `.env` from the current working directory before falling back to code defaults:

- Bootstrap admin username: `admin`
- Bootstrap admin password: `change-me-now`
- Session cookie name: `rapid_inbox_session`
- HTTP host and port: `127.0.0.1:8000`
- SMTP host and port: `127.0.0.1:25`
- Max message size: `52428800`
- Max recipients per message: `20`

The default launcher flow creates the bootstrap admin with username `admin` and password `change-me-now`, so the login step is immediately usable on a fresh local checkout.

Configuration priority is:

1. Real environment variables
2. `.env` in the project root / current working directory
3. Code defaults in `app/config.py`

That means you can copy `.env.example` to `.env`, edit it, and the default `rapid-inbox-http` / `rapid-inbox-smtp` launchers will pick it up automatically.

## Dependency Pins

The direct dependencies in `pyproject.toml` are pinned to exact versions, and `constraints-dev.txt` contains the full tested dependency set used by the development environment.

If you want to minimize pip backtracking and repeated resolver retries, prefer:

` .venv/bin/pip install -c constraints-dev.txt -e .[dev] `

## Notes

- The HTTP runner starts the FastAPI app and the embedded `aiosmtpd` listener with Uvicorn.
- The SMTP runner starts the standalone `aiosmtpd` listener and keeps it alive until interrupted.
- The admin login page uses the bootstrap admin credentials created on startup.
