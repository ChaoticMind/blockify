"""blockify

Usage:
    blockify [-l <path>] [-v...] [-q] [-h]

Options:
    -l, --log=<path>  Enables logging to the logfile/-path specified.
    -q, --quiet       Don't print anything to stdout.
    -v                Verbosity of the logging module, up to -vvv.
    -h, --help        Show this help text.
    --version         Show current version of blockify.
"""
# TODO: Try xlib/_net for minimized window detection.
# TODO: Play local mp3s when ads are muted? Currently only possible with pulse.
import codecs
import logging
import os
import re
import signal
import subprocess
import sys
import time

import gtk
import pygtk
import wnck
import yaml
import requests


try:
    from docopt import docopt
except ImportError:
    print "ImportError: Please install docopt to use the CLI."


pygtk.require("2.0")
log = logging.getLogger("main")


class Blocklist(list):
    "Inheriting from list type is a bad idea. Let's see what happens."

    def __init__(self, load_remotes=False):
        super(Blocklist, self).__init__()
        self.home = os.path.expanduser("~")
        self.location = os.path.join(self.home, ".blockify_list")
        remote_locations = os.path.join(self.home, ".blockify_remotes.yaml")
        self.extend(self.load_file())
        self.timestamp = self.get_timestamp()

        if load_remotes:
            remote_list = self._load_remotes(remote_locations)
            if remote_list:
                self.extend(remote_list)
                self.save_file()


    def _load_remotes(self, remote_locations):
        data = yaml.load(file(remote_locations, 'r'))
        remote_list = []

        for _, remote in data.iteritems():
            name, url = remote['name'], remote['url']
            # TODO: blocking calls (requests.get()) should be done in a separate thread (probably the whole function)
            r = requests.get(url)
            if r.status_code == 200:
                remote_content = set(r.text.split('\n')) # get rid of potential remote duplicates
                filtered_content = [x for x in remote_content if x.strip() and x not in self] # skip blank lines and inefficiently avoid duplicates with local list
                remote_list.extend(filtered_content)
                if filtered_content:
                    log.info("Importing from remote list '{}': {}".format(name, filtered_content))
                else:
                    log.info("No new content from remote list '{}'".format(name))
            else:
                log.info("Couldn't load '{}' from {}".format(name, url))

        return remote_list



    def append(self, item):
        "Overloading list.append to automatically save the list to a file."
        # Only allow nonempty strings.
        if item in self or not item or item == " ":
            log.debug("Not adding empty or duplicate item: {}.".format(item))
            return
        log.info("Adding {} to {}.".format(item, self.location))
        super(Blocklist, self).append(item)
        self.save_file()


    def remove(self, item):
        log.info("Removing {} from {}.".format(item, self.location))
        try:
            super(Blocklist, self).remove(item)
            self.save_file()
        except ValueError as e:
            log.warn("Could not remove {} from blocklist: {}".format(item, e))


    def get_timestamp(self):
        return os.path.getmtime(self.location)


    def load_file(self):
        log.debug("Loading blockfile from {}.".format(self.location))
        try:
            with codecs.open(self.location, "r", encoding="utf-8") as f:
                blocklist = f.read()
        except IOError:
            with codecs.open(self.location, "w+", encoding="utf-8") as f:
                blocklist = f.read()

        return [i for i in blocklist.split("\n") if i]


    def save_file(self):
        log.debug("Saving blocklist to {}.".format(self.location))
        with codecs.open(self.location, "w", encoding="utf-8") as f:
            f.write("\n".join(self) + "\n")
        self.timestamp = self.get_timestamp()


class Blockify(object):

    def __init__(self, blocklist):
        self.blocklist = blocklist
        self.orglist = blocklist[:]
        self.channels = self.get_channels()
        self.automute = True
        # If muting fails, switch to alternative mute_mode (sink>pulse>alsa).
        self.fallback_enabled = True

        # Determine if we can use sinks or have to use alsa.
        try:
            devnull = open(os.devnull)
            subprocess.check_output(["pacmd", "list-sink-inputs"], stderr=devnull)
            self.mute_mode = "pulsesink"
        except (OSError, subprocess.CalledProcessError):
            self.mute_mode = "alsa"

        log.info("Blockify started.")


    def update(self):
        "Main loop. Checks for blocklist match and mutes accordingly."
        # It all relies on current_song.
        self.current_song = self.get_current_song()

        if not self.current_song or not self.automute:
            return

        # Check if the blockfile has changed.
        current_timestamp = self.blocklist.get_timestamp()
        if self.blocklist.timestamp != current_timestamp:
            log.info("Blockfile changed. Reloading.")
            self.blocklist.__init__()

        for i in self.blocklist:
            if i in self.current_song:
                self.toggle_mute(True)
                return True  # We found one! Used in ui.
        else:
            self.toggle_mute()


    def get_windows(self):
        "Libwnck list of currently open windows."
        # Get the current screen.
        screen = wnck.screen_get_default()

        # TODO: screen.force_update(), does this allow minimized?
        # Object list of windows in screen.
        windows = screen.get_windows()
        # Return the actual list of windows or an empty list.
        return [win.get_icon_name() for win in windows if len(windows)]


    def get_current_song(self):
        "Checks if a Spotify window exists and returns the current songname."
        pipe = self.get_windows()
        for line in pipe:
            if line.find("Spotify - ") >= 0:
                # Remove "Spotify - " and return the rest of the songname.
                return " ".join(line.split()[2:])

        # No song playing, so return an empty string.
        return ""


    def block_current(self):
        if self.current_song:
            self.blocklist.append(self.current_song)


    def unblock_current(self):
        if self.current_song:
            try:
                self.blocklist.remove(self.current_song)
            except ValueError as e:
                log.debug("Unable to unblock {}: {}".format(self.current_song,
                                                            e))


    def get_channels(self):
        channel_list = ["Master"]
        amixer_output = subprocess.check_output("amixer")
        if "'Speaker',0" in amixer_output:
            channel_list.append("Speaker")

        return channel_list


    def toggle_mute(self, force=False):

        mutemethod = getattr(self, self.mute_mode + "_mute", None)
        mutemethod(force)


    def is_muted(self):
        master = subprocess.check_output(["amixer", "get", "Master"])
        return True if "[off]" in master else False


    def get_state(self, force):
        muted = self.is_muted()

        state = None
        if not muted and force:
            state = "mute"
            log.info("Muting {}.".format(self.current_song))
        elif muted and not force:
            state = "unmute"
            log.info("Unmuting.")

        return state


    def alsa_mute(self, force):

        state = self.get_state(force)
        if not state:
            return

        for channel in self.channels:
            subprocess.Popen(["amixer", "-q", "set", channel, state])

