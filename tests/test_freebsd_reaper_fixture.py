"""Trusted short-lived fixtures, not proof of complete adversarial containment."""
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = pathlib.Path(__file__).resolve().parents[1] / 'scripts/freebsd_reaper_fixture.c'


@unittest.skipUnless(sys.platform.startswith('freebsd'), 'native FreeBSD procctl fixture')
class ReaperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls.tmp.name)
        cls.exe = cls.root / 'reaper'
        subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(SOURCE),'-o',str(cls.exe)],check=True,timeout=30)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def invoke(self, budget, *command):
        return subprocess.run([str(self.exe),str(budget),*command],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=8)

    def test_exit_code_and_exec_failure(self):
        self.assertEqual(self.invoke(1000,'/bin/sh','-c','exit 17').returncode,17)
        self.assertEqual(self.invoke(1000,str(self.root/'missing')).returncode,127)
        self.assertEqual(self.invoke(0,'/bin/true').returncode,125)

    def test_timeout(self):
        start=time.monotonic()
        result=self.invoke(100,'/bin/sleep','2')
        self.assertEqual(result.returncode,124,result.stderr)
        self.assertLess(time.monotonic()-start,3)

    def test_detached_descendant_after_timeout_and_success(self):
        for parent_mode in ('timeout','success'):
            with self.subTest(parent_mode=parent_mode), tempfile.TemporaryDirectory() as tmp:
                root=pathlib.Path(tmp); ready=root/'ready'; late=root/'late'
                # Double fork + setsid defeats the previous process-group-only approach.
                # Even on regression, this trusted leaf exits after its short marker delay.
                code='''import os,pathlib,sys,time
ready,late,mode=sys.argv[1:]
r,w=os.pipe()
if os.fork()==0:
    os.close(r)
    os.setsid()
    if os.fork()!=0: os._exit(0)
    pathlib.Path(ready).write_text('ready')
    os.write(w,b'1');os.close(w)
    time.sleep(1.2)
    pathlib.Path(late).write_text('escaped')
    os._exit(0)
os.close(w)
assert os.read(r,1)==b'1'
os.close(r)
if mode=='timeout': time.sleep(2)
'''
                result=self.invoke(500,sys.executable,'-c',code,str(ready),str(late),parent_mode)
                self.assertTrue(ready.exists(),'fixture never created its detached descendant')
                self.assertEqual(result.returncode,124 if parent_mode=='timeout' else 0,result.stderr)
                time.sleep(1.3)
                self.assertFalse(late.exists(),'detached descendant escaped reaper cleanup')

    def test_supervisor_signal(self):
        process=subprocess.Popen([str(self.exe),'5000','/bin/sleep','2'])
        try:
            time.sleep(0.1)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=3),128+signal.SIGTERM)
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=3)


if __name__ == '__main__': unittest.main()
