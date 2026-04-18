# Rapid Inbox

Rapid Inbox is a local-first inbound mailbox service. It receives mail over SMTP, stores raw messages and metadata on disk, and exposes public and admin HTTP surfaces for browsing and operations.

## Local Development

1. `python3.12 -m venv .venv`
2. `.venv/bin/pip install -e .[dev]`
3. `.venv/bin/rapid-inbox-http`
4. `.venv/bin/rapid-inbox-smtp`
5. Open `http://127.0.0.1:8000/admin/login`

The default launchers use the current working directory as the storage root, so running them from the repository root creates `./storage/` and `./storage/app.db`.

## Defaults

The startup defaults live in `app/config.py` and are mirrored in `.env.example` for reference:

- Bootstrap admin username: `admin`
- Bootstrap admin password: generated at startup
- Session cookie name: `rapid_inbox_session`
- HTTP host and port: `127.0.0.1:8000`
- SMTP host and port: `127.0.0.1:2525`
- Max message size: `52428800`
- Max recipients per message: `20`

The app does not auto-load `.env` yet, so treat `.env.example` as a reference template unless you add your own loader. If you want a stable bootstrap admin password for manual testing, override `bootstrap_admin_password` when constructing `Settings` in a custom launcher.

## Notes

- The HTTP runner starts the FastAPI app with Uvicorn.
- The SMTP runner starts the `aiosmtpd` listener and keeps it alive until interrupted.
- The admin login page uses the bootstrap admin credentials created on startup.
