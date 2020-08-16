#!/usr/bin/env python3

"""
(bal_gui.py:31743): Gtk-ERROR **: GTK+ 2.x symbols detected. Using GTK+ 2.x and GTK+ 3 in the same process is not supported
"""

from uscope.config import get_config
from uscope.v4l2_util import ctrl_set

from PyQt4 import Qt
from PyQt4.QtGui import *
from PyQt4.QtCore import *
from PyQt4.QtGui import QWidget, QLabel

import queue
import threading
import sys
import traceback
import os
import signal


import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
from gi.repository import Gst
Gst.init(None)
from gi.repository import GstBase, GObject


import io
from PIL import Image

uconfig = get_config()



class MySink(GstBase.BaseSink):
    __gstmetadata__ = ('CustomSink','Sink', \
                      'Custom test sink element', 'Edward Hervey')

    __gsttemplates__ = Gst.PadTemplate.new("sink",
                                           Gst.PadDirection.SINK,
                                           Gst.PadPresence.ALWAYS,
                                           Gst.Caps.new_any())

    def do_render(self, buffer):
        Gst.info("timestamp(buffer):%s" % (Gst.TIME_ARGS(buffer.pts)))
        return Gst.FlowReturn.OK

GObject.type_register(MySink)
__gstelementfactory__ = ("mysink", Gst.Rank.NONE, MySink)



class ImageProcessor(QThread):
    n_frames = pyqtSignal(int) # Number of images

    r_val = pyqtSignal(int) # calc red value
    g_val = pyqtSignal(int) # calc green value
    b_val = pyqtSignal(int) # calc blue value

    r_bal = pyqtSignal(int) # calc red bal (r/g)
    b_bal = pyqtSignal(int) # calc blue bal (b/g)

    def __init__(self):
        QThread.__init__(self)

        self.running = threading.Event()

        self.image_requested = threading.Event()
        self.q = queue.Queue()
        self._n_frames = 0

    def run(self):
        self.running.set()
        self.image_requested.set()
        while self.running.is_set():
            try:
                img = self.q.get(True, 0.1)
            except queue.Empty:
                continue
            img = Image.open(io.StringIO(img))

            rval = 0
            gval = 0
            bval = 0
            xs = 5
            ys = 5
            for y in range(0, img.size[1], ys):
                for x in range(0, img.size[0], xs):
                    (r, g, b) = img.getpixel((x, y))
                    rval += r
                    gval += g
                    bval += b
            sz = img.size[0] * img.size[1] / xs / ys * 256
            rbal = 1.0 * rval / gval
            # gbal = 1.0 * gval / gval
            bbal = 1.0 * bval / gval

            self.r_val.emit(int(rval * 1000.0 / sz))
            self.g_val.emit(int(gval * 1000.0 / sz))
            self.b_val.emit(int(bval * 1000.0 / sz))

            self.r_bal.emit(int((rbal - 1) * 1000.0))
            self.b_bal.emit(int((bbal - 1) * 1000.0))

            self.image_requested.set()

    def stop(self):
        self.running.clear()

    def img_cb(self, buffer):
        self._n_frames += 1
        self.n_frames.emit(self._n_frames)
        '''
        Two major circumstances:
        -Imaging: want next image
        -Snapshot: want next image
        In either case the GUI should listen to all events and clear out the ones it doesn't want
        '''
        #print 'Got image'
        #open('tmp_%d.jpg' % self._n_frames, 'w').write(buffer.data)
        if self.image_requested.is_set():
            #print 'Processing image request'
            # is there a difference between str(buffer) and buffer.data?
            self.q.put(buffer.data)
            # Clear before emitting signal so that it can be re-requested in response
            self.image_requested.clear()


