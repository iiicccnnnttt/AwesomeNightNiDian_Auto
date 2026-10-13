import json
import os
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from auto_location.single_user import ACCOUNT, ensure_key, run_flow
from auto_location.accounts import load_accounts
from auto_location.checkin import CheckinError
from auto_location.vault import CredentialVault


class SingleUserTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.accounts = self.state / 'accounts.json'
        self.key = self.state / 'fixture-key'
        ensure_key(self.key, False)
        self.vault = CredentialVault(self.state / 'credentials', self.key)

    def tearDown(self):
        self.temp.cleanup()

    def initialize(self, **kwargs):
        with patch('builtins.input', return_value='fixture-user'), patch(
                'auto_location.single_user.getpass.getpass', return_value='fixture-password'):
            return run_flow(self.accounts, self.vault, self.state, **kwargs)

    def test_first_login_gui_precedes_checkin_and_credentials_are_encrypted(self):
        with patch('auto_location.single_user.run_cli', return_value=0) as cli:
            self.assertEqual(self.initialize(area_index=2), 0)
        calls = [call.args[0] for call in cli.call_args_list]
        self.assertEqual([call[0] for call in calls], ['login', 'checkin'])
        self.assertIn('--interactive-login', calls[0])
        self.assertIn('--auto-location', calls[1])
        self.assertIn('--simulate-miniprogram', calls[1])
        self.assertEqual(calls[1][calls[1].index('--area-index') + 1], '2')
        self.assertTrue((self.state / 'initialized').exists())
        self.assertEqual(load_accounts(self.accounts)[0]['id'], ACCOUNT)
        self.assertEqual(self.vault.read(ACCOUNT), ('fixture-user', 'fixture-password'))
        for path in (self.accounts, self.vault._path(ACCOUNT)):
            self.assertNotIn('fixture-password', path.read_text(encoding='utf-8'))

    def test_later_runs_auto_login_without_prompt_and_never_repeat_checkin(self):
        with patch('auto_location.single_user.run_cli', return_value=0):
            self.initialize(login_only=True)
        with patch('builtins.input', side_effect=AssertionError('unexpected prompt')), patch(
                'auto_location.single_user.run_cli', side_effect=[0, 1]) as cli:
            self.assertEqual(run_flow(self.accounts, self.vault, self.state, headless=True), 1)
        calls = [call.args[0] for call in cli.call_args_list]
        self.assertEqual([call[0] for call in calls], ['login', 'checkin'])
        self.assertIn('--headless', calls[0])
        self.assertIn('--headless', calls[1])

    def test_headless_challenge_stops_and_gui_mode_allows_verification(self):
        with patch('auto_location.single_user.run_cli', return_value=0):
            self.initialize(login_only=True)
        def challenge(command):
            if '--headless' in command:
                (self.state / 'login-result.json').write_text(json.dumps(
                    {'status': 'failed', 'error': '统一认证需要验证码或二次认证'}), encoding='utf-8')
                return 1
            return 0
        with patch('auto_location.single_user.run_cli', side_effect=challenge) as cli:
            self.assertEqual(run_flow(self.accounts, self.vault, self.state, headless=True), 1)
        calls = [call.args[0] for call in cli.call_args_list]
        self.assertEqual([call[0] for call in calls], ['login'])
        with patch('auto_location.single_user.run_cli', return_value=0) as cli:
            self.assertEqual(run_flow(self.accounts, self.vault, self.state), 0)
        calls = [call.args[0] for call in cli.call_args_list]
        self.assertEqual([call[0] for call in calls], ['login', 'checkin'])
        self.assertIn('--interactive-login', calls[0])
        self.assertNotIn('--headless', calls[1])
        (self.state / 'login-result.json').write_text(json.dumps(
            {'status': 'failed', 'error': '统一认证未通过；请检查账号密码。未自动重试。'}), encoding='utf-8')
        with patch('auto_location.single_user.run_cli', return_value=1) as cli:
            self.assertEqual(run_flow(self.accounts, self.vault, self.state), 1)
            self.assertEqual(cli.call_count, 1)

    def test_uninitialized_background_never_prompts_or_launches_browser(self):
        with patch('builtins.input', side_effect=AssertionError('prompt')), patch(
                'auto_location.single_user.getpass.getpass', side_effect=AssertionError('password prompt')), patch(
                'auto_location.single_user.run_cli') as cli:
            with self.assertRaises(CheckinError):
                run_flow(self.accounts, self.vault, self.state, headless=True)
            cli.assert_not_called()
        self.assertFalse(self.accounts.exists())

    def test_background_main_writes_failed_summary_without_initializing_key(self):
        from auto_location.single_user import main
        with patch('auto_location.single_user.ROOT', self.state), patch(
                'auto_location.single_user.ensure_browser') as browser:
            self.assertEqual(main(['--headless']), 1)
            browser.assert_not_called()
        self.assertFalse((self.state / '.secrets/single-user/vault.key').exists())
        summary = json.loads((self.state / '.state/single-user/run-result.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['mode'], 'headless')
        self.assertEqual(summary['exit_code'], 1)
        self.assertFalse((self.state / '.state/single-user/one-click.lock').exists())

    def test_first_login_failure_never_checks_in_or_marks_initialized(self):
        with patch('auto_location.single_user.run_cli', return_value=1) as cli:
            self.assertEqual(self.initialize(), 1)
            self.assertEqual(cli.call_count, 1)
        self.assertFalse((self.state / 'initialized').exists())

    def test_missing_or_invalid_key_never_replaces_existing_secrets(self):
        original = self.key.read_bytes()
        ensure_key(self.key, True)
        self.assertEqual(self.key.read_bytes(), original)
        self.key.unlink()
        with self.assertRaises(CheckinError):
            ensure_key(self.key, True)
        self.assertFalse(self.key.exists())
        self.key.write_bytes(b'invalid')
        with self.assertRaises(CheckinError):
            ensure_key(self.key, False)
        self.assertEqual(self.key.read_bytes(), b'invalid')

    def test_local_gui_login_bypasses_private_headless_phase_and_disables_saving(self):
        from auto_location.cli import parser, session
        from auto_location.accounts import add_account, account_by_id
        add_account(self.accounts, ACCOUNT)
        self.vault.save(ACCOUNT, 'fixture-user', 'fixture-password')
        args = parser().parse_args(['login', '--accounts', str(self.accounts), '--account-id', ACCOUNT,
                                    '--auto-login', '--interactive-login'])
        args.profile = account_by_id(self.accounts, ACCOUNT)['profile']
        args.profile.mkdir(parents=True)
        (args.profile / 'prefs.js').write_text('fixture initialized')
        launches = []
        @contextmanager
        def browser(**options):
            launches.append(options)
            yield 'fixture-context'
        with patch('auto_location.cli.Camoufox', browser), patch('auto_location.cli.launch_path',
                return_value=self.state / 'fixture-browser'), patch('auto_location.identities.profile_identity',
                return_value={'id': 'fixture', 'config': {}}):
            with session(args, None) as context:
                self.assertEqual(context, 'fixture-context')
        self.assertEqual(len(launches), 1)
        self.assertFalse(launches[0]['headless'])
        self.assertFalse(launches[0]['firefox_user_prefs']['signon.rememberSignons'])
        launches.clear()
        args.interactive_login = False
        args.headless = True
        with patch('auto_location.cli.Camoufox', browser), patch('auto_location.cli.launch_path',
                return_value=self.state / 'fixture-browser'), patch('auto_location.identities.profile_identity',
                return_value={'id': 'fixture', 'config': {}}):
            with session(args, None):
                pass
        self.assertEqual(len(launches), 1)
        self.assertTrue(launches[0]['headless'])
