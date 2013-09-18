#!/usr/bin/env python

# Paddock is a front end gui for Replicape and similar type 
# LCD displays

import os, Queue, re

import random
import wx

import sys, glob, time, datetime, threading, traceback, cStringIO, subprocess
import shlex

from printrun.pronterface_widgets import *
from serial import SerialException

winsize = (800, 480)
layerindex = 0

import printcore
from printrun.printrun_utils import pixmapfile, configfile
from printrun.gui import MainWindow
import pronsole
from pronsole import dosify, wxSetting, HiddenSetting, StringSetting, SpinSetting, FloatSpinSetting, BooleanSetting
from printrun import gcoder

def parse_temperature_report(report, key):
    if key in report:
        return float(filter(lambda x: x.startswith(key), report.split())[0].split(":")[1].split("/")[0])
    else: 
        return -1.0

def format_time(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")

def format_duration(delta):
    return str(datetime.timedelta(seconds = int(delta)))

class Tee(object):
    def __init__(self, target):
        self.stdout = sys.stdout
        sys.stdout = self
        self.target = target
    def __del__(self):
        sys.stdout = self.stdout
    def write(self, data):
        try:
            self.target(data)
        except:
            pass
        try:
            data = data.encode("utf-8")
        except:
            pass
        self.stdout.write(data)
    def flush(self):
        self.stdout.flush()

def parse_build_dimensions(bdim):
    # a string containing up to six numbers delimited by almost anything
    # first 0-3 numbers specify the build volume, no sign, always positive
    # remaining 0-3 numbers specify the coordinates of the "southwest" corner of the build platform
    # "XXX,YYY"
    # "XXXxYYY+xxx-yyy"
    # "XXX,YYY,ZZZ+xxx+yyy-zzz"
    # etc
    bdl = re.findall("([-+]?[0-9]*\.?[0-9]*)", bdim)
    defaults = [200, 200, 100, 0, 0, 0, 0, 0, 0]
    bdl = filter(None, bdl)
    bdl_float = [float(value) if value else defaults[i] for i, value in enumerate(bdl)]
    if len(bdl_float) < len(defaults):
        bdl_float += [defaults[i] for i in range(len(bdl_float), len(defaults))]
    return bdl_float

class BuildDimensionsSetting(wxSetting):

    widgets = None

    def _set_value(self, value):
        self._value = value
        if self.widgets:
            self._set_widgets_values(value)
    value = property(wxSetting._get_value, _set_value)

    def _set_widgets_values(self, value):
        build_dimensions_list = parse_build_dimensions(value)
        for i in range(len(self.widgets)):
            self.widgets[i].SetValue(build_dimensions_list[i])        

    def get_widget(self, parent):
        from wx.lib.agw.floatspin import FloatSpin
        import wx
        build_dimensions = parse_build_dimensions(self.value)
        self.widgets = []
        w = lambda val, m, M: self.widgets.append(FloatSpin(parent, -1, value = val, min_val = m, max_val = M, digits = 2))
        addlabel = lambda name, pos: self.widget.Add(wx.StaticText(parent, -1, name), pos = pos, flag = wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border = 5)
        addwidget = lambda *pos: self.widget.Add(self.widgets[-1], pos = pos, flag = wx.RIGHT, border = 5)
        self.widget = wx.GridBagSizer()
        addlabel(_("Width"), (0, 0))
        w(build_dimensions[0], 0, 2000)
        addwidget(0, 1)
        addlabel(_("Depth"), (0, 2))
        w(build_dimensions[1], 0, 2000)
        addwidget(0, 3)
        addlabel(_("Height"), (0, 4))
        w(build_dimensions[2], 0, 2000)
        addwidget(0, 5)
        addlabel(_("X offset"), (1, 0))
        w(build_dimensions[3], -2000, 2000)
        addwidget(1, 1)
        addlabel(_("Y offset"), (1, 2))
        w(build_dimensions[4], -2000, 2000)
        addwidget(1, 3)
        addlabel(_("Z offset"), (1, 4))
        w(build_dimensions[5], -2000, 2000)
        addwidget(1, 5)
        addlabel(_("X home pos."), (2, 0))
        w(build_dimensions[6], -2000, 2000)
        self.widget.Add(self.widgets[-1], pos = (2, 1))
        addlabel(_("Y home pos."), (2, 2))
        w(build_dimensions[7], -2000, 2000)
        self.widget.Add(self.widgets[-1], pos = (2, 3))
        addlabel(_("Z home pos."), (2, 4))
        w(build_dimensions[8], -2000, 2000)
        self.widget.Add(self.widgets[-1], pos = (2, 5))
        return self.widget

    def update(self):
        values = [float(w.GetValue()) for w in self.widgets]
        self.value = "%.02fx%.02fx%.02f%+.02f%+.02f%+.02f%+.02f%+.02f%+.02f" % tuple(values)

class StringSetting(wxSetting):

    def get_specific_widget(self, parent):
        import wx
        self.widget = wx.TextCtrl(parent, -1, str(self.value))
        return self.widget

class ComboSetting(wxSetting):
    
    def __init__(self, name, default, choices, label = None, help = None):
        super(ComboSetting, self).__init__(name, default, label, help)
        self.choices = choices

    def get_specific_widget(self, parent):
        import wx
        self.widget = wx.ComboBox(parent, -1, str(self.value), choices = self.choices, style = wx.CB_DROPDOWN)
        return self.widget

class PronterWindow(MainWindow, pronsole.pronsole):
    def __init__(self, filename = None, size = winsize):
        pronsole.pronsole.__init__(self)
        #default build dimensions are 200x200x100 with 0, 0, 0 in the corner of the bed and endstops at 0, 0 and 0
        monitorsetting = BooleanSetting("monitor", False)
        monitorsetting.hidden = True        
        self.settings._add(monitorsetting)
        self.settings._add(BuildDimensionsSetting("build_dimensions", "200x200x100+0+0+0+0+0+0", _("Build dimensions"), _("Dimensions of Build Platform\n & optional offset of origin\n & optional switch position\n\nExamples:\n   XXXxYYY\n   XXX,YYY,ZZZ\n   XXXxYYYxZZZ+OffX+OffY+OffZ\nXXXxYYYxZZZ+OffX+OffY+OffZ+HomeX+HomeY+HomeZ")))
        self.settings._add(BooleanSetting("viz3d", False, _("Enable 3D viewer (requires restarting)"), _("Use 3D visualization instead of 2D layered visualization")))
        self.settings._add(ComboSetting("mainviz", "2D", ["2D", "3D", "None"], _("Main visualization"), _("Select visualization for main window.")))
        self.settings._add(HiddenSetting("last_bed_temperature", 0.0))
        self.settings._add(HiddenSetting("last_file_path", ""))
        self.settings._add(HiddenSetting("last_temperature", 0.0))
        self.settings._add(FloatSpinSetting("preview_extrusion_width", 0.5, 0, 10, _("Preview extrusion width"), _("Width of Extrusion in Preview (default: 0.5)")))
        self.settings._add(SpinSetting("preview_grid_step1", 10., 0, 200, _("Fine grid spacing"), _("Fine Grid Spacing (default: 10)")))
        self.settings._add(SpinSetting("preview_grid_step2", 50., 0, 200, _("Coarse grid spacing"), _("Coarse Grid Spacing (default: 50)")))
        self.settings._add(StringSetting("bgcolor", "#FFFFFF", _("Background color"), _("Pronterface background color (default: #FFFFFF)")))
        self.pauseScript = "pause.gcode"
        self.endScript = "end.gcode"
       
        self.filename = filename
        #os.putenv("UBUNTU_MENUPROXY", "0")
        MainWindow.__init__(self, None, title = _("Paddock"), size = size);
        if hasattr(sys,"frozen") and sys.frozen=="windows_exe":
            self.SetIcon(wx.Icon(sys.executable, wx.BITMAP_TYPE_ICO))
        else:
            self.SetIcon(wx.Icon(pixmapfile("P-face.ico"), wx.BITMAP_TYPE_ICO))
        self.panel = wx.Panel(self,-1, size = size)

        self.statuscheck = False
        self.status_thread = None
        self.capture_skip = {}
        self.capture_skip_newline = False
        self.tempreport = ""
        self.monitor = 0
        self.f = None
        self.skeinp = None
        self.monitor_interval = 3
        self.current_pos = [0, 0, 0]
        self.paused = False
        self.sentlines = Queue.Queue(0)
        self.cpbuttons = [
            SpecialButton(_("Motors off"), ("M84"), (250, 250, 250), None, 0, _("Switch all motors off")),
            SpecialButton(_("Check temp"), ("M105"), (225, 200, 200), (2, 5), (1, 1), _("Check current hotend temperature")),
            SpecialButton(_("Extrude"), ("extrude"), (225, 200, 200), (4, 0), (1, 2), _("Advance extruder by set length")),
            SpecialButton(_("Reverse"), ("reverse"), (225, 200, 200), (5, 0), (1, 2), _("Reverse extruder by set length")),
        ]
        self.custombuttons = []
        self.btndict = {}
        self.autoconnect = False
        self.parse_cmdline(sys.argv[1:])
        self.build_dimensions_list = parse_build_dimensions(self.settings.build_dimensions)
        
        #initialize the code analyzer with the correct sizes. There must be a more general way to do so

        # minimum = offset
        self.p.analyzer.minX = self.build_dimensions_list[3]
        self.p.analyzer.minY = self.build_dimensions_list[4]
        self.p.analyzer.minZ = self.build_dimensions_list[5]
        
        #max = offset + bedsize
        self.p.analyzer.maxX = self.build_dimensions_list[3] + self.build_dimensions_list[0]
        self.p.analyzer.maxY = self.build_dimensions_list[4] + self.build_dimensions_list[1]
        self.p.analyzer.maxZ = self.build_dimensions_list[5] + self.build_dimensions_list[2]
        
        self.p.analyzer.homeX = self.build_dimensions_list[6]
        self.p.analyzer.homeY = self.build_dimensions_list[7]
        self.p.analyzer.homeZ = self.build_dimensions_list[8]
                
        #set feedrates in printcore for pause/resume
        self.p.xy_feedrate = self.settings.xy_feedrate
        self.p.z_feedrate = self.settings.z_feedrate
        
        #make printcore aware of me
        self.p.pronterface = self
        
        self.panel.SetBackgroundColour(self.settings.bgcolor)
        customdict = {}        
        self.popmenu()
        self.createGui()
        self.t = Tee(self.catchprint)
        self.stdout = sys.stdout
        self.skeining = 0
        self.mini = False
        self.p.sendcb = self.sentcb
        self.p.printsendcb = self.printsentcb
        self.p.startcb = self.startcb
        self.p.endcb = self.endcb
        self.starttime = 0
        self.extra_print_time = 0
        self.curlayer = 0
        self.cur_button = None
        self.predisconnect_mainqueue = None
        self.predisconnect_queueindex = None
        self.predisconnect_layer = None
        self.hsetpoint = 0.0
        self.bsetpoint = 0.0
        if self.autoconnect:
            self.connect()
        if self.filename is not None:
            self.do_load(self.filename)
        if self.settings.monitor:
            self.setmonitor(None)

    def add_cmdline_arguments(self, parser):
        pronsole.pronsole.add_cmdline_arguments(self, parser)
        parser.add_argument('-g','--gauges', help = _("display graphical temperature gauges in addition to the temperatures graph"), action = "store_true")
        parser.add_argument('-a','--autoconnect', help = _("automatically try to connect to printer on startup"), action = "store_true")

    def process_cmdline_arguments(self, args):
        pronsole.pronsole.process_cmdline_arguments(self, args)
        self.display_gauges = args.gauges
        self.autoconnect = args.autoconnect

    def startcb(self):
        self.starttime = time.time()
        print _("Print Started at: %s") % format_time(self.starttime)

    def endcb(self):
        if self.p.queueindex == 0:
            print_duration = int(time.time () - self.starttime + self.extra_print_time)
            print _("Print ended at: %(end_time)s and took %(duration)s") % {"end_time": format_time(time.time()),
                                                                             "duration": format_duration(print_duration)}
            wx.CallAfter(self.pausebtn.Disable)
            wx.CallAfter(self.printbtn.SetLabel, _("Print"))

            self.p.runSmallScript(self.endScript)
            
            param = self.settings.final_command
            if not param:
                return
            pararray = [i.replace("$s", str(self.filename)).replace("$t", format_duration(print_duration)).encode() for i in shlex.split(param.replace("\\", "\\\\").encode())]
            self.finalp = subprocess.Popen(pararray, stderr = subprocess.STDOUT, stdout = subprocess.PIPE)




    def online(self):
        print _("Printer is now online.")
        self.connectbtn.SetLabel(_("Disconnect"))
        self.connectbtn.SetToolTip(wx.ToolTip("Disconnect from the printer"))
        self.connectbtn.Bind(wx.EVT_BUTTON, self.disconnect)

        for i in self.printerControls:
            wx.CallAfter(i.Enable)

        # Enable XYButtons and ZButtons
        wx.CallAfter(self.xyb.enable)
        wx.CallAfter(self.zb.enable)

        if self.filename:
            wx.CallAfter(self.printbtn.Enable)

    def sentcb(self, line):
        gline = gcoder.Line(line)
        gline.parse_coordinates(imperial = False)
        if gline.is_move:
            if gline.z != None:
                layer = gline.z
                if layer != self.curlayer:
                    self.curlayer = layer
                    self.gviz.clearhilights()
                    wx.CallAfter(self.gviz.setlayer, layer)
        elif gline.command in ["M104", "M109"]:
            gline.parse_coordinates(imperial = False, force = True)
            if gline.s != None:
                temp = gline.s
                if self.display_gauges: wx.CallAfter(self.hottgauge.SetTarget, temp)
                wx.CallAfter(self.graph.SetExtruder0TargetTemperature, temp)
        elif gline.command == "M140":
            gline.parse_coordinates(imperial = False, force = True)
            if gline.s != None:
                temp = gline.s
                if self.display_gauges: wx.CallAfter(self.bedtgauge.SetTarget, temp)
                wx.CallAfter(self.graph.SetBedTargetTemperature, temp)
        else:
            return
        self.sentlines.put_nowait(line)

    def printsentcb(self, gline):
        if gline.is_move and hasattr(self.gwindow, "set_current_gline"):
            wx.CallAfter(self.gwindow.set_current_gline, gline)
        if gline.is_move and hasattr(self.gviz, "set_current_gline"):
            wx.CallAfter(self.gviz.set_current_gline, gline)

    def do_extrude(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(self.edist.GetValue())
            pronsole.pronsole.do_extrude(self, l)
        except:
            raise

    def do_reverse(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(- float(self.edist.GetValue()))
            pronsole.pronsole.do_extrude(self, l)
        except:
            pass

    def setbedgui(self, f):
        self.bsetpoint = f
        if self.display_gauges: self.bedtgauge.SetTarget(int(f))
        wx.CallAfter(self.graph.SetBedTargetTemperature, int(f))
        if f>0:
            wx.CallAfter(self.btemp.SetValue, str(f))
            self.set("last_bed_temperature", str(f))
            wx.CallAfter(self.setboff.SetBackgroundColour, None)
            wx.CallAfter(self.setboff.SetForegroundColour, None)
            wx.CallAfter(self.setbbtn.SetBackgroundColour, "#FFAA66")
            wx.CallAfter(self.setbbtn.SetForegroundColour, "#660000")
            wx.CallAfter(self.btemp.SetBackgroundColour, "#FFDABB")
        else:
            wx.CallAfter(self.setboff.SetBackgroundColour, "#0044CC")
            wx.CallAfter(self.setboff.SetForegroundColour, "white")
            wx.CallAfter(self.setbbtn.SetBackgroundColour, None)
            wx.CallAfter(self.setbbtn.SetForegroundColour, None)
            wx.CallAfter(self.btemp.SetBackgroundColour, "white")
            wx.CallAfter(self.btemp.Refresh)

    def sethotendgui(self, f):
        self.hsetpoint = f
        if self.display_gauges: self.hottgauge.SetTarget(int(f))
        wx.CallAfter(self.graph.SetExtruder0TargetTemperature, int(f))
        if f > 0:
            wx.CallAfter(self.htemp.SetValue, str(f))
            self.set("last_temperature", str(f))
            wx.CallAfter(self.settoff.SetBackgroundColour, None)
            wx.CallAfter(self.settoff.SetForegroundColour, None)
            wx.CallAfter(self.settbtn.SetBackgroundColour, "#FFAA66")
            wx.CallAfter(self.settbtn.SetForegroundColour, "#660000")
            wx.CallAfter(self.htemp.SetBackgroundColour, "#FFDABB")
        else:
            wx.CallAfter(self.settoff.SetBackgroundColour, "#0044CC")
            wx.CallAfter(self.settoff.SetForegroundColour, "white")
            wx.CallAfter(self.settbtn.SetBackgroundColour, None)
            wx.CallAfter(self.settbtn.SetForegroundColour, None)
            wx.CallAfter(self.htemp.SetBackgroundColour, "white")
            wx.CallAfter(self.htemp.Refresh)

    def do_settemp(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(self.htemp.GetValue().split()[0])
            l = l.lower().replace(", ", ".")
            for i in self.temps.keys():
                l = l.replace(i, self.temps[i])
            f = float(l)
            if f >= 0:
                if self.p.online:
                    self.p.send_now("M104 S"+l)
                    print _("Setting hotend temperature to %f degrees Celsius.") % f
                    self.sethotendgui(f)
                else:
                    print _("Printer is not online.")
            else:
                print _("You cannot set negative temperatures. To turn the hotend off entirely, set its temperature to 0.")
        except Exception, x:
            print _("You must enter a temperature. (%s)") % (repr(x),)

    def do_bedtemp(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(self.btemp.GetValue().split()[0])
            l = l.lower().replace(", ", ".")
            for i in self.bedtemps.keys():
                l = l.replace(i, self.bedtemps[i])
            f = float(l)
            if f >= 0:
                if self.p.online:
                    self.p.send_now("M140 S"+l)
                    print _("Setting bed temperature to %f degrees Celsius.") % f
                    self.setbedgui(f)
                else:
                    print _("Printer is not online.")
            else:
                print _("You cannot set negative temperatures. To turn the bed off entirely, set its temperature to 0.")
        except Exception, x:
            print _("You must enter a temperature. (%s)") % (repr(x),)

    def end_macro(self):
        pronsole.pronsole.end_macro(self)
        self.update_macros_menu()

    def delete_macro(self, macro_name):
        pronsole.pronsole.delete_macro(self, macro_name)
        self.update_macros_menu()

    def start_macro(self, macro_name, old_macro_definition = ""):
        if not self.processing_rc:
            def cb(definition):
                if len(definition.strip()) == 0:
                    if old_macro_definition != "":
                        dialog = wx.MessageDialog(self, _("Do you want to erase the macro?"), style = wx.YES_NO|wx.YES_DEFAULT|wx.ICON_QUESTION)
                        if dialog.ShowModal() == wx.ID_YES:
                            self.delete_macro(macro_name)
                            return
                    print _("Cancelled.")
                    return
                self.cur_macro_name = macro_name
                self.cur_macro_def = definition
                self.end_macro()
            MacroEditor(macro_name, old_macro_definition, cb)
        else:
            pronsole.pronsole.start_macro(self, macro_name, old_macro_definition)

    def catchprint(self, l):
        if self.capture_skip_newline and len(l) and not len(l.strip("\n\r")):
            self.capture_skip_newline = False
            return
        for pat in self.capture_skip.keys():
            if self.capture_skip[pat] > 0 and pat.match(l):
                self.capture_skip[pat] -= 1
                self.capture_skip_newline = True
                return
        wx.CallAfter(self.addtexttolog,l);    

    def project(self,event):
        from printrun import projectlayer
        if self.p.online:
            projectlayer.setframe(self,self.p).Show()
        else:
            print _("Printer is not online.")

    def popmenu(self):
        self.menustrip = wx.MenuBar()
        # File menu
        m = wx.Menu()
        self.Bind(wx.EVT_MENU, self.loadfile, m.Append(-1, _("&Open..."), _(" Opens file")))
        self.Bind(wx.EVT_MENU, self.do_editgcode, m.Append(-1, _("&Edit..."), _(" Edit open file")))
        self.Bind(wx.EVT_MENU, self.clearOutput, m.Append(-1, _("Clear console"), _(" Clear output console")))
        self.Bind(wx.EVT_MENU, self.project, m.Append(-1, _("Projector"), _(" Project slices")))
        self.Bind(wx.EVT_MENU, self.OnExit, m.Append(wx.ID_EXIT, _("E&xit"), _(" Closes the Window")))
        self.menustrip.Append(m, _("&File"))

        # Settings menu
        m = wx.Menu()
        self.macros_menu = wx.Menu()
        m.AppendSubMenu(self.macros_menu, _("&Macros"))
        self.Bind(wx.EVT_MENU, self.new_macro, self.macros_menu.Append(-1, _("<&New...>")))
        self.Bind(wx.EVT_MENU, lambda *e: PronterOptions(self), m.Append(-1, _("&Options"), _(" Options dialog")))

        self.Bind(wx.EVT_MENU, lambda x: threading.Thread(target = lambda:self.do_skein("set")).start(), m.Append(-1, _("Slicing Settings"), _(" Adjust slicing settings")))

        mItem = m.AppendCheckItem(-1, _("Debug G-code"),
            _("Print all G-code sent to and received from the printer."))
        m.Check(mItem.GetId(), self.p.loud)
        self.Bind(wx.EVT_MENU, self.setloud, mItem)

        #try:
        #    from SkeinforgeQuickEditDialog import SkeinforgeQuickEditDialog
        #    self.Bind(wx.EVT_MENU, lambda *e:SkeinforgeQuickEditDialog(self), m.Append(-1,_("SFACT Quick Settings"),_(" Quickly adjust SFACT settings for active profile")))
        #except:
        #    pass

        self.menustrip.Append(m, _("&Settings"))
        self.update_macros_menu()
        self.SetMenuBar(self.menustrip)

    def doneediting(self, gcode):
        f = open(self.filename, "w")
        f.write("\n".join(gcode))
        f.close()
        wx.CallAfter(self.loadfile, None, self.filename)

    def do_editgcode(self, e = None):
        if self.filename is not None:
            MacroEditor(self.filename, self.f, self.doneediting, 1)

    def new_macro(self, e = None):
        dialog = wx.Dialog(self, -1, _("Enter macro name"), size = (260, 85))
        panel = wx.Panel(dialog, -1)
        vbox = wx.BoxSizer(wx.VERTICAL)
        wx.StaticText(panel, -1, _("Macro name:"), (8, 14))
        dialog.namectrl = wx.TextCtrl(panel, -1, '', (110, 8), size = (130, 24), style = wx.TE_PROCESS_ENTER)
        hbox = wx.BoxSizer(wx.HORIZONTAL)
        okb = wx.Button(dialog, wx.ID_OK, _("Ok"), size = (60, 24))
        dialog.Bind(wx.EVT_TEXT_ENTER, lambda e:dialog.EndModal(wx.ID_OK), dialog.namectrl)
        #dialog.Bind(wx.EVT_BUTTON, lambda e:self.new_macro_named(dialog, e), okb)
        hbox.Add(okb)
        hbox.Add(wx.Button(dialog, wx.ID_CANCEL, _("Cancel"), size = (60, 24)))
        vbox.Add(panel)
        vbox.Add(hbox, 1, wx.ALIGN_CENTER|wx.TOP|wx.BOTTOM, 10)
        dialog.SetSizer(vbox)
        dialog.Centre()
        macro = ""
        if dialog.ShowModal() == wx.ID_OK:
            macro = dialog.namectrl.GetValue()
            if macro != "":
                wx.CallAfter(self.edit_macro, macro)
        dialog.Destroy()
        return macro

    def edit_macro(self, macro):
        if macro == "": return self.new_macro()
        if self.macros.has_key(macro):
            old_def = self.macros[macro]
        elif len([c for c in macro.encode("ascii", "replace") if not c.isalnum() and c != "_"]):
            print _("Macro name may contain only ASCII alphanumeric symbols and underscores")
            return
        elif hasattr(self.__class__, "do_"+macro):
            print _("Name '%s' is being used by built-in command") % macro
            return
        else:
            old_def = ""
        self.start_macro(macro, old_def)
        return macro

    def update_macros_menu(self):
        if not hasattr(self, "macros_menu"):
            return # too early, menu not yet built
        try:
            while True:
                item = self.macros_menu.FindItemByPosition(1)
                if item is None: return
                self.macros_menu.DeleteItem(item)
        except:
            pass
        for macro in self.macros.keys():
            self.Bind(wx.EVT_MENU, lambda x, m = macro: self.start_macro(m, self.macros[m]), self.macros_menu.Append(-1, macro))

    def OnExit(self, event):
        self.Close()

    def rescanports(self, event = None):
        scan = self.scanserial()
        portslist = list(scan)
        if self.settings.port != "" and self.settings.port not in portslist:
            portslist += [self.settings.port]
            self.serialport.Clear()
            self.serialport.AppendItems(portslist)
        try:
            if os.path.exists(self.settings.port) or self.settings.port in scan:
                self.serialport.SetValue(self.settings.port)
            elif len(portslist) > 0:
                self.serialport.SetValue(portslist[0])
        except:
            pass

    def cbkey(self, e):
        if e.GetKeyCode() == wx.WXK_UP:
            if self.commandbox.histindex == len(self.commandbox.history):
                self.commandbox.history+=[self.commandbox.GetValue()] #save current command
            if len(self.commandbox.history):
                self.commandbox.histindex = (self.commandbox.histindex-1)%len(self.commandbox.history)
                self.commandbox.SetValue(self.commandbox.history[self.commandbox.histindex])
                self.commandbox.SetSelection(0, len(self.commandbox.history[self.commandbox.histindex]))
        elif e.GetKeyCode() == wx.WXK_DOWN:
            if self.commandbox.histindex == len(self.commandbox.history):
                self.commandbox.history+=[self.commandbox.GetValue()] #save current command
            if len(self.commandbox.history):
                self.commandbox.histindex = (self.commandbox.histindex+1)%len(self.commandbox.history)
                self.commandbox.SetValue(self.commandbox.history[self.commandbox.histindex])
                self.commandbox.SetSelection(0, len(self.commandbox.history[self.commandbox.histindex]))
        else:
            e.Skip()

    def plate(self, e):
        import plater
        print "plate function activated"
        plater.stlwin(size = (800, 580), callback = self.platecb, parent = self).Show()

    def platecb(self, name):
        print "plated: "+name
        self.loadfile(None, name)

    def sdmenu(self, e):
        obj = e.GetEventObject()
        popupmenu = wx.Menu()
        item = popupmenu.Append(-1, _("SD Upload"))
        if not self.f:
            item.Enable(False)
        self.Bind(wx.EVT_MENU, self.upload, id = item.GetId())
        item = popupmenu.Append(-1, _("SD Print"))
        self.Bind(wx.EVT_MENU, self.sdprintfile, id = item.GetId())
        self.panel.PopupMenu(popupmenu, obj.GetPosition())

    def htemp_change(self, event):
        if self.hsetpoint > 0:
            self.do_settemp("")
        wx.CallAfter(self.htemp.SetInsertionPoint, 0)

    def btemp_change(self, event):
        if self.bsetpoint > 0:
            self.do_bedtemp("")
        wx.CallAfter(self.btemp.SetInsertionPoint, 0)

    def showwin(self, event):
        if self.f:
            self.gwindow.Show(True)
            self.gwindow.SetToolTip(wx.ToolTip("Mousewheel zooms the display\nShift / Mousewheel scrolls layers"))
            self.gwindow.Raise()

    def setfeeds(self, e):
        self.feedrates_changed = True
        try:
            self.settings._set("e_feedrate", self.efeedc.GetValue())
        except:
            pass
        try:
            self.settings._set("z_feedrate", self.zfeedc.GetValue())
        except:
            pass
        try:
            self.settings._set("xy_feedrate", self.xyfeedc.GetValue())
        except:
            pass

    def toggleview(self, e):
        if(self.mini):
            self.mini = False
            self.mainsizer.Fit(self)

            #self.SetSize(winsize)
            wx.CallAfter(self.minibtn.SetLabel, _("Mini mode"))

        else:
            self.mini = True
            self.uppersizer.Fit(self)

            #self.SetSize(winssize)
            wx.CallAfter(self.minibtn.SetLabel, _("Full mode"))

    def cbuttons_reload(self):
        allcbs = []
        ubs = self.uppersizer
        cs = self.centersizer
        #for item in ubs.GetChildren():
        #    if hasattr(item.GetWindow(),"custombutton"):
        #        allcbs += [(ubs, item.GetWindow())]
        for item in cs.GetChildren():
            if hasattr(item.GetWindow(),"custombutton"):
                allcbs += [(cs, item.GetWindow())]
        for sizer, button in allcbs:
            #sizer.Remove(button)
            button.Destroy()
        self.custombuttonbuttons = []
        newbuttonbuttonindex = len(self.custombuttons)
        while newbuttonbuttonindex>0 and self.custombuttons[newbuttonbuttonindex-1] is None:
            newbuttonbuttonindex -= 1
        while len(self.custombuttons) < 13:
            self.custombuttons.append(None)
        for i in xrange(len(self.custombuttons)):
            btndef = self.custombuttons[i]
            try:
                b = wx.Button(self.panel, -1, btndef.label, style = wx.BU_EXACTFIT)
                b.SetToolTip(wx.ToolTip(_("Execute command: ")+btndef.command))
                if btndef.background:
                    b.SetBackgroundColour(btndef.background)
                    rr, gg, bb = b.GetBackgroundColour().Get()
                    if 0.3*rr+0.59*gg+0.11*bb < 60:
                        b.SetForegroundColour("#ffffff")
            except:
                if i == newbuttonbuttonindex:
                    self.newbuttonbutton = b = wx.Button(self.panel, -1, "+", size = (19, 18), style = wx.BU_EXACTFIT)
                    #b.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
                    b.SetForegroundColour("#4444ff")
                    b.SetToolTip(wx.ToolTip(_("click to add new custom button")))
                    b.Bind(wx.EVT_BUTTON, self.cbutton_edit)
                else:
                    b = wx.Button(self.panel,-1, ".", size = (1, 1))
                    #b = wx.StaticText(self.panel,-1, "", size = (72, 22), style = wx.ALIGN_CENTRE+wx.ST_NO_AUTORESIZE) #+wx.SIMPLE_BORDER
                    b.Disable()
                    #continue
            b.custombutton = i
            b.properties = btndef
            if btndef is not None:
                b.Bind(wx.EVT_BUTTON, self.procbutton)
                b.Bind(wx.EVT_MOUSE_EVENTS, self.editbutton)
            #else:
            #    b.Bind(wx.EVT_BUTTON, lambda e:e.Skip())
            self.custombuttonbuttons.append(b)
            #if i<4:
            #    ubs.Add(b)
            #else:
            cs.Add(b, pos = ((i)/4, (i)%4))
        self.mainsizer.Layout()

    def help_button(self):
        print _('Defines custom button. Usage: button <num> "title" [/c "colour"] command')

    def do_button(self, argstr):
        def nextarg(rest):
            rest = rest.lstrip()
            if rest.startswith('"'):
                return rest[1:].split('"',1)
            else:
                return rest.split(None, 1)
        #try:
        num, argstr = nextarg(argstr)
        num = int(num)
        title, argstr = nextarg(argstr)
        colour = None
        try:
            c1, c2 = nextarg(argstr)
            if c1 == "/c":
                colour, argstr = nextarg(c2)
        except:
            pass
        command = argstr.strip()
        if num<0 or num>=64:
            print _("Custom button number should be between 0 and 63")
            return
        while num >= len(self.custombuttons):
            self.custombuttons.append(None)
        self.custombuttons[num] = SpecialButton(title, command)
        if colour is not None:
            self.custombuttons[num].background = colour
        if not self.processing_rc:
            self.cbuttons_reload()
        #except Exception, x:
        #    print "Bad syntax for button definition, see 'help button'"
        #    print x

    def cbutton_save(self, n, bdef, new_n = None):
        if new_n is None: new_n = n
        if bdef is None or bdef == "":
            self.save_in_rc(("button %d" % n),'')
        elif bdef.background:
            colour = bdef.background
            if type(colour) not in (str, unicode):
                #print type(colour), map(type, colour)
                if type(colour) == tuple and tuple(map(type, colour)) == (int, int, int):
                    colour = map(lambda x:x%256, colour)
                    colour = wx.Colour(*colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
                else:
                    colour = wx.Colour(colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
            self.save_in_rc(("button %d" % n),'button %d "%s" /c "%s" %s' % (new_n, bdef.label, colour, bdef.command))
        else:
            self.save_in_rc(("button %d" % n),'button %d "%s" %s' % (new_n, bdef.label, bdef.command))

    def cbutton_edit(self, e, button = None):
        bedit = ButtonEdit(self)
        if button is not None:
            n = button.custombutton
            bedit.name.SetValue(button.properties.label)
            bedit.command.SetValue(button.properties.command)
            if button.properties.background:
                colour = button.properties.background
                if type(colour) not in (str, unicode):
                    #print type(colour)
                    if type(colour) == tuple and tuple(map(type, colour)) == (int, int, int):
                        colour = map(lambda x:x%256, colour)
                        colour = wx.Colour(*colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
                    else:
                        colour = wx.Colour(colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
                bedit.color.SetValue(colour)
        else:
            n = len(self.custombuttons)
            while n>0 and self.custombuttons[n-1] is None:
                n -= 1
        if bedit.ShowModal() == wx.ID_OK:
            if n == len(self.custombuttons):
                self.custombuttons+=[None]
            self.custombuttons[n]=SpecialButton(bedit.name.GetValue().strip(), bedit.command.GetValue().strip(), custom = True)
            if bedit.color.GetValue().strip()!="":
                self.custombuttons[n].background = bedit.color.GetValue()
            self.cbutton_save(n, self.custombuttons[n])
        wx.CallAfter(bedit.Destroy)
        wx.CallAfter(self.cbuttons_reload)

    def cbutton_remove(self, e, button):
        n = button.custombutton
        self.custombuttons[n]=None
        self.cbutton_save(n, None)
        #while len(self.custombuttons) and self.custombuttons[-1] is None:
        #    del self.custombuttons[-1]
        wx.CallAfter(self.cbuttons_reload)

    def cbutton_order(self, e, button, dir):
        n = button.custombutton
        if dir<0:
            n = n-1
        if n+1 >= len(self.custombuttons):
            self.custombuttons+=[None] # pad
        # swap
        self.custombuttons[n], self.custombuttons[n+1] = self.custombuttons[n+1], self.custombuttons[n]
        self.cbutton_save(n, self.custombuttons[n])
        self.cbutton_save(n+1, self.custombuttons[n+1])
        #if self.custombuttons[-1] is None:
        #    del self.custombuttons[-1]
        wx.CallAfter(self.cbuttons_reload)

    def editbutton(self, e):
        if e.IsCommandEvent() or e.ButtonUp(wx.MOUSE_BTN_RIGHT):
            if e.IsCommandEvent():
                pos = (0, 0)
            else:
                pos = e.GetPosition()
            popupmenu = wx.Menu()
            obj = e.GetEventObject()
            if hasattr(obj, "custombutton"):
                item = popupmenu.Append(-1, _("Edit custom button '%s'") % e.GetEventObject().GetLabelText())
                self.Bind(wx.EVT_MENU, lambda e, button = e.GetEventObject():self.cbutton_edit(e, button), item)
                item = popupmenu.Append(-1, _("Move left <<"))
                self.Bind(wx.EVT_MENU, lambda e, button = e.GetEventObject():self.cbutton_order(e, button,-1), item)
                if obj.custombutton == 0: item.Enable(False)
                item = popupmenu.Append(-1, _("Move right >>"))
                self.Bind(wx.EVT_MENU, lambda e, button = e.GetEventObject():self.cbutton_order(e, button, 1), item)
                if obj.custombutton == 63: item.Enable(False)
                pos = self.panel.ScreenToClient(e.GetEventObject().ClientToScreen(pos))
                item = popupmenu.Append(-1, _("Remove custom button '%s'") % e.GetEventObject().GetLabelText())
                self.Bind(wx.EVT_MENU, lambda e, button = e.GetEventObject():self.cbutton_remove(e, button), item)
            else:
                item = popupmenu.Append(-1, _("Add custom button"))
                self.Bind(wx.EVT_MENU, self.cbutton_edit, item)
            self.panel.PopupMenu(popupmenu, pos)
        elif e.Dragging() and e.ButtonIsDown(wx.MOUSE_BTN_LEFT):
            obj = e.GetEventObject()
            scrpos = obj.ClientToScreen(e.GetPosition())
            if not hasattr(self, "dragpos"):
                self.dragpos = scrpos
                e.Skip()
                return
            else:
                dx, dy = self.dragpos[0]-scrpos[0], self.dragpos[1]-scrpos[1]
                if dx*dx+dy*dy < 5*5: # threshold to detect dragging for jittery mice
                    e.Skip()
                    return
            if not hasattr(self, "dragging"):
                # init dragging of the custom button
                if hasattr(obj, "custombutton") and obj.properties is not None:
                    #self.newbuttonbutton.SetLabel("")
                    #self.newbuttonbutton.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
                    #self.newbuttonbutton.SetForegroundColour("black")
                    #self.newbuttonbutton.SetSize(obj.GetSize())
                    #if self.uppersizer.GetItem(self.newbuttonbutton) is not None:
                    #    self.uppersizer.SetItemMinSize(self.newbuttonbutton, obj.GetSize())
                    #    self.mainsizer.Layout()
                    for b in self.custombuttonbuttons:
                        #if b.IsFrozen(): b.Thaw()
                        if b.properties is None:
                            b.Enable()
                            b.SetLabel("")
                            b.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
                            b.SetForegroundColour("black")
                            b.SetSize(obj.GetSize())
                            if self.uppersizer.GetItem(b) is not None:
                                self.uppersizer.SetItemMinSize(b, obj.GetSize())
                                self.mainsizer.Layout()
                        #    b.SetStyle(wx.ALIGN_CENTRE+wx.ST_NO_AUTORESIZE+wx.SIMPLE_BORDER)
                    self.dragging = wx.Button(self.panel,-1, obj.GetLabel(), style = wx.BU_EXACTFIT)
                    self.dragging.SetBackgroundColour(obj.GetBackgroundColour())
                    self.dragging.SetForegroundColour(obj.GetForegroundColour())
                    self.dragging.sourcebutton = obj
                    self.dragging.Raise()
                    self.dragging.Disable()
                    self.dragging.SetPosition(self.panel.ScreenToClient(scrpos))
                    self.last_drag_dest = obj
                    self.dragging.label = obj.s_label = obj.GetLabel()
                    self.dragging.bgc = obj.s_bgc = obj.GetBackgroundColour()
                    self.dragging.fgc = obj.s_fgc = obj.GetForegroundColour()
            else:
                # dragging in progress
                self.dragging.SetPosition(self.panel.ScreenToClient(scrpos))
                wx.CallAfter(self.dragging.Refresh)
                btns = self.custombuttonbuttons
                dst = None
                src = self.dragging.sourcebutton
                drg = self.dragging
                for b in self.custombuttonbuttons:
                    if b.GetScreenRect().Contains(scrpos):
                        dst = b
                        break
                #if dst is None and self.panel.GetScreenRect().Contains(scrpos):
                #    # try to check if it is after buttons at the end
                #    tspos = self.panel.ClientToScreen(self.uppersizer.GetPosition())
                #    bspos = self.panel.ClientToScreen(self.centersizer.GetPosition())
                #    tsrect = wx.Rect(*(tspos.Get()+self.uppersizer.GetSize().Get()))
                #    bsrect = wx.Rect(*(bspos.Get()+self.centersizer.GetSize().Get()))
                #    lbrect = btns[-1].GetScreenRect()
                #    p = scrpos.Get()
                #    if len(btns)<4 and tsrect.Contains(scrpos):
                #        if lbrect.GetRight() < p[0]:
                #            print "Right of last button on upper cb sizer"
                #    if bsrect.Contains(scrpos):
                #        if lbrect.GetBottom() < p[1]:
                #            print "Below last button on lower cb sizer"
                #        if lbrect.GetRight() < p[0] and lbrect.GetTop() <= p[1] and lbrect.GetBottom() >= p[1]:
                #            print "Right to last button on lower cb sizer"
                if dst is not self.last_drag_dest:
                    if self.last_drag_dest is not None:
                        self.last_drag_dest.SetBackgroundColour(self.last_drag_dest.s_bgc)
                        self.last_drag_dest.SetForegroundColour(self.last_drag_dest.s_fgc)
                        self.last_drag_dest.SetLabel(self.last_drag_dest.s_label)
                    if dst is not None and dst is not src:
                        dst.s_bgc = dst.GetBackgroundColour()
                        dst.s_fgc = dst.GetForegroundColour()
                        dst.s_label = dst.GetLabel()
                        src.SetBackgroundColour(dst.GetBackgroundColour())
                        src.SetForegroundColour(dst.GetForegroundColour())
                        src.SetLabel(dst.GetLabel())
                        dst.SetBackgroundColour(drg.bgc)
                        dst.SetForegroundColour(drg.fgc)
                        dst.SetLabel(drg.label)
                    else:
                        src.SetBackgroundColour(drg.bgc)
                        src.SetForegroundColour(drg.fgc)
                        src.SetLabel(drg.label)
                    self.last_drag_dest = dst
        elif hasattr(self, "dragging") and not e.ButtonIsDown(wx.MOUSE_BTN_LEFT):
            # dragging finished
            obj = e.GetEventObject()
            scrpos = obj.ClientToScreen(e.GetPosition())
            dst = None
            src = self.dragging.sourcebutton
            drg = self.dragging
            for b in self.custombuttonbuttons:
                if b.GetScreenRect().Contains(scrpos):
                    dst = b
                    break
            if dst is not None:
                src_i = src.custombutton
                dst_i = dst.custombutton
                self.custombuttons[src_i], self.custombuttons[dst_i] = self.custombuttons[dst_i], self.custombuttons[src_i]
                self.cbutton_save(src_i, self.custombuttons[src_i])
                self.cbutton_save(dst_i, self.custombuttons[dst_i])
                while self.custombuttons[-1] is None:
                    del self.custombuttons[-1]
            wx.CallAfter(self.dragging.Destroy)
            del self.dragging
            wx.CallAfter(self.cbuttons_reload)
            del self.last_drag_dest
            del self.dragpos
        else:
            e.Skip()

    def homeButtonClicked(self, corner):
        # When user clicks on the XY control, the Z control no longer gets spacebar/repeat signals
        self.zb.clearRepeat()
        if corner == 0: # upper-left
            self.onecmd('home X')
        elif corner == 1: # upper-right
            self.onecmd('home Y')
        elif corner == 2: # lower-right
            self.onecmd('home Z')
        elif corner == 3: # lower-left
            self.onecmd('home')
        else:
            return
        self.p.send_now('M114')

    def moveXY(self, x, y):
        # When user clicks on the XY control, the Z control no longer gets spacebar/repeat signals
        self.zb.clearRepeat()
        if x != 0:
            self.onecmd('move X %s' % x)
        elif y != 0:
            self.onecmd('move Y %s' % y)
        else:
            return
        self.p.send_now('M114')

    def moveZ(self, z):
        if z != 0:
            self.onecmd('move Z %s' % z)
            self.p.send_now('M114')
        # When user clicks on the Z control, the XY control no longer gets spacebar/repeat signals
        self.xyb.clearRepeat()

    def spacebarAction(self):
        self.zb.repeatLast()
        self.xyb.repeatLast()

    def procbutton(self, e):
        try:
            if hasattr(e.GetEventObject(),"custombutton"):
                if wx.GetKeyState(wx.WXK_CONTROL) or wx.GetKeyState(wx.WXK_ALT):
                    return self.editbutton(e)
                self.cur_button = e.GetEventObject().custombutton
            self.onecmd(e.GetEventObject().properties.command)
            self.cur_button = None
        except:
            print _("event object missing")
            self.cur_button = None
            raise

    def kill(self, e):
        self.statuscheck = False
        if self.status_thread:
            self.status_thread.join()
            self.status_thread = None
        self.p.recvcb = None
        self.p.disconnect()
        if hasattr(self, "feedrates_changed"):
            self.save_in_rc("set xy_feedrate", "set xy_feedrate %d" % self.settings.xy_feedrate)
            self.save_in_rc("set z_feedrate", "set z_feedrate %d" % self.settings.z_feedrate)
            self.save_in_rc("set e_feedrate", "set e_feedrate %d" % self.settings.e_feedrate)
        wx.CallAfter(self.gwindow.Destroy)
        wx.CallAfter(self.Destroy)

    def do_monitor(self, l = ""):
        if l.strip()=="":
            self.monitorbox.SetValue(not self.monitorbox.GetValue())
        elif l.strip()=="off":
            wx.CallAfter(self.monitorbox.SetValue, False)
        else:
            try:
                self.monitor_interval = float(l)
                wx.CallAfter(self.monitorbox.SetValue, self.monitor_interval>0)
            except:
                print _("Invalid period given.")
        self.setmonitor(None)
        if self.monitor:
            print _("Monitoring printer.")
        else:
            print _("Done monitoring.")

    def setmonitor(self, e):
        self.monitor = self.monitorbox.GetValue()
        self.set("monitor", self.monitor)
        if self.monitor:
            wx.CallAfter(self.graph.StartPlotting, 1000)
        else:
            wx.CallAfter(self.graph.StopPlotting)

    def addtexttolog(self,text):
        try:
            self.logbox.AppendText(text)
        except:
            print _("Attempted to write invalid text to console, which could be due to an invalid baudrate")

    def setloud(self,e):
        self.p.loud=e.IsChecked()

    def sendline(self, e):
        command = self.commandbox.GetValue()
        if not len(command):
            return
        wx.CallAfter(self.addtexttolog, ">>>" + command + "\n");
        self.onecmd(str(command))
        self.commandbox.SetSelection(0, len(command))
        self.commandbox.history.append(command)
        self.commandbox.histindex = len(self.commandbox.history)

    def clearOutput(self, e):
        self.logbox.Clear()

    def update_tempdisplay(self):
        try:
            hotend_temp = parse_temperature_report(self.tempreport, "T:")
            wx.CallAfter(self.graph.SetExtruder0Temperature, hotend_temp)
            if self.display_gauges: wx.CallAfter(self.hottgauge.SetValue, hotend_temp)
            bed_temp = parse_temperature_report(self.tempreport, "B:")
            wx.CallAfter(self.graph.SetBedTemperature, bed_temp)
            if self.display_gauges: wx.CallAfter(self.bedtgauge.SetValue, bed_temp)
        except:
            traceback.print_exc()

    def update_pos(self, l):
        bits = gcoder.m114_exp.findall(l)
        x = None
        y = None
        z = None
    def online(self):
        print _("Printer is now online.")
        self.connectbtn.SetLabel(_("Disconnect"))
        self.connectbtn.SetToolTip(wx.ToolTip("Disconnect from the printer"))
        self.connectbtn.Bind(wx.EVT_BUTTON, self.disconnect)

        for i in self.printerControls:
            wx.CallAfter(i.Enable)

        # Enable XYButtons and ZButtons
        wx.CallAfter(self.xyb.enable)
        wx.CallAfter(self.zb.enable)

        if self.filename:
            wx.CallAfter(self.printbtn.Enable)

    def sentcb(self, line):
        gline = gcoder.Line(line)
        gline.parse_coordinates(imperial = False)
        if gline.is_move:
            if gline.z != None:
                layer = gline.z
                if layer != self.curlayer:
                    self.curlayer = layer
                    self.gviz.clearhilights()
                    wx.CallAfter(self.gviz.setlayer, layer)
        elif gline.command in ["M104", "M109"]:
            gline.parse_coordinates(imperial = False, force = True)
            if gline.s != None:
                temp = gline.s
                if self.display_gauges: wx.CallAfter(self.hottgauge.SetTarget, temp)
                wx.CallAfter(self.graph.SetExtruder0TargetTemperature, temp)
        elif gline.command == "M140":
            gline.parse_coordinates(imperial = False, force = True)
            if gline.s != None:
                temp = gline.s
                if self.display_gauges: wx.CallAfter(self.bedtgauge.SetTarget, temp)
                wx.CallAfter(self.graph.SetBedTargetTemperature, temp)
        else:
            return
        self.sentlines.put_nowait(line)

    def printsentcb(self, gline):
        if gline.is_move and hasattr(self.gwindow, "set_current_gline"):
            wx.CallAfter(self.gwindow.set_current_gline, gline)
        if gline.is_move and hasattr(self.gviz, "set_current_gline"):
            wx.CallAfter(self.gviz.set_current_gline, gline)

    def do_extrude(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(self.edist.GetValue())
            pronsole.pronsole.do_extrude(self, l)
        except:
            raise

    def do_reverse(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(- float(self.edist.GetValue()))
            pronsole.pronsole.do_extrude(self, l)
        except:
            pass

    def setbedgui(self, f):
        self.bsetpoint = f
        if self.display_gauges: self.bedtgauge.SetTarget(int(f))
        wx.CallAfter(self.graph.SetBedTargetTemperature, int(f))
        if f>0:
            wx.CallAfter(self.btemp.SetValue, str(f))
            self.set("last_bed_temperature", str(f))
            wx.CallAfter(self.setboff.SetBackgroundColour, None)
            wx.CallAfter(self.setboff.SetForegroundColour, None)
            wx.CallAfter(self.setbbtn.SetBackgroundColour, "#FFAA66")
            wx.CallAfter(self.setbbtn.SetForegroundColour, "#660000")
            wx.CallAfter(self.btemp.SetBackgroundColour, "#FFDABB")
        else:
            wx.CallAfter(self.setboff.SetBackgroundColour, "#0044CC")
            wx.CallAfter(self.setboff.SetForegroundColour, "white")
            wx.CallAfter(self.setbbtn.SetBackgroundColour, None)
            wx.CallAfter(self.setbbtn.SetForegroundColour, None)
            wx.CallAfter(self.btemp.SetBackgroundColour, "white")
            wx.CallAfter(self.btemp.Refresh)

    def sethotendgui(self, f):
        self.hsetpoint = f
        if self.display_gauges: self.hottgauge.SetTarget(int(f))
        wx.CallAfter(self.graph.SetExtruder0TargetTemperature, int(f))
        if f > 0:
            wx.CallAfter(self.htemp.SetValue, str(f))
            self.set("last_temperature", str(f))
            wx.CallAfter(self.settoff.SetBackgroundColour, None)
            wx.CallAfter(self.settoff.SetForegroundColour, None)
            wx.CallAfter(self.settbtn.SetBackgroundColour, "#FFAA66")
            wx.CallAfter(self.settbtn.SetForegroundColour, "#660000")
            wx.CallAfter(self.htemp.SetBackgroundColour, "#FFDABB")
        else:
            wx.CallAfter(self.settoff.SetBackgroundColour, "#0044CC")
            wx.CallAfter(self.settoff.SetForegroundColour, "white")
            wx.CallAfter(self.settbtn.SetBackgroundColour, None)
            wx.CallAfter(self.settbtn.SetForegroundColour, None)
            wx.CallAfter(self.htemp.SetBackgroundColour, "white")
            wx.CallAfter(self.htemp.Refresh)

    def do_settemp(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(self.htemp.GetValue().split()[0])
            l = l.lower().replace(", ", ".")
            for i in self.temps.keys():
                l = l.replace(i, self.temps[i])
            f = float(l)
            if f >= 0:
                if self.p.online:
                    self.p.send_now("M104 S"+l)
                    print _("Setting hotend temperature to %f degrees Celsius.") % f
                    self.sethotendgui(f)
                else:
                    print _("Printer is not online.")
            else:
                print _("You cannot set negative temperatures. To turn the hotend off entirely, set its temperature to 0.")
        except Exception, x:
            print _("You must enter a temperature. (%s)") % (repr(x),)

    def do_bedtemp(self, l = ""):
        try:
            if not l.__class__ in (str, unicode) or not len(l):
                l = str(self.btemp.GetValue().split()[0])
            l = l.lower().replace(", ", ".")
            for i in self.bedtemps.keys():
                l = l.replace(i, self.bedtemps[i])
            f = float(l)
            if f >= 0:
                if self.p.online:
                    self.p.send_now("M140 S"+l)
                    print _("Setting bed temperature to %f degrees Celsius.") % f
                    self.setbedgui(f)
                else:
                    print _("Printer is not online.")
            else:
                print _("You cannot set negative temperatures. To turn the bed off entirely, set its temperature to 0.")
        except Exception, x:
            print _("You must enter a temperature. (%s)") % (repr(x),)

    def end_macro(self):
        pronsole.pronsole.end_macro(self)
        self.update_macros_menu()

    def delete_macro(self, macro_name):
        pronsole.pronsole.delete_macro(self, macro_name)
        self.update_macros_menu()

    def start_macro(self, macro_name, old_macro_definition = ""):
        if not self.processing_rc:
            def cb(definition):
                if len(definition.strip()) == 0:
                    if old_macro_definition != "":
                        dialog = wx.MessageDialog(self, _("Do you want to erase the macro?"), style = wx.YES_NO|wx.YES_DEFAULT|wx.ICON_QUESTION)
                        if dialog.ShowModal() == wx.ID_YES:
                            self.delete_macro(macro_name)
                            return
                    print _("Cancelled.")
                    return
                self.cur_macro_name = macro_name
                self.cur_macro_def = definition
                self.end_macro()
            MacroEditor(macro_name, old_macro_definition, cb)
        else:
            pronsole.pronsole.start_macro(self, macro_name, old_macro_definition)

    def catchprint(self, l):
        if self.capture_skip_newline and len(l) and not len(l.strip("\n\r")):
            self.capture_skip_newline = False
            return
        for pat in self.capture_skip.keys():
            if self.capture_skip[pat] > 0 and pat.match(l):
                self.capture_skip[pat] -= 1
                self.capture_skip_newline = True
                return
        wx.CallAfter(self.addtexttolog,l);

    def scanserial(self):
        """scan for available ports. return a list of device names."""
        baselist = []
        if os.name == "nt":
            try:
                key = _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE, "HARDWARE\\DEVICEMAP\\SERIALCOMM")
                i = 0
                while True:
                    baselist += [_winreg.EnumValue(key, i)[1]]
                    i += 1
            except:
                pass
        return baselist+glob.glob('/dev/pts/*') 

    def project(self,event):
        from printrun import projectlayer
        if self.p.online:
            projectlayer.setframe(self,self.p).Show()
        else:
            print _("Printer is not online.")

    def popmenu(self):
        self.menustrip = wx.MenuBar()
        # File menu
        m = wx.Menu()
        self.Bind(wx.EVT_MENU, self.loadfile, m.Append(-1, _("&Open..."), _(" Opens file")))
        self.Bind(wx.EVT_MENU, self.do_editgcode, m.Append(-1, _("&Edit..."), _(" Edit open file")))
        self.Bind(wx.EVT_MENU, self.clearOutput, m.Append(-1, _("Clear console"), _(" Clear output console")))
        self.Bind(wx.EVT_MENU, self.project, m.Append(-1, _("Projector"), _(" Project slices")))
        self.Bind(wx.EVT_MENU, self.OnExit, m.Append(wx.ID_EXIT, _("E&xit"), _(" Closes the Window")))
        self.menustrip.Append(m, _("&File"))

        # Settings menu
        m = wx.Menu()
        self.macros_menu = wx.Menu()
        m.AppendSubMenu(self.macros_menu, _("&Macros"))
        self.Bind(wx.EVT_MENU, self.new_macro, self.macros_menu.Append(-1, _("<&New...>")))
        self.Bind(wx.EVT_MENU, lambda *e: PronterOptions(self), m.Append(-1, _("&Options"), _(" Options dialog")))

        self.Bind(wx.EVT_MENU, lambda x: threading.Thread(target = lambda:self.do_skein("set")).start(), m.Append(-1, _("Slicing Settings"), _(" Adjust slicing settings")))

        mItem = m.AppendCheckItem(-1, _("Debug G-code"),
            _("Print all G-code sent to and received from the printer."))
        m.Check(mItem.GetId(), self.p.loud)
        self.Bind(wx.EVT_MENU, self.setloud, mItem)

        #try:
        #    from SkeinforgeQuickEditDialog import SkeinforgeQuickEditDialog
        #    self.Bind(wx.EVT_MENU, lambda *e:SkeinforgeQuickEditDialog(self), m.Append(-1,_("SFACT Quick Settings"),_(" Quickly adjust SFACT settings for active profile")))
        #except:
        #    pass

        self.menustrip.Append(m, _("&Settings"))
        self.update_macros_menu()
        self.SetMenuBar(self.menustrip)

    def doneediting(self, gcode):
        f = open(self.filename, "w")
        f.write("\n".join(gcode))
        f.close()
        wx.CallAfter(self.loadfile, None, self.filename)

    def do_editgcode(self, e = None):
        if self.filename is not None:
            MacroEditor(self.filename, self.f, self.doneediting, 1)

    def new_macro(self, e = None):
        dialog = wx.Dialog(self, -1, _("Enter macro name"), size = (260, 85))
        panel = wx.Panel(dialog, -1)
        vbox = wx.BoxSizer(wx.VERTICAL)
        wx.StaticText(panel, -1, _("Macro name:"), (8, 14))
        dialog.namectrl = wx.TextCtrl(panel, -1, '', (110, 8), size = (130, 24), style = wx.TE_PROCESS_ENTER)
        hbox = wx.BoxSizer(wx.HORIZONTAL)
        okb = wx.Button(dialog, wx.ID_OK, _("Ok"), size = (60, 24))
        dialog.Bind(wx.EVT_TEXT_ENTER, lambda e:dialog.EndModal(wx.ID_OK), dialog.namectrl)
        #dialog.Bind(wx.EVT_BUTTON, lambda e:self.new_macro_named(dialog, e), okb)
        hbox.Add(okb)
        hbox.Add(wx.Button(dialog, wx.ID_CANCEL, _("Cancel"), size = (60, 24)))
        vbox.Add(panel)
        vbox.Add(hbox, 1, wx.ALIGN_CENTER|wx.TOP|wx.BOTTOM, 10)
        dialog.SetSizer(vbox)
        dialog.Centre()
        macro = ""
        if dialog.ShowModal() == wx.ID_OK:
            macro = dialog.namectrl.GetValue()
            if macro != "":
                wx.CallAfter(self.edit_macro, macro)
        dialog.Destroy()
        return macro

    def edit_macro(self, macro):
        if macro == "": return self.new_macro()
        if self.macros.has_key(macro):
            old_def = self.macros[macro]
        elif len([c for c in macro.encode("ascii", "replace") if not c.isalnum() and c != "_"]):
            print _("Macro name may contain only ASCII alphanumeric symbols and underscores")
            return
        elif hasattr(self.__class__, "do_"+macro):
            print _("Name '%s' is being used by built-in command") % macro
            return
        else:
            old_def = ""
        self.start_macro(macro, old_def)
        return macro

    def update_macros_menu(self):
        if not hasattr(self, "macros_menu"):
            return # too early, menu not yet built
        try:
            while True:
                item = self.macros_menu.FindItemByPosition(1)
                if item is None: return
                self.macros_menu.DeleteItem(item)
        except:
            pass
        for macro in self.macros.keys():
            self.Bind(wx.EVT_MENU, lambda x, m = macro: self.start_macro(m, self.macros[m]), self.macros_menu.Append(-1, macro))

    def OnExit(self, event):
        self.Close()

    def rescanports(self, event = None):
        scan = self.scanserial()
        portslist = list(scan)
        if self.settings.port != "" and self.settings.port not in portslist:
            portslist += [self.settings.port]
            self.serialport.Clear()
            self.serialport.AppendItems(portslist)
        try:
            if os.path.exists(self.settings.port) or self.settings.port in scan:
                self.serialport.SetValue(self.settings.port)
            elif len(portslist) > 0:
                self.serialport.SetValue(portslist[0])
        except:
            pass

    def cbkey(self, e):
        if e.GetKeyCode() == wx.WXK_UP:
            if self.commandbox.histindex == len(self.commandbox.history):
                self.commandbox.history+=[self.commandbox.GetValue()] #save current command
            if len(self.commandbox.history):
                self.commandbox.histindex = (self.commandbox.histindex-1)%len(self.commandbox.history)
                self.commandbox.SetValue(self.commandbox.history[self.commandbox.histindex])
                self.commandbox.SetSelection(0, len(self.commandbox.history[self.commandbox.histindex]))
        elif e.GetKeyCode() == wx.WXK_DOWN:
            if self.commandbox.histindex == len(self.commandbox.history):
                self.commandbox.history+=[self.commandbox.GetValue()] #save current command
            if len(self.commandbox.history):
                self.commandbox.histindex = (self.commandbox.histindex+1)%len(self.commandbox.history)
                self.commandbox.SetValue(self.commandbox.history[self.commandbox.histindex])
                self.commandbox.SetSelection(0, len(self.commandbox.history[self.commandbox.histindex]))
        else:
            e.Skip()

    def plate(self, e):
        import plater
        print "plate function activated"
        plater.stlwin(size = (800, 580), callback = self.platecb, parent = self).Show()

    def platecb(self, name):
        print "plated: "+name
        self.loadfile(None, name)

    def sdmenu(self, e):
        obj = e.GetEventObject()
        popupmenu = wx.Menu()
        item = popupmenu.Append(-1, _("SD Upload"))
        if not self.f:
            item.Enable(False)
        self.Bind(wx.EVT_MENU, self.upload, id = item.GetId())
        item = popupmenu.Append(-1, _("SD Print"))
        self.Bind(wx.EVT_MENU, self.sdprintfile, id = item.GetId())
        self.panel.PopupMenu(popupmenu, obj.GetPosition())

    def htemp_change(self, event):
        if self.hsetpoint > 0:
            self.do_settemp("")
        wx.CallAfter(self.htemp.SetInsertionPoint, 0)

    def btemp_change(self, event):
        if self.bsetpoint > 0:
            self.do_bedtemp("")
        wx.CallAfter(self.btemp.SetInsertionPoint, 0)

    def showwin(self, event):
        if self.f:
            self.gwindow.Show(True)
            self.gwindow.SetToolTip(wx.ToolTip("Mousewheel zooms the display\nShift / Mousewheel scrolls layers"))
            self.gwindow.Raise()

    def setfeeds(self, e):
        self.feedrates_changed = True
        try:
            self.settings._set("e_feedrate", self.efeedc.GetValue())
        except:
            pass
        try:
            self.settings._set("z_feedrate", self.zfeedc.GetValue())
        except:
            pass
        try:
            self.settings._set("xy_feedrate", self.xyfeedc.GetValue())
        except:
            pass

    def toggleview(self, e):
        if(self.mini):
            self.mini = False
            self.mainsizer.Fit(self)

            #self.SetSize(winsize)
            wx.CallAfter(self.minibtn.SetLabel, _("Mini mode"))

        else:
            self.mini = True
            self.uppersizer.Fit(self)

            #self.SetSize(winssize)
            wx.CallAfter(self.minibtn.SetLabel, _("Full mode"))

    def cbuttons_reload(self):
        allcbs = []
        ubs = self.uppersizer
        cs = self.centersizer
        #for item in ubs.GetChildren():
        #    if hasattr(item.GetWindow(),"custombutton"):
        #        allcbs += [(ubs, item.GetWindow())]
        for item in cs.GetChildren():
            if hasattr(item.GetWindow(),"custombutton"):
                allcbs += [(cs, item.GetWindow())]
        for sizer, button in allcbs:
            #sizer.Remove(button)
            button.Destroy()
        self.custombuttonbuttons = []
        newbuttonbuttonindex = len(self.custombuttons)
        while newbuttonbuttonindex>0 and self.custombuttons[newbuttonbuttonindex-1] is None:
            newbuttonbuttonindex -= 1
        while len(self.custombuttons) < 13:
            self.custombuttons.append(None)
        for i in xrange(len(self.custombuttons)):
            btndef = self.custombuttons[i]
            try:
                b = wx.Button(self.panel, -1, btndef.label, style = wx.BU_EXACTFIT)
                b.SetToolTip(wx.ToolTip(_("Execute command: ")+btndef.command))
                if btndef.background:
                    b.SetBackgroundColour(btndef.background)
                    rr, gg, bb = b.GetBackgroundColour().Get()
                    if 0.3*rr+0.59*gg+0.11*bb < 60:
                        b.SetForegroundColour("#ffffff")
            except:
                if i == newbuttonbuttonindex:
                    self.newbuttonbutton = b = wx.Button(self.panel, -1, "+", size = (19, 18), style = wx.BU_EXACTFIT)
                    #b.SetFont(wx.Font(12, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
                    b.SetForegroundColour("#4444ff")
                    b.SetToolTip(wx.ToolTip(_("click to add new custom button")))
                    b.Bind(wx.EVT_BUTTON, self.cbutton_edit)
                else:
                    b = wx.Button(self.panel,-1, ".", size = (1, 1))
                    #b = wx.StaticText(self.panel,-1, "", size = (72, 22), style = wx.ALIGN_CENTRE+wx.ST_NO_AUTORESIZE) #+wx.SIMPLE_BORDER
                    b.Disable()
                    #continue
            b.custombutton = i
            b.properties = btndef
            if btndef is not None:
                b.Bind(wx.EVT_BUTTON, self.procbutton)
                b.Bind(wx.EVT_MOUSE_EVENTS, self.editbutton)
            #else:
            #    b.Bind(wx.EVT_BUTTON, lambda e:e.Skip())
            self.custombuttonbuttons.append(b)
            #if i<4:
            #    ubs.Add(b)
            #else:
            cs.Add(b, pos = ((i)/4, (i)%4))
        self.mainsizer.Layout()

    def help_button(self):
        print _('Defines custom button. Usage: button <num> "title" [/c "colour"] command')

    def do_button(self, argstr):
        def nextarg(rest):
            rest = rest.lstrip()
            if rest.startswith('"'):
                return rest[1:].split('"',1)
            else:
                return rest.split(None, 1)
        #try:
        num, argstr = nextarg(argstr)
        num = int(num)
        title, argstr = nextarg(argstr)
        colour = None
        try:
            c1, c2 = nextarg(argstr)
            if c1 == "/c":
                colour, argstr = nextarg(c2)
        except:
            pass
        command = argstr.strip()
        if num<0 or num>=64:
            print _("Custom button number should be between 0 and 63")
            return
        while num >= len(self.custombuttons):
            self.custombuttons.append(None)
        self.custombuttons[num] = SpecialButton(title, command)
        if colour is not None:
            self.custombuttons[num].background = colour
        if not self.processing_rc:
            self.cbuttons_reload()
        #except Exception, x:
        #    print "Bad syntax for button definition, see 'help button'"
        #    print x

    def cbutton_save(self, n, bdef, new_n = None):
        if new_n is None: new_n = n
        if bdef is None or bdef == "":
            self.save_in_rc(("button %d" % n),'')
        elif bdef.background:
            colour = bdef.background
            if type(colour) not in (str, unicode):
                #print type(colour), map(type, colour)
                if type(colour) == tuple and tuple(map(type, colour)) == (int, int, int):
                    colour = map(lambda x:x%256, colour)
                    colour = wx.Colour(*colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
                else:
                    colour = wx.Colour(colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
            self.save_in_rc(("button %d" % n),'button %d "%s" /c "%s" %s' % (new_n, bdef.label, colour, bdef.command))
        else:
            self.save_in_rc(("button %d" % n),'button %d "%s" %s' % (new_n, bdef.label, bdef.command))

    def cbutton_edit(self, e, button = None):
        bedit = ButtonEdit(self)
        if button is not None:
            n = button.custombutton
            bedit.name.SetValue(button.properties.label)
            bedit.command.SetValue(button.properties.command)
            if button.properties.background:
                colour = button.properties.background
                if type(colour) not in (str, unicode):
                    #print type(colour)
                    if type(colour) == tuple and tuple(map(type, colour)) == (int, int, int):
                        colour = map(lambda x:x%256, colour)
                        colour = wx.Colour(*colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
                    else:
                        colour = wx.Colour(colour).GetAsString(wx.C2S_NAME|wx.C2S_HTML_SYNTAX)
                bedit.color.SetValue(colour)
        else:
            n = len(self.custombuttons)
            while n>0 and self.custombuttons[n-1] is None:
                n -= 1
        if bedit.ShowModal() == wx.ID_OK:
            if n == len(self.custombuttons):
                self.custombuttons+=[None]
            self.custombuttons[n]=SpecialButton(bedit.name.GetValue().strip(), bedit.command.GetValue().strip(), custom = True)
            if bedit.color.GetValue().strip()!="":
                self.custombuttons[n].background = bedit.color.GetValue()
            self.cbutton_save(n, self.custombuttons[n])
        wx.CallAfter(bedit.Destroy)
        wx.CallAfter(self.cbuttons_reload)

    def cbutton_remove(self, e, button):
        n = button.custombutton
        self.custombuttons[n]=None
        self.cbutton_save(n, None)
        #while len(self.custombuttons) and self.custombuttons[-1] is None:
        #    del self.custombuttons[-1]
        wx.CallAfter(self.cbuttons_reload)

    def cbutton_order(self, e, button, dir):
        n = button.custombutton
        if dir<0:
            n = n-1
        if n+1 >= len(self.custombuttons):
            self.custombuttons+=[None] # pad
        # swap
        self.custombuttons[n], self.custombuttons[n+1] = self.custombuttons[n+1], self.custombuttons[n]
        self.cbutton_save(n, self.custombuttons[n])
        self.cbutton_save(n+1, self.custombuttons[n+1])
        #if self.custombuttons[-1] is None:
        #    del self.custombuttons[-1]
        wx.CallAfter(self.cbuttons_reload)

    def homeButtonClicked(self, corner):
        # When user clicks on the XY control, the Z control no longer gets spacebar/repeat signals
        self.zb.clearRepeat()
        if corner == 0: # upper-left
            self.onecmd('home X')
        elif corner == 1: # upper-right
            self.onecmd('home Y')
        elif corner == 2: # lower-right
            self.onecmd('home Z')
        elif corner == 3: # lower-left
            self.onecmd('home')
        else:
            return
        self.p.send_now('M114')

    def moveXY(self, x, y):
        # When user clicks on the XY control, the Z control no longer gets spacebar/repeat signals
        self.zb.clearRepeat()
        if x != 0:
            self.onecmd('move X %s' % x)
        elif y != 0:
            self.onecmd('move Y %s' % y)
        else:
            return
        self.p.send_now('M114')

    def moveZ(self, z):
        if z != 0:
            self.onecmd('move Z %s' % z)
            self.p.send_now('M114')
        # When user clicks on the Z control, the XY control no longer gets spacebar/repeat signals
        self.xyb.clearRepeat()

    def spacebarAction(self):
        self.zb.repeatLast()
        self.xyb.repeatLast()

    def procbutton(self, e):
        try:
            if hasattr(e.GetEventObject(),"custombutton"):
                self.cur_button = e.GetEventObject().custombutton
            self.onecmd(e.GetEventObject().properties.command)
            self.cur_button = None
        except:
            print _("event object missing")
            self.cur_button = None
            raise

    def kill(self, e):
        self.statuscheck = False
        if self.status_thread:
            self.status_thread.join()
            self.status_thread = None
        self.p.recvcb = None
        self.p.disconnect()
        if hasattr(self, "feedrates_changed"):
            self.save_in_rc("set xy_feedrate", "set xy_feedrate %d" % self.settings.xy_feedrate)
            self.save_in_rc("set z_feedrate", "set z_feedrate %d" % self.settings.z_feedrate)
            self.save_in_rc("set e_feedrate", "set e_feedrate %d" % self.settings.e_feedrate)
        wx.CallAfter(self.gwindow.Destroy)
        wx.CallAfter(self.Destroy)

    def do_monitor(self, l = ""):
        if l.strip()=="":
            self.monitorbox.SetValue(not self.monitorbox.GetValue())
        elif l.strip()=="off":
            wx.CallAfter(self.monitorbox.SetValue, False)
        else:
            try:
                self.monitor_interval = float(l)
                wx.CallAfter(self.monitorbox.SetValue, self.monitor_interval>0)
            except:
                print _("Invalid period given.")
        self.setmonitor(None)
        if self.monitor:
            print _("Monitoring printer.")
        else:
            print _("Done monitoring.")

    '''def setmonitor(self, e):
        self.monitor = self.monitorbox.GetValue()
        self.set("monitor", self.monitor)
        if self.monitor:
		    for bit in bits:
		        if x is None and bit.startswith("X"):
		            x = float(bit[1:].replace(":",""))
		        elif y is None and bit.startswith("Y"):
		            y = float(bit[1:].replace(":",""))
		        elif z is None and bit.startswith("Z"):
		            z = float(bit[1:].replace(":",""))
        if x is not None: self.current_pos[0] = x
        if y is not None: self.current_pos[1] = y
        if z is not None: self.current_pos[2] = z
    '''

    def statuschecker(self):
        while self.statuscheck:
            string = ""
            fractioncomplete = 0.0
            if self.sdprinting:
                fractioncomplete = float(self.percentdone / 100.0)
                string += _(" SD printing:%04.2f %%") % (self.percentdone,)
            if self.p.printing:
                fractioncomplete = float(self.p.queueindex) / len(self.p.mainqueue)
                string += _(" Printing: %04.2f%% |") % (100*float(self.p.queueindex)/len(self.p.mainqueue),)
                string += _(" Line# %d of %d lines |" ) % (self.p.queueindex, len(self.p.mainqueue))
            if fractioncomplete > 0.0:
                secondselapsed = int(time.time() - self.starttime + self.extra_print_time)
                secondsestimate = secondselapsed / fractioncomplete
                secondsremain = secondsestimate - secondselapsed
                string += _(" Est: %s of %s remaining | ") % (format_duration(secondsremain),
                                                              format_duration(secondsestimate))
                string += _(" Z: %0.2f mm") % self.curlayer
            wx.CallAfter(self.status.SetStatusText, string)
            wx.CallAfter(self.gviz.Refresh)
            if(self.monitor and self.p.online):
                if self.sdprinting:
                    self.p.send_now("M27")
                if not hasattr(self, "auto_monitor_pattern"):
                    self.auto_monitor_pattern = re.compile(r"(ok\s+)?T:[\d\.]+(\s+B:[\d\.]+)?(\s+@:[\d\.]+)?\s*")
                self.capture_skip[self.auto_monitor_pattern] = self.capture_skip.setdefault(self.auto_monitor_pattern, 0) + 1
                self.p.send_now("M105")
            cur_time = time.time()
            while time.time() < cur_time + self.monitor_interval:
                if not self.statuscheck:
                    break
                time.sleep(0.25)
            while not self.sentlines.empty():
                gc = self.sentlines.get_nowait()
                wx.CallAfter(self.gviz.addgcode, gc, 1)
        wx.CallAfter(self.status.SetStatusText, _("Not connected to printer."))

    def capture(self, func, *args, **kwargs):
        stdout = sys.stdout
        cout = None
        try:
            cout = self.cout
        except:
            pass
        if cout is None:
            cout = cStringIO.StringIO()

        sys.stdout = cout
        retval = None
        try:
            retval = func(*args,**kwargs)
        except:
            traceback.print_exc()
        sys.stdout = stdout
        return retval

    def recvcb(self, l):
        isreport = False
        if "ok C:" in l or "Count" in l:
            self.posreport = l
            self.update_pos(l)
            isreport = True
        if "ok T:" in l:
            self.tempreport = l
            wx.CallAfter(self.tempdisp.SetLabel, self.tempreport.strip().replace("ok ", ""))
            self.update_tempdisplay()
            isreport = True
        tstring = l.rstrip()
        if self.p.loud or (tstring not in ["ok", "wait"] and not isreport):
            wx.CallAfter(self.addtexttolog, tstring + "\n");
        for listener in self.recvlisteners:
            listener(l)


    def skein_func(self):
        try:
            param = self.expandcommand(self.settings.slicecommand).encode()
            print "Slicing: ", param
            pararray = [i.replace("$s", self.filename).replace("$o", self.filename.replace(".stl", "_export.gcode").replace(".STL", "_export.gcode")).encode() for i in shlex.split(param.replace("\\", "\\\\").encode())]
                #print pararray
            self.skeinp = subprocess.Popen(pararray, stderr = subprocess.STDOUT, stdout = subprocess.PIPE)
            while True:
                o = self.skeinp.stdout.read(1)
                if o == '' and self.skeinp.poll() != None: break
                sys.stdout.write(o)
            self.skeinp.wait()
            self.stopsf = 1
        except:
            print _("Failed to execute slicing software: ")
            self.stopsf = 1
            traceback.print_exc(file = sys.stdout)

    def skein_monitor(self):
        while(not self.stopsf):
            try:
                wx.CallAfter(self.status.SetStatusText, _("Slicing..."))#+self.cout.getvalue().split("\n")[-1])
            except:
                pass
            time.sleep(0.1)
        fn = self.filename
        try:
            self.filename = self.filename.replace(".stl", "_export.gcode").replace(".STL", "_export.gcode").replace(".obj", "_export.gcode").replace(".OBJ", "_export.gcode")
            self.f = [line.strip() for line in open(self.filename)]
            self.fgcode = gcoder.GCode(self.f)
            if self.p.online:
                wx.CallAfter(self.printbtn.Enable)

            wx.CallAfter(self.status.SetStatusText, _("Loaded %s, %d lines") % (self.filename, len(self.f),))
            print _("Loaded %s, %d lines") % (self.filename, len(self.f),)
            wx.CallAfter(self.pausebtn.Disable)
            wx.CallAfter(self.printbtn.SetLabel, _("Print"))

            threading.Thread(target = self.loadviz).start()
        except:
            self.filename = fn
        wx.CallAfter(self.loadbtn.SetLabel, _("Load File"))
        self.skeining = 0
        self.skeinp = None

    def skein(self, filename):
        wx.CallAfter(self.loadbtn.SetLabel, _("Cancel"))
        print _("Slicing ") + filename
        self.cout = StringIO.StringIO()
        self.filename = filename
        self.stopsf = 0
        self.skeining = 1
        threading.Thread(target = self.skein_func).start()
        threading.Thread(target = self.skein_monitor).start()

    def do_load(self,l):
        if hasattr(self, 'skeining'):
            self.loadfile(None, l)
        else:
            self._do_load(l)

    def loadfile(self, event, filename = None):
        if self.skeining and self.skeinp is not None:
            self.skeinp.terminate()
            return
        basedir = self.settings.last_file_path
        if not os.path.exists(basedir):
            basedir = "."
            try:
                basedir = os.path.split(self.filename)[0]
            except:
                pass
        dlg = None
        if filename is None:
            dlg = wx.FileDialog(self, _("Open file to print"), basedir, style = wx.FD_OPEN|wx.FD_FILE_MUST_EXIST)
            dlg.SetWildcard(_("OBJ, STL, and GCODE files (*.gcode;*.gco;*.g;*.stl;*.STL;*.obj;*.OBJ)|*.gcode;*.gco;*.g;*.stl;*.STL;*.obj;*.OBJ|All Files (*.*)|*.*"))
        if filename or dlg.ShowModal() == wx.ID_OK:
            if filename:
                name = filename
            else:
                name = dlg.GetPath()
                dlg.Destroy()
            if not os.path.exists(name):
                self.status.SetStatusText(_("File not found!"))
                return
            path = os.path.split(name)[0]
            if path != self.settings.last_file_path:
                self.set("last_file_path", path)
            if name.lower().endswith(".stl"):
                self.skein(name)
            elif name.lower().endswith(".obj"):
                self.skein(name)
            else:
                self.filename = name
                self.f = [line.strip() for line in open(self.filename)]
                self.fgcode = gcoder.GCode(self.f)
                self.status.SetStatusText(_("Loaded %s, %d lines") % (name, len(self.f)))
                print _("Loaded %s, %d lines") % (name, len(self.f))
                wx.CallAfter(self.printbtn.SetLabel, _("Print"))
                wx.CallAfter(self.pausebtn.SetLabel, _("Pause"))
                wx.CallAfter(self.pausebtn.Disable)
                wx.CallAfter(self.recoverbtn.Disable)
                if self.p.online:
                    wx.CallAfter(self.printbtn.Enable)
                threading.Thread(target = self.loadviz).start()
        else:
            dlg.Destroy()

    def loadviz(self):
        gcode = self.fgcode
        print gcode.filament_length, _("mm of filament used in this print")
        print _("The print goes:")
        print _("- from %.2f mm to %.2f mm in X and is %.2f mm wide") % (gcode.xmin, gcode.xmax, gcode.width)
        print _("- from %.2f mm to %.2f mm in Y and is %.2f mm deep") % (gcode.ymin, gcode.ymax, gcode.depth)
        print _("- from %.2f mm to %.2f mm in Z and is %.2f mm high") % (gcode.zmin, gcode.zmax, gcode.height)
        print _("Estimated duration: %s") % gcode.estimate_duration()
        self.gviz.clear()
        self.gwindow.p.clear()
        self.gviz.addfile(gcode)
        self.gwindow.p.addfile(gcode)
        self.gviz.showall = 1
        wx.CallAfter(self.gviz.Refresh)

    def printfile(self, event):
        self.extra_print_time = 0
        if self.paused:
            self.p.paused = 0
            self.paused = 0
            self.on_startprint()
            if self.sdprinting:
                self.p.send_now("M26 S0")
                self.p.send_now("M24")
                return

        if not self.f:
            wx.CallAfter(self.status.SetStatusText, _("No file loaded. Please use load first."))
            return
        if not self.p.online:
            wx.CallAfter(self.status.SetStatusText, _("Not connected to printer."))
            return
        self.on_startprint()
        self.p.startprint(self.fgcode)

    def on_startprint(self):
        wx.CallAfter(self.pausebtn.SetLabel, _("Pause"))
        wx.CallAfter(self.pausebtn.Enable)
        wx.CallAfter(self.printbtn.SetLabel, _("Restart"))

    def pause(self, event):
        print _("Paused.")
        if not self.paused:
            if self.sdprinting:
                self.p.send_now("M25")
            else:
                if(not self.p.printing):
                    #print "Not printing, cannot pause."
                    return
                self.p.pause()
                self.p.runSmallScript(self.pauseScript)
            self.paused = True
            #self.p.runSmallScript(self.pauseScript)
            self.extra_print_time += int(time.time() - self.starttime)
            wx.CallAfter(self.pausebtn.SetLabel, _("Resume"))
        else:
            self.paused = False
            if self.sdprinting:
                self.p.send_now("M24")
            else:
                self.p.resume()
            wx.CallAfter(self.pausebtn.SetLabel, _("Pause"))

    def sdprintfile(self, event):
        self.on_startprint()
        threading.Thread(target = self.getfiles).start()

    def connect(self, event = None):
        print _("Connecting...")
        port = None
        try:
            port = self.scanserial()[0]
        except:
            pass
        if self.serialport.GetValue()!="":
            port = str(self.serialport.GetValue())
        baud = 115200
        if self.paused:
            self.p.paused = 0
            self.p.printing = 0
            wx.CallAfter(self.pausebtn.SetLabel, _("Pause"))
            wx.CallAfter(self.printbtn.SetLabel, _("Print"))
            self.paused = 0
            if self.sdprinting:
                self.p.send_now("M26 S0")
        try:
            self.p.connect(port, baud)
        except SerialException as e:
            # Currently, there is no errno, but it should be there in the future
            if e.errno == 2:
                print _("Error: You are trying to connect to a non-exisiting port.")
            elif e.errno == 8:
                print _("Error: You don't have permission to open %s.") % port
                print _("You might need to add yourself to the dialout group.")
            else:
                print e
            # Kill the scope anyway
            return
        self.statuscheck = True
        self.status_thread = threading.Thread(target = self.statuschecker)
        self.status_thread.start()
        if self.predisconnect_mainqueue:
            self.recoverbtn.Enable()

    def recover(self, event):
        self.extra_print_time = 0
        if not self.p.online:
            wx.CallAfter(self.status.SetStatusText, _("Not connected to printer."))
            return
        # Reset Z
        self.p.send_now("G92 Z%f" % self.predisconnect_layer)
        # Home X and Y
        self.p.send_now("G28 X Y")
        self.on_startprint()
        self.p.startprint(self.predisconnect_mainqueue, self.p.queueindex)

    def store_predisconnect_state(self):
        self.predisconnect_mainqueue = self.p.mainqueue
        self.predisconnect_queueindex = self.p.queueindex
        self.predisconnect_layer = self.curlayer

    def disconnect(self, event):
        print _("Disconnected.")
        if self.p.printing or self.p.paused or self.paused:
            self.store_predisconnect_state()
        self.p.disconnect()
        self.statuscheck = False
        if self.status_thread:
            self.status_thread.join()
            self.status_thread = None

        self.connectbtn.SetLabel(_("Connect"))
        self.connectbtn.SetToolTip(wx.ToolTip("Connect to the printer"))
        self.connectbtn.Bind(wx.EVT_BUTTON, self.connect)

        wx.CallAfter(self.printbtn.Disable)
        wx.CallAfter(self.pausebtn.Disable)
        wx.CallAfter(self.recoverbtn.Disable)
        for i in self.printerControls:
            wx.CallAfter(i.Disable)

        # Disable XYButtons and ZButtons
        wx.CallAfter(self.xyb.disable)
        wx.CallAfter(self.zb.disable)

        if self.paused:
            self.p.paused = 0
            self.p.printing = 0
            wx.CallAfter(self.pausebtn.SetLabel, _("Pause"))
            wx.CallAfter(self.printbtn.SetLabel, _("Print"))
            self.paused = 0
            if self.sdprinting:
                self.p.send_now("M26 S0")

    def reset(self, event):
        print _("Reset.")
        dlg = wx.MessageDialog(self, _("Are you sure you want to reset the printer?"), _("Reset?"), wx.YES|wx.NO)
        if dlg.ShowModal() == wx.ID_YES:
            self.p.reset()
            self.sethotendgui(0)
            self.setbedgui(0)
            self.p.printing = 0
            wx.CallAfter(self.printbtn.SetLabel, _("Print"))
            if self.paused:
                self.p.paused = 0
                wx.CallAfter(self.pausebtn.SetLabel, _("Pause"))
                self.paused = 0
        dlg.Destroy()

class PronterApp(wx.App):

    mainwindow = None

    def __init__(self, *args, **kwargs):
        super(PronterApp, self).__init__(*args, **kwargs)
        self.mainwindow = PronterWindow()
        #self.mainwindow.Show()
        self.mainwindow.ShowFullScreen(True)


if __name__ == '__main__':
    app = PronterApp(False)
    try:
        app.MainLoop()
    except KeyboardInterrupt:
        pass
    del app
'''
########################################################################
class TabPanel(wx.Panel):
    #----------------------------------------------------------------------
    def __init__(self, parent):
        """"""
        wx.Panel.__init__(self, parent=parent)
        llts = wx.BoxSizer(wx.HORIZONTAL)
        #parent.Add(llts, pos = (0, 0), span = (1, 9))

        btn = wx.Button(self, label="Press Me")
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(btn, 0, wx.ALL, 10)
        self.SetSizer(sizer)

########################################################################
class MainWindow(wx.Frame):
    """
    Frame that holds all other widgets
    """
    #----------------------------------------------------------------------
    def __init__(self):
        """Constructor"""
        wx.Frame.__init__(self, None, wx.ID_ANY, 'Full display size', pos=(0, 0), size=(320, 240)) #size=wx.DisplaySize())
        panel = wx.Panel(self)

        notebook = wx.Notebook(panel)
        tabOne = TabPanel(notebook)
        notebook.AddPage(tabOne, "Tab 1")

        tabTwo = TabPanel(notebook)
        notebook.AddPage(tabTwo, "Tab 2")

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(notebook, 1, wx.ALL|wx.EXPAND, 5)
        panel.SetSizer(sizer)
        self.Layout()

        self.Show()

#----------------------------------------------------------------------
if __name__ == "__main__":
    app = wx.App(False)
    frame = MainWindow()
    #frame.ShowFullScreen(True, style=wx.FULLSCREEN_ALL)
    app.MainLoop()
'''
