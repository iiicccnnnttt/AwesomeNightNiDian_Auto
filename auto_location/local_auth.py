"""First-device verification in the trusted user's native browser GUI."""


class LocalAuthentication:
    def __init__(self):
        self.announced = False

    def tick(self, page, before_submit=False):
        if before_submit:
            input('请在 Camoufox 中填写验证码（暂不点击登录），填写后在这里按回车继续：')
            return True
        if not self.announced:
            print('如出现二次验证，请直接在 Camoufox 中完成验证并信任设备；工具会等待登录结果。', flush=True)
            self.announced = True
        return False

    def close(self):
        pass
