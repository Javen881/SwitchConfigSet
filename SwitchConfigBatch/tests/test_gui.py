import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QDialog
from configbatch.gui import Window


def test_blank_default_and_demo_flow(tmp_path):
    app=QApplication.instance() or QApplication([]);w=Window(tmp_path);errors=[];w.error=errors.append
    w.show();app.processEvents()
    assert w.protocol.currentText()=='telnet'
    assert not w.main.toPlainText()
    w.load_demo();assert len(w.devices)==3 and w.protocol.currentText()=='mock'
    def approve():
        d=app.activeModalWidget()
        if isinstance(d,QDialog):d.accept()
    QTimer.singleShot(30,approve);w.start(True)
    end=time.monotonic()+15
    while w.worker and time.monotonic()<end:app.processEvents();time.sleep(.01)
    assert not w.worker and not errors,errors
    assert len(w.records)==3
    assert all(r['status']=='模拟验证完成' for r in w.records.values())
    w.grab().save(str(tmp_path/'gui.png'));w.close();app.processEvents()
