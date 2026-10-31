# Database migrations

Run from the project root:

```powershell
backend\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
```

`DATABASE_URL` is resolved by `app.core.config.Settings`, so relative SQLite paths are
anchored to the project root rather than the caller's working directory.

To inspect SQL without touching a database:

```powershell
backend\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head --sql
```

