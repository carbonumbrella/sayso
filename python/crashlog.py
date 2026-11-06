"""Logs that say what happened and when: every line gets the time, a crash inside a native library (OpenVINO, a
driver) still leaves its stack (faulthandler), and an uncaught error in any thread is written with its traceback
instead of vanishing. Used by dictate.py, npu_server.py and hotkey_helper.py for their log files."""
import faulthandler, sys, threading, time, traceback


class Timed:
    """A text stream that puts "HH:MM:SS " in front of every line written to the file under it."""

    def __init__(self, f):
        self.f, self.bol = f, True

    def write(self, s):
        out = []
        for part in s.splitlines(keepends=True):
            if self.bol:
                out.append(time.strftime("%H:%M:%S "))
            out.append(part)
            self.bol = part.endswith("\n")
        self.f.write("".join(out))
        return len(s)

    def flush(self):
        self.f.flush()

    def fileno(self):
        return self.f.fileno()

    def __getattr__(self, name):
        return getattr(self.f, name)


def setup(f):
    """f: the open log file. Returns the timed stream for sys.stdout / sys.stderr."""
    faulthandler.enable(f)  # raw file: it writes from the crash handler itself, no Python left to format time

    def thread_hook(a):
        print(f"thread {a.thread.name if a.thread else '?'} failed:\n" +
              "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)), file=sys.stderr, flush=True)
    threading.excepthook = thread_hook
    return Timed(f)
