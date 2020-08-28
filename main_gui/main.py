#!/usr/bin/env python3

from uscope.gstwidget import GstVideoPipeline, gstwidget_main

from uscope.config import get_config
from uscope.hal.img.imager import Imager
from uscope.img_util import get_scaled
from uscope.benchmark import Benchmark
from uscope.hal.img.imager import MockImager
from uscope.hal.cnc import hal as cnc_hal
from uscope.hal.cnc import lcnc_ar
from uscope.hal.cnc import lcnc as lcnc_hal
from uscope.lcnc.client import LCNCRPC
from uscope import gst_util
from uscope.v4l2_util import ctrl_set

from main_gui.threads import CncThread, PlannerThread
from io import StringIO

from PyQt4 import Qt
from PyQt4.QtGui import *
from PyQt4.QtCore import *

import datetime
import os.path
from PIL import Image
import re
import signal
import socket
import sys
import traceback
import threading
import json

uconfig = get_config()

"""
gobject = None
gst = None
try:
    import gobject
    import gst
    gst_util.register()
except ImportError:
    if uconfig['imager']['engine'] == 'gstreamer' or uconfig['imager'][
            'engine'] == 'gstreamer-testrc':
        print(
            'Failed to import a gstreamer package when gstreamer is required')
        raise
"""

debug = 1


def dbg(*args):
    if not debug:
        return
    if len(args) == 0:
        print()
    elif len(args) == 1:
        print('main: %s' % (args[0], ))
    else:
        print('main: ' + (args[0] % args[1:]))


def get_cnc_hal(log):
    print('get_cnc_hal', log)
    try:
        lcnc_host = uconfig["cnc"]["lcnc"]["host"]
    except KeyError:
        lcnc_host = "mk"
    engine = uconfig['cnc']['engine']
    if engine == 'mock':
        return cnc_hal.MockHal(log=log)
    elif engine == 'lcnc-py':
        import linuxcnc

        return lcnc_hal.LcncPyHal(linuxcnc=linuxcnc, log=log)
    elif engine == 'lcnc-rpc':
        try:
            return lcnc_hal.LcncPyHal(linuxcnc=LCNCRPC(host=lcnc_host),
                                      log=log)
        except socket.error:
            raise
            raise Exception("Failed to connect to LCNCRPC %s" % lcnc_host)
    elif engine == 'lcnc-arpc':
        return lcnc_ar.LcncPyHalAr(host=lcnc_host, log=log)
    elif engine == 'lcnc-rsh':
        return lcnc_hal.LcncRshHal(log=log)
    else:
        raise Exception("Unknown CNC engine %s" % engine)
    '''
    # pr0ndexer (still on MicroControle hardware though)
    elif engine == 'pdc':
        try:
            #return PDC(debug=False, log=log, config=config)
            return cnc_hal.PdcHal(log=log)
        except IOError:
            print 'Failed to open PD device'
            raise
    '''
    '''
    Instead of auto lets support a fallback allowed option
    elif engine == 'auto':
        raise Exception('FIXME')
        log('Failed to open device, falling back to mock')
        return cnc_hal.MockHal(log=log)
    '''


