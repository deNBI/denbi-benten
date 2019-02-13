"""Provides a view into a CWL component, like a workflow. The view can be of a whole CWL file
or a part of a CWL file, like an in-lined step. Changes to a part of a CWL file are """

import time

from PySide2.QtCore import Qt, QSignalBlocker, QTimer, Slot, Signal
from PySide2.QtWidgets import QHBoxLayout, QSplitter, QTableWidget, QTableWidgetItem, QWidget, \
    QAbstractItemView, QGraphicsSceneMouseEvent
from PySide2.QtGui import QTextCursor, QPainter

from .codeeditor.editor import CodeEditor
from .processview import ProcessView
from .unkscene import UnkScene
from .toolscene import ToolScene
from .workflowscene import WorkflowScene

from ..editing.cwldoc import CwlDoc
from ..models.unk import Unk
from ..models.tool import Tool
from ..models.workflow import Workflow

import logging

logger = logging.getLogger(__name__)


class ProgrammaticEdit:
    def __init__(self, raw_cwl, cursor_line):
        self.raw_cwl = raw_cwl
        self.cursor_line = cursor_line


class ManualEditThrottler:
    """Each manual edit we do (letter we type) triggers a manual edit. We need to manage
    these calls so they don't overwhelm the system and yet not miss out on the final edit in
    a burst of edits. This manager handles that job effectively."""

    def __init__(self):
        self.burst_window = 1.0
        # We allow upto a <burst_window> pause in typing before parsing the edit
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(int(self.burst_window * 1000))

    def restart_edit_clock(self):
        self.timer.start()

    def flush(self):
        if self.timer.isActive():
            self.timer.stop()
            self.timer.timeout.emit()


class PersistentEditorState:
    """Each edit causes us to update everything. We need to remember some things."""

    def __len__(self):
        self.selected_items: list = None


