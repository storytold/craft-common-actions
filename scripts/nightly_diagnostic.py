#!/usr/bin/env python3
"""Cargo-derived diagnostic metadata only. No publication policy/catalog API,
installable identity, target admission, artifact feed or transport."""
import argparse
import json
import os
from pathlib import Path
import tomllib
import nightly_feed as n

def diagnostic(registry,app,cargo,environment):
    n.trusted_context(registry,app,environment)
    with open(cargo,'rb') as stream: raw=stream.read(n.MAX_JSON_BYTES+1)
    n.require(len(raw)<=n.MAX_JSON_BYTES,'diagnostic Cargo metadata exceeds bound')
    manifest=tomllib.loads(raw.decode())
    base=manifest.get('workspace',{}).get('package',{}).get('version',manifest.get('package',{}).get('version'))
    n.require(isinstance(base,str),'diagnostic Cargo version missing')
    base=base.split('-',1)[0].split('+',1)[0]
    n.require(n.BASE.fullmatch(base),'diagnostic Cargo base must be canonical')
    run=int(environment.get('GITHUB_RUN_ID','0')); attempt=int(environment.get('GITHUB_RUN_ATTEMPT','0'))
    n.positive(run,'diagnostic run'); n.positive(attempt,'diagnostic attempt')
    version=f"{base}-nightly.diagnostic.{run}.{attempt}.g{environment['GITHUB_SHA'][:12]}"
    return {'schema':'craft-nightly-diagnostic/v1','app':app,'source_commit':environment['GITHUB_SHA'],'version':version,'ready':False,'installable':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry',required=True); parser.add_argument('--app',required=True); parser.add_argument('--cargo',default='Cargo.toml')
    args=parser.parse_args()
    try:
        result=diagnostic(n.bounded_json(args.registry),args.app,args.cargo,os.environ)
        if output:=os.environ.get('GITHUB_OUTPUT'):
            with open(output,'a',encoding='utf-8') as stream: stream.write('ready=false\nversion='+result['version']+'\n')
        print(json.dumps(result,sort_keys=True))
    except (n.InvalidNightly,OSError,ValueError,TypeError,KeyError) as error: parser.exit(1,f'diagnostic metadata refused: {error}\n')
if __name__=='__main__': main()
