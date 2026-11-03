"""Prevent two app processes from recovering/writing the same data directory."""
from pathlib import Path


class InstanceLock:
    def __init__(self, base):
        self.path = Path(base) / 'data' / 'instance.lock'
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = self.path.open('a+b')
        self.file.seek(0)
        if self.file.read(1) == b'':
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            import os
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError('此数据目录已有运行中的实例，请关闭它或使用其他 --data-dir') from exc

    def close(self):
        self.file.close()
