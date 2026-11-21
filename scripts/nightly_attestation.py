#!/usr/bin/env python3
"""Select a producer-bound subject; pure computation stays offline, live boundary only reads workflow state."""
import argparse
import json
import os
from pathlib import Path
import nightly_feed as n
import nightly_policy as p
import publish_nightly as publisher


def subject(registry,prepared,policy,directory,environment):
    p.validate_prepared(registry,prepared,policy,environment)
    ident,metadata=publisher.prepare_packet(registry,prepared,directory,environment)
    targets=n.app_record(registry,'photocraft')['targets']
    n.require(len(targets)==1 and targets[0]['target']=={'os':'windows','arch':'x86_64','format':'portable_zip'},'attestation subject adapter not reviewed')
    filename=targets[0]['asset_pattern'].replace('{version}',prepared['version'])
    n.require(set(metadata)=={'NIGHTLY.json','SHA256SUMS.txt',filename},'attestation packet differs')
    return {'name':'https://github.com/'+ident['repository']+'/releases/download/'+ident['tag']+'/'+filename,
        'digest':'sha256:'+metadata[filename][1],'source_commit':ident['source_commit']}


def guarded_subject(api,registry,prepared,policy,directory,environment,config,*,identity_directory,identity_sha256,packet_sha256,zip_sha256,asset):
    """Only two read-only active-state GETs; no signing, attestation or writes."""
    import nightly_artifacts as bindings
    p.validate_prepared(registry,prepared,policy,environment,config)
    p.require_active_workflow(api,config,policy) # Before packet hashing/subject work.
    bindings.verify_chain(identity_directory,directory,identity_sha256,packet_sha256,zip_sha256,asset)
    result=subject(registry,prepared,policy,directory,environment)
    # LAST API read before main can expose any subject output. Cooperative,
    # not atomic against an administrator disabling after this read.
    p.require_active_workflow(api,config,policy)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('registry','identity','policy','directory'): parser.add_argument('--'+name,required=True)
    parser.add_argument('--identity-sha256',required=True); parser.add_argument('--packet-sha256',required=True)
    parser.add_argument('--zip-sha256',required=True); parser.add_argument('--asset',required=True)
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    try:
        import nightly_artifacts as bindings
        # Establish bounded identity/policy trust before reading any credential.
        bindings.verify(Path(args.identity).parent,'identity',args.identity_sha256)
        registry=n.bounded_json(args.registry); prepared=n.bounded_json(args.identity)
        policy=n.bounded_json(args.policy); config=n.bounded_json(args.config)
        p.validate_prepared(registry,prepared,policy,os.environ,config)
        p.publication_contract(config,policy) # Approved exact producer identity before token.
        repository=p.trusted_repository(registry,prepared['app'],os.environ)
        api=publisher.HTTPTransport(repository,os.environ.get('GH_TOKEN',''))
        result=guarded_subject(api,registry,prepared,policy,args.directory,os.environ,config,
            identity_directory=Path(args.identity).parent,identity_sha256=args.identity_sha256,
            packet_sha256=args.packet_sha256,zip_sha256=args.zip_sha256,asset=args.asset)
        if output:=os.environ.get('GITHUB_OUTPUT'):
            with open(output,'a') as stream:
                for key,value in result.items():
                    n.require('\n' not in value and '\r' not in value,'invalid attestation output'); stream.write(f'{key}={value}\n')
        print(json.dumps(result,sort_keys=True))
    except (publisher.APIError,ValueError,OSError,KeyError,TypeError) as error: parser.exit(1,f'nightly attestation subject refused: {error}\n')

if __name__=='__main__': main()
