from dataclasses import asdict
import json
from pathlib import Path
import queue
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,
    QLineEdit,QPlainTextEdit,QComboBox,QSpinBox,QCheckBox,QPushButton,QTabWidget,QSplitter,QGroupBox,
    QTableWidget,QTableWidgetItem,QHeaderView,QFileDialog,QMessageBox,QDialog,QDialogButtonBox)
from switchbatch.instance import InstanceLock
from switchbatch.models import Settings
from switchbatch.transports import Credentials
from switchbatch.store import Archive, atomic_json
from .core import Profile,make_plan,render,export_plan,validate_execution,Provisioner,demo_profile


class Worker(QThread):
    done=Signal(object)
    failed=Signal(str)
    def __init__(self,fn,parent):super().__init__(parent);self.fn=fn
    def run(self):
        try:self.done.emit(self.fn())
        except Exception as exc:self.failed.emit(str(exc))


class Window(QMainWindow):
    def __init__(self,base):
        super().__init__();self.base=Path(base);self.base.mkdir(parents=True,exist_ok=True)
        self.setWindowTitle('SwitchConfigBatch · 批量改IP与导入配置');self.resize(1260,940)
        self.devices=[];self.snapshot=None;self.folder=None;self.runner=None;self.worker=None;self.events=queue.Queue(maxsize=500)
        self.skipped=0;self.records={};self.settings=Settings();self.settings.concurrency=1
        self.archive=Archive(self.base);self.archive.recover()
        for path in (self.base/'generated').glob('*/*_result.json'):
            record=json.loads(path.read_text('utf-8'))
            if record['status'] in ('运行','等待新IP核对'):
                record.update(status='中断/待核对',error='上次进程中断，不自动重发配置');atomic_json(path,record)
        self.tabs=QTabWidget();self.setCentralWidget(self.tabs)
        self.build_editor();self.build_preview();self.build_results();self.load_profile(Profile())
        self.timer=QTimer(self);self.timer.timeout.connect(self.drain);self.timer.start(100)
        self.statusBar().showMessage('默认Telnet · 模板为空时不会发送任何命令 · 可先加载离线演示')

    def button(self,title,fn,layout):
        b=QPushButton(title);b.clicked.connect(fn);layout.addWidget(b);return b

    def error(self,text):QMessageBox.warning(self,'请检查',str(text))

    def build_editor(self):
        page=QWidget();v=QVBoxLayout(page)
        title=QLabel('一份模板，生成多台设备配置');title.setStyleSheet('font-size:22px;color:#225b79;font-weight:600;padding:8px');v.addWidget(title)
        v.addWidget(QLabel('不绑定厂家命令。占位符：${IP} 新IP · ${OLD_IP} 原IP · ${MASK} 掩码 · ${PREFIX} 前缀 · ${GATEWAY} 网关 · ${INDEX} 编号'))
        bar=QHBoxLayout();v.addLayout(bar)
        self.button('载入离线演示',self.load_demo,bar);self.button('导入方案 JSON',self.import_profile,bar);self.button('保存方案 JSON',self.save_profile,bar)
        form=QFormLayout();v.addLayout(form)
        self.mode=QComboBox();self.mode.addItems(['逐台接入（相同出厂IP）','已有独立管理IP']);form.addRow('设备接入方式',self.mode)
        self.current=QLineEdit();self.current.setPlaceholderText('同出厂IP填一个；独立IP模式填逗号分隔列表');form.addRow('当前连接IP',self.current)
        row=QHBoxLayout();self.first=QLineEdit();self.count=QSpinBox();self.count.setRange(1,1024)
        self.mask=QLineEdit();self.gateway=QLineEdit();self.gateway.setPlaceholderText('可留空')
        for label,w in [('起始新IP',self.first),('数量',self.count),('掩码',self.mask),('网关',self.gateway)]:row.addWidget(QLabel(label));row.addWidget(w)
        form.addRow('地址分配',row)
        split=QSplitter(Qt.Horizontal)
        left=QWidget();lv=QVBoxLayout(left);lv.addWidget(QLabel('① 原IP会话：配置模板（含改IP命令）'))
        self.main=QPlainTextEdit();self.main.setPlaceholderText('填写或导入实际CLI命令，每行一条。\n将管理IP位置改成 ${IP}。\n若改IP立即断线，把改址命令放在本阶段末尾。')
        lv.addWidget(self.main);buttons=QHBoxLayout();lv.addLayout(buttons)
        self.button('导入 TXT',lambda:self.import_txt(self.main),buttons)
        self.button('将选中文本替换为 ${IP}',self.mark_ip,buttons)
        right=QWidget();rv=QVBoxLayout(right);rv.addWidget(QLabel('② 新IP会话：后续配置 / 保存命令（可空）'))
        self.after=QPlainTextEdit();self.after.setPlaceholderText('重连并核对设备身份后才执行。\n如保存需要进入配置模式，请显式列出。\n没有通用的保存命令，不自动补写。');rv.addWidget(self.after)
        self.button('导入 TXT',lambda:self.import_txt(self.after),rv)
        split.addWidget(left);split.addWidget(right);v.addWidget(split,1)
        rules=QGroupBox('改址前后验证（实际下发前必填；仅生成文件时可留空）');f=QFormLayout(rules)
        self.identity_command=QLineEdit();self.identity_pattern=QLineEdit();self.verify_command=QLineEdit();self.verify_pattern=QLineEdit()
        self.identity_command.setPlaceholderText('读取设备序列号或稳定MAC的只读命令')
        self.identity_pattern.setPlaceholderText(r'从回显提取身份，第一捕获组；例如 (?m)^Serial: (\S+)$')
        self.verify_command.setPlaceholderText('读取当前管理IP的只读命令')
        self.verify_pattern.setPlaceholderText('从回显提取管理IP；例如 (?m)^Management IP: ([0-9.]+)$')
        for name,label in [('identity_command','设备身份查询'),('identity_pattern','身份提取正则'),('verify_command','管理IP查询'),('verify_pattern','IP提取正则')]:f.addRow(label,getattr(self,name))
        row=QHBoxLayout();self.allow=QCheckBox('允许第一阶段最后一条改IP命令断连/超时后尝试新IP核对')
        self.settle=QSpinBox();self.settle.setRange(0,120);self.settle.setSuffix(' 秒')
        row.addWidget(self.allow);row.addWidget(QLabel('重连前等待'));row.addWidget(self.settle);f.addRow(row)
        v.addWidget(rules)
        self.button('生成预览 →',self.preview,v).setMinimumHeight(40)
        self.tabs.addTab(page,'1. 模板与地址')

    def build_preview(self):
        page=QWidget();v=QVBoxLayout(page)
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['编号','当前IP','新IP','执行状态'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows);self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);self.table.itemSelectionChanged.connect(self.show_commands)
        v.addWidget(self.table,1)
        self.preview_text=QPlainTextEdit();self.preview_text.setReadOnly(True);v.addWidget(self.preview_text,1)
        connection=QGroupBox('连接参数');f=QFormLayout(connection)
        row=QHBoxLayout();self.protocol=QComboBox();self.protocol.addItems(['telnet','ssh','mock']);self.port=QSpinBox();self.port.setRange(1,65535);self.port.setValue(23)
        self.protocol.currentTextChanged.connect(lambda p:self.port.setValue(22 if p=='ssh' else 23))
        self.user=QLineEdit();self.user.setPlaceholderText('用户名');self.password=QLineEdit();self.password.setEchoMode(QLineEdit.Password);self.password.setPlaceholderText('密码，仅内存保存')
        for w in (self.protocol,self.port,self.user,self.password):row.addWidget(w)
        f.addRow('协议 / 端口 / 认证',row)
        row=QHBoxLayout();self.encoding=QComboBox();self.encoding.setEditable(True);self.encoding.addItems(['utf-8','gb18030','latin-1'])
        self.timeout=QSpinBox();self.timeout.setRange(1,3600);self.timeout.setValue(30)
        self.prompt=QLineEdit(Settings().prompt)
        row.addWidget(QLabel('编码'));row.addWidget(self.encoding);row.addWidget(QLabel('命令超时秒'));row.addWidget(self.timeout);row.addWidget(QLabel('提示符'));row.addWidget(self.prompt)
        f.addRow(row);self.hosts=QLineEdit();self.hosts.setPlaceholderText('SSH可选；未知或变化的密钥将拒绝连接');f.addRow('known_hosts文件',self.hosts)
        self.button('高级CLI规则',self.edit_settings,f)
        v.addWidget(connection)
        v.addWidget(QLabel('Telnet明文传输。相同出厂IP模式每次只接入一台；目标地址需要自行确认未被其他设备占用。'))
        bar=QHBoxLayout();v.addLayout(bar)
        self.export_btn=self.button('导出全部配置文件',self.export,bar)
        self.one_btn=self.button('执行选中一台',lambda:self.start(False),bar)
        self.all_btn=self.button('顺序执行全部（独立IP / 模拟）',lambda:self.start(True),bar)
        self.tabs.addTab(page,'2. 预览与导入')

    def build_results(self):
        page=QWidget();v=QVBoxLayout(page)
        self.progress=QLabel('尚未执行');v.addWidget(self.progress)
        self.terminal=QPlainTextEdit();self.terminal.setReadOnly(True);self.terminal.document().setMaximumBlockCount(1500)
        self.terminal.setStyleSheet('background:#142638;color:#dcf0f5;font-family:monospace;');v.addWidget(self.terminal,1)
        self.output_note=QLabel('界面仅显示最近内容；完整回显独立写入日志。');v.addWidget(self.output_note)
        bar=QHBoxLayout();v.addLayout(bar)
        self.cancel_btn=self.button('取消后续操作',self.cancel,bar);self.cancel_btn.setEnabled(False)
        self.button('打开当前方案及结果',lambda:self.open_path(self.folder or self.base),bar)
        self.button('打开完整CLI日志',lambda:self.open_path(self.base/'logs'),bar)
        self.button('返回设备清单 / 下一台',lambda:self.tabs.setCurrentIndex(1),bar)
        v.addWidget(QLabel('完成代表身份和运行管理IP回读匹配；没有自动验证重启后配置持久化。失败后不自动重发。'))
        self.tabs.addTab(page,'3. 过程与结果')

    def get_profile(self):
        return Profile(mode=self.mode.currentText(),current=self.current.text(),first=self.first.text(),count=self.count.value(),
            mask=self.mask.text(),gateway=self.gateway.text(),main=self.main.toPlainText(),after=self.after.toPlainText(),
            identity_command=self.identity_command.text(),identity_pattern=self.identity_pattern.text(),
            verify_command=self.verify_command.text(),verify_pattern=self.verify_pattern.text(),allow_disconnect=self.allow.isChecked(),settle_seconds=self.settle.value())

    def load_profile(self,p):
        self.mode.setCurrentText(p.mode);self.count.setValue(p.count);self.allow.setChecked(p.allow_disconnect);self.settle.setValue(p.settle_seconds)
        for name in ('current','first','mask','gateway','identity_command','identity_pattern','verify_command','verify_pattern'):getattr(self,name).setText(getattr(p,name))
        self.main.setPlainText(p.main);self.after.setPlainText(p.after)

    def load_demo(self):
        self.load_profile(demo_profile());self.protocol.setCurrentText('mock');self.preview()

    def import_profile(self):
        filename,_=QFileDialog.getOpenFileName(self,'导入方案','','JSON (*.json)')
        if filename:
            try:
                data=json.loads(Path(filename).read_text('utf-8-sig'));p=Profile(**data.get('profile',data));make_plan(p);self.load_profile(p)
            except Exception as exc:self.error(exc)

    def save_profile(self):
        filename,_=QFileDialog.getSaveFileName(self,'保存方案（不含登录密码）','profile.json','JSON (*.json)')
        if filename:
            try:atomic_json(Path(filename),asdict(self.get_profile()))
            except Exception as exc:self.error(exc)

    def import_txt(self,editor):
        filename,_=QFileDialog.getOpenFileName(self,'导入UTF-8文本','','TXT (*.txt);;所有文件 (*)')
        if filename:
            try:editor.setPlainText(Path(filename).read_text('utf-8-sig'))
            except Exception as exc:self.error(exc)

    def mark_ip(self):
        cursor=self.main.textCursor()
        if not cursor.hasSelection():self.error('先选中模板中需要替换的管理IP文本');return
        cursor.insertText('${IP}');self.main.setTextCursor(cursor)

    def preview(self):
        try:
            p=self.get_profile();devices=make_plan(p)
            if self.snapshot!=asdict(p):self.folder=None;self.records={}
            self.devices=devices;self.snapshot=asdict(p);self.table.setRowCount(len(devices))
            for row,d in enumerate(devices):
                for col,value in enumerate((d.index,d.old_ip,d.ip,self.records.get(d.index,{}).get('status','未执行'))):
                    item=QTableWidgetItem(str(value));item.setFlags(item.flags() & ~Qt.ItemIsEditable);self.table.setItem(row,col,item)
            self.table.selectRow(0);self.show_commands();self.tabs.setCurrentIndex(1)
        except Exception as exc:self.error(exc)

    def show_commands(self):
        row=self.table.currentRow()
        if 0<=row<len(self.devices):
            p=Profile(**self.snapshot);d=self.devices[row]
            text=f'设备 {d.index}：{d.old_ip} → {d.ip}\n\n'
            for label,value in [('原IP会话：先核对身份',p.identity_command),('第一阶段',p.main),('新IP会话：身份必须匹配',p.identity_command),('第二阶段',p.after),('最终管理IP查询',p.verify_command)]:
                text+=f'【{label}】\n{render(value,d) or "（空）"}\n\n'
            self.preview_text.setPlainText(text)

    def ensure_preview(self):
        if self.snapshot!=asdict(self.get_profile()) or not self.devices:raise ValueError('内容已修改，请重新生成预览')

    def export(self):
        try:
            self.ensure_preview()
            if not self.folder:self.folder=export_plan(self.base,Profile(**self.snapshot),self.devices)
            self.statusBar().showMessage(f'已导出：{self.folder}');self.open_path(self.folder)
        except Exception as exc:self.error(exc)

    def edit_settings(self):
        dialog=QDialog(self);dialog.setWindowTitle('CLI适配设置');f=QFormLayout(dialog);fields={}
        for key,label in [('pager','分页正则'),('interaction','未知确认/密码提示'),('error','命令错误正则')]:
            field=QLineEdit(getattr(self.settings,key));field.setMinimumWidth(650);f.addRow(label,field);fields[key]=field
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel);f.addRow(buttons)
        buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            for k,w in fields.items():setattr(self.settings,k,w.text())

    def start(self,all_devices):
        if self.worker:return
        try:
            self.ensure_preview();p=Profile(**self.snapshot);protocol=self.protocol.currentText()
            if all_devices and p.mode.startswith('逐台') and protocol!='mock':raise ValueError('相同出厂IP不能同时连接批量执行，请一次接一台并点“执行选中一台”')
            row=self.table.currentRow()
            if not all_devices and row<0:raise ValueError('请选择设备')
            selected=self.devices if all_devices else [self.devices[row]]
            validate_execution(p,selected,protocol)
            settings=Settings(**vars(self.settings));settings.encoding=self.encoding.currentText();settings.command_timeout=self.timeout.value()
            settings.prompt=self.prompt.text();settings.known_hosts=self.hosts.text().strip();settings.validate()
            for d in selected:
                for value in (p.main,p.after,p.identity_command,p.verify_command):render(value,d).encode(settings.encoding)
            if protocol!='mock' and not self.user.text().strip():raise ValueError('请填写用户名')
            dialog=QDialog(self);dialog.resize(850,580);dialog.setWindowTitle('确认导入范围和命令');v=QVBoxLayout(dialog)
            v.addWidget(QLabel('模拟执行，不连接网络' if protocol=='mock' else f'通过 {protocol}:{self.port.value()} 连接实际设备'))
            text=QPlainTextEdit();text.setReadOnly(True)
            text.setPlainText('\n\n'.join(f'{d.index}. {d.old_ip} → {d.ip}\n身份查询：{render(p.identity_command,d)}\n第一阶段：\n{render(p.main,d)}\n第二阶段：\n{render(p.after,d)}\n最终查询：{render(p.verify_command,d)}' for d in selected));v.addWidget(text)
            confirmation=QCheckBox('已确认目标IP可用；相同出厂IP时仅接入当前这一台；保存命令已按设备要求填写')
            confirmation.setChecked(protocol=='mock');v.addWidget(confirmation)
            buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel);v.addWidget(buttons)
            buttons.button(QDialogButtonBox.Ok).setEnabled(confirmation.isChecked());confirmation.toggled.connect(buttons.button(QDialogButtonBox.Ok).setEnabled)
            buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject)
            if not dialog.exec():return
            if not self.folder:self.folder=export_plan(self.base,p,self.devices)
            self.runner=Provisioner(self.base,p,selected,settings,Credentials(self.user.text(),self.password.text()),protocol,self.port.value(),self.queue_event)
            self.password.clear();self.terminal.clear();self.skipped=0
            def run():
                results=[]
                for d in selected:
                    if self.runner.stop.is_set():break
                    record=self.runner.run_device(d,self.folder);results.append(record)
                    self.queue_event({'direction':'RESULT','text':f"设备{d.index} {d.ip}: {record['status']} {record['error']}"})
                    if record['status'] not in ('模拟验证完成','身份及管理IP核对完成'):break
                return results
            self.worker=Worker(run,self);self.worker.done.connect(self.done);self.worker.failed.connect(self.failed)
            self.worker.finished.connect(self.finished);self.worker.start()
            self.set_busy(True);self.tabs.setCurrentIndex(2);self.progress.setText(f'运行中：计划处理 {len(selected)} 台，失败后停止后续设备')
        except Exception as exc:self.error(exc)

    def queue_event(self,event):
        try:self.events.put_nowait(event)
        except queue.Full:self.skipped+=1

    def drain(self):
        for _ in range(150):
            try:e=self.events.get_nowait()
            except queue.Empty:break
            self.terminal.appendPlainText(f"{e.get('time','')} [{e['direction']}] {e.get('text','')[:8192]}")
        self.output_note.setText(f'完整回显已独立写入磁盘。界面拥塞省略通知数：{self.skipped}')

    def set_busy(self,busy):
        self.tabs.setTabEnabled(0,not busy);self.tabs.setTabEnabled(1,not busy);self.cancel_btn.setEnabled(busy)

    def done(self,results):
        for record in results:
            index=record['device']['index'];self.records[index]=record;self.table.item(index-1,3).setText(record['status'])
        self.progress.setText(f'本次结束：{len(results)} 台；请查看逐台结果，失败项不会自动重试')
        if results:
            next_row=min(results[-1]['device']['index'],len(self.devices)-1);self.table.selectRow(next_row)

    def failed(self,message):self.progress.setText('异常停止，已有日志保留');self.error(message)
    def finished(self):
        self.set_busy(False)
        if self.runner:self.runner.credentials=Credentials()
        self.worker.deleteLater();self.worker=None
    def cancel(self):
        if self.runner:self.runner.cancel();self.cancel_btn.setEnabled(False);self.progress.setText('已请求取消，设备侧改动是否完成需要核对')
    def open_path(self,path):QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))
    def closeEvent(self,event):
        if self.worker:self.cancel();self.error('已请求取消，请等待当前连接/操作结束后关闭');event.ignore()
        else:event.accept()


def main(base):
    app=QApplication.instance() or QApplication([]);app.setStyle('Fusion')
    window=Window(base);window.show();return app.exec()
