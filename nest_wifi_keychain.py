"""Read and write this monitor's macOS Keychain credential."""

from __future__ import annotations

import ctypes as C
import json

from nest_wifi_security import security_framework

SERVICE = b"local.home-network.google-wifi-monitor"
ACCOUNT = b"default"


class MonitorError(Exception):
    """An error safe to show without exposing a credential or response body."""


def _find_keychain_item(security):
    """Find only the monitor's named item; -25300 means it is absent."""
    length, data, item = C.c_uint32(), C.c_void_p(), C.c_void_p()
    result = security.SecKeychainFindGenericPassword(
        None,
        len(SERVICE),
        SERVICE,
        len(ACCOUNT),
        ACCOUNT,
        C.byref(length),
        C.byref(data),
        C.byref(item),
    )
    if result not in (0, -25300):
        raise MonitorError(f"Keychain access unavailable ({result}).")
    return result, length, data, item


def _read_keychain_item(length, data):
    """Validate stored fields before any authentication request is made."""
    try:
        saved = json.loads(C.string_at(data, length.value))
        required = ("email", "master_token", "android_id")
        if not all(isinstance(saved.get(key), str) and saved[key] for key in required):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise MonitorError("Stored credential is invalid; sign in again.") from None
    return saved


def _write_keychain_item(security, result, item, credential):
    """Replace the existing item or create exactly one new named item."""
    raw = json.dumps(credential).encode()
    if result == 0:
        status = security.SecKeychainItemModifyAttributesAndData(item, None, len(raw), raw)
    else:
        status = security.SecKeychainAddGenericPassword(
            None,
            len(SERVICE),
            SERVICE,
            len(ACCOUNT),
            ACCOUNT,
            len(raw),
            raw,
            None,
        )
    if status:
        raise MonitorError(f"Could not save credential in Keychain ({status}).")


def keychain(credential=None, *, interactive=False):
    """Read or save only this tool's credential, without subprocess arguments."""
    # Security.framework owns the returned buffers; always release both handles.
    security, core = security_framework()
    security.SecKeychainSetUserInteractionAllowed(interactive)
    result, length, data, item = _find_keychain_item(security)
    try:
        if credential is not None:
            _write_keychain_item(security, result, item, credential)
            return None
        if result == -25300:
            raise MonitorError("Authentication required; run the auth command.")
        return _read_keychain_item(length, data)
    finally:
        if data:
            security.SecKeychainItemFreeContent(None, data)
        if item:
            core.CFRelease(item)
