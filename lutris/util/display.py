"""Module to deal with various aspects of displays"""
# isort:skip_file
import enum
import os
import subprocess
import gi


try:
    gi.require_version("GnomeDesktop", "3.0")
    from gi.repository import GnomeDesktop
    LIB_GNOME_DESKTOP_AVAILABLE = True
except ValueError:
    LIB_GNOME_DESKTOP_AVAILABLE = False
    GnomeDesktop = None

try:
    from dbus.exceptions import DBusException
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

from gi.repository import Gdk, GLib, Gio, Gtk

from lutris.util import system
from lutris.settings import DEFAULT_RESOLUTION_HEIGHT, DEFAULT_RESOLUTION_WIDTH
from lutris.util.graphics import drivers
from lutris.util.graphics.displayconfig import MutterDisplayManager
from lutris.util.graphics.xrandr import LegacyDisplayManager, change_resolution, get_outputs
from lutris.util.log import logger


class NoScreenDetected(Exception):
    """Raise this when unable to detect screens"""


def get_default_dpi():
    """Computes the DPI to use for the primary monitor
    which we pass to WINE."""
    display = Gdk.Display.get_default()
    if display:
        monitor = display.get_primary_monitor()
        if monitor:
            scale = monitor.get_scale_factor()
            dpi = 96 * scale
            return int(dpi)
    return 96


def restore_gamma():
    """Restores gamma to a normal level."""
    xgamma_path = system.find_executable("xgamma")
    try:
        subprocess.Popen([xgamma_path, "-gamma", "1.0"])  # pylint: disable=consider-using-with
    except (FileNotFoundError, TypeError):
        logger.warning("xgamma is not available on your system")
    except PermissionError:
        logger.warning("you do not have permission to call xgamma")


def has_graphic_adapter_description(match_text):
    """Returns True if a graphics adapter is found with 'match_text' in its description."""
    for adapter in _get_graphics_adapters():
        if match_text in adapter[1]:
            return True
    return False


def get_gpus():
    """Return the number of GPUs from /sys/class/drm without
    requiring a call to lspci"""
    gpus = {}
    for card in drivers.get_gpus():
        gpus[card] = drivers.get_gpu_info(card)
        try:
            gpu_string = f"GPU: {gpus[card]['PCI_ID']} {gpus[card]['PCI_SUBSYS_ID']} ({gpus[card]['DRIVER']} drivers)"
            logger.info(gpu_string)
        except KeyError:
            logger.error("Unable to get GPU information from '%s'", card)
    return gpus


def _get_graphics_adapters():
    """Return the list of graphics cards available on a system

    Returns:
        list: list of tuples containing PCI ID and description of the display controller
    """
    lspci_path = system.find_executable("lspci")
    dev_subclasses = ["VGA", "XGA", "3D controller", "Display controller"]
    if not lspci_path:
        logger.warning("lspci is not available. List of graphics cards not available")
        return []
    return [
        (pci_id, device_desc.split(": ")[1]) for pci_id, device_desc in [
            line.split(maxsplit=1) for line in system.execute(lspci_path, timeout=3).split("\n")
            if any(subclass in line for subclass in dev_subclasses)
        ]
    ]


class DisplayManager:
    """Get display and resolution using GnomeDesktop"""

    def __init__(self):
        if not LIB_GNOME_DESKTOP_AVAILABLE:
            logger.warning("libgnomedesktop unavailable")
            return
        screen = Gdk.Screen.get_default()
        if not screen:
            raise NoScreenDetected
        self.rr_screen = GnomeDesktop.RRScreen.new(screen)
        self.rr_config = GnomeDesktop.RRConfig.new_current(self.rr_screen)
        self.rr_config.load_current()

    def get_display_names(self):
        """Return names of connected displays"""
        return [output_info.get_display_name() for output_info in self.rr_config.get_outputs()]

    def get_resolutions(self):
        """Return available resolutions"""
        resolutions = ["%sx%s" % (mode.get_width(), mode.get_height()) for mode in self.rr_screen.list_modes()]
        if not resolutions:
            logger.error("Failed to generate resolution list from default GdkScreen")
            resolutions = ['%dx%d' % (DEFAULT_RESOLUTION_WIDTH, DEFAULT_RESOLUTION_HEIGHT)]
        return sorted(set(resolutions), key=lambda x: int(x.split("x")[0]), reverse=True)

    def _get_primary_output(self):
        """Return the RROutput used as a primary display"""
        for output in self.rr_screen.list_outputs():
            if output.get_is_primary():
                return output
        return

    def get_current_resolution(self):
        """Return the current resolution for the primary display"""
        output = self._get_primary_output()
        if not output:
            logger.error("Failed to get a default output")
            return str(DEFAULT_RESOLUTION_WIDTH), str(DEFAULT_RESOLUTION_HEIGHT)
        current_mode = output.get_current_mode()
        return str(current_mode.get_width()), str(current_mode.get_height())

    @staticmethod
    def set_resolution(resolution):
        """Set the resolution of one or more displays.
        The resolution can either be a string, which will be applied to the
        primary display or a list of configurations as returned by `get_config`.
        This method uses XrandR and will not work on Wayland.
        """
        return change_resolution(resolution)

    @staticmethod
    def get_config():
        """Return the current display resolution
        This method uses XrandR and will not work on wayland
        The output can be fed in `set_resolution`
        """
        return get_outputs()


