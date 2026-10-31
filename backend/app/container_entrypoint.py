"""Nonroot, single-process container bootstrap and HTTP healthcheck CLI."""
import json
import os
import sys
import urllib.error
import urllib.request

from app.core.config import Settings


def main(arguments=None):
    arguments=list(sys.argv[1:] if arguments is None else arguments)
    settings=Settings.from_env()
    if arguments==['healthcheck']:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{settings.app_port}/api/health/runtime',timeout=5) as response:
                report=json.load(response)
            return 0 if report.get('application_ready') is True else 1
        except (OSError,ValueError,urllib.error.HTTPError):return 1
    if arguments not in ([],['doctor']):
        print('Use no arguments to start the application, or doctor/healthcheck. Admin commands require an explicit --entrypoint.',file=sys.stderr);return 2
    # Health probes must not cold-import SQLAlchemy/Alembic on every interval,
    # especially on ARM emulation or slower machines. Startup still runs every
    # storage/secret/migration prerequisite before launching Uvicorn.
    from app.core.errors import AppError
    from app.core.deployment import validate_container_startup
    try:
        checks=validate_container_startup(settings)
    except (AppError,OSError,ValueError) as error:
        print(json.dumps({'status':'STARTUP_FAILED','code':getattr(error,'code',type(error).__name__),
            'message':getattr(error,'message','Application configuration or persistent storage is invalid; inspect deployment settings.')},ensure_ascii=False),file=sys.stderr)
        return 1
    print(json.dumps({'status':'STARTUP_CHECKS_PASSED','checks':checks},ensure_ascii=False),flush=True)
    if arguments==['doctor']:return 0
    # The SQLite queue and local-GPU transaction lock are process-local. Do not
    # use a second uvicorn worker, reload process, or separate queue consumer.
    os.execv(sys.executable,[sys.executable,'-m','uvicorn','app.main:app','--host','0.0.0.0','--port',str(settings.app_port),
                            '--workers','1','--timeout-graceful-shutdown','30'])


if __name__=='__main__':raise SystemExit(main())
