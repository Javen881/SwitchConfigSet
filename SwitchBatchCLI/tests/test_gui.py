import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QDialog
from switchbatch.gui import Window


def test_gui_mock_execution_and_history(tmp_path):
    app=QApplication.instance() or QApplication([])
    window=Window(tmp_path)
    failures=[];window.error=failures.append
    window.show();app.processEvents()
    assert window.targets.rowCount()==2
    assert window.commands.rowCount()==3
    def accept():
        dialog=app.activeModalWidget()
        if isinstance(dialog,QDialog): dialog.accept()
    QTimer.singleShot(20,accept)
    window.start_batch()
    deadline=time.monotonic()+10
    while (window.busy or window.workers) and time.monotonic()<deadline:
        app.processEvents();time.sleep(.01)
    app.processEvents();window.refresh_live()
    assert not failures,failures
    assert not window.busy
    assert window.batch.store.task['status']=='完成'
    assert window.history.rowCount()==1
    assert window.progress.value()==6
    assert window.archive.export(window.batch.store.root).exists()
    window.grab().save(str(tmp_path/'gui-executed.png'))
    window.close();app.processEvents()
