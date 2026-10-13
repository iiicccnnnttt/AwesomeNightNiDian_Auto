"""Restricted authentication controls; never expose credential fields or values."""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .auth import AUTH_HOST
from .checkin import CheckinError


class WebAuthentication:
    def __init__(self, state_path):
        self.path = Path(state_path)
        self.actions = queue.Queue()
        self.targets = {}
        self.previous = None
        self.error = None
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self):
        for line in sys.stdin:
            try:
                self.actions.put(json.loads(line))
            except ValueError:
                continue

    def publish(self, controls, phase="authenticating"):
        value = {"phase": phase, "controls": controls, "error": self.error}
        if value != self.previous:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)
            self.previous = value

    def tick(self, page, before_submit=False):
        controls, targets = [], {}
        # Only operate on the official authentication origin, including MFA
        # documents/iframes. No arbitrary selectors, JS, URLs or VNC input.
        for frame_index, frame in enumerate(page.frames):
            parsed = urlsplit(frame.url)
            if parsed.scheme != "https" or parsed.hostname != AUTH_HOST:
                continue
            candidates = frame.locator("input, button, a, [role=button], [onclick], .btn, .button")
            for index in range(candidates.count()):
                element = candidates.nth(index)
                if not element.is_visible() or not element.is_enabled():
                    continue
                info = element.evaluate("""el => ({tag:el.tagName.toLowerCase(),
                    type:(el.getAttribute('type')||'text').toLowerCase(),
                    hint:[el.id,el.name,el.getAttribute('placeholder'),el.getAttribute('aria-label')].join(' '),
                    text:el.tagName==='INPUT' ? (['button','submit'].includes(el.type) ? el.value : '') : (el.textContent||'').trim(),
                    label:el.labels ? Array.from(el.labels).map(x=>x.textContent).join(' ') : ''})""")
                hint = info['hint'] + ' ' + info['label']
                info['text'] = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', info['text'])
                # Explicitly exclude password/user/phone fields, including a
                # password input changed to type=text by a site's reveal UI.
                if re.search(r"password|username|account|密码|账号|手机号", hint, re.I) or info['type'] == 'password':
                    continue
                kind, label = None, None
                if info['tag'] == 'input' and info['type'] in ('text', 'tel', 'number') and re.search(
                        r"captcha|dynamicCode|otp|verify.?code|verification.?code|验证码|校验码|短信码", hint, re.I):
                    kind, label = 'code', '验证码'
                elif info['tag'] == 'input' and info['type'] == 'checkbox' and re.search(
                        r"trust|remember.?device|信任|记住.*设备", hint, re.I):
                    kind, label = 'trust', '信任此设备'
                elif (info['tag'] != 'input' or info['type'] in ('button', 'submit')) and len(info['text']) <= 48 and re.fullmatch(
                        r"(?:发送|获取|重新发送|重新获取)(?:短信)?(?:验证码|动态码)|验证(?:并登录|登录)?|提交(?:验证)?|确认(?:登录|验证)?|确定|继续|下一步|信任(?:此|该)?(?:设备|浏览器)|短信验证|邮箱验证|手机验证码|"
                        r"(?:Send|Get|Resend)(?: SMS)? code|Verify|Confirm|Continue|Trust (?:this )?device",
                        info['text'], re.I):
                    kind, label = 'button', info['text']
                elif info['text'] == '登录' and not frame.locator('input[type=password]:visible').count():
                    kind, label = 'button', '登录'
                if kind:
                    key = element.evaluate("el => el.dataset.autoLocationControl ||= crypto.randomUUID()")
                    controls.append({'id': key, 'kind': kind, 'label': label})
                    if kind == 'trust':
                        controls[-1]['checked'] = element.is_checked()
                    targets[key] = (element, kind)
        self.targets = targets
        proceed = False
        while not self.actions.empty():
            action = self.actions.get_nowait()
            if action.get('action') == 'cancel':
                raise CheckinError('用户取消了登录验证。')
            if action.get('action') == 'continue':
                if before_submit:
                    proceed = True
                    self.error = None
                else:
                    # After password submission, continue must click the MFA
                    # document's own verification button, not merely return a
                    # boolean that the outcome loop ignores.
                    submit_labels = {'验证', '验证登录', '验证并登录', '提交', '提交验证',
                                     '确认', '确认登录', '确认验证', '确定', '继续', '下一步',
                                     '登录', 'verify', 'confirm', 'continue'}
                    buttons = [targets[c['id']][0] for c in controls
                               if c['kind'] == 'button' and c['label'].lower() in submit_labels]
                    if len(buttons) == 1:
                        try:
                            buttons[0].click(timeout=3000)
                            self.error = None
                        except Exception:
                            self.error = '验证提交未完成，请检查当前页面。'
                    else:
                        self.error = '未找到唯一的验证提交按钮，请点击页面对应的验证按钮；当前页面可能尚未支持。'
                continue
            key = action.get('id')
            if key not in targets:
                self.error = '验证页面已变化，请根据当前画面重新操作。'
                continue
            element, kind = targets[key]
            try:
                if kind == 'code':
                    element.fill(action['value'])
                elif kind == 'trust':
                    element.set_checked(action['value'])
                else:
                    element.click(timeout=3000)
                self.error = None
            except Exception:
                self.error = '验证操作未完成，请检查当前页面后重试。'
        self.publish(controls, 'verification' if controls else 'authenticating')
        return proceed

    def close(self):
        self.publish([], 'closed')

    def review(self, page, timeout=120, failed=False):
        if failed:
            self.error = '登录未完成，请对照执行结果检查当前画面。'
        self.publish([], 'review')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.actions.empty() and self.actions.get().get('action') in ('cancel', 'continue'):
                break
            page.wait_for_timeout(200)
        self.close()