class BentenWindow(QWidget):

    scene_double_clicked = Signal(object)
    edit_registered = Signal(object)

    def __init__(self):
        QWidget.__init__(self)

        self.code_editor: CodeEditor = CodeEditor()
        self.process_view: ProcessView = ProcessView(None)

        self.conn_table = QTableWidget()
        self.conn_table.horizontalHeader().setStretchLastSection(True)
        self.conn_table.verticalHeader().setVisible(False)
        self.conn_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.conn_table.cellClicked.connect(self.connection_clicked)

        left_pane = QSplitter()
        left_pane.setHandleWidth(1)
        left_pane.setOrientation(Qt.Vertical)
        left_pane.addWidget(self.process_view)
        left_pane.addWidget(self.conn_table)
        left_pane.setStretchFactor(0, 3)
        left_pane.setStretchFactor(1, 1)

        main_pane = QSplitter(self)
        main_pane.setHandleWidth(1)
        main_pane.addWidget(left_pane)
        main_pane.addWidget(self.code_editor)
        main_pane.setStretchFactor(0, 5)
        main_pane.setStretchFactor(1, 3)

        # If we don't put all this in a layout and set zero margin QT puts us in a tiny box within
        # the window
        layout = QHBoxLayout()
        layout.setMargin(0)
        layout.addWidget(main_pane)
        self.setLayout(layout)

        self.manual_edit_throttler = ManualEditThrottler()
        self.manual_edit_throttler.timer.timeout.connect(self.update_from_code)

        self.cwl_doc: CwlDoc = None
        self.process_model: (Workflow,) = None

        # todo: To deprecate and use different mechanism
        self.current_programmatic_edit: ProgrammaticEdit = None

        self.is_active_window = False

        self.code_editor.textChanged.connect(self.manual_edit)

    def set_document(self, cwl_doc):
        # This registers as a manual edit but we wish to skip the throttler
        blk = QSignalBlocker(self.code_editor)
        self.cwl_doc = cwl_doc
        self.code_editor.set_text(self.cwl_doc.raw_cwl)
        self.update_from_code()

    def set_active_window(self):
        """To be called whenever we switch tabs to this window. """
        self.is_active_window = True
        self.update_from_code()
        self.code_editor.setFocus()
        # When we switch back and forth, we expect to be able to see the editor cursor = focus

    def set_inactive_window(self):
        """To be called whenever we switch away from this window"""
        self.manual_edit_throttler.flush()
        self.is_active_window = False

    @Slot()
    def manual_edit(self):
        """Called whenever the code is changed manually"""
        self.manual_edit_throttler.restart_edit_clock()

    @Slot()
    def programmatic_edit(self):
        """Called when we have a programmatic edit to execute"""
        # https://doc.qt.io/qt-5/qsignalblocker.html
        blk = QSignalBlocker(self.code_editor)

        # https://programtalk.com/python-examples/PySide2.QtGui.QTextCursor/
        # https://www.qtcentre.org/threads/43268-Setting-Text-in-QPlainTextEdit-without-Clearing-Undo-Redo-History
        doc = self.code_editor.document()
        insert_cursor = QTextCursor(doc)
        insert_cursor.select(QTextCursor.SelectionType.Document)
        insert_cursor.insertText(self.current_programmatic_edit.raw_cwl)

        # https://stackoverflow.com/questions/27036048/how-to-scroll-to-the-specified-line-in-qplaintextedit
        final_cursor = QTextCursor(
            doc.findBlockByLineNumber(self.current_programmatic_edit.cursor_line))
        self.code_editor.setTextCursor(final_cursor)
        self.code_editor.update_line_number_area_width(0)  # This is needed so that everything aligns right
        self.code_editor.highlight_current_line()

        self.update_from_code()

    @Slot()
    def update_from_code(self):

        if not self.is_active_window:
            # Defer updating until we can be seen
            return

        modified_cwl = self.code_editor.toPlainText()
        if self.process_model is not None:
            if self.process_model.cwl_doc.raw_cwl == modified_cwl:
                logger.debug("Update asked for, but code hasn't changed.")
                return

        t0 = time.time()

        self.cwl_doc = CwlDoc(raw_cwl=modified_cwl,
                              path=self.cwl_doc.path,
                              inline_path=self.cwl_doc.inline_path)

        pt = self.cwl_doc.process_type()
        t1 = time.time()
        if pt == "Workflow":
            self.process_model = Workflow(cwl_doc=self.cwl_doc)
            scene = WorkflowScene(self)
            scene.selectionChanged.connect(self.something_selected)
            scene.double_click.connect(self.something_double_clicked)
            scene.set_workflow(self.process_model)
            if self.process_model.problems_with_wf:
                logger.warning(self.process_model.problems_with_wf)
        elif pt in ["CommandLineTool", "ExpressionTool"]:
            self.process_model = Tool(cwl_doc=self.cwl_doc)
            scene = ToolScene(self)
            scene.set_tool(self.process_model)
        else:
            self.process_model = Unk(cwl_doc=self.cwl_doc)
            scene = UnkScene(self)

        self.process_view.setScene(scene)
        self.process_view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.process_view.fitInView(scene.sceneRect(), Qt.KeepAspectRatio)

        t2 = time.time()

        logger.debug("Parsed workflow in {}s".format(t1 - t0))
        logger.debug("Displayed workflow in {}s".format(t2 - t1))

        self.edit_registered.emit(self.cwl_doc)

    @Slot()
    def something_selected(self):
        items = self.process_view.scene().selectedItems()
        if len(items) == 1:
            info = items[0].data(0)
            if isinstance(info, str):
                if info in ["inputs", "outputs"]:
                    self.highlight_workflow_io(info)
                else:
                    self.highlight_step(info)
            elif isinstance(info, tuple):
                self.highlight_connection_between_nodes(info)

    def highlight_workflow_io(self, info: str):
        if info == "inputs":
            if "inputs" in self.process_model.section_lines:
                self.code_editor.scroll_to(self.process_model.section_lines["inputs"][0])
            conn = [c for c in self.process_model.connections if c.src.node_id is None]
        else:
            if "outputs" in self.process_model.section_lines:
                self.code_editor.scroll_to(self.process_model.section_lines["outputs"][0])
            conn = [c for c in self.process_model.connections if c.dst.node_id is None]
        self.populate_connection_table(info, [conn])

    def highlight_step(self, info: str):
        step = self.process_model.steps[info]
        logger.debug("Scroll to line {}".format(step.line[0]))
        self.code_editor.scroll_to(step.line[0])

        inbound_conn = [c for c in self.process_model.connections if c.dst.node_id == info]
        outbound_conn = [c for c in self.process_model.connections if c.src.node_id == info]

        self.populate_connection_table(step.id, [inbound_conn, outbound_conn])

    def highlight_connection_between_nodes(self, info: tuple):
        def src_is_input(x): return x.src.node_id is None

        def src_is_node(x): return x.src.node_id == id1

        def dst_is_output(x): return x.dst.node_id is None

        def dst_is_node(x): return x.dst.node_id == id2

        id1, id2 = info

        cond1 = src_is_input if id1 == "inputs" else src_is_node
        cond2 = dst_is_output if id2 == "outputs" else dst_is_node

        conn = [c for c in self.process_model.connections if cond1(c) and cond2(c)]
        self.populate_connection_table(str(info), [conn])

    def populate_connection_table(self, title, conns: [dict]):
        row, col = 0, 0
        self.conn_table.clear()
        self.conn_table.setColumnCount(1)
        self.conn_table.setRowCount(sum(len(c) for c in conns))
        self.conn_table.setHorizontalHeaderLabels([title])
        for conn_grp in conns:
            for conn in conn_grp:
                item = QTableWidgetItem(str(conn))
                item.setData(Qt.UserRole, conn)  # All other roles try to replace as display text
                self.conn_table.setItem(row, col, item)
                row += 1

    @Slot(int, int)
    def connection_clicked(self, row, col):
        conn = self.conn_table.item(row, col).data(Qt.UserRole)
        logger.debug("Scroll to line {}".format(conn.line[0]))
        self.code_editor.scroll_to(conn.line[0])

    @Slot(QGraphicsSceneMouseEvent)
    def something_double_clicked(self, event):
        items = self.process_view.scene().selectedItems()
        if len(items) == 0:
            self.process_view.fitInView(self.process_view.scene().sceneRect(), Qt.KeepAspectRatio)
            return

        steps = [self.process_model.steps[item.data(0)] for item in items
                 if item.data(0) not in ["inputs", "outputs"] and isinstance(item.data(0), str)]
        # exclude workflow inputs/outputs and connecting lines (which are tuples)
        if steps:
            self.scene_double_clicked.emit([step.sub_workflow for step in steps])