class AxisWidget(QWidget):
    def __init__(self, axis, cnc_thread, parent=None):
        QWidget.__init__(self, parent)

        self.axis = axis
        self.cnc_thread = cnc_thread

        self.gb = QGroupBox('Axis %s' % self.axis.upper())
        self.gl = QGridLayout()
        self.gb.setLayout(self.gl)
        row = 0

        self.gl.addWidget(QLabel("Pos (mm):"), row, 0)
        self.pos_value = QLabel("Unknown")
        self.gl.addWidget(self.pos_value, row, 1)
        row += 1

        # Return to 0 position
        self.ret0_pb = QPushButton("Ret0")
        self.ret0_pb.clicked.connect(self.ret0)
        self.gl.addWidget(self.ret0_pb, row, 0)
        # Set the 0 position
        self.home_pb = QPushButton("Home")
        self.home_pb.clicked.connect(self.home)
        self.gl.addWidget(self.home_pb, row, 1)
        row += 1

        self.abs_pos_le = QLineEdit('0.0')
        self.gl.addWidget(self.abs_pos_le, row, 0)
        self.mv_abs_pb = QPushButton("Go absolute (mm)")
        self.mv_abs_pb.clicked.connect(self.mv_abs)
        self.gl.addWidget(self.mv_abs_pb, row, 1)
        row += 1

        self.rel_pos_le = QLineEdit('0.0')
        self.gl.addWidget(self.rel_pos_le, row, 0)
        self.mv_rel_pb = QPushButton("Go relative (mm)")
        self.mv_rel_pb.clicked.connect(self.mv_rel)
        self.gl.addWidget(self.mv_rel_pb, row, 1)
        row += 1
        '''
        self.meas_label = QLabel("Meas (um)")
        self.gl.addWidget(self.meas_label, row, 0)
        self.meas_value = QLabel("Unknown")
        self.gl.addWidget(self.meas_value, row, 1)
        # Only resets in the GUI, not related to internal axis position counter
        self.meas_reset_pb = QPushButton("Reset meas")
        self.meas_reset()
        self.meas_reset_pb.clicked.connect(self.meas_reset)
        self.axisSet.connect(self.update_meas)
        self.gl.addWidget(self.meas_reset_pb, row, 0)
        row += 1
        '''

        self.l = QHBoxLayout()
        self.l.addWidget(self.gb)
        self.setLayout(self.l)

    def home(self):
        self.cnc_thread.cmd('home', [self.axis])

    def ret0(self):
        self.cnc_thread.cmd('mv_abs', {self.axis: 0.0})

    def mv_rel(self):
        self.cnc_thread.cmd('mv_rel',
                            {self.axis: float(str(self.rel_pos_le.text()))})

    def mv_abs(self):
        self.cnc_thread.cmd('mv_abs',
                            {self.axis: float(str(self.abs_pos_le.text()))})


class GstImager(Imager):
    def __init__(self, gui):
        Imager.__init__(self)
        self.gui = gui
        self.image_ready = threading.Event()
        self.image_id = None

    def get(self):
        #self.gui.emit_log('gstreamer imager: taking image to %s' % file_name_out)
        def emitSnapshotCaptured(image_id):
            self.gui.emit_log('Image captured reported: %s' % image_id)
            self.image_id = image_id
            self.image_ready.set()

        self.image_id = None
        self.image_ready.clear()
        self.gui.capture_sink.request_image(emitSnapshotCaptured)
        self.gui.emit_log('Waiting for next image...')
        self.image_ready.wait()
        self.gui.emit_log('Got image %s' % self.image_id)
        image = self.gui.capture_sink.pop_image(self.image_id)
        factor = float(uconfig['imager']['scalar'])
        # Use a reasonably high quality filter
        scaled = get_scaled(image, factor, Image.ANTIALIAS)
        #if not self.gui.dry():
        #    scaled.save(file_name_out)
        return scaled


"""
Placeholder class
These are disabled right now and movement must be done from X GUI
"""
class LCNCMovement:
    pass

