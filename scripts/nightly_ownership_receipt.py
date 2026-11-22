"""Emit finite source-bound Actions ownership evidence; not an independent tag-creator attestation."""
import json
from contextlib import contextmanager
import os
from pathlib import Path, PureWindowsPath
import re
import nightly_feed as n

FIELDS = {'schema','app','repository','tag','source_commit','release_id','run_id','run_attempt','tooling_revision'}

def receipt(ident, release, environment):
    n.require(environment.get('GITHUB_REPOSITORY') == ident['repository'] == 'storytold/photocraft' and
              environment.get('GITHUB_REF') == 'refs/heads/main' and
              environment.get('GITHUB_EVENT_NAME') in {'schedule','workflow_dispatch'} and
              environment.get('GITHUB_SHA') == ident['source_commit'], 'ownership context mismatch')
    tooling = environment.get('TOOLING_REVISION','')
    n.require(n.HEX40.fullmatch(tooling) and tooling != '0'*40, 'ownership tooling pin unresolved')
    run = environment.get('GITHUB_RUN_ID',''); attempt = environment.get('GITHUB_RUN_ATTEMPT','')
    n.require(re.fullmatch('[1-9][0-9]{0,18}',run) and re.fullmatch('[1-9][0-9]{0,18}',attempt), 'ownership run identity missing')
    n.require(ident['tag'].endswith('.'+run+'.'+attempt+'.g'+ident['source_commit'][:12]), 'ownership tag run binding differs')
    n.require(release.get('tag_name') == ident['tag'] and release.get('target_commitish') == ident['source_commit'] and
              release.get('prerelease') is True and release.get('draft') is False, 'ownership publication not final/exact')
    n.positive(release.get('id'),'ownership release ID')
    return {'schema':'craft-nightly-ownership/v1','app':ident['app'],'repository':ident['repository'],
            'tag':ident['tag'],'source_commit':ident['source_commit'],'release_id':release['id'],
            'run_id':int(run),'run_attempt':int(attempt),'tooling_revision':tooling}

def owned_output(path):
    """New files may only be created beneath this job's workspace or runner temp.

    CLI path strings are not authority. Fresh trusted jobs run no concurrent
    untrusted code; reject internal symlinks as well as traversal before IO.
    """
    n.require(isinstance(path, (str, Path)) and str(path) and
        not any(char in str(path) for char in ("\0", "\r", "\n")), "invalid output path")
    path = Path(path)
    n.require(".." not in path.parts and ".." not in PureWindowsPath(str(path)).parts, "output traversal refused")
    path = path.absolute()
    roots = [Path(os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))).resolve()]
    if os.environ.get("RUNNER_TEMP"):
        roots.append(Path(os.environ["RUNNER_TEMP"]).resolve())
    for root in roots:
        if path != root and path.is_relative_to(root):
            current = root
            for part in path.relative_to(root).parts:
                current = current/part
                n.require(not current.is_symlink(), "symlink output path refused")
            n.require(path.resolve().is_relative_to(root), "output escaped owned job directory")
            return path
    raise n.InvalidNightly("output must be beneath the owned workspace or runner temp")



@contextmanager
def reserve(directory, *, filename="ownership.json"):
    n.require(filename in ("ownership.json", "ownership.pending.json"), "unreviewed ownership filename")
    output=owned_output(directory)
    n.require(not output.exists() and not output.is_symlink(),'ownership output must be new')
    output.mkdir(parents=True,exist_ok=False)
    with (output/filename).open('x',encoding='utf-8') as stream:
        yield stream


def emit(ident, release, environment, directory, *, reserved_stream=None):
    data = receipt(ident,release,environment)
    if reserved_stream is None:
        with reserve(directory) as stream:
            return emit(ident,release,environment,directory,reserved_stream=stream)
    path=Path(directory)/'ownership.json'
    n.require(not reserved_stream.closed and reserved_stream.writable() and
              Path(reserved_stream.name).absolute()==path.absolute(),'ownership reservation differs')
    json.dump(data,reserved_stream,sort_keys=True,separators=(',',':'))
    reserved_stream.write('\n');reserved_stream.flush();os.fsync(reserved_stream.fileno())
    if destination:=environment.get('GITHUB_OUTPUT'):
        with open(destination,'a',encoding='utf-8') as stream:
            stream.write('release-id='+str(data['release_id'])+'\nownership-artifact-name=nightly-ownership-'+data['app']+'-'+str(data['run_id'])+'-'+str(data['run_attempt'])+'\n')
    return data
