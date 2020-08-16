from PyQt4.Qt import Qt
from PyQt4.QtGui import QSizePolicy, QApplication
from PyQt4.QtGui import QWidget, QLabel

import sys
import traceback
import os
import signal

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
gi.require_version('GstVideo', '1.0')

# Needed for window.get_xid(), xvimagesink.set_window_handle(), respectively:
# from gi.repository import GdkX11, GstVideo
from gi.repository import GstVideo

from gi.repository import Gst
Gst.init(None)
from gi.repository import GstBase, GObject

class GstVideoPipeline:
    def __init__(self, parent=None):
        self.setupWidgets(parent)

    def setupWidgets(self, parent):
        # Raw X-windows canvas
        self.widget = QWidget(parent=parent)
        # Allows for convenient keyboard control by clicking on the video
        self.widget.setFocusPolicy(Qt.ClickFocus)
        w, h = 5440/4, 3648/4
        self.widget.setMinimumSize(w, h)
        self.widget.resize(w, h)
        policy = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.widget.setSizePolicy(policy)

    def prepareSource(self):
        # Must not be initialized until after layout is set
        self.gstWindowId = None
        engine_config = 'gst-v4l2src'
        #engine_config = 'gst-videotestsrc'
        #engine_config = 'gst-toupcamsrc'
        if engine_config == 'gst-v4l2src':
            self.source = Gst.ElementFactory.make('v4l2src', None)
            assert self.source is not None
            self.source.set_property("device", "/dev/video0")
        elif engine_config == 'gst-toupcamsrc':
            self.source = Gst.ElementFactory.make('toupcamsrc', None)
            assert self.source is not None
        elif engine_config == 'gst-videotestsrc':
            print('WARNING: using test source')
            self.source = Gst.ElementFactory.make('videotestsrc', None)
        else:
            raise Exception('Unknown engine %s' % (engine_config,))

    def setupGst(self, tee=None):
        self.prepareSource()
        print("Setting up gstreamer pipeline")
        self.gstWindowId = self.widget.winId()

        self.player = Gst.Pipeline("player")
        self.sinkx = Gst.ElementFactory.make("ximagesink", 'sinkx_overview')
        assert self.sinkx is not None
        self.videoconvert = Gst.ElementFactory.make('videoconvert')
        assert self.videoconvert is not None
        caps = Gst.caps_from_string('video/x-raw,format=rgb')
        assert caps is not None
        self.capture_enc = Gst.ElementFactory.make("jpegenc")
        self.resizer =  Gst.ElementFactory.make("videoscale")
        assert self.resizer is not None

        # Video render stream
        self.player.add(self.source)

        self.player.add(self.videoconvert, self.resizer, self.sinkx)
        if tee:
            self.tee = Gst.ElementFactory.make("tee")
            self.player.add(self.tee)
            self.source.link(self.tee)
    
            self.queue_us = Gst.ElementFactory.make("queue")
            self.player.add(self.queue_us)
            self.tee.link(self.queue_us)
            self.queue_us.link(self.videoconvert)
    
            self.queue_them = Gst.ElementFactory.make("queue")
            self.player.add(self.queue_them)
            self.tee.link(self.queue_them)
            self.player.add(tee)
            self.queue_them.link(tee)
        else:
            self.source.link(self.videoconvert)
        self.videoconvert.link(self.resizer)
        self.resizer.link(self.sinkx)

        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.enable_sync_message_emission()
        bus.connect("message", self.on_message)
        bus.connect("sync-message::element", self.on_sync_message)

        if self.gstWindowId:
            print("Starting gstreamer pipeline")
            self.player.set_state(Gst.State.PLAYING)

    def on_message(self, bus, message):
        t = message.type

        if t == Gst.MessageType.EOS:
            self.player.set_state(Gst.State.NULL)
            print("End of stream")
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print("Error: %s" % err, debug)
            self.player.set_state(Gst.State.NULL)

    def on_sync_message(self, bus, message):
        print("sync1", message.src.get_name())
        if message.get_structure() is None:
            return
        message_name = message.get_structure().get_name()
        print("sync2", message_name)
        if message_name == "prepare-window-handle":
            assert message.src.get_name() == 'sinkx_overview'
            imagesink = message.src
            imagesink.set_property("force-aspect-ratio", True)
            imagesink.set_window_handle(self.gstWindowId)

def excepthook(excType, excValue, tracebackobj):
    print('%s: %s' % (excType, excValue))
    traceback.print_tb(tracebackobj)
    os._exit(1)

def gstwidget_main(AQMainWindow):
    '''
    We are controlling a robot
    '''
    sys.excepthook = excepthook
    # Exit on ^C instead of ignoring
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    GObject.threads_init()

    app = QApplication(sys.argv)
    _gui = AQMainWindow()
    # XXX: what about the gstreamer message bus?
    # Is it simply not running?
    # must be what pygst is doing
    sys.exit(app.exec_())


class CbSink(GstBase.BaseSink):
    __gstmetadata__ = ('CustomSink','Sink', \
                      'Custom test sink element', 'John McMaster')

    __gsttemplates__ = Gst.PadTemplate.new("sink",
                                           Gst.PadDirection.SINK,
                                           Gst.PadPresence.ALWAYS,
                                           Gst.Caps.new_any())

    def __init__(self, *args, **kwargs):
        GstBase.BaseSink.__init__(self, *args, **kwargs)
        self.cb = None

    def do_render(self, buffer):
        print("do_render()")
        if self.cb:
            self.cb(buffer)
        return Gst.FlowReturn.OK

# XXX: these aren't properly registering anymore, but good enough
GObject.type_register(CbSink)
__gstelementfactory__ = ("cbsink", Gst.Rank.NONE, CbSink)