import json
import queue
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QSplitter, QLabel, QPushButton, QPlainTextEdit, QLineEdit, QComboBox,
    QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QFileDialog, QMessageBox,
    QProgressBar, QHeaderView, QGroupBox, QDialog, QDialogButtonBox)
from .models import Target, Command, Settings, parse_targets, parse_commands
from .transports import Credentials, probe
from .engine import Batch
from .store import Archive


class Worker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.succeeded.emit(self.fn())
        except Exception as exc:
            self.failed.emit(f'{type(exc).__name__}: {exc}')


class Window(QMainWindow):
    def __init__(self, base):
        super().__init__()
        self.setWindowTitle('SwitchBatchCLI · 批量配置与信息采集')
        self.resize(1280, 900)
        self.archive = Archive(base)
        self.archive.recover()
        self.batch = None
        self.busy = False
        self.workers = []
        self.events = queue.Queue(maxsize=1000)
        self.skipped = 0
        self.sources = {}
        self.target_snapshot = ''
        self.command_snapshot = ''
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.build_setup()
        self.build_monitor()
        self.build_history()
        self.statusBar().showMessage('准备就绪 · 默认模拟模式，不连接真实设备')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_live)
        self.timer.start(100)
        self.refresh_history()

    def button(self, label, action, layout):
        b = QPushButton(label)
        b.clicked.connect(action)
        layout.addWidget(b)
        return b

    def spawn(self, fn, success, failure=None):
        worker = Worker(fn, self)
        self.workers.append(worker)
        worker.succeeded.connect(success)
        worker.failed.connect(failure or self.error)
        worker.finished.connect(lambda: self.workers.remove(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def error(self, message):
        QMessageBox.critical(self, '操作未完成', str(message))

    def build_setup(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel('批量下发与采集  /  01 准备 → 02 预览 → 03 执行 → 04 导出')
        heading.setStyleSheet('font-size:20px;font-weight:600;padding:10px;color:#244b72')
        layout.addWidget(heading)
        note = QLabel('目标需有唯一管理 IP；命令完成仅表示 CLI 流程完成，业务状态默认未验证。')
        layout.addWidget(note)
        split = QSplitter(Qt.Horizontal)
        left = QWidget(); lv = QVBoxLayout(left)
        lv.addWidget(QLabel('目标 IPv4 / CIDR（换行或逗号分隔，上限 1024）'))
        self.targets_text = QPlainTextEdit('192.0.2.10\n192.0.2.11')
        self.targets_text.setMaximumHeight(100)
        lv.addWidget(self.targets_text)
        bar = QHBoxLayout(); lv.addLayout(bar)
        self.button('导入目标 TXT', lambda: self.import_text(self.targets_text), bar)
        self.button('展开目标', self.expand_targets, bar)
        self.protocol = QComboBox(); self.protocol.addItems(['mock', 'ssh', 'telnet'])
        bar.addWidget(self.protocol)
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(22); bar.addWidget(self.port)
        self.protocol.currentTextChanged.connect(lambda t: self.port.setValue(23 if t == 'telnet' else 22))
        self.button('应用协议/端口', self.apply_connection, bar)
        self.targets = QTableWidget(0, 5)
        self.targets.setHorizontalHeaderLabels(['选择', 'IP', '协议', '端口', '探测结果'])
        self.targets.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.targets.horizontalHeader().setStretchLastSection(True)
        lv.addWidget(self.targets)
        self.probe_button = self.button('探测所选端口（不登录、不执行命令）', self.probe_targets, lv)
        lv.addWidget(QLabel('Telnet 明文传输；真实协议须自行确认目标与授权。模拟模式仅使用内存设备。'))
        right = QWidget(); rv = QVBoxLayout(right)
        rv.addWidget(QLabel('命令 · 一行一条，保持原文与顺序；不自动保存设备配置'))
        self.commands_text = QPlainTextEdit('show version\ndemo pages\nshow spanning-tree')
        self.commands_text.setMaximumHeight(140)
        rv.addWidget(self.commands_text)
        cmdbar = QHBoxLayout(); rv.addLayout(cmdbar)
        self.button('导入命令 TXT', lambda: self.import_text(self.commands_text), cmdbar)
        self.kind = QComboBox(); self.kind.addItems(['查询', '配置', '其他']); cmdbar.addWidget(self.kind)
        self.button('生成发送清单', self.preview_commands, cmdbar)
        self.commands = QTableWidget(0, 3)
        self.commands.setHorizontalHeaderLabels(['源行号', '类型（可逐条修改）', '命令原文'])
        self.commands.horizontalHeader().setStretchLastSection(True)
        rv.addWidget(self.commands)
        split.addWidget(left); split.addWidget(right)
        layout.addWidget(split, 1)
        connection = QGroupBox('连接与执行参数 · 凭据仅在内存使用'); form = QFormLayout(connection)
        row = QHBoxLayout()
        self.username = QLineEdit(); self.username.setPlaceholderText('用户名')
        self.password = QLineEdit(); self.password.setEchoMode(QLineEdit.Password); self.password.setPlaceholderText('登录密码（不落盘）')
        self.key = QLineEdit(); self.key.setPlaceholderText('可选SSH私钥路径（不写任务快照）')
        row.addWidget(self.username); row.addWidget(self.password); row.addWidget(self.key)
        form.addRow('认证', row)
        row2 = QHBoxLayout()
        self.concurrent = QSpinBox(); self.concurrent.setRange(1,32); self.concurrent.setValue(5)
        self.timeout = QSpinBox(); self.timeout.setRange(1,3600); self.timeout.setValue(30)
        self.connect_timeout = QSpinBox(); self.connect_timeout.setRange(1,600); self.connect_timeout.setValue(10)
        self.encoding = QComboBox(); self.encoding.setEditable(True); self.encoding.addItems(['utf-8','gb18030','latin-1'])
        for label, widget in [('并发', self.concurrent), ('连接秒', self.connect_timeout), ('命令秒', self.timeout), ('编码', self.encoding)]:
            row2.addWidget(QLabel(label)); row2.addWidget(widget)
        form.addRow('执行', row2)
        self.prompt = QLineEdit(Settings().prompt)
        self.known_hosts = QLineEdit(); self.known_hosts.setPlaceholderText('可选known_hosts路径；未信任/变化的SSH密钥拒绝连接')
        form.addRow('提示符正则', self.prompt)
        form.addRow('SSH可信主机文件', self.known_hosts)
        self.advanced = QLineEdit(); self.advanced.setPlaceholderText('可选关联用例ID，例如 RG-01')
        form.addRow('关联用例', self.advanced)
        self.custom_settings = Settings()
        self.button('分页 / 错误 / 交互规则与备注', self.edit_rules, form)
        layout.addWidget(connection)
        self.run_button = self.button('预览并创建执行任务', self.start_batch, layout)
        self.run_button.setMinimumHeight(40)
        self.tabs.addTab(page, '准备任务')
        self.expand_targets(); self.preview_commands()

    def edit_rules(self):
        dialog = QDialog(self); dialog.setWindowTitle('通用CLI规则 · 请按设备回显调整')
        form = QFormLayout(dialog); fields = {}
        for key, label in [('pager','分页标记（匹配后仅发送空格）'),('interaction','未知交互（匹配后停止）'),('error','CLI错误检测'),('notes','样机 / 固件 / 备注')]:
            field = QLineEdit(getattr(self.custom_settings,key)); fields[key] = field
            field.setMinimumWidth(680); form.addRow(label,field)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec():
            for key, field in fields.items():
                setattr(self.custom_settings,key,field.text())

    def build_monitor(self):
        page = QWidget(); layout = QVBoxLayout(page)
        self.task_label = QLabel('尚无任务'); layout.addWidget(self.task_label)
        self.progress = QProgressBar(); layout.addWidget(self.progress)
        self.runs = QTableWidget(0,5); self.runs.setHorizontalHeaderLabels(['IP','协议','状态','命令进度','错误'])
        self.runs.horizontalHeader().setStretchLastSection(True); layout.addWidget(self.runs)
        self.display_device = QComboBox(); self.display_device.addItem('全部设备', None)
        layout.addWidget(self.display_device)
        self.terminal = QPlainTextEdit(); self.terminal.setReadOnly(True)
        self.terminal.document().setMaximumBlockCount(2000)
        self.terminal.setStyleSheet('background:#132234;color:#d3e5f4;font-family:monospace;font-size:12px;')
        layout.addWidget(self.terminal,1)
        self.monitor_note = QLabel('界面保留最近 2000 行；完整原始回显写入磁盘。')
        layout.addWidget(self.monitor_note)
        row = QHBoxLayout(); layout.addLayout(row)
        self.cancel_button = self.button('取消后续执行', self.cancel_batch, row); self.cancel_button.setEnabled(False)
        self.button('打开当前任务目录', self.open_current, row)
        self.tabs.addTab(page,'执行过程')

    def build_history(self):
        page = QWidget(); layout = QVBoxLayout(page)
        layout.addWidget(QLabel('本地历史 · 原始回显和配置内容可能包含敏感信息，导出包包含全部任务证据。'))
        self.history = QTableWidget(0,4); self.history.setHorizontalHeaderLabels(['任务ID','状态','创建时间','路径'])
        self.history.setSelectionBehavior(QTableWidget.SelectRows); self.history.setSelectionMode(QTableWidget.SingleSelection)
        self.history.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.history)
        bar = QHBoxLayout(); layout.addLayout(bar)
        self.button('刷新', self.refresh_history, bar)
        self.button('打开任务文件夹', self.open_history, bar)
        self.button('查看任务记录', self.inspect_history, bar)
        self.export_button = self.button('导出完整 ZIP（含校验清单）', self.export_history, bar)
        self.tabs.addTab(page,'历史与导出')

    @staticmethod
    def item(text):
        item = QTableWidgetItem(str(text)); item.setFlags(item.flags() & ~Qt.ItemIsEditable); return item

    def import_text(self, editor):
        filename, _ = QFileDialog.getOpenFileName(self,'导入 UTF-8 TXT','','文本 (*.txt);;所有文件 (*)')
        if filename:
            try:
                editor.setPlainText(Path(filename).read_text('utf-8-sig'))
            except Exception as exc:
                self.error(f'导入失败，请另存为UTF-8：{exc}')

    def expand_targets(self):
        try:
            sources = parse_targets(self.targets_text.toPlainText())
            self.sources = sources; self.target_snapshot = self.targets_text.toPlainText()
            self.targets.setRowCount(len(sources))
            for row, ip in enumerate(sources):
                check = QTableWidgetItem(); check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled); check.setCheckState(Qt.Checked)
                self.targets.setItem(row,0,check); self.targets.setItem(row,1,self.item(ip))
                protocol = QComboBox(); protocol.addItems(['mock','ssh','telnet']); protocol.setCurrentText(self.protocol.currentText())
                self.targets.setCellWidget(row,2,protocol)
                port = QSpinBox(); port.setRange(1,65535); port.setValue(self.port.value()); self.targets.setCellWidget(row,3,port)
                protocol.currentTextChanged.connect(lambda t, p=port: p.setValue(23 if t == 'telnet' else 22))
                self.targets.setItem(row,4,self.item('未探测'))
        except Exception as exc:
            self.error(exc)

    def apply_connection(self):
        for row in range(self.targets.rowCount()):
            if self.targets.item(row,0).checkState() == Qt.Checked:
                self.targets.cellWidget(row,2).setCurrentText(self.protocol.currentText())
                self.targets.cellWidget(row,3).setValue(self.port.value())
                self.targets.setItem(row,4,self.item('未探测'))

    def chosen_targets(self):
        if self.target_snapshot != self.targets_text.toPlainText():
            raise ValueError('目标输入已修改，请重新展开目标')
        return [Target(self.targets.item(r,1).text(), self.targets.cellWidget(r,2).currentText(), self.targets.cellWidget(r,3).value())
                for r in range(self.targets.rowCount()) if self.targets.item(r,0).checkState() == Qt.Checked]

    def probe_targets(self):
        try:
            targets = self.chosen_targets()
            if not targets:
                raise ValueError('请勾选目标')
            self.probe_button.setEnabled(False)
            self.run_button.setEnabled(False)
            self.targets.setEnabled(False)
            def run():
                with ThreadPoolExecutor(max_workers=32) as pool:
                    return list(pool.map(lambda t: (t.ip,t.protocol,t.port, '模拟（未连接网络）' if t.protocol == 'mock' else probe(t.ip,t.port)), targets))
            def done(results):
                for ip, protocol, port, result in results:
                    for r in range(self.targets.rowCount()):
                        if self.targets.item(r,1).text() == ip and self.targets.cellWidget(r,2).currentText() == protocol and self.targets.cellWidget(r,3).value() == port:
                            self.targets.setItem(r,4,self.item(result))
                restore()
            def restore():
                self.probe_button.setEnabled(True); self.run_button.setEnabled(not self.busy); self.targets.setEnabled(True)
            def fail(message):
                restore(); self.error(message)
            self.spawn(run,done,fail)
        except Exception as exc:
            self.error(exc)

    def preview_commands(self):
        try:
            commands = parse_commands(self.commands_text.toPlainText(), self.kind.currentText())
            self.command_snapshot = self.commands_text.toPlainText()
            self.commands.setRowCount(len(commands))
            for r,c in enumerate(commands):
                self.commands.setItem(r,0,self.item(c.line))
                kind = QComboBox(); kind.addItems(['配置','查询','其他']); kind.setCurrentText(c.kind)
                self.commands.setCellWidget(r,1,kind); self.commands.setItem(r,2,self.item(c.text))
        except Exception as exc:
            self.error(exc)

    def start_batch(self):
        if self.busy:
            return
        try:
            targets = self.chosen_targets()
            if not targets:
                raise ValueError('请勾选目标')
            if self.command_snapshot != self.commands_text.toPlainText():
                raise ValueError('命令已修改，请重新生成发送清单')
            commands = [Command(self.commands.item(r,2).text(),int(self.commands.item(r,0).text()),self.commands.cellWidget(r,1).currentText()) for r in range(self.commands.rowCount())]
            settings = Settings(**vars(self.custom_settings))
            settings.concurrency = self.concurrent.value(); settings.command_timeout = self.timeout.value()
            settings.connect_timeout = self.connect_timeout.value(); settings.encoding = self.encoding.currentText()
            settings.prompt = self.prompt.text(); settings.known_hosts = self.known_hosts.text().strip()
            settings.related_case_id = self.advanced.text(); settings.validate()
            real = any(t.protocol != 'mock' for t in targets)
            if real and any(c.text.strip().startswith('demo ') for c in commands):
                raise ValueError('真实设备任务中包含demo模拟命令，请先替换并重新预览清单')
            if real and not self.username.text().strip():
                raise ValueError('真实设备连接需要填写用户名')
            dialog = QDialog(self); dialog.setWindowTitle('确认本次目标与完整发送清单'); dialog.resize(850,600)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel('真实设备任务' if real else '模拟任务 · 不连接网络'))
            preview = QPlainTextEdit(); preview.setReadOnly(True)
            preview.setPlainText('\n'.join(f'{t.ip}:{t.port} [{t.protocol}]' for t in targets) + '\n\n' + '\n'.join(f'{i}. [{c.kind}] {c.text}' for i,c in enumerate(commands,1)))
            layout.addWidget(preview)
            layout.addWidget(QLabel('不自动重试、保存或回滚。若修改管理IP导致断连，结果将标记不完整。'))
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); layout.addWidget(buttons)
            buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
            if not dialog.exec():
                return
            credentials = Credentials(self.username.text(), self.password.text(), self.key.text().strip())
            self.batch = Batch(self.archive, targets, commands, settings, credentials,
                               input_snapshot={'text': self.target_snapshot, 'sources': self.sources}, callback=self.queue_event)
            self.password.clear()
            self.terminal.clear(); self.skipped = 0
            self.display_device.clear(); self.display_device.addItem('全部设备',None)
            for d in self.batch.store.task['devices']:
                self.display_device.addItem(d['ip'],d['device_run_id'])
            self.busy = True; self.run_button.setEnabled(False); self.cancel_button.setEnabled(True)
            self.task_label.setText(f"任务 {self.batch.store.task['task_id']} · 业务验证：未验证")
            self.tabs.setCurrentIndex(1)
            self.spawn(self.batch.run,self.batch_done,self.batch_failed)
        except Exception as exc:
            self.error(exc)

    def queue_event(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            self.skipped += 1  # Only UI notification skipped; disk evidence is already persisted.

    def refresh_live(self):
        for _ in range(300):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            selected = self.display_device.currentData()
            if selected is None or event.get('device_run_id') == selected:
                ip = event.get('device_run_id','')[:8]
                if self.batch:
                    ip = next((d['ip'] for d in self.batch.store.task['devices'] if d['device_run_id'] == event.get('device_run_id')),ip)
                text = event.get('text','')
                # Bound UI even for a device emitting a single giant line.
                self.terminal.appendPlainText(f"[{ip}] {event['direction']} {text[:8192]}" + ('\n[界面截断，完整内容已落盘]' if len(text)>8192 else ''))
        self.monitor_note.setText(f'界面最近2000行；完整数据在磁盘。因显示拥塞省略界面通知：{self.skipped}')
        if self.batch:
            devices = self.batch.store.task['devices']; self.runs.setRowCount(len(devices))
            completed = 0
            for row,d in enumerate(devices):
                count = sum(c['status'] not in ('排队','运行') for c in d['commands'])
                completed += count
                active = next((str(c['sequence']) for c in d['commands'] if c['status']=='运行'),'-')
                for col,value in enumerate([d['ip'],d['protocol'],d['status'],f"{count}/{len(d['commands'])} 当前#{active}",d.get('error','')]):
                    existing = self.runs.item(row,col)
                    if existing is None or existing.text() != str(value):
                        self.runs.setItem(row,col,self.item(value))
            self.progress.setMaximum(sum(len(d['commands']) for d in devices)); self.progress.setValue(completed)

    def batch_done(self, path):
        self.busy = False; self.run_button.setEnabled(True); self.cancel_button.setEnabled(False)
        self.batch.credentials = Credentials()
        self.refresh_history(); self.statusBar().showMessage(f'任务结束，证据目录：{path}')

    def batch_failed(self, message):
        self.batch_done(self.batch.store.root)
        self.error(message)

    def cancel_batch(self):
        if self.batch and self.busy:
            self.batch.cancel(); self.cancel_button.setEnabled(False)
            self.statusBar().showMessage('已请求取消；连接阶段可能需要等待连接超时，已收回显保留。')

    def open_current(self):
        if self.batch:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.batch.store.root)))

    def refresh_history(self):
        entries = self.archive.history(); self.history.setRowCount(len(entries))
        for row,(tid,path,status,created) in enumerate(entries):
            for col,value in enumerate([tid,status,created,path]):
                self.history.setItem(row,col,self.item(value))

    def history_path(self):
        row = self.history.currentRow()
        if row < 0:
            raise ValueError('请选择一个历史任务')
        return Path(self.history.item(row,3).text())

    def open_history(self):
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.history_path())))
        except Exception as exc:
            self.error(exc)

    def inspect_history(self):
        try:
            task = json.loads((self.history_path()/'task.json').read_text('utf-8'))
            dialog = QDialog(self); dialog.resize(950,650); dialog.setWindowTitle('历史任务 · 逐命令记录')
            layout = QVBoxLayout(dialog); text = QPlainTextEdit(); text.setReadOnly(True)
            text.setPlainText(json.dumps(task,ensure_ascii=False,indent=2)); layout.addWidget(text); dialog.exec()
        except Exception as exc:
            self.error(exc)

    def export_history(self):
        try:
            path = self.history_path()
            self.export_button.setEnabled(False)
            def done(result):
                self.export_button.setEnabled(True)
                QMessageBox.information(self,'导出已验证',f'ZIP解压及SHA-256校验通过：\n{result}')
            def fail(message):
                self.export_button.setEnabled(True); self.error(message)
            self.spawn(lambda: self.archive.export(path),done,fail)
        except Exception as exc:
            self.export_button.setEnabled(True); self.error(exc)

    def closeEvent(self, event):
        if self.workers:
            self.cancel_batch()
            QMessageBox.information(self,'后台工作尚未结束','已请求停止执行，请等待后台任务结束后关闭。')
            event.ignore()
        else:
            event.accept()


def main(base):
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    window = Window(base)
    window.show()
    return app.exec()