#         if self.fallback_enabled:
#             muted = self.is_muted()
#             if state == "mute" and not muted:
#                 log.error("Muting with alsa failed.")


    def pulse_mute(self, force):

        state = self.get_state(force)
        if not state:
            return

        for channel in self.channels:
            subprocess.Popen(["amixer", "-qD", "pulse", "set", channel, state])

#         if self.fallback_enabled:
#             muted = self.is_muted()
#             if state == "mute" and not muted:
#                 log.error("Muting with pulse failed. Trying alsa.")
#                 self.mute_mode = "alsa"


    def pulsesink_mute(self, force):
        "Finds spotify's audio sink and toggles its mute state."

        try:
            pacmd_out = subprocess.check_output(["pacmd", "list-sink-inputs"])
            pidof_out = subprocess.check_output(["pidof", "spotify"])
        except subprocess.CalledProcessError:
            log.error("Sink or process not found. Is Pulse/Spotify running?")
            log.error("Resorting to amixer as mute method.")
            self.mute_mode = "pulse"  # Fall back to amixer mute mode.
            return

        pattern = re.compile(r'(?: index|muted|application\.process\.id).*?(\w+)')
        pids = pidof_out.decode('utf-8').strip().split(' ')
        output = pacmd_out.decode('utf-8')

        # Every third element is a key, the value is the preceding two
        # elements in the form of a tuple - {pid : (index, muted)}
        info = pattern.findall(output)
        idxd = {info[3 * n + 2]: (info[3 * n], info[3 * n + 1])
                for n in range(len(info) // 3)}

        try:
            pid = [k for k in idxd.keys() if k in pids][0]
            index, muted = idxd[pid]
        except IndexError:
            return

        if muted == "no" and force:
            log.info("Muting {}.".format(self.current_song))
            subprocess.call(["pacmd", "set-sink-input-mute", index, '1'])
        elif muted == "yes" and not force:
            log.info("Unmuting.")
            subprocess.call(["pacmd", "set-sink-input-mute", index, '0'])


    def bind_signals(self):
        signal.signal(signal.SIGUSR1, lambda sig, hdl: self.block_current())
        signal.signal(signal.SIGTERM, lambda sig, hdl: self.shutdown())
        signal.signal(signal.SIGINT, lambda sig, hdl: self.shutdown())


    def shutdown(self):
        # Save the list only if it changed during runtime.
        log.info("Exiting safely. Bye.")
        if self.blocklist != self.orglist:
            self.blocklist.save_file()
        self.toggle_mute()
        sys.exit()


def init_logger(logpath=None, loglevel=1, quiet=False):
    "Initializes the logger for system messages."
    logger = logging.getLogger()

    # Set the loglevel.
    if loglevel > 3:
        loglevel = 3  # Cap at 3, incase someone likes their v-key too much.
    levels = [logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG]
    logger.setLevel(levels[loglevel])

    logformat = "%(asctime)-14s %(levelname)-8s %(message)s"

    formatter = logging.Formatter(logformat, "%Y-%m-%d %H:%M:%S")

    # Only attach a console handler if both nologs and quiet are disabled.
    if not quiet:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        log.debug("Added logging console handler.")
        log.info("Loglevel is {}.".format(levels[loglevel]))
    if logpath:
        try:
            logfile = os.path.abspath(logpath)
            file_handler = logging.FileHandler(logfile)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            log.debug("Added logging file handler: {}.".format(logfile))
        except IOError:
            log.error("Could not attach file handler.")


def main():
    args = docopt(__doc__, version="1.0")
    init_logger(args["--log"], args["-v"], args["--quiet"])

    blocklist = Blocklist(load_remotes=True)
    blockify = Blockify(blocklist)

    blockify.bind_signals()
    blockify.toggle_mute()

    while True:
        # Initiate gtk loop to enable the window list for .get_windows().
        while gtk.events_pending():
            gtk.main_iteration(False)
        blockify.update()
        time.sleep(1)


if __name__ == "__main__":
    main()
