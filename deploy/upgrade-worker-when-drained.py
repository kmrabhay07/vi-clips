"""Graceful OCI worker handoff: never kill an in-progress source analysis."""
import subprocess
import os
from pathlib import Path
import sys
import time

root=str(Path(os.environ.get('VI_DEPLOY_ROOT',Path(__file__).resolve().parents[1]))/'app')
container='vi-clips-oci-worker-1'
compose=['docker','compose','-p','vi-clips-oci','--env-file','../.env','-f','docker-compose.yml','-f','compose.oci.yml']
if '--wait-idle' in sys.argv:
    print('Waiting for analysis and previews to finish before requesting handoff',flush=True)
    code="from db import Session,Job; from sqlalchemy import select,func; import sys; db=Session(); sys.exit(1 if db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(['running','queued']))) else 0)"
    while subprocess.run(compose+['exec','-T','api','python','-c',code],cwd=root,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        time.sleep(15)
print('Waiting for current worker jobs to finish; no forced timeout',flush=True)
subprocess.run(['docker','stop','--time','-1',container],check=True)
subprocess.run(['docker','compose','-p','vi-clips-oci','--env-file','../.env',
                '-f','docker-compose.yml','-f','compose.oci.yml',
                'up','--no-build','--no-deps','-d','worker'],cwd=root,check=True)
print('Optimized worker activated',flush=True)
