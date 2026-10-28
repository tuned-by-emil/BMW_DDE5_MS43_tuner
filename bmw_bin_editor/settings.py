"""Stable release settings, migrating accepted preview preferences once."""
from PySide6.QtCore import QSettings
from urllib.parse import quote
from . import BUILD_CHANNEL


class ECUSettings:
    """A QSettings view for one ECU/software, with a one-time legacy copy.

    Keep the old keys as a backup. Only 361830 inherits the historical diesel
    preferences; new ECUs start with their own defaults.
    """

    PREFIXES = ("log/", "log_graph_layouts/", "live126/", "testo_mcp/")

    def __init__(self, settings, profile_id="361830"):
        self.base = settings
        self.profile_id = str(profile_id)
        self.prefix = "ecu_profiles/" + quote(self.profile_id, safe="") + "/"
        marker = self.prefix + "migrated"
        if self.profile_id == "361830" and not settings.contains(marker):
            for key in settings.allKeys():
                if key.startswith(self.PREFIXES) and not settings.contains(self.prefix + key):
                    settings.setValue(self.prefix + key, settings.value(key))
            settings.setValue(marker, True)
            settings.sync()

    def value(self, key, *args, **kwargs):
        return self.base.value(self.prefix + key, *args, **kwargs)

    def setValue(self, key, value):
        self.base.setValue(self.prefix + key, value)

    def contains(self, key):
        return self.base.contains(self.prefix + key)

    def remove(self, key):
        self.base.remove(self.prefix + key)

    def sync(self):
        self.base.sync()


def editor_settings(factory=QSettings, *, channel=None):
    channel = BUILD_CHANNEL if channel is None else channel
    if channel == "patch-test":
        # A local test installation must not migrate or change release settings.
        return factory("EmilTools", "BMWBinEditorPatchTest")
    settings = factory("EmilTools", "BMWBinEditor")
    if not settings.value("settings/migrated", False, type=bool):
        for name in ("BMWBinEditorV1265ReviewPreview", "BMWBinEditorV1"):
            previous = factory("EmilTools", name)
            if previous.allKeys():
                for key in previous.allKeys():
                    if not settings.contains(key):
                        settings.setValue(key, previous.value(key))
                break
        settings.setValue("settings/migrated", True)
    return settings
