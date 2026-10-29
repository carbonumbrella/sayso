# SaySo plugins: a plugin is a Python module with register(api). Built-in ones ship in <app>\plugins,
# the user's own go to %APPDATA%\SaySo\plugins (a user plugin with the same file name replaces the built-in).
# A plugin can add:
#   api.route(fn, stage="before"|"after")  fn(p, text) -> (reply, speak_it) or None. "before" runs ahead of
#                                          the built-in commands, "after" only when nothing else matched.
#   api.tool(name, args, description, fn)  a tool for the LLM: fn(args: dict, phrase) -> (reply, speak_it)
#   api.apps(fn)                           fn() -> [(spoken name, path or AppID)] extra launchable apps
#   api.help(line)                         a line for "что ты умеешь" / the command list
# Plugins listed in settings "plugins_disabled" (file stem) are skipped. ABOUT = "..." in a plugin is its
# one-line description in the Settings plugin list.
import importlib.util, os, sys, traceback
import voice_settings

HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIR = os.path.join(HERE, "plugins")
USER_DIR = os.path.join(voice_settings.USER_DIR, "plugins")

REG = {"before": [], "after": [], "tools": {}, "apps": [], "help": [], "loaded": [], "failed": {}, "modules": {}}


class API:
    def __init__(self, agent, name):
        self.agent, self.name = agent, name
        self.settings = voice_settings

    def route(self, fn, stage="before"):
        REG["before" if stage == "before" else "after"].append((self.name, fn))

    def tool(self, name, args, description, fn):
        REG["tools"][name] = (args, description, fn, self.name)

    def apps(self, fn):
        REG["apps"].append(fn)

    def help(self, line):
        REG["help"].append(line)

    def log(self, msg):
        self.agent.log(f"[{self.name}] {msg}")


def files():
    """Plugin files to load (user ones replace built-ins of the same name), for loading and for the
    file-change reload in dictate.py."""
    found = {}
    for d in (BUILTIN_DIR, USER_DIR):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".py") and not f.startswith("_"):
                    found[f[:-3]] = os.path.join(d, f)
    return found


def load(agent):
    for k in REG:
        REG[k] = {} if isinstance(REG[k], dict) else []
    disabled = set(voice_settings.get("plugins_disabled") or [])
    for name, path in files().items():
        if name in disabled:
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"sayso_plugin_{name}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.register(API(agent, name))
            REG["loaded"].append(name)
            REG["modules"][name] = mod
        except Exception as e:
            REG["failed"][name] = repr(e)
            agent.log(f"plugin {name} failed: {e!r}\n{traceback.format_exc(limit=3)}")


def route(stage, p, text):
    for name, fn in REG[stage]:
        try:
            r = fn(p, text)
        except Exception as e:
            REG["failed"][name] = repr(e)
            return f"Плагин {name}: ошибка {e}", True
        if r:
            return r
    return None


def extra_apps():
    out = []
    for fn in REG["apps"]:
        try:
            out += fn()
        except Exception:
            pass
    return out