def get_display_manager():
    """Return the appropriate display manager instance.
    Defaults to Mutter if available. This is the only one to support Wayland.
    """
    if DBUS_AVAILABLE:
        try:
            return MutterDisplayManager()
        except DBusException as ex:
            logger.debug("Mutter DBus service not reachable: %s", ex)
        except Exception as ex:  # pylint: disable=broad-except
            logger.exception("Failed to instantiate MutterDisplayConfig. Please report with exception: %s", ex)
    else:
        logger.error("DBus is not available, Lutris was not properly installed.")
    if LIB_GNOME_DESKTOP_AVAILABLE:
        try:
            return DisplayManager()
        except (GLib.Error, NoScreenDetected):
            pass
    return LegacyDisplayManager()


DISPLAY_MANAGER = get_display_manager()
USE_DRI_PRIME = len(get_gpus()) > 1


class DesktopEnvironment(enum.Enum):

    """Enum of desktop environments."""

    PLASMA = 0
    MATE = 1
    XFCE = 2
    DEEPIN = 3
    UNKNOWN = 999


def get_desktop_environment():
    """Converts the value of the DESKTOP_SESSION environment variable
    to one of the constants in the DesktopEnvironment class.
    Returns None if DESKTOP_SESSION is empty or unset.
    """
    desktop_session = os.environ.get("DESKTOP_SESSION", "").lower()
    if not desktop_session:
        return None
    if desktop_session.endswith("mate"):
        return DesktopEnvironment.MATE
    if desktop_session.endswith("xfce"):
        return DesktopEnvironment.XFCE
    if desktop_session.endswith("deepin"):
        return DesktopEnvironment.DEEPIN
    if "plasma" in desktop_session:
        return DesktopEnvironment.PLASMA
    return DesktopEnvironment.UNKNOWN


def _get_command_output(*command):
    """Some rogue function that gives no shit about residing in the correct module"""
    try:
        return subprocess.Popen(  # pylint: disable=consider-using-with
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            close_fds=True
        ).communicate()[0]
    except FileNotFoundError:
        logger.error("Unable to run command, %s not found", command[0])


def is_compositing_enabled():
    """Checks whether compositing is currently disabled or enabled.
    Returns True for enabled, False for disabled, and None if unknown.
    """
    desktop_environment = get_desktop_environment()
    if desktop_environment is DesktopEnvironment.PLASMA:
        return _get_command_output(
            "qdbus", "org.kde.KWin", "/Compositor", "org.kde.kwin.Compositing.active"
        ) == b"true\n"
    if desktop_environment is DesktopEnvironment.MATE:
        return _get_command_output("gsettings", "get org.mate.Marco.general", "compositing-manager") == b"true\n"
    if desktop_environment is DesktopEnvironment.XFCE:
        return _get_command_output(
            "xfconf-query", "--channel=xfwm4", "--property=/general/use_compositing"
        ) == b"true\n"
    if desktop_environment is DesktopEnvironment.DEEPIN:
        return _get_command_output(
            "dbus-send", "--session", "--dest=com.deepin.WMSwitcher", "--type=method_call",
            "--print-reply=literal", "/com/deepin/WMSwitcher", "com.deepin.WMSwitcher.CurrentWM"
        ) == b"deepin wm\n"
    return None


# One element is appended to this for every invocation of disable_compositing:
# True if compositing has been disabled, False if not. enable_compositing
# removes the last element, and only re-enables compositing if that element
# was True.
_COMPOSITING_DISABLED_STACK = []


def _get_compositor_commands():
    """Returns the commands to enable/disable compositing on the current
    desktop environment as a 2-tuple.
    """
    start_compositor = None
    stop_compositor = None
    desktop_environment = get_desktop_environment()
    if desktop_environment is DesktopEnvironment.PLASMA:
        stop_compositor = ("qdbus", "org.kde.KWin", "/Compositor", "org.kde.kwin.Compositing.suspend")
        start_compositor = ("qdbus", "org.kde.KWin", "/Compositor", "org.kde.kwin.Compositing.resume")
    elif desktop_environment is DesktopEnvironment.MATE:
        stop_compositor = ("gsettings", "set org.mate.Marco.general", "compositing-manager", "false")
        start_compositor = ("gsettings", "set org.mate.Marco.general", "compositing-manager", "true")
    elif desktop_environment is DesktopEnvironment.XFCE:
        stop_compositor = ("xfconf-query", "--channel=xfwm4", "--property=/general/use_compositing", "--set=false")
        start_compositor = ("xfconf-query", "--channel=xfwm4", "--property=/general/use_compositing", "--set=true")
    elif desktop_environment is DesktopEnvironment.DEEPIN:
        start_compositor = (
            "dbus-send", "--session", "--dest=com.deepin.WMSwitcher", "--type=method_call",
            "/com/deepin/WMSwitcher", "com.deepin.WMSwitcher.RequestSwitchWM",
        )
        stop_compositor = start_compositor
    return start_compositor, stop_compositor


