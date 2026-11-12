"""Private-bus fixture: a locked Secret Service item with an unanswered prompt."""

import argparse
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib


class Service(dbus.service.Object):
    @dbus.service.method("org.freedesktop.Secret.Service", in_signature="sv", out_signature="vo")
    def OpenSession(self, algorithm, _input):
        assert algorithm == "dh-ietf1024-sha256-aes128-cbc-pkcs7"
        return dbus.ByteArray(b"\x02", variant_level=1), dbus.ObjectPath("/session")

    @dbus.service.method("org.freedesktop.Secret.Service", in_signature="a{ss}", out_signature="aoao")
    def SearchItems(self, _attributes):
        print("search", flush=True)
        return [], [] if args.locked_collection else [dbus.ObjectPath("/locked_item")]

    @dbus.service.method("org.freedesktop.Secret.Service", in_signature="s", out_signature="o")
    def ReadAlias(self, alias):
        assert alias == "default"
        return dbus.ObjectPath("/collection")

    @dbus.service.method("org.freedesktop.Secret.Service", in_signature="ao", out_signature="aoo")
    def Unlock(self, _objects):
        print("unlock", flush=True)
        return [], dbus.ObjectPath("/prompt")


class Collection(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, interface, property_name):
        assert interface == "org.freedesktop.Secret.Collection" and property_name == "Locked"
        return dbus.Boolean(True, variant_level=1)


class Prompt(dbus.service.Object):
    @dbus.service.method("org.freedesktop.Secret.Prompt", in_signature="s", out_signature="")
    def Prompt(self, _window):
        print("prompt-requested", flush=True)
        # Deliberately omit Completed, as when the user leaves an unlock dialog open.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--locked-collection", action="store_true")
    args = parser.parse_args()
    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(args.address)
    name = dbus.service.BusName("org.freedesktop.secrets", bus)
    service = Service(bus, "/org/freedesktop/secrets")
    collection = Collection(bus, "/collection")
    prompt = Prompt(bus, "/prompt")
    GLib.MainLoop().run()
