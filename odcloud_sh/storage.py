"""Private, atomic local state. Passwords are never persisted."""

import json
import os
from pathlib import Path
import tempfile

from .errors import ClientError


def default_state_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "0dcloud-sh"


def private_dir(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ClientError("状态目录不能是符号链接。")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        raise ClientError("状态目录不属于当前用户。")
    path.chmod(0o700)
    return path


def atomic_write(path, text):
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, directory=None):
        self.directory = private_dir(directory or default_state_dir())

    def path(self, name):
        return self.directory / name

    def read(self, name="settings.json"):
        path = self.path(name)
        if not path.exists():
            return {}
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise ClientError("状态文件所有者或类型异常。")
        path.chmod(0o600)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise ClientError("状态文件损坏或无法读取。") from None
        if not isinstance(value, dict):
            raise ClientError("状态文件必须是 JSON 对象。")
        return value

    def write(self, value, name="settings.json"):
        atomic_write(self.path(name), json.dumps(value, ensure_ascii=False, indent=2) + "\n")

