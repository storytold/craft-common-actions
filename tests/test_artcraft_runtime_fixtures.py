"""Runtime fixture setup stays opt-in and never reaches an application database."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
import yaml
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'actions/artcraft-runtime-fixtures/start-mysql.sh').read_text()

class FixtureTests(unittest.TestCase):
    def test_opt_in_only_test_jobs(self):
        w = yaml.load((ROOT / '.github/workflows/sonarcloud.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(w['on']['workflow_call']['inputs']['artcraft-runtime-fixtures']['default'], 'false')
        for job in ['coverage', 'sonarcloud']:
            step = next(x for x in w['jobs'][job]['steps'] if x.get('name') == 'Provision ArtCraft runtime fixtures')
            self.assertIn('inputs.artcraft-runtime-fixtures', step['if'])
        self.assertFalse(any(x.get('name') == 'Provision ArtCraft runtime fixtures' for x in w['jobs']['build']['steps']))

    def test_existing_socket_refused_before_commands(self):
        with tempfile.TemporaryDirectory() as td:
            socket = Path(td) / 'mysql.sock'
            socket.touch()
            script = SCRIPT.replace('socket=/tmp/mysql.sock', f'socket={socket}')
            p = subprocess.run(['bash', '-c', script], env={**os.environ, 'RUNNER_TEMP': td}, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('Refusing to replace', p.stderr)

    def test_network_and_configuration_are_disabled(self):
        self.assertIn('mysqld --no-defaults --initialize-insecure', SCRIPT)
        self.assertIn('nohup mysqld --no-defaults', SCRIPT)
        self.assertIn('--skip-networking --mysqlx=0', SCRIPT)
        self.assertIn('--protocol=SOCKET', SCRIPT)
        self.assertIn('SELECT @@datadir', SCRIPT)
        self.assertNotIn('DATABASE_URL', SCRIPT)
        self.assertNotIn('sudo mysqld', SCRIPT)

if __name__ == '__main__':
    unittest.main()
