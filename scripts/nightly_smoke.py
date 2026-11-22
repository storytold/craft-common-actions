#!/usr/bin/env python3
"""Future native quality smoke only; never a profile/IPC/updater A/B proof."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time
import nightly_feed as n
from nightly_unix import verify_version_output

LIMIT=4096
TIMEOUT=15

def smoke(executable,profile_root,version):
    from nightly_policy import semver_key
    semver_key(version)
    executable=Path(executable); profile_root=Path(profile_root)
    n.require(executable.is_absolute() and executable.is_file() and not executable.is_symlink(),'explicit packaged GUI executable required')
    n.require(profile_root.is_absolute() and not profile_root.exists() and not profile_root.is_symlink(),'fresh absolute isolated smoke profile required')
    executable=executable.resolve(); profile_root=profile_root.resolve()
    n.require(executable.parent not in profile_root.parents and profile_root not in executable.parents,'smoke profile must be separate from the installation')
    profile_root.mkdir(parents=True,exist_ok=False)
    # No shell, descriptor-provided selector, startup files, version override or stable fallback.
    argv=[str(executable),'--launch-profile','nightly-v1','--profile-root',str(profile_root),'--version']
    environment={key:value for key,value in os.environ.items() if not key.startswith('PHOTOCRAFT_')}
    # File capture avoids pipe deadlocks and holds at most LIMIT+1 bytes in memory.
    # Polling is bounded to this one owned process; no CI-status waiter/retry loop.
    with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
        process=subprocess.Popen(argv,stdout=stdout,stderr=stderr,env=environment)
        deadline=time.monotonic()+TIMEOUT
        try:
            while process.poll() is None:
                n.require(time.monotonic()<deadline,'packaged version smoke timed out')
                n.require(all(os.fstat(stream.fileno()).st_size<=LIMIT for stream in (stdout,stderr)),'smoke output exceeded bound')
                time.sleep(0.02)
            outputs=[]
            for stream in (stdout,stderr):
                stream.seek(0); data=stream.read(LIMIT+1)
                n.require(len(data)<=LIMIT,'smoke output exceeded bound'); outputs.append(data)
            n.require(process.returncode==0,'packaged GUI --version failed')
            n.require(not outputs[1],'unexpected smoke stderr')
            verify_version_output(outputs[0].decode('utf-8'),version)
            return {'exact_version_and_flavor':True,'isolated_selector':'nightly-v1','runtime_qualified':False}
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=5)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',required=True); parser.add_argument('--profile-root',required=True); parser.add_argument('--version',required=True)
    args=parser.parse_args()
    try: print(smoke(args.executable,args.profile_root,args.version))
    except (ValueError,OSError,subprocess.SubprocessError) as error: parser.exit(1,f'packaged nightly smoke refused: {error}\n')

if __name__=='__main__': main()