def _run_command(*command):
    """Random _run_command lost in the middle of the project,
    are you lost little _run_command?
    """
    try:
        return subprocess.Popen(  # pylint: disable=consider-using-with
            command,
            stdin=subprocess.DEVNULL,
            close_fds=True
        )
    except FileNotFoundError:
        logger.error("Oh no")


def disable_compositing():
    """Disable compositing if not already disabled."""
    compositing_enabled = is_compositing_enabled()
    if compositing_enabled is None:
        compositing_enabled = True
    if any(_COMPOSITING_DISABLED_STACK):
        compositing_enabled = False
    _COMPOSITING_DISABLED_STACK.append(compositing_enabled)
    if not compositing_enabled:
        return
    _, stop_compositor = _get_compositor_commands()
    if stop_compositor:
        _run_command(*stop_compositor)


def enable_compositing():
    """Re-enable compositing if the corresponding call to disable_compositing
    disabled it."""

    compositing_disabled = _COMPOSITING_DISABLED_STACK.pop()
    if not compositing_disabled:
        return
    start_compositor, _ = _get_compositor_commands()
    if start_compositor:
        _run_command(*start_compositor)


class DBusScreenSaverInhibitor:

    """Inhibit and uninhibit the screen saver using DBus.

    It will use the Gtk.Application's inhibit and uninhibit methods to inhibit
    the screen saver.

    For enviroments which don't support either org.freedesktop.ScreenSaver or
    org.gnome.ScreenSaver interfaces one can declare a DBus interface which
    requires the Inhibit() and UnInhibit() methods to be exposed."""

    def __init__(self):
        self.proxy = None

    def set_dbus_iface(self, name, path, interface, bus_type=Gio.BusType.SESSION):
        """Sets a dbus proxy to be used instead of Gtk.Application methods, this
        method can raise an exception."""
        self.proxy = Gio.DBusProxy.new_for_bus_sync(
            bus_type, Gio.DBusProxyFlags.NONE, None, name, path, interface, None)

    def inhibit(self, game_name):
        """Inhibit the screen saver.
        Returns a cookie that must be passed to the corresponding uninhibit() call.
        If an error occurs, None is returned instead."""
        reason = "Running game: %s" % game_name

        if self.proxy:
            try:
                return self.proxy.Inhibit("(ss)", "Lutris", reason)
            except Exception:
                return None
        else:
            app = Gio.Application.get_default()
            window = app.window
            flags = Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.IDLE
            cookie = app.inhibit(window, flags, reason)

            # Gtk.Application.inhibit returns 0 if there was an error.
            if cookie == 0:
                return None

            return cookie

    def uninhibit(self, cookie):
        """Uninhibit the screen saver.
        Takes a cookie as returned by inhibit. If cookie is None, no action is taken."""
        if not cookie:
            return

        if self.proxy:
            self.proxy.UnInhibit("(u)", cookie)
        else:
            app = Gio.Application.get_default()
            app.uninhibit(cookie)


def _get_screen_saver_inhibitor():
    """Return the appropriate screen saver inhibitor instance.
    If the required interface isn't available, it will default to GTK's
    implementation."""
    desktop_environment = get_desktop_environment()

    name = None
    inhibitor = DBusScreenSaverInhibitor()

    if desktop_environment is DesktopEnvironment.MATE:
        name = "org.mate.ScreenSaver"
        path = "/"
        interface = "org.mate.ScreenSaver"
    elif desktop_environment is DesktopEnvironment.XFCE:
        # According to
        # https://github.com/xfce-mirror/xfce4-session/blob/master/xfce4-session/xfce-screensaver.c#L240
        # The XFCE enviroment does support the org.freedesktop.ScreenSaver interface
        # but this might be not present in older releases.
        name = "org.xfce.ScreenSaver"
        path = "/"
        interface = "org.xfce.ScreenSaver"

    if name:
        try:
            inhibitor.set_dbus_iface(name, path, interface)
        except GLib.Error as err:
            logger.warning("Failed to set up a DBus proxy for name %s, path %s, "
                           "interface %s: %s", name, path, interface, err)

    return inhibitor


SCREEN_SAVER_INHIBITOR = _get_screen_saver_inhibitor()
