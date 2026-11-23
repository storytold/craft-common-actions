#!/usr/bin/env python3
"""Finite cross-job bindings. Expected hashes/IDs come from producer job outputs,
never from a downloaded manifest or artifact name. No transport or credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import nightly_feed as n

SHA256=re.compile(r'[0-9a-f]{64}')
IDENTITY_FILES={'nightly-identity.json','nightly-producer-policy.json'}

def download_contract(artifact_id,expected_sha256):
    n.require(isinstance(artifact_id,str) and re.fullmatch(r'[1-9][0-9]{0,19}',artifact_id), 'exact single positive artifact ID required; no name/all-artifact fallback')
    n.require(int(artifact_id)<=9007199254740991,'artifact ID exceeds exact JavaScript numeric transport range')
    n.require(isinstance(expected_sha256,str) and SHA256.fullmatch(expected_sha256), 'independent producer SHA256 required')

LINUX_SUFFIX='-linux-x86_64-nightly.tar.gz'

def asset_names(asset,kind):
    """One exact ZIP for a build boundary; a packet may list every admitted platform (comma-separated).

    Only a packet may carry the admitted Linux tar.gz, always with its detached .asc.
    """
    n.require(isinstance(asset,str),'exact nightly ZIP asset required')
    names=asset.split(',') if kind=='packet' else [asset]
    n.require(0<len(names)<=8 and len(set(names))==len(names) and
              all(n.NAME.fullmatch(name) and 'nightly' in name and
                  (name.endswith('.zip') or (kind=='packet' and name.endswith(LINUX_SUFFIX))) for name in names),'exact nightly ZIP asset required')
    return names

def packet_signatures(assets):
    return {name+'.asc' for name in assets if name.endswith(LINUX_SUFFIX)}

def inventory(directory,kind,asset=None):
    directory=Path(directory)
    n.require(directory.is_dir() and not directory.is_symlink(),'artifact directory must be a regular directory')
    if kind=='identity': expected=IDENTITY_FILES; assets=set()
    else:
        assets=asset_names(asset,kind)
        expected=set(assets) if kind=='zip' else set(assets)|{'NIGHTLY.json','SHA256SUMS.txt'}|packet_signatures(assets)
    n.require(kind in ('identity','zip','packet'),'unknown artifact boundary')
    # Stop before retaining an attacker-controlled unbounded inventory.
    actual=set()
    for path in directory.iterdir():
        n.require(len(actual)<len(expected) and path.name in expected,'unexpected artifact file')
        actual.add(path.name)
    n.require(actual==expected,'missing artifact file')
    metadata={}; total=0
    for name in sorted(expected):
        if name not in assets:
            stream,info=n.regular_file(directory,name)
            with stream:
                n.require(info.st_size<=n.MAX_JSON_BYTES,'artifact metadata exceeds bound')
                raw=stream.read(n.MAX_JSON_BYTES+1); after=os.fstat(stream.fileno())
                stamp=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns)
                n.require(len(raw)==info.st_size and stamp(info)==stamp(after),'artifact metadata changed or exceeds bound')
                size,digest=len(raw),hashlib.sha256(raw).hexdigest()
        else: size,digest=n.digest_file(directory,name)
        total+=size
        n.require(total<=n.MAX_TOTAL_BYTES,'artifact boundary exceeds aggregate bound')
        metadata[name]={'size':size,'sha256':digest}
    return metadata

def fingerprint(directory,kind,asset=None):
    metadata=inventory(directory,kind,asset)
    if kind=='zip': return metadata[asset]['sha256'] # Literal original ZIP SHA256.
    # Domain separation, names, sizes and every digest bind the whole finite set.
    encoded=json.dumps({'schema':'craft-nightly-artifact-binding/v1','kind':kind,'files':metadata},sort_keys=True,separators=(',',':')).encode()
    return hashlib.sha256(encoded).hexdigest()

def verify(directory,kind,expected_sha256,asset=None,zip_sha256=None):
    n.require(isinstance(expected_sha256,str) and SHA256.fullmatch(expected_sha256),'independent producer SHA256 required')
    n.require(fingerprint(directory,kind,asset)==expected_sha256,kind+' artifact differs from its independent producer output')
    if kind=='packet':
        names=asset_names(asset,kind)
        hashes=zip_sha256.split(',') if isinstance(zip_sha256,str) else []
        n.require(len(hashes)==len(names) and all(SHA256.fullmatch(value) for value in hashes),'original ZIP producer binding required')
        # Each ZIP fingerprint is its original build job's hash, not a packet claim.
        for name,expected in zip(names,hashes):
            _,digest=n.digest_file(directory,name)
            n.require(digest==expected,'packet ZIP differs from original build producer output')
    return True

def verify_chain(identity_directory,packet_directory,identity_sha256,packet_sha256,zip_sha256,asset):
    verify(identity_directory,'identity',identity_sha256)
    verify(packet_directory,'packet',packet_sha256,asset,zip_sha256)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('download-contract','seal','verify'))
    parser.add_argument('--kind',choices=('identity','zip','packet'))
    parser.add_argument('--directory'); parser.add_argument('--asset'); parser.add_argument('--artifact-id')
    parser.add_argument('--expected-sha256'); parser.add_argument('--zip-sha256')
    args=parser.parse_args()
    try:
        if args.mode=='download-contract': download_contract(args.artifact_id,args.expected_sha256)
        else:
            n.require(args.directory and args.kind,'artifact kind/directory required')
            if args.mode=='verify': verify(args.directory,args.kind,args.expected_sha256,args.asset,args.zip_sha256)
            else:
                digest=fingerprint(args.directory,args.kind,args.asset)
                if args.kind=='packet': verify(args.directory,args.kind,digest,args.asset,args.zip_sha256)
                if output:=os.environ.get('GITHUB_OUTPUT'):
                    with open(output,'a',encoding='utf-8') as stream: stream.write('sha256='+digest+'\n')
                print(digest)
    except (n.InvalidNightly,OSError,ValueError,TypeError) as error: parser.exit(1,f'artifact binding refused: {error}\n')

if __name__=='__main__': main()
