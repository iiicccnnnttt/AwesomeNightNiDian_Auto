import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from auto_location.vault import CredentialVault
from auto_location.checkin import CheckinError


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = self.root / 'secrets' / 'vault.key'
        self.key.parent.mkdir()
        self.key.write_bytes(os.urandom(32))
        self.vault = CredentialVault(self.root / 'data' / 'credentials', self.key)

    def tearDown(self):
        self.temp.cleanup()

    def test_encrypt_replacement_restart_and_no_plaintext(self):
        self.vault.save('alice','private-user','private-password')
        first = (self.vault.root / 'alice.json').read_text()
        self.assertNotIn('private-user', first)
        self.assertNotIn('private-password', first)
        self.assertEqual(CredentialVault(self.vault.root,self.key).read('ALICE'),('private-user','private-password'))
        self.vault.save('alice','private-user','private-password')
        second = (self.vault.root / 'alice.json').read_text()
        self.assertNotEqual(json.loads(first)['nonce'],json.loads(second)['nonce'])
        if os.name != 'nt':
            self.assertEqual((self.vault.root / 'alice.json').stat().st_mode & 0o777,0o600)

    def test_tamper_swap_and_wrong_key_fail_closed(self):
        self.vault.save('alice','private-user','private-password')
        path = self.vault.root / 'alice.json'
        original = path.read_bytes()
        shutil.copyfile(path,self.vault.root / 'bob.json')
        with self.assertRaises(CheckinError):
            self.vault.read('bob')
        value=json.loads(original)
        value['ciphertext']='A'+value['ciphertext'][1:]
        if value['ciphertext']==json.loads(original)['ciphertext']:
            value['ciphertext']='B'+value['ciphertext'][1:]
        path.write_text(json.dumps(value))
        with self.assertRaises(CheckinError) as caught:
            self.vault.read('alice')
        self.assertNotIn('private-password',str(caught.exception))
        path.write_bytes(original)
        self.key.write_bytes(os.urandom(32))
        with self.assertRaises(CheckinError):
            self.vault.read('alice')

    def test_missing_key_does_not_overwrite_ciphertext_or_generate_key(self):
        self.vault.save('alice','private-user','private-password')
        original=(self.vault.root / 'alice.json').read_bytes()
        self.key.unlink()
        with self.assertRaises(CheckinError):
            self.vault.save('alice','new-user','new-password')
        self.assertEqual((self.vault.root / 'alice.json').read_bytes(),original)
        self.assertFalse(self.key.exists())

    def test_key_initializer_preserves_key_and_rejects_invalid_key(self):
        script=Path(__file__).resolve().parents[1] / 'docker' / 'init_vault_key.py'
        original=self.key.read_bytes()
        result=subprocess.run([sys.executable,str(script),str(self.key)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertEqual(self.key.read_bytes(),original)
        self.assertNotIn(original.hex(),result.stdout)
        self.key.write_bytes(b'bad-key')
        result=subprocess.run([sys.executable,str(script),str(self.key)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.key.read_bytes(),b'bad-key')