class TestGUI(QMainWindow):
    def __init__(self):
        QMainWindow.__init__(self)
        self.showMaximized()

        self.initUI()

        self.vid_fd = None

        # Must not be initialized until after layout is set
        self.gstWindowId = None
        engine_config = 'gstreamer'
        engine_config = 'gstreamer-testsrc'
        if engine_config == 'gstreamer':
            self.source = Gst.ElementFactory.make("v4l2src", "vsource")
            self.source.set_property("device", "/dev/video0")
            self.vid_fd = -1
            self.setupGst()
        elif engine_config == 'gstreamer-testsrc':
            print('WARNING: using test source')
            self.source = Gst.ElementFactory.make("videotestsrc", "video-source")
            self.setupGst()
        else:
            raise Exception('Unknown engine %s' % (engine_config,))

        self.processor = ImageProcessor()
        self.processor.n_frames.connect(self.n_frames.setNum)
        self.processor.r_val.connect(self.r_val.setNum)
        self.processor.g_val.connect(self.g_val.setNum)
        self.processor.b_val.connect(self.b_val.setNum)
        self.processor.r_bal.connect(self.r_bal.setNum)
        self.processor.b_bal.connect(self.b_bal.setNum)
        self.capture_sink.img_cb = self.processor.img_cb

        self.processor.start()

        if self.gstWindowId:
            print("Starting gstreamer pipeline")
            self.player.set_state(Gst.State.PLAYING)

    def awb(self):
        # makes one step for now

        # note
        # rb is out of 1000
        # actual is out of 1024

        rv = int(self.r_val.text())
        gv = int(self.g_val.text())
        bv = int(self.b_val.text())

        rb = int(self.r_bal.text())
        bb = int(self.b_bal.text())

        # Using hacked driver where these are set directly
        setg = int(self.ctrls["Gain"].text())
        setr = int(self.ctrls["Red Balance"].text())
        setb = int(self.ctrls["Blue Balance"].text())


        # make a linear guess based on the difference
        # it might under or overshoot, but should converge in time
        limit = lambda x: max(min(int(x), 1023), 0)
        sf = 0.2
        rb_new = limit(setr - rb * sf)
        bb_new = limit(setb - bb * sf)
        print('Step')
        print('  R: %d w/ %d => %d' % (setr, rb, rb_new))
        print('  B: %d w/ %d => %d' % (setb, bb, bb_new))

        #ctrl_set(self.vid_fd, "Red Balance", rb_new)
        self.ctrls["Red Balance"].setText(str(rb_new))

        #ctrl_set(self.vid_fd, "Blue Balance", bb_new)
        self.ctrls["Blue Balance"].setText(str(bb_new))

    def get_video_layout(self):
        # Overview
        def low_res_layout():
            layout = QVBoxLayout()
            layout.addWidget(QLabel("Overview"))

            # Raw X-windows canvas
            self.video_container = QWidget()
            # Allows for convenient keyboard control by clicking on the video
            self.video_container.setFocusPolicy(Qt.ClickFocus)
            w, h = 3264/4, 2448/4
            self.video_container.setMinimumSize(w, h)
            self.video_container.resize(w, h)
            policy = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.video_container.setSizePolicy(policy)

            layout.addWidget(self.video_container)

            return layout

        self.awb_pb = QPushButton("AWG (G, E fixed)")
        self.awb_pb.clicked.connect(self.awb)

        layout = QHBoxLayout()
        layout.addLayout(low_res_layout())
        layout.addWidget(self.awb_pb)
        return layout

    def get_ctrl_layout(self):

        layout = QGridLayout()
        row = 0

        self.ctrls = {}
        for name in ("Red Balance", "Gain", "Blue Balance", "Exposure"):
            def textChanged(name):
                def f():
                    if self.vid_fd >= 0:
                        try:
                            val = int(self.ctrls[name].text())
                        except ValueError:
                            pass
                        else:
                            print('%s changed => %d' % (name, val))
                            ctrl_set(self.vid_fd, name, val)
                return f

            layout.addWidget(QLabel(name), row, 0)
            ctrl = QLineEdit('0')
            ctrl.textChanged.connect(textChanged(name))
            self.ctrls[name] = ctrl
            layout.addWidget(ctrl, row, 1)
            row += 1

        return layout

    def get_rgb_layout(self):

        layout = QGridLayout()
        row = 0

        layout.addWidget(QLabel('N'), row, 0)
        self.n_frames = QLabel('0')
        layout.addWidget(self.n_frames, row, 1)
        row += 1

        layout.addWidget(QLabel('R_V'), row, 0)
        self.r_val = QLabel('0')
        layout.addWidget(self.r_val, row, 1)
        row += 1

        layout.addWidget(QLabel('G_V'), row, 0)
        self.g_val = QLabel('0')
        layout.addWidget(self.g_val, row, 1)
        row += 1

        layout.addWidget(QLabel('B_V'), row, 0)
        self.b_val = QLabel('0')
        layout.addWidget(self.b_val, row, 1)
        row += 1

        layout.addWidget(QLabel('R_B'), row, 0)
        self.r_bal = QLabel('0')
        layout.addWidget(self.r_bal, row, 1)
        row += 1

        layout.addWidget(QLabel('B_B'), row, 0)
        self.b_bal = QLabel('0')
        layout.addWidget(self.b_bal, row, 1)
        row += 1

        return layout

    def setupGst(self):
        print("Setting up gstreamer pipeline")
        self.gstWindowId = self.video_container.winId()

        self.player = Gst.Pipeline("player")
        self.tee = Gst.ElementFactory.make("tee")
        sinkx = Gst.ElementFactory.make("ximagesink", 'sinkx_overview')
        # fcs = Gst.ElementFactory.make('ffmpegcolorspace')
        fcs = Gst.ElementFactory.make('videoconvert')
        assert fcs
        #caps = Gst.Caps.from_string('video/x-raw-yuv')
        caps = Gst.Caps.from_string('video/x-raw,format=yuv')
        self.capture_enc = Gst.ElementFactory.make("jpegenc")
        self.capture_sink = Gst.ElementFactory.make("mysink")
        assert self.capture_sink
        self.capture_sink_queue = Gst.ElementFactory.make("queue")
        self.resizer =  Gst.ElementFactory.make("videoscale")

        # Video render stream
        self.player.add(      self.source, self.tee)
        #Gst.element_link_many(self.source, self.tee)
        self.source.link(self.tee)

        assert self.resizer
        assert sinkx
        self.player.add(fcs,                 self.resizer, sinkx)
        #Gst.element_link_many(self.tee, fcs, self.resizer, sinkx)
        self.tee.link(fcs)
        fcs.link(self.resizer)
        self.resizer.link(sinkx)

        assert self.capture_sink_queue
        assert self.capture_enc
        self.player.add(self.capture_sink_queue, self.capture_enc, self.capture_sink)
        #Gst.element_link_many(self.tee, self.capture_sink_queue, self.capture_enc, self.capture_sink)
        self.tee.link(self.capture_sink_queue)
        self.capture_sink_queue.link(self.capture_enc)
        self.capture_enc.link(self.capture_sink)

        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.enable_sync_message_emission()
        bus.connect("message", self.on_message)
        bus.connect("sync-message::element", self.on_sync_message)

    def on_message(self, bus, message):
        t = message.type

        if self.vid_fd is not None and self.vid_fd < 0:
            self.vid_fd = self.source.get_property("device-fd")
            if self.vid_fd >= 0:
                print('Initializing V4L controls')
                self.v4l_load()

        if t == Gst.MessageType.EOS:
            self.player.set_state(Gst.State.NULL)
            print("End of stream")
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print("Error: %s" % err, debug)
            self.player.set_state(Gst.State.NULL)
            ''

    def v4l_load(self):
        vconfig = uconfig["imager"].get("v4l2", None)
        if not vconfig:
            return
        for configk, configv in vconfig.items():
            break
        #if type(configv) != dict or '"Gain"' not in configv:
        #    raise Exception("Bad v4l default config (old style?)")

        print('Selected config %s' % configk)
        for k, v in configv.items():
            if k in self.ctrls:
                self.ctrls[k].setText(str(v))
            else:
                ctrl_set(self.vid_fd, k, v)

    def on_sync_message(self, bus, message):
        if message.structure is None:
            return
        message_name = message.structure.get_name()
        if message_name == "prepare-xwindow-id":
            if message.src.get_name() == 'sinkx_overview':
                print('sinkx_overview win_id')
                win_id = self.gstWindowId
            else:
                raise Exception('oh noes')

            assert win_id
            imagesink = message.src
            imagesink.set_xwindow_id(win_id)

    def initUI(self):
        self.setGeometry(300, 300, 250, 150)
        self.setWindowTitle('pyv4l test')

        # top layout
        layout = QHBoxLayout()

        layout.addLayout(self.get_ctrl_layout())
        layout.addLayout(self.get_rgb_layout())
        layout.addLayout(self.get_video_layout())

        w = QWidget()
        w.setLayout(layout)
        self.setCentralWidget(w)
        self.show()

def excepthook(excType, excValue, tracebackobj):
    print('%s: %s' % (excType, excValue))
    traceback.print_tb(tracebackobj)
    os._exit(1)

if __name__ == '__main__':
    '''
    We are controlling a robot
    '''
    sys.excepthook = excepthook
    # Exit on ^C instead of ignoring
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    GObject.threads_init()

    app = QApplication(sys.argv)
    _gui = TestGUI()
    # XXX: what about the gstreamer message bus?
    # Is it simply not running?
    # must be what pygst is doing
    sys.exit(app.exec_())