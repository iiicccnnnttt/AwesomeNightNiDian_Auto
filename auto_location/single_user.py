"""Windows one-click flow with isolated encrypted credentials and GUI MFA."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from camoufox.pkgman import launch_path
from camoufox.exceptions import CamoufoxNotInstalled

from .accounts import add_account, account_by_id, update_account
from .checkin import CheckinError
from .cli import main as run_cli
from .vault import CredentialVault, remove_browser_logins

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'single-user'


def restrict_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        path.chmod(0o700)
        return
    identity = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, check=True)
    match = re.search(rb'S-1-(?:\d+-)*\d+', identity.stdout)
    if not match:
        raise CheckinError('无法识别当前 Windows 用户，未保存凭据。')
    sid = match.group().decode('ascii')
    result = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F'],
                            capture_output=True)
    if result.returncode:
        raise CheckinError('无法限制单人数据目录的 Windows 权限，未保存凭据。')


def ensure_key(path, configured):
    if not path.exists():
        if configured:
            raise CheckinError('已有加密凭据但本机密钥丢失；请恢复密钥，不要重新生成。')
        with path.open('xb') as stream:
            stream.write(os.urandom(32))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            path.chmod(0o600)
    if len(path.read_bytes()) != 32:
        raise CheckinError('本机密钥无效；请恢复原密钥。')


def ensure_browser():
    try:
        if Path(launch_path()).is_file():
            return
    except (FileNotFoundError, CamoufoxNotInstalled):
        pass
    print('首次使用：正在下载 Camoufox 浏览器，请等待。', flush=True)
    if subprocess.run([sys.executable, '-m', 'camoufox', 'fetch']).returncode:
        raise CheckinError('Camoufox 下载失败；请检查网络或下载代理后重试。')


def run_flow(accounts, vault, state, *, setup=False, login_only=False, area_index=None, headless=False):
    marker = state / 'initialized'
    if headless and (setup or not accounts.exists() or not marker.exists() or not vault.configured(ACCOUNT)):
        raise CheckinError('后台模式需要先运行单人登录初始化.cmd 完成凭据配置、首次验证和设备信任；不会弹出 GUI 或请求输入。')
    if not accounts.exists():
        add_account(accounts, ACCOUNT)
    account = account_by_id(accounts, ACCOUNT)
    if area_index is not None:
        update_account(accounts, ACCOUNT, True, True, area_index)
        account = account_by_id(accounts, ACCOUNT)
    if setup or not vault.configured(ACCOUNT):
        username = input('请输入学校账号（仅在本机输入）：').strip()
        password = getpass.getpass('请输入学校密码（输入不会显示）：')
        try:
            vault.save(ACCOUNT, username, password)
            remove_browser_logins(account['profile'])
        finally:
            del username, password
        marker.unlink(missing_ok=True)
    common = ['--accounts', str(accounts), '--account-id', ACCOUNT, '--auto-login']
    login_output = state / 'login-result.json'
    if not marker.exists():
        print('首次登录：即将打开 Camoufox；请完成二次验证并信任设备。', flush=True)
        code = run_cli(['login', *common, '--interactive-login', '--timeout', '600', '--output', str(login_output)])
    elif headless:
        print('正在自动登录并检查现有会话。', flush=True)
        code = run_cli(['login', *common, '--headless', '--output', str(login_output)])
        if code:
            print('后台登录未完成，已停止；请运行前台模式检查凭据或完成人工验证。不会弹出 GUI。', flush=True)
            return code
    else:
        print('前台观察模式：显示自动登录过程，如需验证请在浏览器中完成。', flush=True)
        code = run_cli(['login', *common, '--interactive-login', '--timeout', '600', '--output', str(login_output)])
    if code:
        return code
    marker.write_text('login confirmed\n', encoding='utf-8')
    if login_only:
        print('登录已确认。下次运行一键脚本可自动打卡。', flush=True)
        return 0
    print('登录已确认，开始打卡并刷新核验。', flush=True)
    command = ['checkin', *common, '--auto-location', '--simulate-miniprogram',
               '--output', str(state / 'checkin-result.json')]
    if headless:
        command.append('--headless')
    if account['area_index'] is not None:
        command.extend(['--area-index', str(account['area_index'])])
    return run_cli(command)


def main(argv=None):
    parser = argparse.ArgumentParser(description='单人登录初始化及一键打卡')
    parser.add_argument('--setup', action='store_true', help='重新设置加密凭据并打开首次登录 GUI')
    parser.add_argument('--login-only', action='store_true', help='只完成登录，不提交打卡')
    parser.add_argument('--area-index', type=int, help='多个允许区域时指定序号并保存')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--headless', action='store_true', help='后台无浏览器 GUI、无人工输入；需先完成初始化')
    mode.add_argument('--gui', action='store_true', help='前台显示登录及打卡过程，允许人工验证（默认）')
    args = parser.parse_args(argv)
    if args.area_index is not None and args.area_index < 0:
        parser.error('area-index 必须非负')
    if args.headless and args.setup:
        parser.error('后台模式不能配置凭据；请使用前台初始化脚本')
    state = ROOT / '.state' / 'single-user'
    secret = ROOT / '.secrets' / 'single-user'
    lock_path = state / 'one-click.lock'
    previous_key_path = os.environ.get('AUTO_LOCATION_VAULT_KEY_FILE')
    lock = None
    exit_code = 1
    error = None
    try:
        restrict_directory(state)
        restrict_directory(secret)
        try:
            lock = lock_path.open('x', encoding='utf-8')
        except FileExistsError:
            raise CheckinError('单人脚本已在运行；若上次异常退出，请确认浏览器已关闭后删除 .state/single-user/one-click.lock。') from None
        lock.write(str(os.getpid()))
        lock.flush()
        accounts = state / 'accounts.json'
        key = secret / 'vault.key'
        vault = CredentialVault(state / 'credentials', key)
        if args.headless and (not accounts.exists() or not (state / 'initialized').exists() or
                              not vault.configured(ACCOUNT) or not key.is_file()):
            raise CheckinError('后台模式尚未初始化；请先运行单人登录初始化.cmd，不会弹出 GUI 或请求输入。')
        ensure_key(key, vault.configured(ACCOUNT))
        os.environ['AUTO_LOCATION_VAULT_KEY_FILE'] = str(key)
        ensure_browser()
        exit_code = run_flow(accounts, vault, state, setup=args.setup, login_only=args.login_only,
                             area_index=args.area_index, headless=args.headless)
        return exit_code
    except KeyboardInterrupt:
        print('\n操作已取消；不会自动重试打卡。', file=sys.stderr)
        exit_code = 130
        return 130
    except (CheckinError, OSError, subprocess.SubprocessError) as exc:
        # Do not stringify subprocess arguments or credential-related errors.
        # CheckinError messages in this runner are fixed and contain no inputs.
        error = str(exc) if isinstance(exc, CheckinError) else '本机初始化失败，请检查目录权限、依赖和浏览器安装。'
        print(error, file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            from datetime import datetime
            try:
                result = {'mode': 'headless' if args.headless else 'gui',
                          'status': 'success' if exit_code == 0 else 'failed', 'exit_code': exit_code,
                          'login_only': args.login_only, 'error': error,
                          'finished_at': datetime.now().astimezone().isoformat()}
                temporary = state / 'run-result.json.tmp'
                temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(state / 'run-result.json')
            except OSError:
                print('无法保存本次运行摘要，请以退出码和任务日志为准。', file=sys.stderr)
        if lock is not None:
            lock.close()
            lock_path.unlink(missing_ok=True)
        if previous_key_path is None:
            os.environ.pop('AUTO_LOCATION_VAULT_KEY_FILE', None)
        else:
            os.environ['AUTO_LOCATION_VAULT_KEY_FILE'] = previous_key_path


if __name__ == '__main__':
    raise SystemExit(main())
