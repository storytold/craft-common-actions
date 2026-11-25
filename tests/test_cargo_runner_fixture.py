import hashlib
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts/cargo_runner_fixture.py'
spec = importlib.util.spec_from_file_location('runner', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.exe = self.root / 'fake'
        self.exe.write_text('#!/bin/sh\nexit 0\n')
        self.exe.chmod(0o755)
        self.entry = {'sha256': hashlib.sha256(self.exe.read_bytes()).hexdigest(), 'cwd': str(self.root), 'serial': True}
        self.manifest = {str(self.exe): self.entry}

    def check(self, args):
        return runner.checked_args(str(self.exe), args, self.manifest, self.root)[1]

    def test_serial_and_existing_argument(self):
        self.assertEqual(self.check(['--nocapture']), ['--nocapture', '--test-threads=1'])
        for args in (['--test-threads=1'], ['--test-threads', '1']):
            self.assertEqual(self.check(args), args)
        for args in (['--test-threads=2'], ['--test-threads'], ['--test-threads=1', '--test-threads=1']):
            with self.assertRaises(ValueError): self.check(args)

    def test_harnessless_passthrough(self):
        self.entry['serial'] = False
        args = ['--custom', 'two words', '--test-threads=9']
        self.assertEqual(self.check(args), args)

    def test_unknown_changed_wrong_cwd(self):
        with self.assertRaises(ValueError): runner.checked_args(str(self.exe), [], {}, self.root)
        with self.assertRaises(ValueError): runner.checked_args(str(self.exe), [], self.manifest, self.root.parent)
        self.exe.write_text('changed')
        with self.assertRaises(ValueError): self.check([])

    def test_exit_and_signal(self):
        for body, expected in [('exit 17', 17), ('kill -TERM $$', -signal.SIGTERM)]:
            self.exe.write_text('#!/bin/sh\n' + body + '\n')
            self.entry['sha256'] = hashlib.sha256(self.exe.read_bytes()).hexdigest()
            manifest = self.root / 'allow.json'
            manifest.write_text(json.dumps(self.manifest))
            result = subprocess.run([sys.executable, str(SCRIPT), str(self.exe)], cwd=self.root,
                                    env={**os.environ, 'FIXTURE_ALLOWLIST': str(manifest), 'FIXTURE_RUNNER_LOG': str(self.root/'runner.log')}, timeout=5,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, expected, result.stderr)


@unittest.skipUnless(os.environ.get('RUN_NATIVE_CARGO_FIXTURE') == '1', 'native Cargo fixture opt-in')
class NativeCargoTests(unittest.TestCase):
    def test_native_runner_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp).resolve()
            def write(name, content):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            write('Cargo.toml', '[workspace]\nmembers=["gpu", "other"]\nresolver="2"\n')
            write('gpu/Cargo.toml', '[package]\nname="fixture-gpu"\nversion="0.1.0"\nedition="2021"\n')
            write('other/Cargo.toml', '[package]\nname="fixture-other"\nversion="0.1.0"\nedition="2021"\n[[test]]\nname="custom"\nharness=false\n')
            # Native shared library in OUT_DIR exercises Cargo's runtime loader search.
            write('gpu/build.rs', '''use std::{env,fs,process::Command};
fn main(){ let out=env::var("OUT_DIR").unwrap(); let src=format!("{out}/fixture.c");
fs::write(&src,"int fixture_answer(void){return 42;}").unwrap();
assert!(Command::new("cc").args(["-shared","-fPIC",&src,"-o",&format!("{out}/libfixtureanswer.so")]).status().unwrap().success());
println!("cargo:rustc-link-search=native={out}"); println!("cargo:rustc-link-lib=dylib=fixtureanswer"); }
''')
            write('gpu/src/lib.rs', '''/// ```
/// assert_eq!(2+2,4);
/// ```
pub fn answer()->u32{42}
#[cfg(test)] mod tests {
use std::sync::atomic::{AtomicUsize,Ordering}; static ACTIVE:AtomicUsize=AtomicUsize::new(0);
extern "C" { fn fixture_answer()->i32; }
fn check(){ assert_eq!(std::env::current_dir().unwrap(),std::path::Path::new(env!("CARGO_MANIFEST_DIR")));
assert_eq!(std::env::var("FIXTURE_MARKER").unwrap(),"inherited"); assert_eq!(unsafe{fixture_answer()},42);
assert_eq!(ACTIVE.fetch_add(1,Ordering::SeqCst),0); std::thread::sleep(std::time::Duration::from_millis(80));
assert_eq!(ACTIVE.fetch_sub(1,Ordering::SeqCst),1); }
#[test] fn first(){check()} #[test] fn second(){check()}
}
''')
            write('gpu/tests/it.rs', '#[test] fn integration(){assert_eq!(fixture_gpu::answer(),42); assert_eq!(std::env::current_dir().unwrap(),std::path::Path::new(env!("CARGO_MANIFEST_DIR")));}\n')
            write('other/src/lib.rs', '#[test] fn other(){assert_eq!(std::env::var("FIXTURE_MARKER").unwrap(),"inherited");}\n')
            write('other/tests/custom.rs', 'fn main(){assert_eq!(std::env::args().skip(1).collect::<Vec<_>>(),Vec::<String>::new());}\n')
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(('CARGO_TARGET_', 'CARGO_BUILD_TARGET', 'RUSTFLAGS', 'CARGO_ENCODED_RUSTFLAGS')):
                    env.pop(key)
            env.update(CARGO_TARGET_DIR=str(root/'target'), CARGO_HOME=str(root/'cargo-home'), FIXTURE_MARKER='inherited')
            def run(args):
                # Tiny trusted dependency-free fixture; bound time and capture to disk.
                with tempfile.TemporaryFile() as output:
                    process=subprocess.Popen(args,cwd=root,env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                    try:
                        deadline=time.monotonic()+90
                        while process.poll() is None:
                            if os.fstat(output.fileno()).st_size > 2*1024*1024 or time.monotonic()>deadline:
                                raise subprocess.TimeoutExpired(args,90)
                            time.sleep(0.05)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL)
                        process.wait(timeout=5)
                        raise
                    size=output.tell()
                    self.assertLess(size,2*1024*1024)
                    output.seek(0); data=output.read().decode()
                    self.assertEqual(process.returncode,0,data)
                    return data
            version=run(['rustc','-vV'])
            print(version, end='')
            cargo_version=run(['cargo','-Vv'])
            print(cargo_version, end='')
            expected=os.environ.get('FIXTURE_EXPECTED_RUST_VERSION')
            if expected:
                self.assertEqual(version.splitlines()[0].split()[1],expected)
                self.assertEqual(cargo_version.splitlines()[0].split()[1],expected)
            host=next(line.split(': ',1)[1] for line in version.splitlines() if line.startswith('host: '))
            self.assertTrue(host.endswith(('unknown-freebsd','unknown-linux-gnu')),host)
            output=run(['cargo','test','--workspace','--lib','--tests','--offline','--no-run','--message-format=json'])
            manifest={}; identities=[]
            for line in output.splitlines():
                if not line.startswith('{'): continue
                record=json.loads(line)
                if record.get('reason')!='compiler-artifact' or not record.get('executable'): continue
                path=pathlib.Path(record['executable']).resolve()
                name=record['target']['name']; kind=record['target']['kind']
                gpu=name in ('fixture_gpu','it')
                self.assertIn((name,tuple(kind)), [('fixture_gpu',('lib',)),('it',('test',)),('fixture_other',('lib',)),('custom',('test',))])
                self.assertNotIn(str(path),manifest)
                manifest[str(path)]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'cwd':str(root/('gpu' if gpu else 'other')),'serial':gpu}
                identities.append(name)
            self.assertCountEqual(identities,['fixture_gpu','it','fixture_other','custom'])
            allow=root/'allow.json';allow.write_text(json.dumps(manifest))
            env['FIXTURE_ALLOWLIST']=str(allow)
            runner_log=root/'runner.log'
            env['FIXTURE_RUNNER_LOG']=str(runner_log)
            env['CARGO_TARGET_'+host.upper().replace('-','_')+'_RUNNER']=f'{sys.executable} {SCRIPT}'
            output=run(['cargo','test','--workspace','--lib','--tests','--offline'])
            self.assertIn('2 passed; 0 failed',output)
            self.assertEqual(output.count('1 passed; 0 failed'),2)
            calls=[json.loads(line) for line in runner_log.read_text().splitlines()]
            self.assertCountEqual([c['executable'] for c in calls],list(manifest))
            for call in calls:
                expected=['--test-threads=1'] if manifest[call['executable']]['serial'] else []
                self.assertEqual(call['args'],expected)
            # Cargo forwards the native runner to rustdoc too. Disable only our
            # fixture runner for the separate doctest phase, never widen allowlist.
            env.pop('CARGO_TARGET_'+host.upper().replace('-','_')+'_RUNNER')
            log_before_doc=runner_log.read_text()
            output=run(['cargo','test','--workspace','--doc','--offline','--','--test-threads=1'])
            self.assertIn('1 passed; 0 failed',output)
            self.assertEqual(runner_log.read_text(),log_before_doc)
            print('Native fixture: exact 4 executable identities, 4 lib/integration tests + harnessless target, 1 doctest passed')


if __name__ == '__main__': unittest.main()