class MainWindow(QMainWindow):
    cncProgress = pyqtSignal(int, int, str, int)
    snapshotCaptured = pyqtSignal(int)

    def __init__(self, source=None):
        QMainWindow.__init__(self)
        self.showMaximized()

        # FIXME: pull from config file etc
        if source is None:
            pass
        self.vidpip = GstVideoPipeline(source=source, full=True, roi=True)
        # FIXME: review sizing
        self.vidpip.size_widgets(frac=0.2)
        self.vidpip.setupGst(raw_tees=[])

        self.uconfig = uconfig

        # must be created early to accept early logging
        # not displayed until later though
        self.log_widget = QTextEdit()
        # Special case for logging that might occur out of thread
        self.connect(self, SIGNAL('log'), self.log)
        self.connect(self, SIGNAL('pos'), self.update_pos)

        self.pt = None
        self.log_fd = None
        hal = get_cnc_hal(log=self.emit_log)
        hal.progress = self.hal_progress
        self.cnc_thread = CncThread(hal=hal, cmd_done=self.cmd_done)
        self.connect(self.cnc_thread, SIGNAL('log'), self.log)
        self.initUI()

        self.vid_fd = None

        # Must not be initialized until after layout is set
        self.gstWindowId = None
        engine_config = self.uconfig['imager']['engine']

        self.cnc_thread.start()

        # Offload callback to GUI thread so it can do GUI ops
        self.cncProgress.connect(self.processCncProgress)

        self.vidpip.run()
        if self.uconfig['cnc']['startup_run']:
            self.run()

    def __del__(self):
        self.shutdown()

    def shutdown(self):
        self.cnc_thread.hal.ar_stop()
        if self.cnc_thread:
            self.cnc_thread.stop()
            self.cnc_thread = None
        if self.pt:
            self.pt.stop()
            self.pt = None

    def log(self, s='', newline=True):
        if newline:
            s += '\n'

        c = self.log_widget.textCursor()
        c.clearSelection()
        c.movePosition(QTextCursor.End)
        c.insertText(s)
        self.log_widget.setTextCursor(c)

        if self.log_fd is not None:
            self.log_fd.write(s)

    def emit_log(self, s='', newline=True):
        # event must be omitted from the correct thread
        # however, if it hasn't been created yet assume we should log from this thread
        self.emit(SIGNAL('log'), s)

    def update_pos(self, pos):
        for axis, axis_pos in pos.items():
            self.axes[axis].pos_value.setText('%0.3f' % axis_pos)

    def hal_progress(self, pos):
        self.emit(SIGNAL('pos'), pos)

    def emit_pos(self, pos):
        self.emit(SIGNAL('pos'), pos)

    def cmd_done(self, cmd, args, ret):
        print("FIXME: poll position instead of manually querying")

    def reload_obj_cb(self):
        '''Re-populate the objective combo box'''
        self.obj_cb.clear()
        self.obj_config = None
        self.obj_configi = None
        for objective in self.uconfig['objective']:
            self.obj_cb.addItem(objective['name'])

    def update_obj_config(self):
        '''Make resolution display reflect current objective'''
        self.obj_configi = self.obj_cb.currentIndex()
        self.obj_config = self.uconfig['objective'][self.obj_configi]
        self.log('Selected objective %s' % self.obj_config['name'])

        im_w_pix = int(self.uconfig['imager']['width'])
        im_h_pix = int(self.uconfig['imager']['height'])
        im_w_um = self.obj_config["x_view"]
        im_h_um = im_w_um * im_h_pix / im_w_pix
        self.obj_view.setText('View : %0.3fx %0.3fy' % (im_w_um, im_h_um))

    def update_v4l_config(self):
        pass

    def v4l_updated(self):
        for k, v in self.v4ls.items():
            try:
                val = int(str(v.text()))
            except ValueError:
                continue
            if k == 'E':
                val = min(val, 800)
            else:
                val = min(val, 1023)
            ctrl_set(self.vid_fd, k, val)

    def add_v4l_controls(self, cl, row):
        self.v4ls = {}
        # hacked driver to directly drive values
        for ki, (label, v4l_name) in enumerate(
            (("Red", "Red Balance"), ("Green", "Gain"),
             ("Blue", "Blue Balance"), ("Exp", "Exposure"))):
            cols = 4
            rowoff = ki / cols
            coloff = cols * (ki % cols)

            cl.addWidget(QLabel(label), row + rowoff, coloff)
            le = QLineEdit('')
            self.v4ls[v4l_name] = le
            cl.addWidget(le, row + rowoff, coloff + 1)
            le.textChanged.connect(self.v4l_updated)
            row += 2

    def get_config_layout(self):
        cl = QGridLayout()

        row = 0
        l = QLabel("Objective")
        cl.addWidget(l, row, 0)

        self.obj_cb = QComboBox()
        cl.addWidget(self.obj_cb, row, 1)
        self.obj_cb.currentIndexChanged.connect(self.update_obj_config)
        self.obj_view = QLabel("")
        cl.addWidget(self.obj_view, row, 2)
        # seed it
        self.reload_obj_cb()
        self.update_obj_config()
        row += 1

        if 0:
            cl.addWidget(QLabel("Sensor config"), row, 0)
            self.v4l_cb = QComboBox()
            cl.addWidget(self.v4l_cb, row, 1)
            self.v4l_cb.currentIndexChanged.connect(self.update_v4l_config)
            row += 1

        # FIXME: integrate gst controls instead
        # row = self.add_v4l_controls(cl, row)

        return cl

    def get_video_layout(self):
        # Overview
        def low_res_layout():
            layout = QVBoxLayout()
            layout.addWidget(QLabel("Overview"))
            layout.addWidget(self.vidpip.full_widget)

            return layout

        # Higher res in the center for focusing
        def high_res_layout():
            layout = QVBoxLayout()
            layout.addWidget(QLabel("Focus"))
            layout.addWidget(self.vidpip.roi_widget)

            return layout

        layout = QHBoxLayout()
        layout.addLayout(low_res_layout())
        layout.addLayout(high_res_layout())
        return layout

    def setupGst(self):
        pass

    def init_v4l_ctrl(self):
        """
        Was being called on
        self.source.get_property("device-fd")
        v4l is lower priority right now. Revisit later
        """
        print('Initializing V4L controls')
        vconfig = uconfig["imager"].get("v4l2", None)
        if vconfig:
            for configk, configv in vconfig.items():
                break
            print('Selected config %s' % configk)

            for k, v in configv.items():
                #ctrl_set(self.vid_fd, k, v)
                if k in self.v4ls:
                    self.v4ls[k].setText(str(v))

    def ret0(self):
        pos = dict([(k, 0.0) for k in self.axes])
        self.cnc_thread.cmd('mv_abs', pos)

    def home(self):
        self.cnc_thread.cmd('home', [k for k in self.axes])

    def mv_rel(self):
        pos = dict([(k, float(str(axis.rel_pos_le.text())))
                    for k, axis in self.axes.items()])
        self.cnc_thread.cmd('mv_rel', pos)

    def mv_abs(self):
        pos = dict([(k, float(str(axis.abs_pos_le.text())))
                    for k, axis in self.axes.items()])
        self.cnc_thread.cmd('mv_abs', pos)

    def processCncProgress(self, pictures_to_take, pictures_taken, image,
                           first):
        #dbg('Processing CNC progress')
        if first:
            #self.log('First CB with %d items' % pictures_to_take)
            self.pb.setMinimum(0)
            self.pb.setMaximum(pictures_to_take)
            self.bench = Benchmark(pictures_to_take)
        else:
            #self.log('took %s at %d / %d' % (image, pictures_taken, pictures_to_take))
            self.bench.set_cur_items(pictures_taken)
            self.log('Captured: %s' % (image, ))
            self.log('%s' % (str(self.bench)))

        self.pb.setValue(pictures_taken)

    def dry(self):
        return self.dry_cb.isChecked()

    def pause(self):
        if self.pause_pb.text() == 'Pause':
            self.pause_pb.setText('Run')
            self.cnc_thread.setRunning(False)
            if self.pt:
                self.pt.setRunning(False)
            self.log('Pause requested')
        else:
            self.pause_pb.setText('Pause')
            self.cnc_thread.setRunning(True)
            if self.pt:
                self.pt.setRunning(True)
            self.log('Resume requested')

    def write_scan_json(self):
        scan_json = {
            "overlap": 0.7,
            "border": 0.1,
            "start": {
                "x": None,
                "y": None
            },
            "end": {
                "x": None,
                "y": None
            }
        }

        try:
            scan_json['overlap'] = float(self.overlap_le.text())
            scan_json['border'] = float(self.border_le.text())

            scan_json['start']['x'] = float(self.start_pos_x_le.text())
            scan_json['start']['y'] = float(self.start_pos_y_le.text())
            scan_json['end']['x'] = float(self.end_pos_x_le.text())
            scan_json['end']['y'] = float(self.end_pos_y_le.text())
        except ValueError:
            self.log("Bad position")
            return False
        json.dump(scan_json, open('scan.json', 'w'), indent=4, sort_keys=True)
        return True

    def run(self):
        if not self.snapshot_pb.isEnabled():
            self.log("Wait for snapshot to complete before CNC'ing")
            return

        dry = self.dry()
        if dry:
            dbg('Dry run checked')

        if not self.write_scan_json():
            return

        imager = None
        if not dry:
            self.log('Loading imager...')
            itype = self.uconfig['imager']['engine']

            if itype == 'auto':
                if os.path.exists('/dev/video0'):
                    itype = 'gstreamer'
                else:
                    itype = 'gstreamer-testsrc'

            if itype == 'mock':
                imager = MockImager()
            elif itype == 'gstreamer' or itype == 'gstreamer-testsrc':
                imager = GstImager(self)
            else:
                raise Exception('Invalid imager type %s' % itype)

        def emitCncProgress(pictures_to_take, pictures_taken, image, first):
            #print 'Emitting CNC progress'
            if image is None:
                image = ''
            self.cncProgress.emit(pictures_to_take, pictures_taken, image,
                                  first)

        if not dry and not os.path.exists(self.uconfig['out_dir']):
            os.mkdir(self.uconfig['out_dir'])

        out_dir = os.path.join(self.uconfig['out_dir'],
                               str(self.job_name_le.text()))
        if os.path.exists(out_dir):
            self.log("job name dir %s already exists" % out_dir)
            return
        if not dry:
            os.mkdir(out_dir)

        rconfig = {
            'cnc_hal': self.cnc_thread.hal,

            # Will be offloaded to its own thread
            # Operations must be blocking
            # We enforce that nothing is running and disable all CNC GUI controls
            'imager': imager,

            # Callback for progress
            'progress_cb': emitCncProgress,
            'out_dir': out_dir,

            # Comprehensive config structure
            'uscope': self.uconfig,
            # Which objective to use in above config
            'obj': self.obj_configi,

            # Set to true if should try to mimimize hardware actions
            'dry': dry,
            'overwrite': False,
        }

        # If user had started some movement before hitting run wait until its done
        dbg("Waiting for previous movement (if any) to cease")
        # TODO: make this not block GUI
        self.cnc_thread.wait_idle()
        """
        {
            //input directly into planner
            "params": {
                x0: 123,
                y0: 356,
            }
            //planner generated parameters
            "planner": {
                "mm_width": 2.280666667,
                "mm_height": 2.232333333,
                "pix_width": 6842,
                "pix_height": 6697,
                "pix_nm": 333.000000,
            },
            //source specific parameters 
            "imager": {
                "microscope.json": {
                    ...
                }
                "objective": "mit20x",
                "v4l": {
                    "rbal": 123,
                    "bbal": 234,
                    "gain": 345,
                    "exposure": 456
            },
            "sticher": {
                "type": "xystitch"
            },
            "copyright": "&copy; 2020 John McMaster, CC-BY",
        }
        """
        # obj = rconfig['uscope']['objective'][rconfig['obj']]

        imagerj = {}
        imagerj["microscope.json"] = uconfig

        # not sure if this is the right place to add this
        # imagerj['copyright'] = "&copy; %s John McMaster, CC-BY" % datetime.datetime.today().year
        imagerj['objective'] = rconfig['obj']

        # TODO: instead dump from actual v4l
        # safer and more comprehensive
        v4lj = {}
        for k, v in self.v4ls.iteritems():
            v4lj[k] = int(str(v.text()))
        imagerj["v4l"] = v4lj

        self.pt = PlannerThread(self, rconfig, imagerj)
        self.connect(self.pt, SIGNAL('log'), self.log)
        self.pt.plannerDone.connect(self.plannerDone)
        self.setControlsEnabled(False)
        if dry:
            self.log_fd = StringIO()
        else:
            self.log_fd = open(os.path.join(out_dir, 'log.txt'), 'w')

        self.pt.start()

    def setControlsEnabled(self, yes):
        self.go_pb.setEnabled(yes)
        self.mv_abs_pb.setEnabled(yes)
        self.mv_rel_pb.setEnabled(yes)
        self.snapshot_pb.setEnabled(yes)

    def plannerDone(self):
        self.log('RX planner done')
        # Cleanup camera objects
        self.log_fd = None
        self.pt = None
        self.cnc_thread.hal.dry = False
        self.setControlsEnabled(True)
        if self.uconfig['cnc']['startup_run_exit']:
            print('Planner debug break on completion')
            os._exit(1)
        # Prevent accidental start after done
        self.dry_cb.setChecked(True)

    def stop(self):
        '''Stop operations after the next operation'''
        self.cnc_thread.stop()

    def estop(self):
        '''Stop operations immediately.  Position state may become corrupted'''
        self.cnc_thread.estop()

    def clear_estop(self):
        '''Stop operations immediately.  Position state may become corrupted'''
        self.cnc_thread.unestop()

    def set_start_pos(self):
        '''
        try:
            lex = float(self.start_pos_x_le.text())
        except ValueError:
            self.log('WARNING: bad X value')

        try:
            ley = float(self.start_pos_y_le.text())
        except ValueError:
            self.log('WARNING: bad Y value')
        '''
        # take as upper left corner of view area
        # this is the current XY position
        pos = self.cnc_thread.pos()
        #self.log("Updating start pos w/ %s" % (str(pos)))
        self.start_pos_x_le.setText('%0.3f' % pos['x'])
        self.start_pos_y_le.setText('%0.3f' % pos['y'])

    def set_end_pos(self):
        # take as lower right corner of view area
        # this is the current XY position + view size
        pos = self.cnc_thread.pos()
        #self.log("Updating end pos from %s" % (str(pos)))
        x_view = self.obj_config["x_view"]
        y_view = 1.0 * x_view * self.uconfig['imager'][
            'height'] / self.uconfig['imager']['width']
        x1 = pos['x'] + x_view
        y1 = pos['y'] + y_view
        self.end_pos_x_le.setText('%0.3f' % x1)
        self.end_pos_y_le.setText('%0.3f' % y1)

    def get_axes_layout(self):
        layout = QHBoxLayout()
        gb = QGroupBox('Axes')

        def get_general_layout():
            layout = QVBoxLayout()

            def get_go():
                layout = QHBoxLayout()

                self.ret0_pb = QPushButton("Ret0 all")
                self.ret0_pb.clicked.connect(self.ret0)
                layout.addWidget(self.ret0_pb)

                self.mv_abs_pb = QPushButton("Go abs all")
                self.mv_abs_pb.clicked.connect(self.mv_abs)
                layout.addWidget(self.mv_abs_pb)

                self.mv_rel_pb = QPushButton("Go rel all")
                self.mv_rel_pb.clicked.connect(self.mv_rel)
                layout.addWidget(self.mv_rel_pb)

                return layout

            def get_stop():
                layout = QHBoxLayout()

                self.stop_pb = QPushButton("Stop")
                self.stop_pb.clicked.connect(self.stop)
                layout.addWidget(self.stop_pb)

                self.estop_pb = QPushButton("Emergency stop")
                self.estop_pb.clicked.connect(self.estop)
                layout.addWidget(self.estop_pb)

                self.clear_estop_pb = QPushButton("Clear e-stop")
                self.clear_estop_pb.clicked.connect(self.clear_estop)
                layout.addWidget(self.clear_estop_pb)

                return layout

            def get_pos_start():
                layout = QHBoxLayout()

                layout.addWidget(QLabel("Start X0 Y0"))
                self.start_pos_x_le = QLineEdit('0.0')
                layout.addWidget(self.start_pos_x_le)
                self.start_pos_y_le = QLineEdit('0.0')
                layout.addWidget(self.start_pos_y_le)
                self.start_pos_pb = QPushButton("Set")
                self.start_pos_pb.clicked.connect(self.set_start_pos)
                layout.addWidget(self.start_pos_pb)

                return layout

            def get_pos_end():
                layout = QHBoxLayout()

                layout.addWidget(QLabel("End X0 Y0"))
                self.end_pos_x_le = QLineEdit('0.0')
                layout.addWidget(self.end_pos_x_le)
                self.end_pos_y_le = QLineEdit('0.0')
                layout.addWidget(self.end_pos_y_le)
                self.end_pos_pb = QPushButton("Set")
                self.end_pos_pb.clicked.connect(self.set_end_pos)
                layout.addWidget(self.end_pos_pb)

                return layout

            def get_pos_misc():
                layout = QGridLayout()

                layout.addWidget(QLabel('Overlap'), 0, 0)
                self.overlap_le = QLineEdit('0.7')
                layout.addWidget(self.overlap_le, 0, 1)

                layout.addWidget(QLabel('Border'), 1, 0)
                self.border_le = QLineEdit('0.1')
                layout.addWidget(self.border_le, 1, 1)

                return layout

            layout.addLayout(get_go())
            layout.addLayout(get_stop())
            layout.addLayout(get_pos_start())
            layout.addLayout(get_pos_end())
            layout.addLayout(get_pos_misc())
            return layout

        layout.addLayout(get_general_layout())

        self.axes = {}
        dbg('Axes: %u' % len(self.cnc_thread.hal.axes()))
        for axis in sorted(self.cnc_thread.hal.axes()):
            axisw = AxisWidget(axis, self.cnc_thread)
            self.axes[axis] = axisw
            layout.addWidget(axisw)

        gb.setLayout(layout)
        return gb

    def get_snapshot_layout(self):
        gb = QGroupBox('Snapshot')
        layout = QGridLayout()

        snapshot_dir = self.uconfig['imager']['snapshot_dir']
        if not os.path.isdir(snapshot_dir):
            self.log('Snapshot dir %s does not exist' % snapshot_dir)
            if os.path.exists(snapshot_dir):
                raise Exception("Snapshot directory is not accessible")
            os.mkdir(snapshot_dir)
            self.log('Snapshot dir %s created' % snapshot_dir)

        # nah...just have it in the config
        # d = QFileDialog.getExistingDirectory(self, 'Select snapshot directory', snapshot_dir)

        self.snapshot_serial = -1
        layout.addWidget(QLabel('File name'), 0, 0)
        self.snapshot_fn_le = QLineEdit('')
        self.snapshot_suffix_le = QLineEdit('.jpg')
        hl = QHBoxLayout()
        hl.addWidget(self.snapshot_fn_le)
        hl.addWidget(self.snapshot_suffix_le)
        layout.addLayout(hl, 0, 1)

        layout.addWidget(QLabel('Auto-number?'), 1, 0)
        self.auto_number_cb = QCheckBox()
        self.auto_number_cb.setChecked(True)
        layout.addWidget(self.auto_number_cb, 1, 1)

        self.snapshot_pb = QPushButton("Snapshot")
        self.snapshot_pb.clicked.connect(self.take_snapshot)

        self.time_lapse_timer = None
        self.time_lapse_pb = QPushButton("Time lapse")
        self.time_lapse_pb.clicked.connect(self.time_lapse)
        layout.addWidget(self.time_lapse_pb, 2, 1)
        layout.addWidget(self.snapshot_pb, 2, 0)

        gb.setLayout(layout)
        print('snap serial')
        self.snapshot_next_serial()
        return gb

    def snapshot_next_serial(self):
        if not self.auto_number_cb.isChecked():
            print('snap serial not checked')
            return
        prefix = self.snapshot_fn_le.text().split('.')[0]
        if prefix == '':
            print('no base')
            self.snapshot_serial = 0
            prefix = 'snapshot_'
        else:
            dbg('Image prefix: %s' % prefix)
            m = re.search('([a-zA-z0-9_\-]*_)([0-9]+)', prefix)
            if m:
                dbg('snapshot Group 1: ' + m.group(1))
                dbg('snapshot Group 2: ' + m.group(2))
                prefix = m.group(1)
                self.snapshot_serial = int(m.group(2))

        while True:
            self.snapshot_serial += 1
            fn_base = '%s%03u' % (prefix, self.snapshot_serial)
            fn_full = os.path.join(
                self.uconfig['imager']['snapshot_dir'],
                fn_base + str(self.snapshot_suffix_le.text()))
            #print 'check %s' % fn_full
            if os.path.exists(fn_full):
                #dbg('Snapshot %s already exists, skipping' % fn_full)
                continue
            # Omit base to make GUI easier to read
            self.snapshot_fn_le.setText(fn_base)
            break

    def take_snapshot(self):
        self.log('Requesting snapshot')
        # Disable until snapshot is completed
        self.snapshot_pb.setEnabled(False)

        def emitSnapshotCaptured(image_id):
            self.log('Image captured: %s' % image_id)
            self.snapshotCaptured.emit(image_id)

        self.capture_sink.request_image(emitSnapshotCaptured)

    def time_lapse(self):
        if self.time_lapse_pb.text() == 'Stop':
            self.time_lapse_timer.stop()
            self.time_lapse_pb.setText('Time lapse')
        else:
            self.time_lapse_pb.setText('Stop')
            self.time_lapse_timer = QTimer()

            def f():
                self.take_snapshot()

            self.time_lapse_timer.timeout.connect(f)
            # 5 seconds
            # Rather be more aggressive for now
            self.time_lapse_timer.start(5000)
            self.take_snapshot()

    def captureSnapshot(self, image_id):
        self.log('RX image for saving')

        def try_save():
            image = self.capture_sink.pop_image(image_id)
            txt = str(self.snapshot_fn_le.text()) + str(
                self.snapshot_suffix_le.text())

            fn_full = os.path.join(self.uconfig['imager']['snapshot_dir'], txt)
            if os.path.exists(fn_full):
                self.log('WARNING: refusing to overwrite %s' % fn_full)
                return
            factor = float(self.uconfig['imager']['scalar'])
            # Use a reasonably high quality filter
            try:
                get_scaled(image, factor, Image.ANTIALIAS).save(fn_full)
            # FIXME: refine
            except Exception:
                self.log('WARNING: failed to save %s' % fn_full)

        try_save()

        # That image is done, get read for the next
        self.snapshot_next_serial()
        self.snapshot_pb.setEnabled(True)

    def get_scan_layout(self):
        gb = QGroupBox('Scan')
        layout = QGridLayout()

        # TODO: add overlap widgets

        layout.addWidget(QLabel('Job name'), 0, 0)
        self.job_name_le = QLineEdit('default')
        layout.addWidget(self.job_name_le, 0, 1)
        self.go_pb = QPushButton("Go")
        self.go_pb.clicked.connect(self.run)
        layout.addWidget(self.go_pb, 1, 0)
        self.pb = QProgressBar()
        layout.addWidget(self.pb, 1, 1)
        layout.addWidget(QLabel('Dry?'), 2, 0)
        self.dry_cb = QCheckBox()
        self.dry_cb.setChecked(self.uconfig['cnc']['dry'])
        layout.addWidget(self.dry_cb, 2, 1)

        self.pause_pb = QPushButton("Pause")
        self.pause_pb.clicked.connect(self.pause)
        layout.addWidget(self.pause_pb, 3, 0)

        gb.setLayout(layout)
        return gb

    def get_bottom_layout(self):
        layout = QHBoxLayout()
        layout.addWidget(self.get_axes_layout())

        def get_lr_layout():
            layout = QVBoxLayout()
            layout.addWidget(self.get_snapshot_layout())
            layout.addWidget(self.get_scan_layout())
            return layout

        layout.addLayout(get_lr_layout())
        return layout

    def initUI(self):
        self.vidpip.setupWidgets()
        self.setWindowTitle('pr0ncnc')

        # top layout
        layout = QVBoxLayout()

        layout.addLayout(self.get_config_layout())
        layout.addLayout(self.get_video_layout())
        layout.addLayout(self.get_bottom_layout())
        self.log_widget.setReadOnly(True)
        layout.addWidget(self.log_widget)

        w = QWidget()
        w.setLayout(layout)
        self.setCentralWidget(w)
        self.show()

    def keyPressEvent(self, event):
        k = event.key()
        if k == Qt.Key_Escape:
            self.stop()


if __name__ == '__main__':
    gstwidget_main(MainWindow)